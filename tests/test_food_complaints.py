from __future__ import annotations

from app.database import get_connection


def _chain(client, lot_code="LOT-C1"):
    lot = client.post("/api/food/lots", json={
        "lot_code": lot_code, "product_name": "青菜", "category": "叶菜",
        "supplier": "安心农场", "origin": "山东", "harvest_date": "2026-09-23",
        "quantity_kg": 300, "trace_code": lot_code + "-T",
    }).json()
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={
        "sample_code": lot_code + "-S", "collected_at": "2026-09-23T08:00:00+00:00",
        "collector": "监管员", "location": "食堂后厨", "sample_weight_g": 200,
    }).json()
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={
        "analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.01,
        "limit_mg_kg": 0.05, "lab_operator": "实验员",
        "tested_at": "2026-09-23T12:00:00+00:00", "certificate_no": "CERT-1",
    }).json()
    shipment = client.post(f"/api/food/lots/{lot['id']}/shipments", json={
        "shipment_code": lot_code + "-SHIP", "carrier": "冷链", "vehicle_no": "鲁B01",
        "departure_at": "2026-09-23T20:00:00+00:00", "arrival_due_at": "2026-09-24T05:00:00+00:00",
        "destination": "机关食堂", "target_temp_min": 0, "target_temp_max": 8,
    }).json()
    temp = client.post(f"/api/food/shipments/{shipment['id']}/temperatures", json={
        "recorded_at": "2026-09-23T23:00:00+00:00", "temperature_c": 4, "source": "sensor-1",
    }).json()
    return lot, sample, result, shipment, temp


