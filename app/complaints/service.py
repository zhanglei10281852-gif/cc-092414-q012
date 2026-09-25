from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_connection, transaction


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
# registered 已登记(热线录入) -> assigned 已分派 -> processing 限时处置中
#   -> supplementing 待补充材料 -> processing ... -> reviewing 待结案复核
#   -> closed 已结案 / reopened 复查重办
# merged 已并入主投诉（独立联系人与时间线仍保留在原投诉记录上）

STATUS_TRANSITIONS: dict[str, set[str]] = {
    "registered": {"assigned", "merged"},
    "assigned": {"processing", "merged"},
    "processing": {"reviewing", "supplementing", "merged"},
    "supplementing": {"processing", "reviewing", "merged"},
    "reviewing": {"closed", "reopened"},
    "reopened": {"processing", "reviewing", "merged"},
    "closed": {"reopened"},
    "merged": set(),
}

TERMINAL_STATUSES = {"closed", "merged"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS food_complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_no TEXT NOT NULL UNIQUE,
    hotline_channel TEXT NOT NULL DEFAULT '12315',
    contact_name TEXT NOT NULL,
    contact_phone TEXT NOT NULL,
    contact_address TEXT NOT NULL DEFAULT '',
    canteen_name TEXT NOT NULL,
    canteen_address TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    incident_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    lot_id INTEGER REFERENCES food_lots(id) ON DELETE RESTRICT,
    shipment_id INTEGER REFERENCES food_shipments(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK(status IN ('registered','assigned','processing','supplementing','reviewing','closed','reopened','merged')),
    assignee TEXT,
    assigned_at TEXT,
    deadline TEXT,
    supplement_requested_at TEXT,
    close_result TEXT,
    closed_at TEXT,
    review_opinion TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    master_id INTEGER REFERENCES food_complaints(id) ON DELETE RESTRICT,
    merged_at TEXT,
    merge_reason TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_complaint_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE RESTRICT,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('certificate','temperature','lot','shipment','note','file')),
    source_table TEXT NOT NULL DEFAULT '',
    source_id INTEGER,
    source_descriptor TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked','superseded')),
    revoked_at TEXT,
    revoke_reason TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0,1)),
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_complaint_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('request','submission')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_complaint_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL REFERENCES food_complaints(id) ON DELETE RESTRICT,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_complaints_status ON food_complaints(status, deadline);
CREATE INDEX IF NOT EXISTS idx_food_complaints_lot ON food_complaints(lot_id);
CREATE INDEX IF NOT EXISTS idx_food_complaints_master ON food_complaints(master_id);
CREATE INDEX IF NOT EXISTS idx_food_complaint_evidence_complaint ON food_complaint_evidence(complaint_id, id);
CREATE INDEX IF NOT EXISTS idx_food_complaint_evidence_review ON food_complaint_evidence(needs_review, status);
CREATE INDEX IF NOT EXISTS idx_food_complaint_timeline_complaint ON food_complaint_timeline(complaint_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ComplaintService:
    """投诉登记、证据快照引用、分派限时处置与结案复核的事务边界。"""

    DEFAULT_DEADLINE_HOURS = 48

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ helpers
    def _timeline(self, conn: sqlite3.Connection, complaint_id: int, event: str,
                  actor: str, detail: str = "", at: str | None = None) -> None:
        conn.execute(
            "INSERT INTO food_complaint_timeline(complaint_id,event,actor,detail,created_at)"
            " VALUES(?,?,?,?,?)",
            (complaint_id, event, actor, detail, at or _now()),
        )

    def _require_transition(self, current: str, target: str) -> None:
        if target not in STATUS_TRANSITIONS.get(current, set()):
            raise ValueError(f"illegal_transition:{current}->{target}")

    def _get_complaint(self, conn: sqlite3.Connection, complaint_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM food_complaints WHERE id=?", (complaint_id,)).fetchone()
        if row is None:
            raise KeyError("complaint_not_found")
        return row

    def _next_complaint_no(self, conn: sqlite3.Connection) -> str:
        today = _now()[:10].replace("-", "")
        prefix = f"TS{today}"
        count = conn.execute(
            "SELECT COUNT(*) FROM food_complaints WHERE complaint_no LIKE ?", (prefix + "%",)
        ).fetchone()[0]
        return f"{prefix}-{count + 1:04d}"

    # ------------------------------------------------------------- 快照采集
    def _snapshot_lot(self, conn: sqlite3.Connection, lot_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if row is None:
            raise KeyError("lot_not_found")
        return dict(row)

    def _snapshot_shipment(self, conn: sqlite3.Connection, shipment_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM food_shipments WHERE id=?", (shipment_id,)).fetchone()
        if row is None:
            raise KeyError("shipment_not_found")
        item = dict(row)
        item["temperatures"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM food_temperatures WHERE shipment_id=? ORDER BY recorded_at,id",
                (shipment_id,),
            ).fetchall()
        ]
        return item

    def _snapshot_certificate(self, conn: sqlite3.Connection, result_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM food_test_results WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise KeyError("test_result_not_found")
        item = dict(row)
        sample = conn.execute("SELECT * FROM food_samples WHERE id=?", (row["sample_id"],)).fetchone()
        item["sample"] = dict(sample) if sample else None
        lot = conn.execute("SELECT * FROM food_lots WHERE id=?", (sample["lot_id"],)).fetchone() if sample else None
        item["lot"] = dict(lot) if lot else None
        return item

    def _add_evidence_row(self, conn: sqlite3.Connection, complaint_id: int,
                          evidence_type: str, source_table: str, source_id: int | None,
                          descriptor: str, snapshot: dict[str, Any], at: str) -> dict[str, Any]:
        cursor = conn.execute(
            "INSERT INTO food_complaint_evidence"
            "(complaint_id,evidence_type,source_table,source_id,source_descriptor,"
            "snapshot_json,snapshot_hash,snapshot_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (complaint_id, evidence_type, source_table, source_id, descriptor,
             json.dumps(snapshot, ensure_ascii=False, default=str), _hash(snapshot), at, at),
        )
        return dict(conn.execute(
            "SELECT * FROM food_complaint_evidence WHERE id=?", (cursor.lastrowid,)
        ).fetchone())

    # ------------------------------------------------------------- 1. 登记
    def register(self, payload: dict[str, Any], actor: str = "热线人员") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            if payload.get("lot_id") is not None and conn.execute(
                "SELECT 1 FROM food_lots WHERE id=?", (payload["lot_id"],)
            ).fetchone() is None:
                raise KeyError("lot_not_found")
            if payload.get("shipment_id") is not None and conn.execute(
                "SELECT 1 FROM food_shipments WHERE id=?", (payload["shipment_id"],)
            ).fetchone() is None:
                raise KeyError("shipment_not_found")
            complaint_no = payload.get("complaint_no") or self._next_complaint_no(conn)
            cursor = conn.execute(
                "INSERT INTO food_complaints(complaint_no,hotline_channel,contact_name,contact_phone,"
                "contact_address,canteen_name,canteen_address,description,incident_at,received_at,"
                "lot_id,shipment_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (complaint_no, payload.get("hotline_channel", "12315"),
                 payload["contact_name"], payload["contact_phone"],
                 payload.get("contact_address", ""), payload["canteen_name"],
                 payload.get("canteen_address", ""), payload["description"],
                 payload["incident_at"], payload.get("received_at") or now,
                 payload.get("lot_id"), payload.get("shipment_id"), now, now),
            )
            complaint_id = cursor.lastrowid
            self._timeline(conn, complaint_id, "登记", actor,
                           f"热线渠道登记，投诉对象：{payload['canteen_name']}", now)
            # 登记时若已关联批次/配送单，立即固化初始证据快照
            if payload.get("lot_id") is not None:
                snapshot = self._snapshot_lot(conn, payload["lot_id"])
                self._add_evidence_row(conn, complaint_id, "lot", "food_lots",
                                       payload["lot_id"], snapshot["lot_code"], snapshot, now)
            if payload.get("shipment_id") is not None:
                snapshot = self._snapshot_shipment(conn, payload["shipment_id"])
                self._add_evidence_row(conn, complaint_id, "shipment", "food_shipments",
                                       payload["shipment_id"], snapshot["shipment_code"], snapshot, now)
            return self._detail(conn, complaint_id)

    # ------------------------------------------------------------- 2. 证据引用
    def add_evidence(self, complaint_id: int, payload: dict[str, Any],
                     actor: str = "调查员") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            if complaint["status"] == "merged":
                raise ValueError("complaint_merged_readonly")
            evidence_type = payload["evidence_type"]
            descriptor = payload.get("source_descriptor", "")
            snapshot: dict[str, Any]
            if evidence_type == "certificate":
                result_id = int(payload["source_id"])
                snapshot = self._snapshot_certificate(conn, result_id)
                descriptor = descriptor or snapshot.get("certificate_no") or f"result#{result_id}"
                row = self._add_evidence_row(conn, complaint_id, "certificate",
                                             "food_test_results", result_id, descriptor, snapshot, now)
            elif evidence_type == "temperature":
                shipment_id = int(payload["source_id"])
                snapshot = self._snapshot_shipment(conn, shipment_id)
                descriptor = descriptor or snapshot["shipment_code"]
                row = self._add_evidence_row(conn, complaint_id, "temperature",
                                             "food_shipments", shipment_id, descriptor, snapshot, now)
            elif evidence_type == "lot":
                lot_id = int(payload["source_id"])
                snapshot = self._snapshot_lot(conn, lot_id)
                descriptor = descriptor or snapshot["lot_code"]
                row = self._add_evidence_row(conn, complaint_id, "lot",
                                             "food_lots", lot_id, descriptor, snapshot, now)
            elif evidence_type == "shipment":
                shipment_id = int(payload["source_id"])
                snapshot = self._snapshot_shipment(conn, shipment_id)
                descriptor = descriptor or snapshot["shipment_code"]
                row = self._add_evidence_row(conn, complaint_id, "shipment",
                                             "food_shipments", shipment_id, descriptor, snapshot, now)
            elif evidence_type in ("note", "file"):
                snapshot = {"content": payload.get("snapshot", payload.get("note", ""))}
                row = self._add_evidence_row(conn, complaint_id, evidence_type, "", None,
                                             descriptor, snapshot, now)
            else:
                raise ValueError("unsupported_evidence_type")
            self._timeline(conn, complaint_id, "引用证据", actor,
                           f"{evidence_type}:{descriptor}", now)
            return row

    def revoke_evidence(self, complaint_id: int, evidence_id: int,
                        reason: str, actor: str = "系统") -> dict[str, Any]:
        """证书/温度记录被撤销：不删除快照，标记 revoked 并要求复查。"""
        now = _now()
        with transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM food_complaint_evidence WHERE id=? AND complaint_id=?",
                (evidence_id, complaint_id),
            ).fetchone()
            if row is None:
                raise KeyError("evidence_not_found")
            conn.execute(
                "UPDATE food_complaint_evidence SET status='revoked',revoked_at=?,"
                "revoke_reason=?,needs_review=1 WHERE id=?",
                (now, reason, evidence_id),
            )
            complaint = self._get_complaint(conn, complaint_id)
            # 已结案的投诉因证据撤销重新进入复查；未结案的保持当前处置状态，仅挂复查标记
            if complaint["status"] == "closed":
                conn.execute(
                    "UPDATE food_complaints SET status='reopened',reopen_count=reopen_count+1,"
                    "updated_at=? WHERE id=?",
                    (now, complaint_id),
                )
            self._timeline(conn, complaint_id, "证据撤销", actor,
                           f"证据#{evidence_id}（{row['evidence_type']}:{row['source_descriptor']}）"
                           f"被撤销，快照已保留，需复查；原因：{reason}", now)
            return dict(conn.execute(
                "SELECT * FROM food_complaint_evidence WHERE id=?", (evidence_id,)
            ).fetchone())

    def review_evidence(self, complaint_id: int, evidence_id: int, note: str,
                        keep_review_flag: bool = False, actor: str = "复核员") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM food_complaint_evidence WHERE id=? AND complaint_id=?",
                (evidence_id, complaint_id),
            ).fetchone()
            if row is None:
                raise KeyError("evidence_not_found")
            if not row["needs_review"]:
                raise ValueError("evidence_not_flagged_review")
            conn.execute(
                "UPDATE food_complaint_evidence SET needs_review=?,reviewed_at=?,review_note=? WHERE id=?",
                (1 if keep_review_flag else 0, now, note, evidence_id),
            )
            self._timeline(conn, complaint_id, "证据复查", actor,
                           f"证据#{evidence_id}复查结论：{note}", now)
            return dict(conn.execute(
                "SELECT * FROM food_complaint_evidence WHERE id=?", (evidence_id,)
            ).fetchone())

    # ------------------------------------------------------------- 3. 分派
    def assign(self, complaint_id: int, payload: dict[str, Any],
               actor: str = "热线班长") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            self._require_transition(complaint["status"], "assigned")
            deadline = payload.get("deadline")
            if not deadline:
                hours = int(payload.get("deadline_hours", self.DEFAULT_DEADLINE_HOURS))
                if hours <= 0:
                    raise ValueError("deadline_invalid")
                deadline = (datetime.fromisoformat(complaint["received_at"])
                            + timedelta(hours=hours)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE food_complaints SET status='assigned',assignee=?,assigned_at=?,"
                "deadline=?,updated_at=? WHERE id=?",
                (payload["assignee"], now, deadline, now, complaint_id),
            )
            self._timeline(conn, complaint_id, "分派", actor,
                           f"责任人：{payload['assignee']}；办结时限：{deadline}", now)
            return self._detail(conn, complaint_id)

    def start_handling(self, complaint_id: int, actor: str | None = None) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            self._require_transition(complaint["status"], "processing")
            conn.execute(
                "UPDATE food_complaints SET status='processing',updated_at=? WHERE id=?",
                (now, complaint_id),
            )
            self._timeline(conn, complaint_id, "开始处置", actor or complaint["assignee"] or "责任人", at=now)
            return self._detail(conn, complaint_id)

    # ------------------------------------------------------------- 4. 限时处置
    def request_supplement(self, complaint_id: int, payload: dict[str, Any],
                           actor: str | None = None) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            self._require_transition(complaint["status"], "supplementing")
            conn.execute(
                "UPDATE food_complaints SET status='supplementing',supplement_requested_at=?,"
                "updated_at=? WHERE id=?",
                (now, now, complaint_id),
            )
            conn.execute(
                "INSERT INTO food_complaint_materials(complaint_id,kind,title,content,submitted_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (complaint_id, "request", payload["title"], payload["content"],
                 actor or complaint["assignee"] or "责任人", now),
            )
            self._timeline(conn, complaint_id, "要求补充材料",
                           actor or complaint["assignee"] or "责任人",
                           payload["title"], now)
            return self._detail(conn, complaint_id)

    # ------------------------------------------------------------- 5. 补充材料
    def submit_supplement(self, complaint_id: int, payload: dict[str, Any],
                          actor: str | None = None) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            if complaint["status"] != "supplementing":
                raise ValueError("illegal_transition:supplement_only_in_supplementing")
            conn.execute(
                "INSERT INTO food_complaint_materials(complaint_id,kind,title,content,submitted_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (complaint_id, "submission", payload["title"], payload["content"],
                 actor or payload.get("submitted_by") or complaint["contact_name"], now),
            )
            conn.execute(
                "UPDATE food_complaints SET status='processing',updated_at=? WHERE id=?",
                (now, complaint_id),
            )
            self._timeline(conn, complaint_id, "补充材料提交",
                           actor or payload.get("submitted_by") or complaint["contact_name"],
                           payload["title"], now)
            return self._detail(conn, complaint_id)

    def submit_handling(self, complaint_id: int, payload: dict[str, Any],
                        actor: str | None = None) -> dict[str, Any]:
        """处置完成，提交结案复核。"""
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            self._require_transition(complaint["status"], "reviewing")
            conn.execute(
                "UPDATE food_complaints SET status='reviewing',close_result=?,updated_at=? WHERE id=?",
                (payload["result"], now, complaint_id),
            )
            self._timeline(conn, complaint_id, "提交结案复核",
                           actor or complaint["assignee"] or "责任人",
                           payload["result"], now)
            return self._detail(conn, complaint_id)

    # ------------------------------------------------------------- 6. 结案复核
    def review_close(self, complaint_id: int, payload: dict[str, Any],
                     actor: str = "复核员") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            complaint = self._get_complaint(conn, complaint_id)
            if complaint["status"] != "reviewing":
                raise ValueError("illegal_transition:review_only_in_reviewing")
            if payload["passed"]:
                conn.execute(
                    "UPDATE food_complaints SET status='closed',review_opinion=?,"
                    "reviewed_by=?,reviewed_at=?,closed_at=?,updated_at=? WHERE id=?",
                    (payload["opinion"], actor, now, now, now, complaint_id),
                )
                self._timeline(conn, complaint_id, "结案复核通过", actor, payload["opinion"], now)
            else:
                conn.execute(
                    "UPDATE food_complaints SET status='reopened',reopen_count=reopen_count+1,"
                    "review_opinion=?,reviewed_by=?,reviewed_at=?,updated_at=? WHERE id=?",
                    (payload["opinion"], actor, now, now, complaint_id),
                )
                self._timeline(conn, complaint_id, "结案复核退回", actor, payload["opinion"], now)
            return self._detail(conn, complaint_id)

    # ------------------------------------------------------------- 合并重复投诉
    def merge(self, source_id: int, master_id: int, reason: str,
              actor: str = "热线班长") -> dict[str, Any]:
        """重复投诉并入主投诉；联系人、时间线与材料保留在原投诉上，不被吞掉。"""
        now = _now()
        if source_id == master_id:
            raise ValueError("cannot_merge_self")
        with transaction(immediate=True) as conn:
            source = self._get_complaint(conn, source_id)
            master = self._get_complaint(conn, master_id)
            if source["status"] == "merged":
                raise ValueError("source_already_merged")
            if master["status"] == "merged":
                raise ValueError("master_is_merged")
            if master["master_id"] is not None:
                raise ValueError("master_is_itself_merged")
            self._require_transition(source["status"], "merged")
            conn.execute(
                "UPDATE food_complaints SET status='merged',master_id=?,merged_at=?,"
                "merge_reason=?,updated_at=? WHERE id=?",
                (master_id, now, reason, now, source_id),
            )
            self._timeline(conn, source_id, "合并", actor,
                           f"并入主投诉 {master['complaint_no']}：{reason}", now)
            self._timeline(conn, master_id, "并入重复投诉", actor,
                           f"{source['complaint_no']}（联系人：{source['contact_name']} "
                           f"{source['contact_phone']}）并入：{reason}", now)
            return self._detail(conn, source_id)

    # ------------------------------------------------------------- 查询
    def _remaining_deadline(self, row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        if row["status"] in TERMINAL_STATUSES or not row["deadline"]:
            return {"deadline": row["deadline"], "remaining_hours": None, "overdue": False}
        deadline = _parse_ts(row["deadline"])
        remaining = (deadline - now).total_seconds() / 3600 if deadline else None
        overdue = remaining is not None and remaining < 0 and row["status"] != "closed"
        return {"deadline": row["deadline"], "remaining_hours": round(remaining, 2) if remaining is not None else None,
                "overdue": overdue}

    def _evidence_completeness(self, conn: sqlite3.Connection, complaint_id: int) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT evidence_type,status,needs_review FROM food_complaint_evidence "
            "WHERE complaint_id=?",
            (complaint_id,),
        ).fetchall()
        active = [r for r in rows if r["status"] != "revoked"]
        revoked = [r for r in rows if r["status"] == "revoked"]
        needs_review = conn.execute(
            "SELECT COUNT(*) FROM food_complaint_evidence WHERE complaint_id=? AND needs_review=1",
            (complaint_id,),
        ).fetchone()[0]
        has_lot = any(r["evidence_type"] in ("lot",) for r in active)
        has_cert = any(r["evidence_type"] in ("certificate",) for r in active)
        has_temp = any(r["evidence_type"] in ("temperature",) for r in active)
        linked_complaint = conn.execute(
            "SELECT lot_id,shipment_id FROM food_complaints WHERE id=?", (complaint_id,)
        ).fetchone()
        checks = {
            "lot_linked_or_evidence": (linked_complaint["lot_id"] is not None) or has_lot,
            "certificate": has_cert,
            "temperature_or_shipment": (linked_complaint["shipment_id"] is not None) or has_temp,
        }
        score = round(100 * sum(1 for ok in checks.values() if ok) / len(checks))
        return {"score": score, "checks": checks, "evidence_count": len(rows),
                "active_count": len(active), "revoked_count": len(revoked),
                "needs_review_count": needs_review}

    def _list_item(self, conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        item = dict(row)
        item.update(self._remaining_deadline(row, now))
        item["evidence"] = self._evidence_completeness(conn, row["id"])
        item["close_reason"] = row["close_result"]
        return item

    def list_complaints(self, status: str | None = None, assignee: str | None = None,
                        lot_id: int | None = None, include_merged: bool = False,
                        overdue_only: bool = False, limit: int = 100) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        elif not include_merged:
            clauses.append("status<>'merged'")
        if assignee:
            clauses.append("assignee=?")
            params.append(assignee)
        if lot_id is not None:
            clauses.append("lot_id=?")
            params.append(lot_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        if overdue_only:
            where += (" AND " if where else " WHERE ") + "deadline IS NOT NULL AND deadline<? AND status NOT IN ('closed','merged')"
            params.append(now_dt.isoformat(timespec="seconds"))
        rows = self.connection.execute(
            f"SELECT * FROM food_complaints{where} ORDER BY received_at DESC,id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        data = [self._list_item(self.connection, row, now_dt) for row in rows]
        return {"total": len(data), "data": data}

    def detail(self, complaint_id: int) -> dict[str, Any]:
        with transaction() as conn:
            return self._detail(conn, complaint_id)

    def _detail(self, conn: sqlite3.Connection, complaint_id: int) -> dict[str, Any]:
        complaint = self._get_complaint(conn, complaint_id)
        now_dt = datetime.now(timezone.utc)
        result = dict(complaint)
        result.update(self._remaining_deadline(complaint, now_dt))
        result["evidence"] = self._evidence_completeness(conn, complaint_id)
        result["evidence_items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM food_complaint_evidence WHERE complaint_id=? ORDER BY id",
            (complaint_id,),
        ).fetchall()]
        result["materials"] = [dict(r) for r in conn.execute(
            "SELECT * FROM food_complaint_materials WHERE complaint_id=? ORDER BY id",
            (complaint_id,),
        ).fetchall()]
        result["timeline"] = [dict(r) for r in conn.execute(
            "SELECT * FROM food_complaint_timeline WHERE complaint_id=? ORDER BY id",
            (complaint_id,),
        ).fetchall()]
        if complaint["master_id"] is not None:
            master = conn.execute(
                "SELECT id,complaint_no,status FROM food_complaints WHERE id=?",
                (complaint["master_id"],),
            ).fetchone()
            result["master"] = dict(master) if master else None
        merged = conn.execute(
            "SELECT id,complaint_no,contact_name,contact_phone,received_at,merged_at,merge_reason"
            " FROM food_complaints WHERE master_id=? ORDER BY received_at,id",
            (complaint_id,),
        ).fetchall()
        result["merged_complaints"] = [dict(r) for r in merged]
        result["close_reason"] = complaint["close_result"]
        return result

    def pending_evidence_reviews(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT e.*,c.complaint_no,c.status AS complaint_status "
            "FROM food_complaint_evidence e JOIN food_complaints c ON c.id=e.complaint_id"
            " WHERE e.needs_review=1 ORDER BY e.snapshot_at"
        ).fetchall()
        return [dict(r) for r in rows]
