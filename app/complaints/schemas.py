from __future__ import annotations

from pydantic import BaseModel, Field


class ComplaintRegister(BaseModel):
    contact_name: str = Field(..., min_length=1, max_length=80)
    contact_phone: str = Field(..., min_length=3, max_length=40)
    contact_address: str = Field(default="", max_length=200)
    canteen_name: str = Field(..., min_length=1, max_length=120)
    canteen_address: str = Field(default="", max_length=200)
    description: str = Field(..., min_length=1, max_length=1000)
    incident_at: str = Field(..., min_length=10, max_length=40)
    received_at: str | None = Field(default=None, max_length=40)
    hotline_channel: str = Field(default="12315", max_length=40)
    lot_id: int | None = None
    shipment_id: int | None = None


class EvidenceCreate(BaseModel):
    evidence_type: str = Field(..., pattern="^(certificate|temperature|lot|shipment|note|file)$")
    source_id: int | None = None
    source_descriptor: str = Field(default="", max_length=200)
    snapshot: dict | str | None = None
    note: str = Field(default="", max_length=1000)


class EvidenceRevoke(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


class EvidenceReview(BaseModel):
    note: str = Field(..., min_length=1, max_length=300)
    keep_review_flag: bool = False


class ComplaintAssign(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=80)
    deadline_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    deadline: str | None = Field(default=None, max_length=40)


class SupplementRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    content: str = Field(..., min_length=1, max_length=1000)


class SupplementSubmit(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    content: str = Field(..., min_length=1, max_length=1000)
    submitted_by: str | None = Field(default=None, max_length=80)


class HandlingSubmit(BaseModel):
    result: str = Field(..., min_length=1, max_length=1000)


class CloseReview(BaseModel):
    passed: bool
    opinion: str = Field(..., min_length=1, max_length=500)


class ComplaintMerge(BaseModel):
    master_id: int
    reason: str = Field(..., min_length=1, max_length=300)