def _register(client, lot, shipment, phone="13800000001", name="张女士", canteen="机关食堂"):
    resp = client.post("/api/food/complaints", json={
        "canteen_name": canteen, "description": "炒青菜有刺鼻农药味",
        "contact_name": name, "contact_phone": phone,
        "occurred_at": "2026-09-24T07:30:00+00:00",
        "lot_code": lot["lot_code"], "shipment_code": shipment["shipment_code"],
        "link_deadline_hours": 24,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _close(client, complaint_id, result_id, temp_id=None):
    ev = client.post(f"/api/food/complaints/{complaint_id}/evidence",
                     json={"evidence_type": "certificate", "source_id": result_id})
    assert ev.status_code == 201, ev.text
    if temp_id is not None:
        tev = client.post(f"/api/food/complaints/{complaint_id}/evidence",
                          json={"evidence_type": "temperature", "source_id": temp_id})
        assert tev.status_code == 201, tev.text
    assert client.post(f"/api/food/complaints/{complaint_id}/assign",
                       json={"assignee": "李监管", "deadline_hours": 48}).status_code == 200
    assert client.post(f"/api/food/complaints/{complaint_id}/start",
                       json={"operator": "李监管"}).status_code == 200
    assert client.post(f"/api/food/complaints/{complaint_id}/resolve",
                       json={"resolution": "批次检测合格，异味为储运闷捂，已责令食堂整改",
                             "operator": "李监管"}).status_code == 200
    review = client.post(f"/api/food/complaints/{complaint_id}/review",
                         json={"passed": True, "opinion": "证据齐全，同意结案", "reviewer": "王科长"})
    assert review.status_code == 200, review.text
    return review.json()


def test_full_complaint_lifecycle_with_deadlines_and_completeness(client):
    lot, sample, result, shipment, temp = _chain(client)
    complaint = _register(client, lot, shipment)
    cid = complaint["id"]

    # 登记时自动固定批次与配送节点快照，尚缺证书，完整度 2/3
    assert complaint["evidence"]["completeness"] == round(2 / 3, 4)
    assert complaint["evidence"]["missing_types"] == ["certificate"]
    assert complaint["current_assignee"] is None
    assert 23 * 3600 < complaint["link_remaining_seconds"] <= 24 * 3600

    # 未配齐证据时复核结案被拒绝
    client.post(f"/api/food/complaints/{cid}/assign", json={"assignee": "李监管", "deadline_hours": 48})
    client.post(f"/api/food/complaints/{cid}/start", json={"operator": "李监管"})
    assert client.post(f"/api/food/complaints/{cid}/resolve",
                       json={"resolution": "未查实", "operator": "李监管"}).status_code == 200
    blocked = client.post(f"/api/food/complaints/{cid}/review",
                          json={"passed": True, "opinion": "同意结案", "reviewer": "王科长"})
    assert blocked.status_code == 409

    # 补充材料流程：申请补充后挂起，提交后自动恢复处置
    assert client.post(f"/api/food/complaints/{cid}/review",
                       json={"passed": False, "opinion": "缺证书，退回", "reviewer": "王科长"}).status_code == 200
    detail = client.get(f"/api/food/complaints/{cid}").json()
    assert detail["status"] == "reopened"
    assert 0 < detail["handle_remaining_seconds"] <= 72 * 3600
    client.post(f"/api/food/complaints/{cid}/assign", json={"assignee": "李监管", "deadline_hours": 48})
    client.post(f"/api/food/complaints/{cid}/start", json={"operator": "李监管"})
    client.post(f"/api/food/complaints/{cid}/supplement/request",
                json={"reason": "需食堂提供留样照片", "requested_by": "李监管"})
    assert client.get(f"/api/food/complaints/{cid}").json()["status"] == "awaiting_supplement"
    sub = client.post(f"/api/food/complaints/{cid}/supplement/submit",
                      json={"title": "留样照片说明", "content": "照片与签收单已补交", "submitted_by": "食堂经理"})
    assert sub.status_code == 201
    assert sub.json()["status"] == "processing"

    closed = _close(client, cid, result["id"], temp["id"])
    assert closed["status"] == "closed"
    assert closed["evidence"]["complete"] is True
    assert closed["evidence"]["completeness"] == 1.0
    assert closed["current_assignee"] == "李监管"
    assert closed["handle_remaining_seconds"] is None
    assert "证据齐全，同意结案" in closed["close_reason"]

    detail = client.get(f"/api/food/complaints/{cid}").json()
    actions = [item["action"] for item in detail["timeline"]]
    assert "complaint.register" in actions and "review.pass" in actions
    assert any(m["title"] == "留样照片说明" for m in detail["materials"])


def test_overdue_flags_in_query(client):
    lot, _, _, shipment, _ = _chain(client, "LOT-C2")
    complaint = _register(client, lot, shipment, phone="13800000002")
    cid = complaint["id"]
    client.post(f"/api/food/complaints/{cid}/assign", json={"assignee": "李监管", "deadline_hours": 48})

    conn = get_connection()
    conn.execute("UPDATE food_complaints SET handle_deadline=? WHERE id=?",
                 ("2000-01-01T00:00:00+00:00", cid))
    conn.commit()

    detail = client.get(f"/api/food/complaints/{cid}").json()
    assert detail["handle_overdue"] is True and detail["handle_remaining_seconds"] < 0
    listed = client.get("/api/food/complaints", params={"overdue_only": True}).json()
    assert any(item["id"] == cid for item in listed["data"])


def test_certificate_revocation_after_close_keeps_snapshot_and_reopens_review(client):
    lot, _, result, shipment, _ = _chain(client, "LOT-C3")
    complaint = _register(client, lot, shipment, phone="13800000003")
    cid = complaint["id"]
    closed = _close(client, cid, result["id"])
    assert closed["status"] == "closed"
    original_close_reason = closed["close_reason"]

    # 证书被撤销：快照原样保留，证据标记待复查，已结案件自动转入复查
    revoked = client.post(f"/api/food/results/{result['id']}/revoke",
                          json={"reason": "实验室发现仪器未校准", "operator": "检测中心"})
    assert revoked.status_code == 200, revoked.text

    detail = client.get(f"/api/food/complaints/{cid}").json()
    assert detail["status"] == "reviewing"
    assert detail["needs_recheck"] == 1
    cert_evidence = next(e for e in detail["evidence_list"] if e["evidence_type"] == "certificate")
    assert cert_evidence["source_status"] == "revoked"
    assert cert_evidence["review_required"] == 1
    assert "仪器未校准" in cert_evidence["review_reason"]

    # 快照内容仍是引用当时的合格证书，哈希不变
    conn = get_connection()
    row = conn.execute("SELECT snapshot_json, snapshot_hash FROM food_complaint_evidence WHERE id=?",
                       (cert_evidence["id"],)).fetchone()
    import json
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["verdict"] == "pass" and snapshot["certificate_no"] == "CERT-1"
    assert snapshot["revoked"] == 0
    assert row["snapshot_hash"] == cert_evidence["snapshot_hash"]

    # 撤销后不能直接结案
    again = client.post(f"/api/food/complaints/{cid}/review",
                        json={"passed": True, "opinion": "再结案", "reviewer": "王科长"})
    assert again.status_code == 409

    # 复查确认后回到待复核，原结案理由仍保留在时间线/字段中
    confirmed = client.post(f"/api/food/complaints/{cid}/evidence/recheck",
                            json={"operator": "王科长", "note": "已调取留样复检合格"})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "resolved"
    assert confirmed.json()["needs_recheck"] == 0

    review = client.post(f"/api/food/complaints/{cid}/review",
                         json={"passed": True, "opinion": "复检合格，维持原结论", "reviewer": "王科长"})
    assert review.status_code == 200 and review.json()["status"] == "closed"
    assert original_close_reason in review.json()["close_reason"]

    # 被撤销证据仍永久在册（revoked + 快照），没有被删除
    detail = client.get(f"/api/food/complaints/{cid}").json()
    cert_evidence = next(e for e in detail["evidence_list"] if e["evidence_type"] == "certificate")
    assert cert_evidence["source_status"] == "revoked"
    assert "evidence.revoked" in [t["action"] for t in detail["timeline"]]


def test_lot_recall_does_not_destroy_original_evidence(client):
    lot, _, _, shipment, _ = _chain(client, "LOT-C4")
    complaint = _register(client, lot, shipment, phone="13800000004")
    cid = complaint["id"]
    lot_evidence_id = complaint["evidence_list"] if "evidence_list" in complaint else None
    detail = client.get(f"/api/food/complaints/{cid}").json()
    lot_evidence = next(e for e in detail["evidence_list"] if e["evidence_type"] == "lot")

    decision = client.post(f"/api/food/lots/{lot['id']}/risk",
                           json={"decision": "recall", "reason": "接外地通报", "operator": "监管员"})
    assert decision.status_code == 200 and decision.json()["status"] == "recalled"

    # 批次虽被召回，投诉证据仍在，快照保留召回前的原始状态
    conn = get_connection()
    row = conn.execute("SELECT snapshot_json FROM food_complaint_evidence WHERE id=?",
                       (lot_evidence["id"],)).fetchone()
    import json
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["status"] in {"testing", "released", "held", "pending"}
    still = client.get(f"/api/food/complaints/{cid}").json()
    assert any(e["evidence_type"] == "lot" for e in still["evidence_list"])


def test_merge_keeps_independent_contacts_and_timelines(client):
    lot, _, _, shipment, _ = _chain(client, "LOT-C5")
    first = _register(client, lot, shipment, phone="13800000005", name="张女士")
    second = _register(client, lot, shipment, phone="13900000006", name="李先生", canteen="机关食堂")
    # 被合并案有自己的补充材料
    client.post(f"/api/food/complaints/{second['id']}/assign", json={"assignee": "赵执法", "deadline_hours": 24})
    client.post(f"/api/food/complaints/{second['id']}/start", json={"operator": "赵执法"})
    client.post(f"/api/food/complaints/{second['id']}/supplement/submit",
                json={"title": "李先生的购菜小票", "content": "9月24日早7点购买", "submitted_by": "李先生"})

    merge = client.post(f"/api/food/complaints/{first['id']}/merge",
                        json={"duplicate_id": second["id"], "reason": "同一批次同餐次"})
    assert merge.status_code == 200, merge.text

    # 被合并投诉仍可独立查询，联系人、材料、时间线未被吞掉
    child = client.get(f"/api/food/complaints/{second['id']}").json()
    assert child["status"] == "merged" and child["merged_into"] == first["id"]
    assert child["contact_name"] == "李先生" and child["contact_phone"] == "13900000006"
    assert any(m["title"] == "李先生的购菜小票" for m in child["materials"])
    child_actions = [t["action"] for t in child["timeline"]]
    assert "merge.duplicate" in child_actions and "supplement.submit" in child_actions

    # 主投诉挂着子投诉摘要，且自身流程不受影响
    parent = client.get(f"/api/food/complaints/{first['id']}").json()
    assert parent["merged_children"][0]["contact_name"] == "李先生"
    assert parent["merged_children"][0]["timeline_count"] == len(child["timeline"])
    assert client.post(f"/api/food/complaints/{second['id']}/assign",
                       json={"assignee": "任何人", "deadline_hours": 24}).status_code == 409
    assert client.post(f"/api/food/complaints/{first['id']}/assign",
                       json={"assignee": "李监管", "deadline_hours": 48}).status_code == 200


def test_temperature_revocation_flags_recheck(client):
    lot, _, result, shipment, temp = _chain(client, "LOT-C6")
    complaint = _register(client, lot, shipment, phone="13800000007")
    cid = complaint["id"]
    closed = _close(client, cid, result["id"], temp["id"])
    assert closed["status"] == "closed"

    resp = client.post(f"/api/food/temperatures/{temp['id']}/revoke",
                       json={"reason": "传感器事后发现漂移", "operator": "物流主管"})
    assert resp.status_code == 200, resp.text
    detail = client.get(f"/api/food/complaints/{cid}").json()
    assert detail["status"] == "reviewing" and detail["needs_recheck"] == 1
    temp_evidence = next(e for e in detail["evidence_list"] if e["evidence_type"] == "temperature")
    assert temp_evidence["source_status"] == "revoked"
    # 温度不是必备证据类别，复查确认后即可重新结案
    client.post(f"/api/food/complaints/{cid}/evidence/recheck", json={"operator": "王科长"})
    review = client.post(f"/api/food/complaints/{cid}/review",
                         json={"passed": True, "opinion": "温度证据瑕疵不影响结论", "reviewer": "王科长"})
    assert review.status_code == 200 and review.json()["status"] == "closed"
