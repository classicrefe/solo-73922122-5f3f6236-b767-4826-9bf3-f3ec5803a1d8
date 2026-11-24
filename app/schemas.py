"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------- 请求模型 ----------------

class OverlapIn(BaseModel):
    new_code: str = Field(..., min_length=1, description="新区编码")
    old_code: str = Field(..., min_length=1, description="旧区编码")
    building_total: int = Field(..., gt=0, description="该重叠范围楼栋总数（正整数）")


class NewDistrictIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    fee: int = Field(..., gt=0, description="调档费用，正整数")


class OldDistrictIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    completed: int = Field(..., ge=0, description="该旧区历年完工楼栋总数（底账）")


class PeriodSetupIn(BaseModel):
    code: str = Field(..., min_length=1, description="期次编码，如 2023")
    name: str = Field(..., min_length=1, description="期次名称")
    old_districts: list[OldDistrictIn] = Field(..., min_length=1)
    new_districts: list[NewDistrictIn] = Field(..., min_length=1, max_length=18)
    overlaps: list[OverlapIn] = Field(..., min_length=1)


class RetrievalIn(BaseModel):
    new_code: str = Field(..., min_length=1, description="所调档的新区")
    counts: dict[str, int] = Field(
        ...,
        description="该新区分属各旧区的完工数，键为旧区编码，值为非负整数",
    )


# ---------------- 响应模型 ----------------

class OverlapOut(BaseModel):
    new_code: str
    old_code: str
    building_total: int


class NewDistrictOut(BaseModel):
    code: str
    name: str
    fee: int


class OldDistrictOut(BaseModel):
    code: str
    name: str
    completed: int


class PeriodOut(BaseModel):
    code: str
    name: str
    old_districts: list[OldDistrictOut]
    new_districts: list[NewDistrictOut]
    overlaps: list[OverlapOut]


class RetrievalItemOut(BaseModel):
    new_code: str
    old_code: str
    completed_count: int


class RegisteredNewOut(BaseModel):
    new_code: str
    fee: int
    counts: dict[str, int]


class PlanOut(BaseModel):
    period_code: str
    status: str = Field(..., description="retrieval_required / completed / infeasible")
    plan: list[str] = Field(..., description="总费用最低的待调新区编码（有序）")
    total_fee: int
    registered: list[RegisteredNewOut]
    old_remaining: dict[str, int]
    message: str


class NewCompletedOut(BaseModel):
    new_code: str
    name: str
    completed: int


class AccountingOut(BaseModel):
    period_code: str
    status: str
    determined: bool
    new_completed: list[NewCompletedOut] | None = None
    total_completed: int | None = None
    plan: list[str] | None = None
    total_fee: int | None = None
    old_remaining: dict[str, int]
    message: str


class MessageOut(BaseModel):
    detail: str
