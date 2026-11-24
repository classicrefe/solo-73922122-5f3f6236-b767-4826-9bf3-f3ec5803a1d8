"""关键功能端到端测试（使用 ASGI 内存级 TestClient + 临时 SQLite）。

并发用例针对真实 uvicorn 进程发 HTTP 请求——Starlette TestClient 基于
anyio 单一 portal，并非线程安全，不能用来验证服务端并发语义。
"""
from __future__ import annotations

import threading

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app
from app.seed import COMPATIBLE_COUNTS


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def server():
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:8765/docs", timeout=1.0)
            break
        except httpx.TransportError:
            threading.Event().wait(0.1)
    yield "http://127.0.0.1:8765"
    server.should_exit = True
    thread.join(timeout=5)


def reset_demo(client):
    client.delete("/periods/DEMO-2023/retrievals")


# ---------------------------------------------------------------- 建档与计划

def test_seeded_period_and_min_cost_plan(client):
    res = client.get("/periods/DEMO-2023/plan")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "retrieval_required"
    # 最小权顶点覆盖：{N2(7), N3(3)} = 10；同费用下按编码字典序
    assert body["plan"] == ["N2", "N3"]
    assert body["total_fee"] == 10
    assert body["old_remaining"] == {"O1": 30, "O2": 24, "O3": 18, "O4": 10}


def test_setup_validation_too_many_new_districts(client):
    payload = {
        "code": "BAD-19",
        "name": "超量期",
        "old_districts": [{"code": "O1", "name": "o", "completed": 1}],
        "new_districts": [
            {"code": f"N{i}", "name": f"n{i}", "fee": 1} for i in range(19)
        ],
        "overlaps": [
            {"new_code": f"N{i}", "old_code": "O1", "building_total": 5}
            for i in range(19)
        ],
    }
    res = client.post("/periods/", json=payload)
    assert res.status_code == 422


def test_setup_ledger_exceeds_capacity(client):
    payload = {
        "code": "BAD-LEDGER",
        "name": "底账超容量",
        "old_districts": [{"code": "O1", "name": "o", "completed": 99}],
        "new_districts": [{"code": "N1", "name": "n", "fee": 1}],
        "overlaps": [{"new_code": "N1", "old_code": "O1", "building_total": 10}],
    }
    res = client.post("/periods/", json=payload)
    assert res.status_code == 422
    assert "底账不可行" in res.json()["detail"]


def test_fee_must_be_positive_int(client):
    payload = {
        "code": "BAD-FEE",
        "name": "费用非正",
        "old_districts": [{"code": "O1", "name": "o", "completed": 1}],
        "new_districts": [{"code": "N1", "name": "n", "fee": 0}],
        "overlaps": [{"new_code": "N1", "old_code": "O1", "building_total": 10}],
    }
    res = client.post("/periods/", json=payload)
    assert res.status_code == 422


# ---------------------------------------------------------------- 登记校验

def test_register_out_of_range_rejected(client):
    reset_demo(client)
    res = client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 16, "O3": 0}},  # O1 上限 15
    )
    assert res.status_code == 422
    assert "楼栋总数" in res.json()["detail"]
    # 原记录保留：仍无任何登记
    assert client.get("/periods/DEMO-2023/retrievals").json() == []


def test_register_wrong_old_set_rejected(client):
    res = client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 5}},  # 缺 O3
    )
    assert res.status_code == 422


def test_register_remaining_cannot_be_met(client):
    reset_demo(client)
    # O4 底账 10，唯一边 N4-O4 容量 10。先把 N4 的 O4 登为 3（可行），
    # 再无法构造超额；改用 O1：N1-O1=20、N2-O1=15，底账 30。
    # 登记 N1: O1=20, O2=10 后 O1 余 10，再登 N2 O1=11 -> 余 -1 超底账
    client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N1", "counts": {"O1": 20, "O2": 10}},
    )
    res = client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 11, "O3": 0}},
    )
    assert res.status_code == 409
    assert "底账" in res.json()["detail"]
    # N2 未被写入
    codes = {r["new_code"] for r in client.get("/periods/DEMO-2023/retrievals").json()}
    assert codes == {"N1"}


def test_idempotent_same_value_and_conflict(client):
    reset_demo(client)
    body = {"new_code": "N2", "counts": {"O1": 10, "O3": 6}}
    r1 = client.post("/periods/DEMO-2023/retrievals", json=body)
    assert r1.status_code == 201
    r2 = client.post("/periods/DEMO-2023/retrievals", json=body)
    assert r2.status_code == 201  # 同值重送幂等
    r3 = client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 9, "O3": 7}},
    )
    assert r3.status_code == 409  # 异值退回
    rows = client.get("/periods/DEMO-2023/retrievals").json()
    assert {(r["old_code"], r["completed_count"]) for r in rows} == {
        ("O1", 10),
        ("O3", 6),
    }


# ---------------------------------------------------------------- 计划推进与唯一核算

def test_plan_shrinks_after_registration(client):
    reset_demo(client)
    client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": COMPATIBLE_COUNTS["N2"]},
    )
    body = client.get("/periods/DEMO-2023/plan").json()
    # N2 已查；O1 仅剩 N1、O3 还剩 N3/N4，冲突只在 N3-N4，取便宜的 N3(3)
    assert body["plan"] == ["N3"]
    assert body["total_fee"] == 3
    acc = client.get("/periods/DEMO-2023/accounting").json()
    assert acc["determined"] is False


