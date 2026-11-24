"""FastAPI 应用：补算 API。

端点
----
POST   /periods/                     建立期次底账（旧区完工数、重叠楼栋总数、调档费用）
GET    /periods/                     期次列表
GET    /periods/{period_code}        期次底账详情
GET    /periods/{period_code}/plan   只读快照：最低费用待调名单与金额
GET    /periods/{period_code}/accounting  核算；无需再调档时返回各新区完工数
POST   /periods/{period_code}/retrievals  登记一份新区档案（同值幂等、异值退回）
GET    /periods/{period_code}/retrievals  已登记档案
DELETE /periods/{period_code}/retrievals  清空登记（便于在 /docs 重复试用）
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import services
from .db import connect, get_conn, init_db, snapshot_read
from .schemas import (
    AccountingOut,
    MessageOut,
    NewCompletedOut,
    PeriodOut,
    PeriodSetupIn,
    PlanOut,
    RegisteredNewOut,
    RetrievalIn,
    RetrievalItemOut,
)
from .seed import seed_demo


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if os.environ.get("APP_SEED", "1") == "1":
        # 仅在库中无期次时写入演示期次，重复启动不会覆盖已登记数据
        conn = connect()
        try:
            seed_demo(conn)
        finally:
            conn.close()
    yield


app = FastAPI(
    title="建设年报改造完工楼栋补算 API",
    version="1.0.0",
    description=(
        "按现行区界补算历年改造完工楼栋数。"
        "登记调档结果并在底账相容前提下，给出总费用最低的待调名单；"
        "无需再调档时唯一核算各新区完工数。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(services.AppError)
def app_error_handler(_request, exc: services.AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.post("/periods/", status_code=201, response_model=MessageOut, tags=["期次底账"])
def create_period(payload: PeriodSetupIn, conn: sqlite3.Connection = Depends(get_conn)):
    services.create_period(conn, payload)
    return {"detail": f"期次 {payload.code} 建档完成"}


@app.get("/periods/", response_model=list[PeriodOut], tags=["期次底账"])
def list_periods(conn: sqlite3.Connection = Depends(get_conn)):
    with snapshot_read(conn):
        rows = conn.execute("SELECT code FROM period ORDER BY id").fetchall()
        return [_period_payload(conn, r["code"]) for r in rows]


@app.get("/periods/{period_code}", response_model=PeriodOut, tags=["期次底账"])
def get_period(period_code: str, conn: sqlite3.Connection = Depends(get_conn)):
    with snapshot_read(conn):
        return _period_payload(conn, period_code)


def _period_payload(conn: sqlite3.Connection, period_code: str) -> dict:
    data = services._load_period(conn, period_code)
    return {
        "code": data.code,
        "name": data.name,
        "old_districts": [
            {"code": c, "name": data.old_names[c], "completed": data.old_completed[c]}
            for c in data.old_codes
        ],
        "new_districts": [
            {"code": c, "name": data.new_names[c], "fee": data.new_fee[c]}
            for c in data.new_codes
        ],
        "overlaps": [
            {"new_code": n, "old_code": o, "building_total": t}
            for o in data.old_codes
            for n, t in data.old_edges[o]
        ],
    }


@app.get(
    "/periods/{period_code}/plan",
    response_model=PlanOut,
    tags=["核算计划"],
    summary="总费用最低的待调名单（只读同一已提交状态）",
)
def get_plan(period_code: str, conn: sqlite3.Connection = Depends(get_conn)):
    with snapshot_read(conn):
        data = services._load_period(conn, period_code)
        try:
            status, state, registered, plan, total_fee = services.evaluate(data)
        except services.AppError as exc:
            bad_state = services._old_state(data, data.registered)
            return PlanOut(
                period_code=period_code,
                status="infeasible",
                plan=[],
                total_fee=0,
                registered=_registered_out(data, registered=data.registered),
                old_remaining={o: int(s["remaining"]) for o, s in bad_state.items()},
                message=f"当前登记与底账不相容：{exc.detail}；失败登记已保留，请处理后重试",
            )
        message = {
            "retrieval_required": "照单调档即可在相容前提下唯一算出各新区完工数",
            "completed": "已无需再调档，可调用 /accounting 获取各新区完工数",
        }[status]
        return PlanOut(
            period_code=period_code,
            status=status,
            plan=plan,
            total_fee=total_fee,
            registered=_registered_out(data, registered=registered),
            old_remaining={o: int(s["remaining"]) for o, s in state.items()},
            message=message,
        )


def _registered_out(
    data: services.PeriodData, registered: dict[str, dict[str, int]]
) -> list[RegisteredNewOut]:
    return [
        RegisteredNewOut(
            new_code=n, fee=data.new_fee[n], counts=dict(v)
        )
        for n, v in sorted(registered.items())
    ]


@app.get(
    "/periods/{period_code}/accounting",
    response_model=AccountingOut,
    tags=["核算计划"],
    summary="核算；无需再调档时返回各新区完工数",
)
def get_accounting(period_code: str, conn: sqlite3.Connection = Depends(get_conn)):
    with snapshot_read(conn):
        data = services._load_period(conn, period_code)
        try:
            status, state, _registered, plan, total_fee = services.evaluate(data)
        except services.AppError as exc:
            return AccountingOut(
                period_code=period_code,
                status="infeasible",
                determined=False,
                new_completed=None,
                total_completed=None,
                plan=None,
                total_fee=None,
                old_remaining={
                    o: int(s["remaining"])
                    for o, s in services._old_state(data, data.registered).items()
                },
                message=f"登记与底账不相容：{exc.detail}",
            )

        if status != "completed":
            return AccountingOut(
                period_code=period_code,
                status=status,
                determined=False,
                new_completed=None,
                total_completed=None,
                plan=plan,
                total_fee=total_fee,
                old_remaining={o: int(s["remaining"]) for o, s in state.items()},
                message="仍需调档，先按 /plan 名单登记查回数",
            )

        rows = services.solve_completed(data, state)
        items = [
            NewCompletedOut(new_code=n, name=name, completed=v) for n, name, v in rows
        ]
        total = sum(v for _, _, v in rows)
        # 校核：新区完工数之和应等于各旧区底账完工数之和
        ledger_total = sum(data.old_completed.values())
        note = "各新区完工数已唯一确定"
        if total != ledger_total:
            note += f"（警告：合计 {total} 与底账合计 {ledger_total} 不符）"
        return AccountingOut(
            period_code=period_code,
            status="completed",
            determined=True,
            new_completed=items,
            total_completed=total,
            plan=[],
            total_fee=0,
            old_remaining={o: 0 for o in data.old_codes},
            message=note,
        )


@app.post(
    "/periods/{period_code}/retrievals",
    response_model=PlanOut,
    status_code=201,
    tags=["调档登记"],
    summary="登记一份新区档案（同值幂等，异值退回，并发合起来仍须对底账）",
)
def post_retrieval(
    period_code: str,
    payload: RetrievalIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    merged = services.register_retrieval(conn, period_code, payload)
    # 登记成功后，用新的只读快照返回最新计划
    with snapshot_read(conn):
        data = services._load_period(conn, period_code)
        status, state, registered, plan, total_fee = services.evaluate(data)
        return PlanOut(
            period_code=period_code,
            status=status,
            plan=plan,
            total_fee=total_fee,
            registered=_registered_out(data, registered=registered),
            old_remaining={o: int(s["remaining"]) for o, s in state.items()},
            message="登记成功" + (
                "，已无需再调档" if status == "completed" else "，请继续按名单调档"
            ),
        )


@app.get(
    "/periods/{period_code}/retrievals",
    response_model=list[RetrievalItemOut],
    tags=["调档登记"],
)
def list_retrievals(period_code: str, conn: sqlite3.Connection = Depends(get_conn)):
    with snapshot_read(conn):
        data = services._load_period(conn, period_code)
        out: list[RetrievalItemOut] = []
        for ncode in data.new_codes:
            for old_code, v in data.registered.get(ncode, {}).items():
                out.append(
                    RetrievalItemOut(
                        new_code=ncode, old_code=old_code, completed_count=v
                    )
                )
        return out


@app.delete(
    "/periods/{period_code}/retrievals",
    response_model=MessageOut,
    tags=["调档登记"],
    summary="清空该期登记（演示/重试用）",
)
def delete_retrievals(period_code: str, conn: sqlite3.Connection = Depends(get_conn)):
    services.reset_retrievals(conn, period_code)
    return {"detail": f"期次 {period_code} 的登记已清空"}

