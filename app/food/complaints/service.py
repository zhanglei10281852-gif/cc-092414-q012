"""食品安全投诉处置：登记、证据快照、分派、限时处置、补充材料、结案复核与合并。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_no TEXT NOT NULL UNIQUE,
    canteen_name TEXT NOT NULL,
    description TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    contact_phone TEXT NOT NULL,
    contact_channel TEXT NOT NULL DEFAULT 'hotline',
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK(status IN ('registered','assigned','processing','awaiting_supplement','resolved','reviewing','closed','reopened','merged')),
    assignee TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT '',
    lot_id INTEGER,
    lot_code TEXT NOT NULL DEFAULT '',
    shipment_id INTEGER,
    shipment_code TEXT NOT NULL DEFAULT '',
    link_deadline TEXT,
    handle_deadline TEXT,
    needs_recheck INTEGER NOT NULL DEFAULT 0 CHECK(needs_recheck IN (0,1)),
    merged_into INTEGER REFERENCES food_complaints(id),
    resolution TEXT NOT NULL DEFAULT '',
    close_reason TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_complaint_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE RESTRICT,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('lot','certificate','shipment','temperature')),
    source_id INTEGER NOT NULL,
    source_code TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    source_status TEXT NOT NULL DEFAULT 'active' CHECK(source_status IN ('active','revoked','missing')),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK(review_required IN (0,1)),
    review_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(complaint_id, evidence_type, source_id)
);
CREATE TABLE IF NOT EXISTS food_complaint_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE RESTRICT,
    material_type TEXT NOT NULL DEFAULT 'supplement',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    submitted_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_complaint_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_id INTEGER NOT NULL REFERENCES food_complaints(id),
    duplicate_id INTEGER NOT NULL REFERENCES food_complaints(id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(duplicate_id)
);
CREATE TABLE IF NOT EXISTS food_complaint_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_complaints_status ON food_complaints(status, created_at);
CREATE INDEX IF NOT EXISTS idx_food_complaint_ev_source ON food_complaint_evidence(evidence_type, source_id);
CREATE INDEX IF NOT EXISTS idx_food_complaint_timeline ON food_complaint_timeline(complaint_id, created_at, id);
"""

