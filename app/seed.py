"""演示期次种子数据（仅当库中没有任何期次时写入）。

底账设计（期次 DEMO-2023）
--------------------------
旧区完工底账：O1=30, O2=24, O3=18, O4=10
新区调档费用：N1=12, N2=7, N3=3, N4=10
重叠范围（楼栋总数）：
    N1-O1=20, N1-O2=10
    N2-O1=15, N2-O3=12
    N3-O2=16, N3-O3=8
    N4-O3=10, N4-O4=10

冲突图（共享旧区的新区相连）：
    N1-N2 (O1), N1-N3 (O2), N2-N3 (O2/O3), N2-N4 (O3), N3-N4 (O3)
最大权独立集 {N1, N4}（费用 22 可省），故最小费用待调名单 = {N2, N3}，金额 10。
"""
from __future__ import annotations

import sqlite3

from .schemas import (
    NewDistrictIn,
    OldDistrictIn,
    OverlapIn,
    PeriodSetupIn,
)
from .services import create_period

DEMO = PeriodSetupIn(
    code="DEMO-2023",
    name="2023 年度改造补算演示期",
    old_districts=[
        OldDistrictIn(code="O1", name="旧一区", completed=30),
        OldDistrictIn(code="O2", name="旧二区", completed=24),
        OldDistrictIn(code="O3", name="旧三区", completed=18),
        OldDistrictIn(code="O4", name="旧四区", completed=10),
    ],
    new_districts=[
        NewDistrictIn(code="N1", name="新一街道", fee=12),
        NewDistrictIn(code="N2", name="新二街道", fee=7),
        NewDistrictIn(code="N3", name="新三街道", fee=3),
        NewDistrictIn(code="N4", name="新四街道", fee=10),
    ],
    overlaps=[
        OverlapIn(new_code="N1", old_code="O1", building_total=20),
        OverlapIn(new_code="N1", old_code="O2", building_total=10),
        OverlapIn(new_code="N2", old_code="O1", building_total=15),
        OverlapIn(new_code="N2", old_code="O3", building_total=12),
        OverlapIn(new_code="N3", old_code="O2", building_total=16),
        OverlapIn(new_code="N3", old_code="O3", building_total=8),
        OverlapIn(new_code="N4", old_code="O3", building_total=10),
        OverlapIn(new_code="N4", old_code="O4", building_total=10),
    ],
)


def seed_demo(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM period").fetchone()
    if row["c"] > 0:
        return
    try:
        create_period(conn, DEMO)
    except sqlite3.IntegrityError:
        # 并发启动下另一进程已建档，忽略
        pass


# 一组与底账相容的查回数，供 /docs 试用与测试参考：
# N2: O1=10, O3=6 ；N3: O2=14, O3=2
# 登记后余数：O1=20（唯一未查边 N1）、O2=10（唯一未查边 N1）、
#            O3=10（唯一未查边 N4）、O4=10（唯一未查边 N4），全部唯一确定。
COMPATIBLE_COUNTS = {
    "N2": {"O1": 10, "O3": 6},
    "N3": {"O2": 14, "O3": 2},
}
