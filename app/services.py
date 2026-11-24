"""核心业务逻辑。

数学模型
========
对每个期次：
- 旧区 O 底账完工数 c_O；
- 新区 N 与旧区 O 的重叠范围有楼栋总数 t_{N,O}；
- 未知量 x_{N,O}：现行新区 N 在原旧区 O 范围内的完工楼栋数；
  约束 0 <= x_{N,O} <= t_{N,O}，且对每个旧区 Σ_N x_{N,O} = c_O。
- 调一份新区 N 的档案，即得知该新区所有边上的 x_{N,O}，费用 f_N（每期最多 18 个新区）。

唯一性
------
底账相容时，只要每个旧区至多剩一条「未查」边，全部未知量即被唯一确定
（0 条未查边则剩余量必须为 0；1 条则其值等于旧区完工余数）。
因此，同一旧区下不允许同时存在两个未查新区——这些新区两两构成「冲突」，
待查集合必须覆盖冲突图的所有边，即冲突图的顶点覆盖；目标是费用最小的顶点覆盖。
新区数 <= 18，直接枚举全部 2^18 个子集即可精确求解。

可行性
------
登记后对每个旧区独立判断：
    0 <= 余数 r_O = c_O - Σ已查 x <= Σ未查边 t
（再结合各边 0 <= x <= t，整数边容量分配必然可凑足）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import immediate_transaction
from .schemas import PeriodSetupIn, RetrievalIn


class AppError(Exception):
    """业务错误，由路由层映射为 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class Edge:
    new_code: str
    old_code: str
    building_total: int


@dataclass
class PeriodData:
    code: str
    name: str
    old_codes: list[str]
    old_names: dict[str, str]
    old_completed: dict[str, int]
    new_codes: list[str]
    new_names: dict[str, str]
    new_fee: dict[str, int]
    # old_code -> [(new_code, total)]
    old_edges: dict[str, list[tuple[str, int]]]
    # new_code -> {old_code: total}
    new_edges: dict[str, dict[str, int]]
    # new_code -> {old_code: registered completed count}
    registered: dict[str, dict[str, int]]


def _load_period(conn: sqlite3.Connection, period_code: str) -> PeriodData:
    prow = conn.execute(
        "SELECT id, code, name FROM period WHERE code = ?", (period_code,)
    ).fetchone()
    if prow is None:
        raise AppError(404, f"期次不存在: {period_code}")
    pid = prow["id"]

    old_rows = conn.execute(
        "SELECT code, name, completed FROM old_district WHERE period_id = ? ORDER BY id",
        (pid,),
    ).fetchall()
    new_rows = conn.execute(
        "SELECT code, name, fee FROM new_district WHERE period_id = ? ORDER BY id",
        (pid,),
    ).fetchall()
    overlap_rows = conn.execute(
        """
        SELECT n.code AS new_code, o.code AS old_code, x.building_total
        FROM overlap x
        JOIN new_district n ON n.id = x.new_district_id
        JOIN old_district o ON o.id = x.old_district_id
        WHERE x.period_id = ?
        ORDER BY x.id
        """,
        (pid,),
    ).fetchall()
    retrieval_rows = conn.execute(
        """
        SELECT n.code AS new_code, o.code AS old_code, r.completed_count
        FROM retrieval r
        JOIN new_district n ON n.id = r.new_district_id
        JOIN old_district o ON o.id = r.old_district_id
        WHERE r.period_id = ?
        ORDER BY r.id
        """,
        (pid,),
    ).fetchall()

    old_codes = [r["code"] for r in old_rows]
    new_codes = [r["code"] for r in new_rows]
    old_edges: dict[str, list[tuple[str, int]]] = {c: [] for c in old_codes}
    new_edges: dict[str, dict[str, int]] = {c: {} for c in new_codes}
    for r in overlap_rows:
        old_edges[r["old_code"]].append((r["new_code"], r["building_total"]))
        new_edges[r["new_code"]][r["old_code"]] = r["building_total"]

    registered: dict[str, dict[str, int]] = {}
    for r in retrieval_rows:
        registered.setdefault(r["new_code"], {})[r["old_code"]] = r["completed_count"]

    return PeriodData(
        code=prow["code"],
        name=prow["name"],
        old_codes=old_codes,
        old_names={r["code"]: r["name"] for r in old_rows},
        old_completed={r["code"]: r["completed"] for r in old_rows},
        new_codes=new_codes,
        new_names={r["code"]: r["name"] for r in new_rows},
        new_fee={r["code"]: r["fee"] for r in new_rows},
        old_edges=old_edges,
        new_edges=new_edges,
        registered=registered,
    )


