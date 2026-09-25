from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.complaints.schemas import (
    CloseReview,
    ComplaintAssign,
    ComplaintMerge,
    ComplaintRegister,
    EvidenceCreate,
    EvidenceReview,
    EvidenceRevoke,
    HandlingSubmit,
    SupplementRequest,
    SupplementSubmit,
)
from app.complaints.service import ComplaintService

router = APIRouter(prefix="/api/food/complaints", tags=["食品投诉"])


def service() -> ComplaintService:
    return ComplaintService()


def _not_found(exc: KeyError) -> HTTPException:
    messages = {
        "complaint_not_found": "投诉不存在",
        "lot_not_found": "批次不存在",
        "shipment_not_found": "配送单不存在",
        "test_result_not_found": "检测结果/证书不存在",
        "evidence_not_found": "证据不存在",
    }
    return HTTPException(status_code=404, detail=messages.get(str(exc), str(exc)))


@router.post("", status_code=201)
def register_complaint(payload: ComplaintRegister):
    try:
        return service().register(payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("")
def list_complaints(
    status: str | None = None,
    assignee: str | None = None,
    lot_id: int | None = None,
    include_merged: bool = False,
    overdue_only: bool = False,
):
    return service().list_complaints(
        status=status, assignee=assignee, lot_id=lot_id,
        include_merged=include_merged, overdue_only=overdue_only,
    )


@router.get("/evidence-reviews")
def pending_evidence_reviews():
    return {"data": service().pending_evidence_reviews()}


@router.get("/{complaint_id}")
def get_complaint(complaint_id: int):
    try:
        return service().detail(complaint_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/{complaint_id}/evidence", status_code=201)
def add_evidence(complaint_id: int, payload: EvidenceCreate):
    try:
        return service().add_evidence(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/evidence/{evidence_id}/revoke", status_code=200)
def revoke_evidence(complaint_id: int, evidence_id: int, payload: EvidenceRevoke):
    try:
        return service().revoke_evidence(complaint_id, evidence_id, payload.reason)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/{complaint_id}/evidence/{evidence_id}/review", status_code=200)
def review_evidence(complaint_id: int, evidence_id: int, payload: EvidenceReview):
    try:
        return service().review_evidence(
            complaint_id, evidence_id, payload.note, payload.keep_review_flag
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/assign", status_code=200)
def assign_complaint(complaint_id: int, payload: ComplaintAssign):
    try:
        return service().assign(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/start", status_code=200)
def start_handling(complaint_id: int):
    try:
        return service().start_handling(complaint_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/supplement-request", status_code=200)
def request_supplement(complaint_id: int, payload: SupplementRequest):
    try:
        return service().request_supplement(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/supplements", status_code=201)
def submit_supplement(complaint_id: int, payload: SupplementSubmit):
    try:
        return service().submit_supplement(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/submit", status_code=200)
def submit_handling(complaint_id: int, payload: HandlingSubmit):
    try:
        return service().submit_handling(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{complaint_id}/review", status_code=200)
def review_close(complaint_id: int, payload: CloseReview):
    try:
        return service().review_close(complaint_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{source_id}/merge", status_code=200)
def merge_complaint(source_id: int, payload: ComplaintMerge):
    try:
        return service().merge(source_id, payload.master_id, payload.reason)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
