"""枚举求解器与不可行状态的单元测试。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app


def _full_period(code: str, n_new: int, fee: int = 5, completed: int = 1):
    """所有新区都与同一个旧区 O1 相连 -> 冲突图完全图，最小顶点覆盖取 n-1 个。"""
    return {
        "code": code,
        "name": code,
        "old_districts": [{"code": "O1", "name": "o", "completed": completed}],
        "new_districts": [
            {"code": f"N{i:02d}", "name": f"n{i}", "fee": fee} for i in range(n_new)
        ],
        "overlaps": [
            {"new_code": f"N{i:02d}", "old_code": "O1", "building_total": 10}
            for i in range(n_new)
        ],
    }


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def test_min_cost_cover_complete_graph_18_districts(client):
    payload = _full_period("P18", 18)
    r = client.post("/periods/", json=payload)
    assert r.status_code == 201
    start = time.perf_counter()
    body = client.get("/periods/P18/plan").json()
    elapsed = time.perf_counter() - start
    # 完全图：任意 17 个新区组成顶点覆盖，留 1 个不查；费用 17*5=85
    assert len(body["plan"]) == 17
    assert body["total_fee"] == 85
    assert elapsed < 2.0  # 2^18 枚举在秒级内完成


def test_min_cost_cover_prefers_cheaper_cover(client):
    # 链状冲突 N1-N2-N3（两个旧区），最小覆盖为 {N2}
    payload = {
        "code": "PCHAIN",
        "name": "chain",
        "old_districts": [
            {"code": "O1", "name": "o1", "completed": 1},
            {"code": "O2", "name": "o2", "completed": 1},
        ],
        "new_districts": [
            {"code": "N1", "name": "n1", "fee": 100},
            {"code": "N2", "name": "n2", "fee": 1},
            {"code": "N3", "name": "n3", "fee": 100},
        ],
        "overlaps": [
            {"new_code": "N1", "old_code": "O1", "building_total": 5},
            {"new_code": "N2", "old_code": "O1", "building_total": 5},
            {"new_code": "N2", "old_code": "O2", "building_total": 5},
            {"new_code": "N3", "old_code": "O2", "building_total": 5},
        ],
    }
    assert client.post("/periods/", json=payload).status_code == 201
    body = client.get("/periods/PCHAIN/plan").json()
    assert body["plan"] == ["N2"]
    assert body["total_fee"] == 1


def test_infeasible_dirty_state_reported_not_500(client):
    payload = _full_period("PDIRTY", 3, completed=5)
    assert client.post("/periods/", json=payload).status_code == 201
    # 直接写一条与底账不相容的检索（绕过 API 校验，模拟历史脏数据）
    conn = connect()
    try:
        pid = conn.execute(
            "SELECT id FROM period WHERE code='PDIRTY'"
        ).fetchone()["id"]
        nid = conn.execute(
            "SELECT id FROM new_district WHERE period_id=? AND code='N00'", (pid,)
        ).fetchone()["id"]
        oid = conn.execute(
            "SELECT id FROM old_district WHERE period_id=? AND code='O1'", (pid,)
        ).fetchone()["id"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO retrieval(period_id, new_district_id, old_district_id, "
            "completed_count) VALUES (?, ?, ?, ?)",
            (pid, nid, oid, 9),  # 底账仅 5，已查 9 -> 余数 -4
        )
        conn.commit()
    finally:
        conn.close()

    plan = client.get("/periods/PDIRTY/plan").json()
    assert plan["status"] == "infeasible"
    assert plan["old_remaining"]["O1"] == -4
    acc = client.get("/periods/PDIRTY/accounting").json()
    assert acc["status"] == "infeasible" and acc["determined"] is False


def test_solve_uniqueness_when_one_free_edge_per_old(client):
    # 两个旧区各两条边，登记其中一条边后另一条被唯一确定
    payload = {
        "code": "PUNIQ",
        "name": "uniq",
        "old_districts": [
            {"code": "O1", "name": "o1", "completed": 7},
            {"code": "O2", "name": "o2", "completed": 3},
        ],
        "new_districts": [
            {"code": "N1", "name": "n1", "fee": 1},
            {"code": "N2", "name": "n2", "fee": 1},
        ],
        "overlaps": [
            {"new_code": "N1", "old_code": "O1", "building_total": 10},
            {"new_code": "N2", "old_code": "O1", "building_total": 10},
            {"new_code": "N1", "old_code": "O2", "building_total": 10},
            {"new_code": "N2", "old_code": "O2", "building_total": 10},
        ],
    }
    assert client.post("/periods/", json=payload).status_code == 201
    # 调 N1：O1=4, O2=2；则 N2 被唯一确定为 O1=3, O2=1
    r = client.post(
        "/periods/PUNIQ/retrievals",
        json={"new_code": "N1", "counts": {"O1": 4, "O2": 2}},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "completed"
    acc = client.get("/periods/PUNIQ/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    assert got == {"N1": 6, "N2": 4}
    assert acc["total_completed"] == 10  # 等于底账 7+3