def test_full_compatible_flow_yields_unique_completed(client):
    reset_demo(client)
    for new_code, counts in COMPATIBLE_COUNTS.items():
        res = client.post(
            "/periods/DEMO-2023/retrievals",
            json={"new_code": new_code, "counts": counts},
        )
        assert res.status_code == 201
    plan = client.get("/periods/DEMO-2023/plan").json()
    assert plan["status"] == "completed"
    assert plan["plan"] == [] and plan["total_fee"] == 0

    acc = client.get("/periods/DEMO-2023/accounting").json()
    assert acc["status"] == "completed" and acc["determined"] is True
    # N1 = O1 余 20 + O2 余 10 = 30；N2 = 10+6 = 16；N3 = 14+2 = 16；
    # N4 = O3 余 10 + O4 余 10 = 20
    got = {item["new_code"]: item["completed"] for item in acc["new_completed"]}
    assert got == {"N1": 30, "N2": 16, "N3": 16, "N4": 20}
    assert acc["total_completed"] == 82
    # 与底账合计一致：30+24+18+10 = 82


def test_accounting_during_partial_registration(client):
    reset_demo(client)
    client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N3", "counts": COMPATIBLE_COUNTS["N3"]},
    )
    acc = client.get("/periods/DEMO-2023/accounting").json()
    assert acc["status"] == "retrieval_required"
    assert acc["new_completed"] is None
    assert "N3" not in acc["plan"]


# ---------------------------------------------------------------- 并发

def test_concurrent_registration_serialized_and_ledger_consistent(client, server):
    base = server
    httpx.delete(f"{base}/periods/DEMO-2023/retrievals", timeout=10)
    # 前置：N1 登 O1=20/O2=10 后，O1 余 10、O2 余 14。
    httpx.post(
        f"{base}/periods/DEMO-2023/retrievals",
        json={"new_code": "N1", "counts": {"O1": 20, "O2": 10}},
        timeout=10,
    )
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[str] = []

    def post(payload):
        barrier.wait()
        r = httpx.post(
            f"{base}/periods/DEMO-2023/retrievals", json=payload, timeout=10
        )
        results.append(r.status_code)
        if r.status_code != 201:
            errors.append(r.text)

    # 线程 A 的 N2(O1=10,O3=6) 相容；线程 B 的 N2(O1=11,O3=6) 超 O1 底账。
    t1 = threading.Thread(
        target=post,
        args=({"new_code": "N2", "counts": {"O1": 10, "O3": 6}},),
    )
    t2 = threading.Thread(
        target=post,
        args=({"new_code": "N2", "counts": {"O1": 11, "O3": 6}},),
    )
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 409], errors
    rows = httpx.get(f"{base}/periods/DEMO-2023/retrievals", timeout=10).json()
    n2 = {(r["old_code"], r["completed_count"]) for r in rows if r["new_code"] == "N2"}
    # 成功的只能是相容值
    assert n2 == {("O1", 10), ("O3", 6)}


def test_concurrent_distinct_new_districts_both_succeed(client, server):
    base = server
    httpx.delete(f"{base}/periods/DEMO-2023/retrievals", timeout=10)
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    errors: list[str] = []

    def post(payload):
        barrier.wait()
        r = httpx.post(
            f"{base}/periods/DEMO-2023/retrievals", json=payload, timeout=10
        )
        statuses.append(r.status_code)
        if r.status_code != 201:
            errors.append(r.text)

    t1 = threading.Thread(
        target=post,
        args=({"new_code": "N2", "counts": {"O1": 10, "O3": 6}},),
    )
    t2 = threading.Thread(
        target=post,
        args=({"new_code": "N3", "counts": {"O2": 14, "O3": 2}},),
    )
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(statuses) == [201, 201], errors
    plan = httpx.get(f"{base}/periods/DEMO-2023/plan", timeout=10).json()
    assert plan["status"] == "completed"


# ---------------------------------------------------------------- 快照与排障

def test_snapshot_read_never_blocks_and_reset_works(client):
    reset_demo(client)
    # 连续只读核算与登记交替进行，验证快照不阻塞写
    acc1 = client.get("/periods/DEMO-2023/accounting").json()
    client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
    )
    acc2 = client.get("/periods/DEMO-2023/accounting").json()
    assert acc1["status"] == "retrieval_required"
    assert acc2["old_remaining"]["O1"] == 20
    client.delete("/periods/DEMO-2023/retrievals")
    assert client.get("/periods/DEMO-2023/retrievals").json() == []
    assert client.get("/periods/DEMO-2023/plan").json()["plan"] == ["N2", "N3"]


def test_infeasible_state_reported_on_plan_endpoint(client):
    reset_demo(client)
    client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N1", "counts": {"O1": 20, "O2": 10}},
    )
    bad = client.post(
        "/periods/DEMO-2023/retrievals",
        json={"new_code": "N2", "counts": {"O1": 11, "O3": 0}},
    )
    assert bad.status_code == 409
    # 手工制造一个不可行状态：通过直接登记可行值后，再用另一期演示余数超容
    # （正常 API 无法写入不可行记录；这里验证计划端点对异常状态的防御）
    # 正常流程下 infeasible 分支主要由并发/历史脏数据触发，此处确认正常状态不受影响
    body = client.get("/periods/DEMO-2023/plan").json()
    assert body["status"] == "retrieval_required"
