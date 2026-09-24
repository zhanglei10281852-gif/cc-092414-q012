"""食品安全投诉处置的输入模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ComplaintRegister(BaseModel):
    canteen_name: str = Field(..., min_length=1, max_length=120, description="被投诉食堂名称")
    description: str = Field(..., min_length=1, max_length=1000, description="异味等问题描述")
    contact_name: str = Field(..., min_length=1, max_length=80, description="投诉联系人（独立保留）")
    contact_phone: str = Field(..., min_length=3, max_length=40)
    contact_channel: str = Field(default="hotline", min_length=1, max_length=40)
    occurred_at: str = Field(..., min_length=20, max_length=40, description="问题发生时间 ISO8601")
    recorded_by: str = Field(default="热线人员", min_length=1, max_length=80)
    lot_code: str | None = Field(default=None, max_length=64, description="关联供应批次编码")
    shipment_code: str | None = Field(default=None, max_length=64, description="关联配送节点（运输单）编码")
    link_deadline_hours: int = Field(default=24, ge=1, le=168, description="关联批次/证据的规定时限（小时）")


class AssignRequest(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=80, description="当前责任人")
    deadline_hours: int = Field(default=72, ge=1, le=720, description="限时处置时限（小时）")
    operator: str = Field(default="热线主管", min_length=1, max_length=80)


class StartRequest(BaseModel):
    operator: str = Field(..., min_length=1, max_length=80)


class EvidenceCreate(BaseModel):
    evidence_type: str = Field(..., pattern="^(lot|certificate|shipment|temperature)$")
    source_id: int = Field(..., ge=1)
    title: str = Field(default="", max_length=200)
    operator: str = Field(default="热线人员", min_length=1, max_length=80)


class SupplementRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500, description="要求补充材料的原因")
    requested_by: str = Field(..., min_length=1, max_length=80)


class SupplementSubmit(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=2000)
    submitted_by: str = Field(default="承办人", min_length=1, max_length=80)
    material_type: str = Field(default="supplement", min_length=1, max_length=40)


class ResolveRequest(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=1000, description="处置结果与结案理由")
    operator: str = Field(..., min_length=1, max_length=80)


class ReviewRequest(BaseModel):
    passed: bool
    opinion: str = Field(..., min_length=1, max_length=1000, description="复核意见")
    reviewer: str = Field(..., min_length=1, max_length=80)
    deadline_hours: int = Field(default=72, ge=1, le=720, description="复核不通过、退回重办时的新时限")


class MergeRequest(BaseModel):
    duplicate_id: int = Field(..., ge=1, description="被合并的重复投诉编号")
    reason: str = Field(default="同一食堂同一批次的重复投诉", max_length=300)


class RecheckRequest(BaseModel):
    operator: str = Field(..., min_length=1, max_length=80)
    note: str = Field(default="复查完成，证据已重新核实", max_length=500)
