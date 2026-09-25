from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _make_lot_with_cert_and_shipment(client, lot_code="LOT-C1"):
    """构造 批次->样品->检测证书->配送->温度 的完整追溯链。"""
    lot = client.post("/api/food/lots", json={
        "lot_code": lot_code, "product_name": "小白菜", "category": "叶菜",
        "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20",
        "quantity_kg": 300, "trace_code": lot_code + "-TRACE",
    }).json()
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={
        "sample_code": "S-" + lot_code, "collected_at": "2026-09-21T08:00:00+00:00",
        "collector": "监管员", "location": "食堂后厨", "sample_weight_g": 250,
    }).json()
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={
        "analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.02,
        "limit_mg_kg": 0.05, "lab_operator": "实验员",
        "tested_at": "2026-09-21T18:00:00+00:00", "certificate_no": "CERT-001",
    }).json()
    shipment = client.post(f"/api/food/lots/{lot['id']}/shipments", json={
        "shipment_code": "SHIP-" + lot_code, "carrier": "冷链物流", "vehicle_no": "鲁A002",
        "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00",
        "destination": "机关食堂", "target_temp_min": 0, "target_temp_max": 8,
    }).json()
    client.post(f"/api/food/shipments/{shipment['id']}/temperatures", json={
        "recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 5.2, "source": "sensor-A",
    })
    return lot, sample, result, shipment


