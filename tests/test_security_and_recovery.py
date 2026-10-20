import io
import sys
from pathlib import Path
import pytest

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient
from main import app
from core.database import connect
from core.security import create_access_token, get_password_hash

client = TestClient(app)

@pytest.fixture(scope="session", autouse=True)
def setup_test_users():
    """Ensure test users for Laboratory Manager and Pathologist exist in database for testing."""
    test_users = [
        ("test_lab_mgr", "Test Lab Manager", "Laboratory Manager"),
        ("dr_alice", "Dr. Alice", "Pathologist"),
        ("dr_bob", "Dr. Bob", "Pathologist"),
        ("disabled_user", "Disabled User", "Pathologist"),
    ]
    hash_val = get_password_hash("ValidPass123!")
    with connect() as db:
        for username, display_name, role in test_users:
            existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if not existing:
                is_active = 0 if username == "disabled_user" else 1
                db.execute(
                    "INSERT INTO users (username, display_name, password_hash, role, is_active) VALUES (?, ?, ?, ?, ?)",
                    (username, display_name, hash_val, role, is_active),
                )
        db.commit()

def make_auth_headers(username: str, role: str):
    token = create_access_token({"sub": username, "role": role})
    return {"Authorization": f"Bearer {token}"}

# =====================================================================
# 1. AUTHENTICATION & SESSION SECURITY TESTS
# =====================================================================

def test_unauthenticated_request_rejected():
    response = client.get("/api/cases")
    assert response.status_code == 401
    assert "Not authenticated" in response.text

def test_invalid_token_rejected():
    response = client.get("/api/cases", headers={"Authorization": "Bearer invalid.token.payload"})
    assert response.status_code == 401
    assert "Invalid token" in response.text

def test_disabled_user_rejected():
    headers = make_auth_headers("disabled_user", "Pathologist")
    response = client.get("/api/cases", headers=headers)
    assert response.status_code == 401
    assert "Inactive user" in response.text

def test_login_invalid_credentials_rejected():
    response = client.post(
        "/api/auth/login",
        data={"username": "non_existent_user_123", "password": "WrongPassword123!"}
    )
    assert response.status_code == 401
    assert "Incorrect username or password" in response.json()["detail"]

# =====================================================================
# 2. ROLE-BASED ACCESS CONTROL (RBAC) & ROLE PERMISSIONS
# =====================================================================

def test_lab_manager_forbidden_from_privileged_audit_log():
    # Laboratory Manager must NOT access privileged security audit logs
    headers = make_auth_headers("test_lab_mgr", "Laboratory Manager")
    response = client.get("/api/platform/audit", headers=headers)
    assert response.status_code == 403
    assert "Not enough permissions" in response.text

def test_pathologist_forbidden_from_privileged_audit_log():
    headers = make_auth_headers("dr_alice", "Pathologist")
    response = client.get("/api/platform/audit", headers=headers)
    assert response.status_code == 403
    assert "Not enough permissions" in response.text

def test_system_admin_allowed_audit_log_access():
    headers = make_auth_headers("etdadmin", "System Administrator")
    response = client.get("/api/platform/audit", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_audit_log_csv_export_authorized_for_admin():
    headers = make_auth_headers("etdadmin", "System Administrator")
    response = client.get("/api/platform/audit?format=csv", headers=headers)
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    assert "actor_username" in response.text

def test_backup_endpoint_restricted_to_admin():
    headers_lab = make_auth_headers("test_lab_mgr", "Laboratory Manager")
    response = client.get("/api/platform/backups", headers=headers_lab)
    assert response.status_code == 403

    headers_admin = make_auth_headers("etdadmin", "System Administrator")
    response_admin = client.get("/api/platform/backups", headers=headers_admin)
    assert response_admin.status_code == 200

# =====================================================================
# 3. PATHOLOGIST ASSIGNED-CASE BOUNDARIES & RECORD-LEVEL ACCESS
# =====================================================================

def test_pathologist_assigned_case_restriction_enforced():
    # Pathologist Dr. Alice cannot access a case assigned to Dr. Bob
    headers_alice = make_auth_headers("dr_alice", "Pathologist")
    
    response = client.get("/api/cases", headers=headers_alice)
    assert response.status_code == 200
    cases = response.json()
    for c in cases:
        assert (c.get("reviewer") or "").lower() in ["dr_alice", ""]

def test_pathologist_forbidden_from_unassigned_report_export():
    headers_alice = make_auth_headers("dr_alice", "Pathologist")
    # Trying to export a report ID that is not assigned to dr_alice
    response = client.get("/api/reports/999999/pdf", headers=headers_alice)
    # Either 404 (not found) or 403 (unassigned) - strictly never 200
    assert response.status_code in [403, 404]

# =====================================================================
# 4. DOCUMENT UPLOAD & MALWARE QUARANTINE PROCESSING
# =====================================================================

def test_eicar_malware_upload_quarantined():
    headers_admin = make_auth_headers("etdadmin", "System Administrator")
    
    # EICAR Standard Anti-Virus Test signature string
    eicar_sig = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    
    # Ensure patient exists
    p_resp = client.get("/api/platform/patients", headers=headers_admin)
    assert p_resp.status_code == 200
    patients = p_resp.json()
    patient_id = patients[0]["id"] if patients else 1

    files = {"file": ("test_doc.txt", io.BytesIO(eicar_sig), "text/plain")}
    data = {"document_type": "Lab Report", "description": "EICAR security test"}
    
    upload_resp = client.post(f"/api/patients/{patient_id}/documents", data=data, files=files, headers=headers_admin)
    if upload_resp.status_code == 201:
        doc = upload_resp.json()
        doc_id = doc["id"]
        
        # Verify document download by non-admin is rejected due to quarantine
        headers_pathologist = make_auth_headers("dr_alice", "Pathologist")
        dl_resp = client.get(f"/api/patients/{patient_id}/documents/{doc_id}/download", headers=headers_pathologist)
        assert dl_resp.status_code == 403
        assert "quarantined or rejected" in dl_resp.text.lower()

# =====================================================================
# 5. RETENTION PREVIEW SAFETY (READ-ONLY)
# =====================================================================

def test_retention_preview_safe_and_read_only():
    headers = make_auth_headers("etdadmin", "System Administrator")
    response = client.get("/api/platform/retention/preview?days=3650", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["preview_only"] is True
    assert "eligible" in data
    assert "audit_events" in data["eligible"]

# =====================================================================
# 6. STORAGE MONITORING & OPERATIONAL HEALTH
# =====================================================================

def test_system_health_endpoint():
    headers = make_auth_headers("etdadmin", "System Administrator")
    response = client.get("/api/platform/health", headers=headers)
    assert response.status_code == 200
    health = response.json()
    assert health["ok"] is True
    assert "storage" in health
    assert "free" in health["storage"]
    assert "used_percent" in health["storage"]
    assert "hardware" in health
    assert "cpu_percent" in health["hardware"]
    assert "memory_used_percent" in health["hardware"]