# ---------------------------------------------------------------- 期次建档

def create_period(conn: sqlite3.Connection, payload: PeriodSetupIn) -> None:
    old_codes = [o.code for o in payload.old_districts]
    new_codes = [n.code for n in payload.new_districts]
    if len(set(old_codes)) != len(old_codes):
        raise AppError(422, "旧区编码存在重复")
    if len(set(new_codes)) != len(new_codes):
        raise AppError(422, "新区编码存在重复")
    if len(new_codes) > 18:
        raise AppError(422, "每期最多 18 个新区")

    old_set, new_set = set(old_codes), set(new_codes)
    seen_edges: set[tuple[str, str]] = set()
    for ov in payload.overlaps:
        if ov.old_code not in old_set:
            raise AppError(422, f"重叠范围引用了不存在的旧区: {ov.old_code}")
        if ov.new_code not in new_set:
            raise AppError(422, f"重叠范围引用了不存在的新区: {ov.new_code}")
        key = (ov.new_code, ov.old_code)
        if key in seen_edges:
            raise AppError(422, f"重叠范围重复: {ov.new_code}/{ov.old_code}")
        seen_edges.add(key)

    # 每个旧区、新区都必须至少参与一个重叠范围，否则底账/档案无意义
    covered_old = {ov.old_code for ov in payload.overlaps}
    covered_new = {ov.new_code for ov in payload.overlaps}
    missing_old = old_set - covered_old
    missing_new = new_set - covered_new
    if missing_old:
        raise AppError(422, f"旧区缺少重叠范围: {sorted(missing_old)}")
    if missing_new:
        raise AppError(422, f"新区缺少重叠范围: {sorted(missing_new)}")

    # 底账本身必须可由全部楼栋凑足：0 <= c_O <= Σ t_{N,O}
    capacity: dict[str, int] = {c: 0 for c in old_codes}
    for ov in payload.overlaps:
        capacity[ov.old_code] += ov.building_total
    for o in payload.old_districts:
        if o.completed > capacity[o.code]:
            raise AppError(
                422,
                f"旧区 {o.code} 底账完工数 {o.completed} 超过重叠楼栋总数 "
                f"{capacity[o.code]}，底账不可行",
            )

    with immediate_transaction(conn):
        cur = conn.execute(
            "INSERT INTO period(code, name) VALUES (?, ?)",
            (payload.code, payload.name),
        )
        pid = cur.lastrowid
        old_id: dict[str, int] = {}
        for o in payload.old_districts:
            cur = conn.execute(
                "INSERT INTO old_district(period_id, code, name, completed) "
                "VALUES (?, ?, ?, ?)",
                (pid, o.code, o.name, o.completed),
            )
            old_id[o.code] = cur.lastrowid
        new_id: dict[str, int] = {}
        for n in payload.new_districts:
            cur = conn.execute(
                "INSERT INTO new_district(period_id, code, name, fee) "
                "VALUES (?, ?, ?, ?)",
                (pid, n.code, n.name, n.fee),
            )
            new_id[n.code] = cur.lastrowid
        for ov in payload.overlaps:
            conn.execute(
                "INSERT INTO overlap(period_id, new_district_id, old_district_id, "
                "building_total) VALUES (?, ?, ?, ?)",
                (pid, new_id[ov.new_code], old_id[ov.old_code], ov.building_total),
            )


# ---------------------------------------------------------------- 登记调档