def _register(client, **overrides):
    payload = {
        "contact_name": "王女士", "contact_phone": "13800000001",
        "canteen_name": "机关第一食堂", "description": "炒青菜有刺鼻异味",
        "incident_at": "2026-09-23T12:30:00+00:00",
    }
    payload.update(overrides)
    resp = client.post("/api/food/complaints", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _drive_to_closed(client, complaint_id, assignee="李调查员", deadline_hours=48, result="已责令食堂整改，同批次蔬菜下架"):
    assert client.post(f"/api/food/complaints/{complaint_id}/assign",
                       json={"assignee": assignee, "deadline_hours": deadline_hours}).status_code == 200
    assert client.post(f"/api/food/complaints/{complaint_id}/start").status_code == 200
    assert client.post(f"/api/food/complaints/{complaint_id}/submit",
                       json={"result": result}).status_code == 200
    review = client.post(f"/api/food/complaints/{complaint_id}/review",
                         json={"passed": True, "opinion": "证据充分，处置到位"})
    assert review.status_code == 200, review.text
    return review.json()


# ---------------------------------------------------------------------------
# 登记 + 关联批次/配送节点
# ---------------------------------------------------------------------------
def test_register_links_lot_and_shipment_with_initial_snapshots(client):
    lot, _, _, shipment = _make_lot_with_cert_and_shipment(client)
    complaint = _register(client, lot_id=lot["id"], shipment_id=shipment["id"])
    assert complaint["status"] == "registered"
    assert complaint["complaint_no"].startswith("TS")
    # 登记即固化批次与配送节点快照
    types_ = {e["evidence_type"] for e in complaint["evidence_items"]}
    assert types_ == {"lot", "shipment"}
    assert complaint["evidence"]["evidence_count"] == 2
    assert complaint["timeline"][0]["event"] == "登记"
    # 列表默认不显示已合并投诉之外的全部，且含证据完整度
    listed = client.get("/api/food/complaints").json()["data"]
    assert listed[0]["evidence"]["checks"]["lot_linked_or_evidence"] is True


def test_register_missing_lot_returns_404(client):
    resp = client.post("/api/food/complaints", json={
        "contact_name": "赵先生", "contact_phone": "13800000002",
        "canteen_name": "某食堂", "description": "有异味",
        "incident_at": "2026-09-23T12:30:00+00:00", "lot_id": 9999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 证据引用：证书与温度记录快照
# ---------------------------------------------------------------------------
def test_certificate_and_temperature_evidence_snapshots(client):
    lot, _, result, shipment = _make_lot_with_cert_and_shipment(client)
    complaint = _register(client, lot_id=lot["id"])
    cert = client.post(f"/api/food/complaints/{complaint['id']}/evidence",
                       json={"evidence_type": "certificate", "source_id": result["id"]})
    assert cert.status_code == 201, cert.text
    cert_row = cert.json()
    assert cert_row["source_descriptor"] == "CERT-001"
    assert cert_row["snapshot_hash"]
    # 快照内嵌证书当时的检测结论
    import json
    snapshot = json.loads(cert_row["snapshot_json"])
    assert snapshot["verdict"] == "pass" and snapshot["certificate_no"] == "CERT-001"
    assert snapshot["lot"]["id"] == lot["id"]

    temp = client.post(f"/api/food/complaints/{complaint['id']}/evidence",
                       json={"evidence_type": "temperature", "source_id": shipment["id"]})
    assert temp.status_code == 201
    temp_snapshot = json.loads(temp.json()["snapshot_json"])
    assert len(temp_snapshot["temperatures"]) == 1

    detail = client.get(f"/api/food/complaints/{complaint['id']}").json()
    assert detail["evidence"]["score"] == 100
    assert detail["evidence"]["checks"] == {
        "lot_linked_or_evidence": True, "certificate": True,
        "temperature_or_shipment": True,
    }


def test_evidence_snapshot_survives_lot_recall(client):
    """批次被召回不能让投诉失去原始证据：快照记录召回前的原始状态。"""
    lot, _, result, _ = _make_lot_with_cert_and_shipment(client, "LOT-C2")
    # 检测合格后放行
    client.post(f"/api/food/lots/{lot['id']}/risk",
                json={"decision": "release", "reason": "检测合格", "operator": "监管员"})
    complaint = _register(client, lot_id=lot["id"])
    cert = client.post(f"/api/food/complaints/{complaint['id']}/evidence",
                       json={"evidence_type": "certificate", "source_id": result["id"]}).json()
    # 批次随后被召回
    recall = client.post(f"/api/food/lots/{lot['id']}/risk",
                         json={"decision": "recall", "reason": "异味投诉属实", "operator": "监管员"})
    assert recall.json()["status"] == "recalled"
    # 投诉证据快照仍是引用时的原始内容
    detail = client.get(f"/api/food/complaints/{complaint['id']}").json()
    snap_cert = next(e for e in detail["evidence_items"] if e["id"] == cert["id"])
    import json
    snapshot = json.loads(snap_cert["snapshot_json"])
    assert snapshot["lot"]["status"] == "released"  # 召回前状态已固化
    assert snap_cert["status"] == "active"


# ---------------------------------------------------------------------------
# 证书/温度记录撤销：保留快照 + 标记复查
# ---------------------------------------------------------------------------
def test_revoked_certificate_keeps_snapshot_and_flags_review(client):
    lot, _, result, shipment = _make_lot_with_cert_and_shipment(client, "LOT-C3")
    complaint = _register(client, lot_id=lot["id"], shipment_id=shipment["id"])
    cert = client.post(f"/api/food/complaints/{complaint['id']}/evidence",
                       json={"evidence_type": "certificate", "source_id": result["id"]}).json()
    revoked = client.post(f"/api/food/complaints/{complaint['id']}/evidence/{cert['id']}/revoke",
                          json={"reason": "检测机构发现证书编号被冒用"})
    assert revoked.status_code == 200
    row = revoked.json()
    assert row["status"] == "revoked" and row["needs_review"] == 1
    assert row["revoked_at"] and row["snapshot_json"]  # 快照未删除
    # 撤销的证书不计入完整度，复查队列可见
    detail = client.get(f"/api/food/complaints/{complaint['id']}").json()
    assert detail["evidence"]["revoked_count"] == 1
    assert detail["evidence"]["needs_review_count"] == 1
    assert detail["evidence"]["checks"]["certificate"] is False
    pending = client.get("/api/food/complaints/evidence-reviews").json()["data"]
    assert any(e["id"] == cert["id"] for e in pending)
    # 时间线记录撤销事件
    assert any(e["event"] == "证据撤销" for e in detail["timeline"])
    # 复查后清除标记，快照继续保留
    reviewed = client.post(f"/api/food/complaints/{complaint['id']}/evidence/{cert['id']}/review",
                           json={"note": "已换用复检报告 CERT-002", "keep_review_flag": False})
    assert reviewed.json()["needs_review"] == 0
    assert reviewed.json()["status"] == "revoked"


def test_revoked_evidence_after_close_reopens_complaint(client):
    lot, _, result, shipment = _make_lot_with_cert_and_shipment(client, "LOT-C4")
    complaint = _register(client, lot_id=lot["id"], shipment_id=shipment["id"])
    cert = client.post(f"/api/food/complaints/{complaint['id']}/evidence",
                       json={"evidence_type": "certificate", "source_id": result["id"]}).json()
    closed = _drive_to_closed(client, complaint["id"])
    assert closed["status"] == "closed"
    # 结案后证书被撤销 -> 自动进入复查重办，结案理由仍可查
    revoked = client.post(f"/api/food/complaints/{complaint['id']}/evidence/{cert['id']}/revoke",
                          json={"reason": "温度记录被温控设备商更正"})
    assert revoked.status_code == 200
    detail = client.get(f"/api/food/complaints/{complaint['id']}").json()
    assert detail["status"] == "reopened"
    assert detail["reopen_count"] == 1
    assert detail["close_reason"]  # 历史结案理由未丢
    # 重办后可再次提交复核并结案
    assert client.post(f"/api/food/complaints/{complaint['id']}/start").status_code == 200
    assert client.post(f"/api/food/complaints/{complaint['id']}/submit",
                       json={"result": "依据替代证据重新处置完毕"}).status_code == 200
    again = client.post(f"/api/food/complaints/{complaint['id']}/review",
                        json={"passed": True, "opinion": "复查通过"})
    assert again.json()["status"] == "closed"


# ---------------------------------------------------------------------------
# 分派 + 限时处置
# ---------------------------------------------------------------------------
def test_assign_deadline_and_overdue_query(client):
    lot, _, _, _ = _make_lot_with_cert_and_shipment(client, "LOT-C5")
    c1 = _register(client, lot_id=lot["id"], contact_phone="13800000010")
    c2 = _register(client, lot_id=lot["id"], contact_phone="13800000011",
                   canteen_name="机关第二食堂")

    past = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    resp = client.post(f"/api/food/complaints/{c1['id']}/assign",
                       json={"assignee": "李调查员", "deadline": past})
    assert resp.status_code == 200
    assert resp.json()["overdue"] is True
    assert resp.json()["remaining_hours"] < 0
    assert resp.json()["assignee"] == "李调查员"

    future = _iso(datetime.now(timezone.utc) + timedelta(hours=47, minutes=30))
    client.post(f"/api/food/complaints/{c2['id']}/assign",
                json={"assignee": "王调查员", "deadline": future})

    overdue = client.get("/api/food/complaints", params={"overdue_only": True}).json()["data"]
    ids = {c["id"] for c in overdue}
    assert c1["id"] in ids and c2["id"] not in ids

    mine = client.get("/api/food/complaints", params={"assignee": "王调查员"}).json()["data"]
    assert len(mine) == 1 and mine[0]["remaining_hours"] > 47


def test_illegal_transition_rejected(client):
    complaint = _register(client)
    # 未分派不能直接提交复核
    resp = client.post(f"/api/food/complaints/{complaint['id']}/submit",
                       json={"result": "x"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 补充材料
# ---------------------------------------------------------------------------
def test_supplement_request_and_submission_flow(client):
    complaint = _register(client)
    client.post(f"/api/food/complaints/{complaint['id']}/assign",
                json={"assignee": "李调查员", "deadline_hours": 24})
    client.post(f"/api/food/complaints/{complaint['id']}/start")
    req = client.post(f"/api/food/complaints/{complaint['id']}/supplement-request",
                      json={"title": "需要采购小票", "content": "请提供9月23日蔬菜采购验收记录"})
    assert req.json()["status"] == "supplementing"
    # 补充期间不能重复要求补充
    bad = client.post(f"/api/food/complaints/{complaint['id']}/supplement-request",
                      json={"title": "再次索要", "content": "x"})
    assert bad.status_code == 409
    sub = client.post(f"/api/food/complaints/{complaint['id']}/supplements",
                      json={"title": "采购验收单", "content": "随附9月23日验收记录扫描件",
                            "submitted_by": "食堂管理员周师傅"})
    assert sub.status_code == 201
    assert sub.json()["status"] == "processing"
    detail = client.get(f"/api/food/complaints/{complaint['id']}").json()
    kinds = [m["kind"] for m in detail["materials"]]
    assert kinds == ["request", "submission"]
    events = [e["event"] for e in detail["timeline"]]
    assert "要求补充材料" in events and "补充材料提交" in events


# ---------------------------------------------------------------------------
# 结案复核
# ---------------------------------------------------------------------------
def test_close_review_reject_reopens_then_pass(client):
    complaint = _register(client)
    client.post(f"/api/food/complaints/{complaint['id']}/assign",
                json={"assignee": "李调查员", "deadline_hours": 24})
    client.post(f"/api/food/complaints/{complaint['id']}/start")
    client.post(f"/api/food/complaints/{complaint['id']}/submit",
                json={"result": "口头警告食堂"})
    rejected = client.post(f"/api/food/complaints/{complaint['id']}/review",
                           json={"passed": False, "opinion": "处置过轻，需下架同批次蔬菜"})
    assert rejected.json()["status"] == "reopened"
    assert rejected.json()["reopen_count"] == 1
    # 重办后再次走完
    client.post(f"/api/food/complaints/{complaint['id']}/start")
    client.post(f"/api/food/complaints/{complaint['id']}/submit",
                json={"result": "同批次蔬菜已下架销毁，食堂停业整顿3天"})
    passed = client.post(f"/api/food/complaints/{complaint['id']}/review",
                         json={"passed": True, "opinion": "处置到位"})
    body = passed.json()
    assert body["status"] == "closed"
    assert body["close_reason"] == "同批次蔬菜已下架销毁，食堂停业整顿3天"
    assert body["reviewed_by"] == "复核员"
    # 已结案无剩余时限概念
    assert body["remaining_hours"] is None and body["overdue"] is False


# ---------------------------------------------------------------------------
# 重复投诉合并：独立联系人与时间线不被吞掉
# ---------------------------------------------------------------------------
def test_merge_duplicates_preserves_contacts_and_timelines(client):
    lot, _, _, _ = _make_lot_with_cert_and_shipment(client, "LOT-C6")
    master = _register(client, lot_id=lot["id"], contact_name="王女士",
                       contact_phone="13800000001", description="中午就餐后闻到农药味")
    dup = _register(client, lot_id=lot["id"], contact_name="陈先生",
                    contact_phone="13800000099", description="同一食堂同一批菜，我也投诉",
                    incident_at="2026-09-23T13:00:00+00:00")
    merged = client.post(f"/api/food/complaints/{dup['id']}/merge",
                         json={"master_id": master["id"], "reason": "同一食堂同一批次同批次异味"})
    assert merged.status_code == 200
    body = merged.json()
    assert body["status"] == "merged"
    assert body["master"]["id"] == master["id"]

    # 被合并投诉自身的联系人与时间线仍然完整可查
    dup_detail = client.get(f"/api/food/complaints/{dup['id']}").json()
    assert dup_detail["contact_name"] == "陈先生"
    assert dup_detail["contact_phone"] == "13800000099"
    assert any(e["event"] == "合并" for e in dup_detail["timeline"])

    # 主投诉可见被并入的独立联系人
    master_detail = client.get(f"/api/food/complaints/{master['id']}").json()
    merged_list = master_detail["merged_complaints"]
    assert len(merged_list) == 1
    assert merged_list[0]["contact_name"] == "陈先生"
    assert merged_list[0]["contact_phone"] == "13800000099"
    assert any(e["event"] == "并入重复投诉" for e in master_detail["timeline"])

    # 默认列表隐藏已合并投诉，显式参数可查
    assert all(c["id"] != dup["id"] for c in client.get("/api/food/complaints").json()["data"])
    with_merged = client.get("/api/food/complaints", params={"include_merged": True}).json()["data"]
    assert any(c["id"] == dup["id"] for c in with_merged)

    # 已合并投诉只读，不能再追加证据
    blocked = client.post(f"/api/food/complaints/{dup['id']}/evidence",
                          json={"evidence_type": "note", "note": "x"})
    assert blocked.status_code == 409
    # 不能自合并 / 重复合并
    again = client.post(f"/api/food/complaints/{dup['id']}/merge",
                        json={"master_id": master["id"], "reason": "x"})
    assert again.status_code == 409
