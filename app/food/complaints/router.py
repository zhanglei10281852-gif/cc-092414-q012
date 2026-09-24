from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.food.complaints.schemas import (
    AssignRequest,
    ComplaintRegister,
    EvidenceCreate,
    MergeRequest,
    RecheckRequest,
    ResolveRequest,
    ReviewRequest,
    StartRequest,
    SupplementRequest,
    SupplementSubmit,
)
from app.food.complaints.service import ComplaintService

router = APIRouter(prefix="/api/food/complaints", tags=["食品投诉处置"])


def service() -> ComplaintService:
    return ComplaintService()


_NOT_FOUND = {
    "complaint_not_found": "投诉不存在",
    "lot_not_found": "批次不存在",
    "shipment_not_found": "配送节点（运输单）不存在",
    "certificate_not_found": "检测证书不存在",
    "temperature_not_found": "温度记录不存在",
}

_CONFLICT = {
    "complaint_merged": "投诉已被合并，请在主投诉下办理",
    "cannot_merge_self": "不能将投诉合并到自身",
    "already_merged": "该投诉已经合并过",
    "only_open_complaints_merge": "只有办理中的投诉可以合并",
    "recheck_pending": "存在被撤销证据待复查，暂不能结案",
    "evidence_incomplete": "证据不完整（批次/证书/配送节点缺一不可），不能结案",
    "evidence_type_invalid": "证据类型无效",
}


def _raise_for(exc: KeyError | ValueError) -> None:
    code = str(exc).strip("'\"")
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=_NOT_FOUND.get(code, code)) from exc
    if code.startswith("status_not_allowed_for_"):
        raise HTTPException(status_code=409, detail="当前状态不允许该操作") from exc
    raise HTTPException(status_code=409, detail=_CONFLICT.get(code, code)) from exc


@router.post("", status_code=201)
def register_complaint(payload: ComplaintRegister):
    try:
        return service().register(payload.model_dump(), actor=payload.recorded_by)
    except KeyError as exc:
        _raise_for(exc)


@router.get("")
def list_complaints(
    status: str | None = Query(default=None),
    overdue_only: bool = Query(default=False),
    canteen_name: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    return service().list_complaints(
        status=status,
        overdue_only=overdue_only,
        canteen_name=canteen_name,
        limit=size,
        offset=(page - 1) * size,
    )


@router.get("/{complaint_id}")
def get_complaint(complaint_id: int):
    value = service().get_complaint(complaint_id)
    if value is None:
        raise HTTPException(status_code=404, detail="投诉不存在")
    return value


@router.post("/{complaint_id}/evidence", status_code=201)
def add_evidence(complaint_id: int, payload: EvidenceCreate):
    try:
        return service().add_evidence(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/evidence/recheck")
def confirm_recheck(complaint_id: int, payload: RecheckRequest):
    try:
        return service().confirm_recheck(complaint_id, payload.operator, payload.note)
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/assign")
def assign(complaint_id: int, payload: AssignRequest):
    try:
        return service().assign(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/start")
def start_handling(complaint_id: int, payload: StartRequest):
    try:
        return service().start_handling(complaint_id, payload.operator)
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/supplement/request")
def request_supplement(complaint_id: int, payload: SupplementRequest):
    try:
        return service().request_supplement(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/supplement/submit", status_code=201)
def submit_supplement(complaint_id: int, payload: SupplementSubmit):
    try:
        return service().submit_supplement(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/resolve")
def resolve(complaint_id: int, payload: ResolveRequest):
    try:
        return service().resolve(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/review")
def review(complaint_id: int, payload: ReviewRequest):
    try:
        return service().review(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)


@router.post("/{complaint_id}/merge")
def merge(complaint_id: int, payload: MergeRequest):
    try:
        return service().merge(complaint_id, payload.model_dump())
    except (KeyError, ValueError) as exc:
        _raise_for(exc)