# 限时处置过程中仍在计时的状态
OPEN_STATUSES = ("registered", "assigned", "processing", "awaiting_supplement", "reopened")
# 证据完整度要求的必配证据类别
REQUIRED_EVIDENCE = ("lot", "certificate", "shipment")
STATUS_LABELS = {
    "registered": "已登记",
    "assigned": "已分派",
    "processing": "处置中",
    "awaiting_supplement": "待补充材料",
    "resolved": "待复核",
    "reviewing": "复查中",
    "closed": "已结案",
    "reopened": "退回重办",
    "merged": "已合并",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _remaining(deadline: str | None, now: datetime) -> int | None:
    moment = _parse(deadline)
    if moment is None:
        return None
    return int((moment - now).total_seconds())


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _log(connection: sqlite3.Connection, complaint_id: int, action: str, actor: str = "", detail: str = "", now: str | None = None) -> None:
    connection.execute(
        "INSERT INTO food_complaint_timeline(complaint_id,action,actor,detail,created_at) VALUES(?,?,?,?,?)",
        (complaint_id, action, actor, detail, now or _now()),
    )


def _build_snapshot(connection: sqlite3.Connection, evidence_type: str, source_id: int) -> dict[str, Any]:
    """抓取引用对象的当时状态；撤销后本快照不再改变。"""
    if evidence_type == "lot":
        row = connection.execute("SELECT * FROM food_lots WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError("lot_not_found")
        return dict(row)
    if evidence_type == "shipment":
        row = connection.execute(
            """SELECT s.*, l.lot_code, l.product_name
               FROM food_shipments s JOIN food_lots l ON l.id=s.lot_id WHERE s.id=?""",
            (source_id,),
        ).fetchone()
        if row is None:
            raise KeyError("shipment_not_found")
        return dict(row)
    if evidence_type == "certificate":
        row = connection.execute(
            """SELECT r.*, s.sample_code, s.lot_id, l.lot_code, l.product_name
               FROM food_test_results r
               JOIN food_samples s ON s.id=r.sample_id
               JOIN food_lots l ON l.id=s.lot_id
               WHERE r.id=?""",
            (source_id,),
        ).fetchone()
        if row is None:
            raise KeyError("certificate_not_found")
        return dict(row)
    if evidence_type == "temperature":
        row = connection.execute(
            """SELECT t.*, sh.shipment_code, sh.carrier, sh.vehicle_no, l.lot_code
               FROM food_temperatures t
               JOIN food_shipments sh ON sh.id=t.shipment_id
               JOIN food_lots l ON l.id=sh.lot_id
               WHERE t.id=?""",
            (source_id,),
        ).fetchone()
        if row is None:
            raise KeyError("temperature_not_found")
        return dict(row)
    raise ValueError("evidence_type_invalid")


def _source_code(evidence_type: str, snapshot: dict[str, Any]) -> str:
    if evidence_type == "lot":
        return str(snapshot.get("lot_code", ""))
    if evidence_type == "shipment":
        return str(snapshot.get("shipment_code", ""))
    if evidence_type == "certificate":
        return str(snapshot.get("certificate_no", "")) or f"RESULT-{snapshot.get('id')}"
    return f"TEMP-{snapshot.get('id')}"


def _evidence_overview(connection: sqlite3.Connection, complaint_id: int) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT * FROM food_complaint_evidence WHERE complaint_id=?",
        (complaint_id,),
    ).fetchall()
    # 有效证据：当前有效，或被撤销但已完成复查（历史快照经复查后继续作为证据）
    valid_types = {row["evidence_type"] for row in rows if row["review_required"] == 0}
    revoked = [
        {"id": row["id"], "evidence_type": row["evidence_type"], "source_id": row["source_id"], "review_reason": row["review_reason"]}
        for row in rows if row["source_status"] == "revoked"
    ]
    missing = [name for name in REQUIRED_EVIDENCE if name not in valid_types]
    score = round(len([name for name in REQUIRED_EVIDENCE if name in valid_types]) / len(REQUIRED_EVIDENCE), 4)
    recheck_pending = connection.execute(
        "SELECT COUNT(*) FROM food_complaint_evidence WHERE complaint_id=? AND review_required=1",
        (complaint_id,),
    ).fetchone()[0]
    return {
        "total": len(rows),
        "complete": not missing,
        "completeness": score,
        "missing_types": missing,
        "revoked_evidence": revoked,
        "recheck_pending": recheck_pending,
    }


def propagate_source_revocation(connection: sqlite3.Connection, evidence_type: str, source_id: int, reason: str, operator: str) -> list[int]:
    """证书/温度记录撤销时：快照原样保留，证据标记待复查，并联动案件状态。在既有事务内调用。"""
    now = _now()
    affected = connection.execute(
        "SELECT id, complaint_id FROM food_complaint_evidence WHERE evidence_type=? AND source_id=? AND source_status='active'",
        (evidence_type, source_id),
    ).fetchall()
    complaint_ids: list[int] = []
    for item in affected:
        connection.execute(
            "UPDATE food_complaint_evidence SET source_status='revoked', review_required=1, review_reason=? WHERE id=?",
            (reason, item["id"]),
        )
        complaint_ids.append(item["complaint_id"])
    for complaint_id in dict.fromkeys(complaint_ids):
        complaint = connection.execute("SELECT status FROM food_complaints WHERE id=?", (complaint_id,)).fetchone()
        connection.execute("UPDATE food_complaints SET needs_recheck=1, updated_at=? WHERE id=?", (now, complaint_id))
        _log(connection, complaint_id, "evidence.revoked", operator, f"{evidence_type}#{source_id} 被撤销：{reason}", now)
        if complaint is not None and complaint["status"] == "closed":
            # 已结案也不能丢失原始证据：转入复查，结案理由保留
            connection.execute(
                "UPDATE food_complaints SET status='reviewing', updated_at=? WHERE id=?",
                (now, complaint_id),
            )
            _log(connection, complaint_id, "case.reopen_recheck", operator, "结案后引用证据被撤销，重新复查", now)
    return complaint_ids


class ComplaintService:
    """投诉登记到结案复核的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ---- 内部工具 ----
    def _get(self, connection: sqlite3.Connection, complaint_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone()
        if row is None:
            raise KeyError("complaint_not_found")
        return row

    @staticmethod
    def _require_open(row: sqlite3.Row, allowed: tuple[str, ...], action: str) -> None:
        if row["status"] == "merged":
            raise ValueError("complaint_merged")
        if row["status"] not in allowed:
            raise ValueError(f"status_not_allowed_for_{action}")

    def _attach_evidence(self, connection: sqlite3.Connection, complaint_id: int, evidence_type: str, source_id: int, title: str, operator: str, now: str) -> dict[str, Any]:
        snapshot = _build_snapshot(connection, evidence_type, source_id)
        snapshot_json = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        snapshot_hash = _canonical_hash(snapshot)
        source_code = _source_code(evidence_type, snapshot)
        revoked = bool(snapshot.get("revoked", 0))
        try:
            cursor = connection.execute(
                """INSERT INTO food_complaint_evidence
                   (complaint_id,evidence_type,source_id,source_code,title,snapshot_json,snapshot_hash,source_status,review_required,review_reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (complaint_id, evidence_type, source_id, source_code, title, snapshot_json, snapshot_hash,
                 "revoked" if revoked else "active", 1 if revoked else 0, "引用时已撤销" if revoked else "", now),
            )
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT * FROM food_complaint_evidence WHERE complaint_id=? AND evidence_type=? AND source_id=?",
                (complaint_id, evidence_type, source_id),
            ).fetchone()
            return dict(existing) if existing else {}
        evidence_id = cursor.lastrowid
        _log(connection, complaint_id, "evidence.add", operator, f"{evidence_type}#{source_id} 快照已固定", now)
        return dict(connection.execute("SELECT * FROM food_complaint_evidence WHERE id=?", (evidence_id,)).fetchone())

    # ---- 1. 登记 ----
    def register(self, payload: dict[str, Any], actor: str = "热线人员") -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        link_deadline = (now_dt + timedelta(hours=payload.get("link_deadline_hours", 24))).isoformat(timespec="seconds")
        with transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM food_complaints").fetchone()[0]
            complaint_no = f"TS{datetime.now(timezone.utc).strftime('%Y%m%d')}-{count + 1:04d}"
            lot_id = None
            lot_code = ""
            shipment_id = None
            shipment_code = ""
            if payload.get("lot_code"):
                lot = connection.execute("SELECT id FROM food_lots WHERE lot_code=?", (payload["lot_code"].strip().upper(),)).fetchone()
                if lot is None:
                    raise KeyError("lot_not_found")
                lot_id, lot_code = lot["id"], payload["lot_code"].strip().upper()
            if payload.get("shipment_code"):
                shipment = connection.execute("SELECT id FROM food_shipments WHERE shipment_code=?", (payload["shipment_code"].strip().upper(),)).fetchone()
                if shipment is None:
                    raise KeyError("shipment_not_found")
                shipment_id, shipment_code = shipment["id"], payload["shipment_code"].strip().upper()
            cursor = connection.execute(
                """INSERT INTO food_complaints
                   (complaint_no,canteen_name,description,contact_name,contact_phone,contact_channel,occurred_at,
                    status,recorded_by,lot_id,lot_code,shipment_id,shipment_code,link_deadline,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'registered',?,?,?,?,?,?,?,?)""",
                (complaint_no, payload["canteen_name"], payload["description"], payload["contact_name"],
                 payload["contact_phone"], payload.get("contact_channel", "hotline"), payload["occurred_at"], actor,
                 lot_id, lot_code, shipment_id, shipment_code, link_deadline, now, now),
            )
            complaint_id = cursor.lastrowid
            _log(connection, complaint_id, "complaint.register", actor, f"热线登记：{payload['canteen_name']}", now)
            if lot_id is not None:
                self._attach_evidence(connection, complaint_id, "lot", lot_id, "供应批次", actor, now)
            if shipment_id is not None:
                self._attach_evidence(connection, complaint_id, "shipment", shipment_id, "配送节点", actor, now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    # ---- 2. 证据引用 ----
    def add_evidence(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, OPEN_STATUSES, "add_evidence")
            now = _now()
            evidence = self._attach_evidence(connection, complaint_id, payload["evidence_type"], payload["source_id"], payload["title"], payload["operator"], now)
            connection.execute("UPDATE food_complaints SET updated_at=? WHERE id=?", (now, complaint_id))
            return evidence

    def confirm_recheck(self, complaint_id: int, operator: str, note: str = "复查完成，证据已重新核实") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            if row["status"] == "merged":
                raise ValueError("complaint_merged")
            now = _now()
            # 复查确认后清除待复查标记；source_status='revoked' 与快照、撤销原因仍永久保留
            connection.execute(
                "UPDATE food_complaint_evidence SET review_required=0 WHERE complaint_id=?",
                (complaint_id,),
            )
            pending = connection.execute(
                "SELECT COUNT(*) FROM food_complaint_evidence WHERE complaint_id=? AND review_required=1",
                (complaint_id,),
            ).fetchone()[0]
            if pending == 0:
                connection.execute("UPDATE food_complaints SET needs_recheck=0, updated_at=? WHERE id=?", (now, complaint_id))
            _log(connection, complaint_id, "evidence.recheck_confirmed", operator, note, now)
            if row["status"] == "reviewing" and pending == 0:
                connection.execute("UPDATE food_complaints SET status='resolved', updated_at=? WHERE id=?", (now, complaint_id))
                _log(connection, complaint_id, "case.back_to_review", operator, "复查完成，回到结案复核", now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    # ---- 3. 分派与限时处置 ----
    def assign(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("registered", "reopened", "assigned", "processing"), "assign")
            now = _now()
            handle_deadline = (datetime.fromisoformat(now) + timedelta(hours=payload["deadline_hours"])).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE food_complaints SET status='assigned', assignee=?, handle_deadline=?, updated_at=? WHERE id=?",
                (payload["assignee"], handle_deadline, now, complaint_id),
            )
            _log(connection, complaint_id, "complaint.assign", payload["operator"], f"分派给 {payload['assignee']}，限时 {payload['deadline_hours']} 小时", now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    def start_handling(self, complaint_id: int, operator: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("assigned",), "start")
            now = _now()
            connection.execute("UPDATE food_complaints SET status='processing', updated_at=? WHERE id=?", (now, complaint_id))
            _log(connection, complaint_id, "handling.start", operator, "责任人开始处置", now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    # ---- 4. 补充材料 ----
    def request_supplement(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("assigned", "processing", "reopened"), "request_supplement")
            now = _now()
            connection.execute("UPDATE food_complaints SET status='awaiting_supplement', updated_at=? WHERE id=?", (now, complaint_id))
            _log(connection, complaint_id, "supplement.request", payload["requested_by"], payload["reason"], now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    def submit_supplement(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("awaiting_supplement", "processing", "assigned", "reopened"), "submit_supplement")
            now = _now()
            cursor = connection.execute(
                "INSERT INTO food_complaint_materials(complaint_id,material_type,title,content,submitted_by,created_at) VALUES(?,?,?,?,?,?)",
                (complaint_id, payload["material_type"], payload["title"], payload["content"], payload["submitted_by"], now),
            )
            material_id = cursor.lastrowid
            if row["status"] == "awaiting_supplement":
                connection.execute("UPDATE food_complaints SET status='processing', updated_at=? WHERE id=?", (now, complaint_id))
            _log(connection, complaint_id, "supplement.submit", payload["submitted_by"], payload["title"], now)
            result = self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())
            result["material_id"] = material_id
            return result

    # ---- 5. 结案与复核 ----
    def resolve(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("processing", "assigned"), "resolve")
            if row["needs_recheck"]:
                raise ValueError("recheck_pending")
            now = _now()
            connection.execute(
                "UPDATE food_complaints SET status='resolved', resolution=?, updated_at=? WHERE id=?",
                (payload["resolution"], now, complaint_id),
            )
            _log(connection, complaint_id, "complaint.resolve", payload["operator"], "提交处置结果待复核", now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    def review(self, complaint_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._get(connection, complaint_id)
            self._require_open(row, ("resolved", "reviewing"), "review")
            now = _now()
            if payload["passed"]:
                overview = _evidence_overview(connection, complaint_id)
                if not overview["complete"]:
                    raise ValueError("evidence_incomplete")
                if row["needs_recheck"] or overview["recheck_pending"]:
                    raise ValueError("recheck_pending")
                new_segment = f"{row['resolution']}｜复核意见：{payload['opinion']}"
                # 重新结案时保留历次结案理由，原始结论不被覆盖
                close_reason = f"{row['close_reason']} ⇒ 重新结案：{new_segment}" if row["close_reason"] else new_segment
                connection.execute(
                    "UPDATE food_complaints SET status='closed', close_reason=?, closed_at=?, updated_at=? WHERE id=?",
                    (close_reason, now, now, complaint_id),
                )
                _log(connection, complaint_id, "review.pass", payload["reviewer"], payload["opinion"], now)
            else:
                deadline_hours = payload.get("deadline_hours", 72)
                handle_deadline = (datetime.fromisoformat(now) + timedelta(hours=deadline_hours)).isoformat(timespec="seconds")
                connection.execute(
                    "UPDATE food_complaints SET status='reopened', handle_deadline=?, updated_at=? WHERE id=?",
                    (handle_deadline, now, complaint_id),
                )
                _log(connection, complaint_id, "review.reject", payload["reviewer"], f"退回重办：{payload['opinion']}", now)
            return self._view(connection, connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone())

    # ---- 6. 合并（保留独立联系人与时间线）----
    def merge(self, primary_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        duplicate_id = payload["duplicate_id"]
        if primary_id == duplicate_id:
            raise ValueError("cannot_merge_self")
        with transaction(immediate=True) as connection:
            primary = self._get(connection, primary_id)
            duplicate = self._get(connection, duplicate_id)
            if primary["status"] == "merged" or duplicate["status"] == "merged":
                raise ValueError("complaint_merged")
            if primary["status"] not in OPEN_STATUSES or duplicate["status"] not in OPEN_STATUSES:
                raise ValueError("only_open_complaints_merge")
            if connection.execute("SELECT id FROM food_complaint_links WHERE duplicate_id=?", (duplicate_id,)).fetchone():
                raise ValueError("already_merged")
            now = _now()
            connection.execute(
                "INSERT INTO food_complaint_links(primary_id,duplicate_id,reason,created_at) VALUES(?,?,?,?)",
                (primary_id, duplicate_id, payload["reason"], now),
            )
            connection.execute(
                "UPDATE food_complaints SET status='merged', merged_into=?, updated_at=? WHERE id=?",
                (primary_id, now, duplicate_id),
            )
            _log(connection, primary_id, "merge.primary", "热线主管", f"合并重复投诉 #{duplicate_id}：{payload['reason']}", now)
            _log(connection, duplicate_id, "merge.duplicate", "热线主管", f"并入主投诉 #{primary_id}，联系人与时间线独立保留", now)
            return self._view(connection, primary)

    # ---- 查询 ----
    def _view(self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        result = dict(row)
        result["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
        result["current_assignee"] = row["assignee"] or None
        # 关联时限：证据未配齐前持续计时
        overview = _evidence_overview(connection, row["id"])
        result["evidence"] = overview
        if overview["complete"]:
            result["link_remaining_seconds"] = 0
            result["link_overdue"] = False
        else:
            remaining = _remaining(row["link_deadline"], moment)
            result["link_remaining_seconds"] = remaining
            result["link_overdue"] = remaining is not None and remaining < 0 and row["status"] != "merged"
        # 处置时限：办理中的状态计时；已结案保留结案理由
        if row["status"] in ("closed", "merged", "resolved", "reviewing"):
            result["handle_remaining_seconds"] = None
            result["handle_overdue"] = False
        else:
            remaining = _remaining(row["handle_deadline"], moment)
            result["handle_remaining_seconds"] = remaining
            result["handle_overdue"] = remaining is not None and remaining < 0
        result["close_reason"] = row["close_reason"] or None
        return result

    def get_complaint(self, complaint_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone()
        if row is None:
            return None
        with transaction() as connection:
            result = self._view(connection, row)
            result["evidence_list"] = [
                {**dict(item), "snapshot": json.loads(item["snapshot_json"])}
                for item in connection.execute(
                    "SELECT id,evidence_type,source_id,source_code,title,snapshot_json,snapshot_hash,source_status,review_required,review_reason,created_at FROM food_complaint_evidence WHERE complaint_id=? ORDER BY id",
                    (complaint_id,),
                ).fetchall()
            ]
            result["materials"] = [dict(item) for item in connection.execute(
                "SELECT id,material_type,title,content,submitted_by,created_at FROM food_complaint_materials WHERE complaint_id=? ORDER BY id",
                (complaint_id,),
            ).fetchall()]
            result["timeline"] = [dict(item) for item in connection.execute(
                "SELECT id,action,actor,detail,created_at FROM food_complaint_timeline WHERE complaint_id=? ORDER BY id",
                (complaint_id,),
            ).fetchall()]
            children = connection.execute(
                "SELECT duplicate_id FROM food_complaint_links WHERE primary_id=? ORDER BY duplicate_id",
                (complaint_id,),
            ).fetchall()
            result["merged_children"] = []
            for child in children:
                child_row = connection.execute("SELECT * FROM food_complaints WHERE id=?", (child["duplicate_id"],)).fetchone()
                result["merged_children"].append({
                    "id": child_row["id"],
                    "complaint_no": child_row["complaint_no"],
                    "contact_name": child_row["contact_name"],
                    "contact_phone": child_row["contact_phone"],
                    "occurred_at": child_row["occurred_at"],
                    "created_at": child_row["created_at"],
                    "timeline_count": connection.execute(
                        "SELECT COUNT(*) FROM food_complaint_timeline WHERE complaint_id=?", (child_row["id"],)
                    ).fetchone()[0],
                    "materials_count": connection.execute(
                        "SELECT COUNT(*) FROM food_complaint_materials WHERE complaint_id=?", (child_row["id"],)
                    ).fetchone()[0],
                })
            result["merged_into"] = row["merged_into"]
            return result

    def list_complaints(self, status: str | None = None, overdue_only: bool = False, canteen_name: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if canteen_name:
            conditions.append("canteen_name LIKE ?")
            params.append(f"%{canteen_name}%")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = self.connection.execute(f"SELECT COUNT(*) FROM food_complaints{where}", params).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT * FROM food_complaints{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        items = []
        moment = datetime.now(timezone.utc)
        with transaction() as connection:
            for row in rows:
                view = self._view(connection, row, moment)
                if overdue_only and not (view["link_overdue"] or view["handle_overdue"]):
                    continue
                items.append(view)
        return {"total": total, "count": len(items), "data": items}