def register_retrieval(
    conn: sqlite3.Connection, period_code: str, payload: RetrievalIn
) -> dict[str, dict[str, int]]:
    """登记一份新区档案。

    校验（全部在同一个 BEGIN IMMEDIATE 事务内）：
    1. 新区存在，且 keys 恰好等于该新区涉及的全部旧区；
    2. 各查回数 0 <= x <= 重叠楼栋总数；
    3. 合入后各旧区：已查总数 <= 底账，且完工余数能由未查楼栋凑足；
    4. 同新区同值重送幂等成功，异值退回（409，原记录保留）。
    """
    with immediate_transaction(conn):
        data = _load_period(conn, period_code)
        if payload.new_code not in data.new_edges:
            raise AppError(404, f"新区不存在: {payload.new_code}")

        expected_olds = set(data.new_edges[payload.new_code])
        got_olds = set(payload.counts)
        if got_olds != expected_olds:
            missing = sorted(expected_olds - got_olds)
            extra = sorted(got_olds - expected_olds)
            raise AppError(
                422,
                f"查回旧区集合必须恰好为 {sorted(expected_olds)}；"
                f"缺少 {missing}，多余 {extra}",
            )

        # 规则 2：单条范围上下界
        for old_code, value in payload.counts.items():
            if value < 0:
                raise AppError(422, f"查回数不能为负: {old_code}={value}")
            total = data.new_edges[payload.new_code][old_code]
            if value > total:
                raise AppError(
                    422,
                    f"查回数 {value} 超过范围 {payload.new_code}/{old_code} "
                    f"楼栋总数 {total}",
                )

        # 规则 4：同新区重送，相同结果幂等，异值冲突
        previous = data.registered.get(payload.new_code)
        if previous is not None and previous != dict(payload.counts):
            raise AppError(
                409,
                f"新区 {payload.new_code} 已登记不同结果 {previous}，"
                f"异值退回，原记录保留",
            )

        # 模拟合入后的已查总数，逐旧区做底账相容性判断（规则 3）
        merged: dict[str, dict[str, int]] = {
            n: dict(v) for n, v in data.registered.items()
        }
        merged[payload.new_code] = dict(payload.counts)

        _check_feasible(data, merged)

        if previous is None:
            pid = conn.execute(
                "SELECT id FROM period WHERE code = ?", (period_code,)
            ).fetchone()["id"]
            nid = conn.execute(
                "SELECT id FROM new_district WHERE period_id = ? AND code = ?",
                (pid, payload.new_code),
            ).fetchone()["id"]
            for old_code, value in payload.counts.items():
                oid = conn.execute(
                    "SELECT id FROM old_district WHERE period_id = ? AND code = ?",
                    (pid, old_code),
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO retrieval(period_id, new_district_id, old_district_id, "
                    "completed_count) VALUES (?, ?, ?, ?)",
                    (pid, nid, oid, value),
                )
        # previous 相同：幂等重送，不重复写入
        return merged


def _old_state(
    data: PeriodData, registered: dict[str, dict[str, int]]
) -> dict[str, dict[str, object]]:
    """计算每个旧区的已查总数、完工余数与未查边容量。"""
    state: dict[str, dict[str, object]] = {}
    for old in data.old_codes:
        used = sum(reg.get(old, 0) for reg in registered.values())
        remaining = data.old_completed[old] - used
        unretrieved_edges = [
            (n, t) for (n, t) in data.old_edges[old] if n not in registered
        ]
        free_capacity = sum(t for _, t in unretrieved_edges)
        state[old] = {
            "used": used,
            "remaining": remaining,
            "free_capacity": free_capacity,
            "unretrieved_edges": unretrieved_edges,
        }
    return state


def _check_feasible(
    data: PeriodData, registered: dict[str, dict[str, int]]
) -> dict[str, dict[str, object]]:
    """底账相容性：各旧区余数介于 0 与未查楼栋总数之间。"""
    state = _old_state(data, registered)
    for old, s in state.items():
        remaining = s["remaining"]
        if remaining < 0:
            raise AppError(
                409,
                f"旧区 {old} 已查完工数 {s['used']} 超过底账 {data.old_completed[old]}，"
                "与底账不相容，登记退回",
            )
        if remaining > s["free_capacity"]:
            raise AppError(
                409,
                f"旧区 {old} 完工余数 {remaining} 无法由未查楼栋 "
                f"（总容量 {s['free_capacity']}）凑足，与底账不相容，登记退回",
            )
    return state


# ---------------------------------------------------------------- 计划与核算

def _conflict_masks(data: PeriodData, registered: dict[str, dict[str, int]]) -> list[int]:
    """冲突图：共享同一旧区的未查新区两两冲突。返回每个新区的冲突邻接掩码。"""
    n = len(data.new_codes)
    masks = [0] * n
    index = {code: i for i, code in enumerate(data.new_codes)}
    for old in data.old_codes:
        members = [index[ncode] for ncode, _ in data.old_edges[old]]
        for a in members:
            for b in members:
                if a != b:
                    masks[a] |= 1 << b
    registered_mask = 0
    for ncode in registered:
        registered_mask |= 1 << index[ncode]
    # 已查新区不再属于「未查集合」，其所有冲突边一律屏蔽
    return [m & ~registered_mask for m in masks]


def min_cost_plan(
    data: PeriodData, registered: dict[str, dict[str, int]]
) -> tuple[list[str], int]:
    """求费用最低的待调名单。

    等价于在冲突图上求最小权顶点覆盖，即补集为最大权独立集。
    新区数 k <= 18，用位掩码 DP 求最大权独立集（O(2^k) 整数运算），
    tie-break：覆盖费用低 -> 待调个数少 -> 名单编码字典序小。
    """
    codes = data.new_codes
    n = len(codes)
    index = {code: i for i, code in enumerate(codes)}
    registered_idx = {index[c] for c in registered}
    candidates = [i for i in range(n) if i not in registered_idx]
    k = len(candidates)
    if k == 0:
        return [], 0

    candidate_mask = sum(1 << i for i in candidates)
    raw_masks = _conflict_masks(data, registered)
    # 压缩到候选编号空间（位 j 对应 candidates[j]）
    adj = [0] * k
    for j, i in enumerate(candidates):
        for h, i2 in enumerate(candidates):
            if raw_masks[i] & (1 << i2):
                adj[j] |= 1 << h
    weights = [data.new_fee[codes[i]] for i in candidates]
    total_weight = sum(weights)

    # dp[mask]：诱导子图 mask 上最大权独立集的权重；parent 记录取法用于还原
    size = 1 << k
    dp = [0] * size
    take = [False] * size
    for mask in range(1, size):
        bit = mask & -mask
        j = bit.bit_length() - 1
        without = dp[mask ^ bit]
        with_v = weights[j] + dp[mask & ~(bit | adj[j])]
        if with_v > without:
            dp[mask] = with_v
            take[mask] = True
        else:
            dp[mask] = without

    # 还原最大权独立集（优先取权重更大的节点；等权时 DP 顺序天然稳定）
    independent_local: list[int] = []
    mask = size - 1
    while mask:
        bit = mask & -mask
        j = bit.bit_length() - 1
        if take[mask]:
            independent_local.append(j)
            mask &= ~(bit | adj[j])
        else:
            mask ^= bit
    independent_set = set(independent_local)
    cover = [
        codes[candidates[j]] for j in range(k) if j not in independent_set
    ]
    cover_fee = total_weight - dp[size - 1]
    return cover, cover_fee


def evaluate(
    data: PeriodData,
) -> tuple[str, dict[str, dict[str, object]], dict[str, dict[str, int]], list[str], int]:
    """在当前已提交登记状态上评估：可行性、待调计划、是否可唯一核算。"""
    # 先算各旧区状态，再在其上做相容性判断；不可行时状态仍可供排障展示
    state = _old_state(data, data.registered)
    _check_feasible(data, data.registered)
    plan, total_fee = min_cost_plan(data, data.registered)

    # 唯一性：每个旧区至多 1 条未查边（可行前提下即可唯一确定全部 x）
    determined = all(len(s["unretrieved_edges"]) <= 1 for s in state.values())
    if determined:
        status = "completed"
    else:
        status = "retrieval_required"
    return status, state, dict(data.registered), plan, total_fee


def solve_completed(
    data: PeriodData, state: dict[str, dict[str, object]]
) -> list[tuple[str, str, int]]:
    """唯一核算各新区完工数：已查边取查回数，唯一未查边取旧区余数。"""
    values: dict[tuple[str, str], int] = {}
    for ncode, reg in data.registered.items():
        for old_code, v in reg.items():
            values[(ncode, old_code)] = v
    for old, s in state.items():
        edges: list[tuple[str, int]] = s["unretrieved_edges"]  # type: ignore[assignment]
        if len(edges) == 1:
            values[(edges[0][0], old)] = int(s["remaining"])
        # len == 0 时余数必须为 0（可行性已保证），无未知边
    result = []
    for ncode in data.new_codes:
        total = sum(v for (n, _), v in values.items() if n == ncode)
        result.append((ncode, data.new_names[ncode], total))
    return result


def reset_retrievals(conn: sqlite3.Connection, period_code: str) -> None:
    with immediate_transaction(conn):
        prow = conn.execute(
            "SELECT id FROM period WHERE code = ?", (period_code,)
        ).fetchone()
        if prow is None:
            raise AppError(404, f"期次不存在: {period_code}")
        conn.execute("DELETE FROM retrieval WHERE period_id = ?", (prow["id"],))
