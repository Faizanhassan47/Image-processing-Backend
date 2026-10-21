from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.database import connect
from core.security import create_access_token, get_password_hash
from main import app
from services.ai_subscription_service import AISubscriptionService, HOVERNET, utcnow_naive


client = TestClient(app)
service = AISubscriptionService(connect)


@pytest.fixture
def ai_user():
    username = f"ai_test_{uuid4().hex[:10]}"
    with connect() as db:
        cur = db.execute("INSERT INTO users (username,display_name,password_hash,role,is_active,is_premium) VALUES (?,?,?,?,1,0)",
                         (username, "AI Test User", get_password_hash("ValidPass123!"), "Pathologist"))
        user_id = cur.lastrowid
        db.commit()
    user = {"id": user_id, "username": username, "display_name": "AI Test User", "role": "Pathologist", "is_active": 1}
    yield user
    with connect() as db:
        db.execute("DELETE FROM user_subscription_features WHERE subscription_id IN (SELECT id FROM user_subscriptions WHERE user_id=?)", (user_id,))
        db.execute("DELETE FROM user_subscriptions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()


def test_catalog_contains_required_features_and_preserves_legacy_catalog_entries(ai_user):
    features = service.list_features(ai_user["id"])
    assert {"HOVERNET", "IMAGE_QC", "TUMOR_DETECTION", "MITOTIC_DETECTION"}.issubset(
        {item["feature_code"] for item in features}
    )
    assert "TISSUE_SEGMENTATION" in {item["feature_code"] for item in features}
    assert [item["feature_code"] for item in features if item["is_implemented"]] == ["HOVERNET"]


def test_free_user_cannot_run_hovernet(ai_user):
    with pytest.raises(HTTPException) as denied:
        service.require_entitlement(ai_user, HOVERNET)
    assert denied.value.status_code == 403


def test_system_administrator_without_subscription_cannot_run_ai(ai_user):
    admin_user = {**ai_user, "role": "System Administrator"}
    with pytest.raises(HTTPException) as denied:
        service.require_entitlement(admin_user, HOVERNET)
    assert denied.value.status_code == 403


def test_protected_super_administrator_can_run_implemented_ai_without_subscription(ai_user):
    admin_user = {**ai_user, "username": "ETDDev", "role": "System Administrator"}
    entitlements = service.require_entitlement(admin_user, HOVERNET)
    assert entitlements["administrative_access"] is True
    assert HOVERNET in entitlements["effective_feature_codes"]


def test_active_hovernet_entitlement_allows_access(ai_user):
    now = utcnow_naive()
    service.create(ai_user["id"], "Research", now - timedelta(days=1), now + timedelta(days=30), [HOVERNET], "etdadmin")
    assert HOVERNET in service.require_entitlement(ai_user, HOVERNET)["effective_feature_codes"]
    with connect() as db:
        assert db.execute("SELECT is_premium FROM users WHERE id=?", (ai_user["id"],)).fetchone()["is_premium"] is True


def test_future_subscription_becomes_usable_when_its_start_date_arrives(ai_user):
    now = utcnow_naive()
    subscription = service.create(ai_user["id"], "Research", now + timedelta(days=1), now + timedelta(days=30), [HOVERNET], "etdadmin")
    assert subscription["status"] == "Active"
    with pytest.raises(HTTPException):
        service.require_entitlement(ai_user, HOVERNET)

    service.update(subscription["id"], start_date=now - timedelta(seconds=1))
    assert HOVERNET in service.require_entitlement(ai_user, HOVERNET)["effective_feature_codes"]


def test_granting_feature_reenables_a_user_preference(ai_user):
    now = utcnow_naive()
    subscription = service.create(ai_user["id"], "Research", now - timedelta(days=1), now + timedelta(days=30), [HOVERNET], "etdadmin")
    feature = next(item for item in service.list_features(ai_user["id"]) if item["feature_code"] == HOVERNET)
    service.set_user_preference(ai_user, feature["id"], False)
    assert HOVERNET not in service.effective_entitlements(ai_user["id"], ai_user["username"])["effective_feature_codes"]

    service.set_features(subscription["id"], [HOVERNET])
    assert HOVERNET in service.effective_entitlements(ai_user["id"], ai_user["username"])["effective_feature_codes"]


@pytest.mark.parametrize("status", ["Expired", "Suspended", "Cancelled"])
def test_inactive_subscription_states_deny_new_analysis(ai_user, status):
    now = utcnow_naive()
    subscription = service.create(ai_user["id"], "Research", now - timedelta(days=1), now + timedelta(days=30), [HOVERNET], "etdadmin")
    service.update(subscription["id"], status=status)
    with pytest.raises(HTTPException) as denied:
        service.require_entitlement(ai_user, HOVERNET)
    assert denied.value.status_code == 403


def test_planned_feature_cannot_execute_even_when_preassigned(ai_user):
    now = utcnow_naive()
    service.create(ai_user["id"], "Research", now - timedelta(days=1), now + timedelta(days=30), ["TUMOR_DETECTION"], "etdadmin")
    with pytest.raises(HTTPException) as denied:
        service.require_entitlement(ai_user, "TUMOR_DETECTION")
    assert denied.value.status_code == 409


def test_non_administrator_cannot_manage_subscriptions(ai_user):
    token = create_access_token({"sub": ai_user["username"], "role": "Pathologist"})
    response = client.post("/api/admin/ai/subscriptions", headers={"Authorization": f"Bearer {token}"}, json={
        "user_id": ai_user["id"], "plan_name": "Research", "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2027-01-01T00:00:00Z", "feature_codes": [HOVERNET],
    })
    assert response.status_code == 403


def test_system_administrator_can_list_subscriptions(ai_user):
    with connect() as db:
        db.execute("UPDATE users SET role='System Administrator' WHERE id=?", (ai_user["id"],))
        db.commit()
    token = create_access_token({"sub": ai_user["username"], "role": "System Administrator"})
    response = client.get("/api/admin/ai/subscriptions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    usernames = [acc["username"] for acc in response.json()]
    assert "etdadmin" not in usernames
    assert "etddev" not in usernames


def test_system_administrator_cannot_assign_unpaid_features(ai_user):
    with connect() as db:
        db.execute("UPDATE users SET role='System Administrator' WHERE id=?", (ai_user["id"],))
        cur = db.execute("INSERT INTO users (username,display_name,password_hash,role,is_active,is_premium) VALUES (?,?,?,?,1,0)",
                         ("doc_lab_user", "Lab Manager", get_password_hash("ValidPass123!"), "Laboratory Manager"))
        doc_user_id = cur.lastrowid
        db.commit()
    token = create_access_token({"sub": ai_user["username"], "role": "System Administrator"})
    # System administrator has not paid for HOVERNET, attempting to assign it fails
    response = client.post("/api/admin/ai/subscriptions", headers={"Authorization": f"Bearer {token}"}, json={
        "user_id": doc_user_id, "plan_name": "Lab Plan", "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2027-01-01T00:00:00Z", "feature_codes": [HOVERNET],
    })
    assert response.status_code == 403
    assert "You can only grant AI features included in your active subscription" in response.json()["detail"]

    # Now give the System Administrator an active subscription for HOVERNET
    now = utcnow_naive()
    service.create(ai_user["id"], "Admin Paid Plan", now - timedelta(days=1), now + timedelta(days=365), [HOVERNET], "etdadmin")

    # Now the System Administrator can grant HOVERNET to doc/lab manager
    response_ok = client.post("/api/admin/ai/subscriptions", headers={"Authorization": f"Bearer {token}"}, json={
        "user_id": doc_user_id, "plan_name": "Lab Plan", "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2027-01-01T00:00:00Z", "feature_codes": [HOVERNET],
    })
    assert response_ok.status_code == 201

    # But still cannot grant unpurchased feature e.g. IMAGE_QC
    sub_id = response_ok.json()["id"]
    response_denied = client.put(f"/api/admin/ai/subscriptions/{sub_id}/features", headers={"Authorization": f"Bearer {token}"}, json={
        "feature_codes": [HOVERNET, "IMAGE_QC"],
    })
    assert response_denied.status_code == 403

    # Clean up doc_user
    with connect() as db:
        db.execute("DELETE FROM user_subscription_features WHERE subscription_id=?", (sub_id,))
        db.execute("DELETE FROM user_subscriptions WHERE id=?", (sub_id,))
        db.execute("DELETE FROM users WHERE id=?", (doc_user_id,))
        db.commit()


def test_protected_super_administrator_can_manage_all_subscriptions_unrestricted():
    token = create_access_token({"sub": "ETDadmin", "role": "System Administrator"})
    response = client.get("/api/admin/ai/subscriptions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_only_super_administrator_can_publish_and_remove_catalog_cards(ai_user):
    with connect() as db:
        feature = dict(db.execute("SELECT * FROM ai_features WHERE feature_code='HOVERNET'").fetchone())
        db.execute("UPDATE users SET role='System Administrator' WHERE id=?", (ai_user["id"],))
        db.commit()
    ordinary_token = create_access_token({"sub": ai_user["username"], "role": "System Administrator"})
    denied = client.patch(f"/api/admin/ai/features/{feature['id']}", headers={"Authorization": f"Bearer {ordinary_token}"}, json={"is_catalog_visible": False})
    assert denied.status_code == 403

    super_token = create_access_token({"sub": "ETDadmin", "role": "Super Administrator"})
    updated = client.patch(f"/api/admin/ai/features/{feature['id']}", headers={"Authorization": f"Bearer {super_token}"}, json={
        "is_catalog_visible": True,
        "is_catalog_deleted": False,
        "catalog_title": "HoVer-Net Nuclei Analysis",
        "catalog_display_order": 3,
        "catalog_category": "Cell analysis",
        "catalog_summary": "Test catalog summary",
        "catalog_purpose": "Test purpose",
        "catalog_value": "Test clinical value",
        "catalog_outputs": ["Overlay", "Counts"],
        "catalog_session_price_usd": "12.50",
        "catalog_usd_to_pkr_rate": "280",
    })
    assert updated.status_code == 200
    stored = next(item for item in service.list_features(ai_user["id"]) if item["feature_code"] == "HOVERNET")
    assert stored["catalog_summary"] == "Test catalog summary"
    assert stored["catalog_outputs_json"] == '["Overlay", "Counts"]'
    assert stored["catalog_title"] == "HoVer-Net Nuclei Analysis"
    assert stored["catalog_display_order"] == 3
    assert float(stored["catalog_session_price_usd"]) == 12.5
    assert float(stored["catalog_usd_to_pkr_rate"]) == 280
    assert float(stored["catalog_session_price_pkr"]) == 3500

    removed = client.patch(f"/api/admin/ai/features/{feature['id']}", headers={"Authorization": f"Bearer {super_token}"}, json={"is_catalog_visible": False, "is_catalog_deleted": True})
    assert removed.status_code == 200
    assert next(item for item in service.list_features(ai_user["id"]) if item["id"] == feature["id"])["is_catalog_deleted"] is True

    with connect() as db:
        db.execute("""UPDATE ai_features SET catalog_category=?,catalog_summary=?,catalog_purpose=?,catalog_value=?,
            catalog_outputs_json=?,is_catalog_visible=?,is_featured=?,catalog_title=?,catalog_display_order=?,is_catalog_deleted=?,
            catalog_session_price_usd=?,catalog_usd_to_pkr_rate=?,catalog_session_price_pkr=? WHERE id=?""", (
            feature.get("catalog_category"), feature.get("catalog_summary"), feature.get("catalog_purpose"),
            feature.get("catalog_value"), feature.get("catalog_outputs_json"), feature.get("is_catalog_visible", 1),
            feature.get("is_featured", 0), feature.get("catalog_title"), feature.get("catalog_display_order", 0), feature.get("is_catalog_deleted", 0),
            feature.get("catalog_session_price_usd"), feature.get("catalog_usd_to_pkr_rate"), feature.get("catalog_session_price_pkr"), feature["id"],
        ))
        db.commit()
