import json
import pytest
import main
from test_patient_api import patient_payload


def test_patient_governance_retention_and_access_controls(client):
    # Ensure test user etdadmin exists in DB for this test run
    with main.connect() as db:
        pw = main.get_password_hash("password123")
        if not db.execute("SELECT id FROM users WHERE username='etdadmin'").fetchone():
            db.execute("INSERT INTO users (username, display_name, role, password_hash, is_active) VALUES ('etdadmin', 'Protected Admin', 'System Administrator', ?, 1)", (pw,))
            db.commit()

    # 1. Create patient with organization, facility, department, care team, and consent status
    payload = patient_payload(
        cnic="3520199999991",
        medical_record_number="MRN-GOV-01",
        external_id="EXT-GOV-01",
        organization_id="ORG-CENTRAL-HEALTH",
        facility_name="Memorial Pathology Lab",
        department="Histopathology",
        care_team=["dr.amina", "dr.pathologist", "tech.manager"],
        consent_status="Granted",
        legal_hold=False,
    )
    res = client.post("/api/patients", json=payload)
    assert res.status_code == 201, res.text
    patient = res.json()
    patient_id = patient["id"]

    assert patient["organization_id"] == "ORG-CENTRAL-HEALTH"
    assert patient["facility_name"] == "Memorial Pathology Lab"
    assert patient["department"] == "Histopathology"
    assert patient["consent_status"] == "Granted"
    assert patient["legal_hold"] is False or patient["legal_hold"] == 0

    # 2. Place active legal hold and verify deletion is strictly blocked
    patch_res = client.patch(f"/api/patients/{patient_id}", json={"legal_hold": True})
    assert patch_res.status_code == 200, patch_res.text
    assert patch_res.json()["legal_hold"] in (True, 1)

    # Protected System Administrator tries to delete record under legal hold
    admin_token = main.create_access_token({"sub": "etdadmin", "role": "System Administrator"})
    client.cookies.set("access_token", admin_token)

    blocked_del = client.delete(f"/api/patients/{patient_id}")
    assert blocked_del.status_code == 400
    assert "legal hold" in blocked_del.json()["detail"].lower()

    # Release legal hold and verify deletion succeeds
    release_res = client.patch(f"/api/patients/{patient_id}", json={"legal_hold": False})
    assert release_res.status_code == 200

    del_res = client.delete(f"/api/patients/{patient_id}")
    assert del_res.status_code == 200


def test_patient_document_malware_quarantine_and_approval_workflow(client):
    # Ensure users exist
    with main.connect() as db:
        pw = main.get_password_hash("password123")
        for u, r, name in [("pathologist", "Pathologist", "Dr. Path"), ("manager", "Laboratory Manager", "Lab Manager"), ("etdadmin", "System Administrator", "Protected Admin")]:
            if not db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone():
                db.execute("INSERT INTO users (username, display_name, role, password_hash, is_active) VALUES (?, ?, ?, ?, 1)", (u, name, r, pw))
        db.commit()

    # 1. Create a patient
    p_res = client.post("/api/patients", json=patient_payload(cnic="3520188888882", medical_record_number="MRN-MAL-01", external_id="EXT-MAL-01"))
    assert p_res.status_code == 201, p_res.text
    patient_id = p_res.json()["id"]

    # 2. Upload clean document
    clean_upload = client.post(
        f"/api/patients/{patient_id}/documents",
        data={"document_type": "Referral", "description": "Safe clinical report"},
        files={"file": ("safe_report.txt", b"Safe patient notes without malware", "text/plain")},
    )
    assert clean_upload.status_code == 201
    clean_doc_id = clean_upload.json()["id"]

    # Clean document download succeeds for staff
    dl_clean = client.get(f"/api/patients/{patient_id}/documents/{clean_doc_id}/download")
    assert dl_clean.status_code == 200

    # 3. Upload document with malware pattern (EICAR standard test string)
    eicar_bytes = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    mal_upload = client.post(
        f"/api/patients/{patient_id}/documents",
        data={"document_type": "Referral", "description": "Suspicious referral attachment"},
        files={"file": ("suspicious.txt", eicar_bytes, "text/plain")},
    )
    assert mal_upload.status_code == 201
    mal_doc_id = mal_upload.json()["id"]

    # Verify document is marked Quarantined in database
    with main.connect() as db:
        doc_row = db.execute("SELECT scan_status, quarantine_reason FROM patient_documents WHERE id=?", (mal_doc_id,)).fetchone()
        assert doc_row["scan_status"] == "Quarantined"
        assert doc_row["quarantine_reason"] is not None

    # Pathologist attempt to download quarantined document is forbidden (403)
    path_token = main.create_access_token({"sub": "pathologist", "role": "Pathologist"})
    client.cookies.set("access_token", path_token)
    blocked_dl = client.get(f"/api/patients/{patient_id}/documents/{mal_doc_id}/download")
    assert blocked_dl.status_code == 403
    assert "quarantined" in blocked_dl.json()["detail"].lower()

    # Laboratory Manager or Admin reviews and approves document
    mgr_token = main.create_access_token({"sub": "manager", "role": "Laboratory Manager"})
    client.cookies.set("access_token", mgr_token)
    approve_res = client.post(
        f"/api/patients/{patient_id}/documents/{mal_doc_id}/quarantine-action",
        data={"action": "approve", "reason": "Reviewed and cleared by safety team"},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["scan_status"] == "Clean"

    # Now pathologist can download the cleared document
    client.cookies.set("access_token", path_token)
    allowed_dl = client.get(f"/api/patients/{patient_id}/documents/{mal_doc_id}/download")
    assert allowed_dl.status_code == 200
    assert allowed_dl.content == eicar_bytes

    # Clean up patient
    admin_token = main.create_access_token({"sub": "etdadmin", "role": "System Administrator"})
    client.cookies.set("access_token", admin_token)
    client.delete(f"/api/patients/{patient_id}")


def test_audit_log_real_actions_create_complete_records(client, png_file):
    # Ensure etdadmin user exists in DB
    with main.connect() as db:
        pw = main.get_password_hash("password123")
        if not db.execute("SELECT id FROM users WHERE username='etdadmin'").fetchone():
            db.execute("INSERT INTO users (username, display_name, role, password_hash, is_active) VALUES ('etdadmin', 'Protected Admin', 'System Administrator', ?, 1)", (pw,))
            db.commit()

    admin_token = main.create_access_token({"sub": "etdadmin", "role": "System Administrator"})
    client.cookies.set("access_token", admin_token)

    # 1. Perform distinct real actions: create patient, update patient, export patient, delete patient
    p_res = client.post("/api/patients", json=patient_payload(cnic="3520177777773", medical_record_number="MRN-AUDIT-01", external_id="EXT-AUDIT-01"))
    assert p_res.status_code == 201
    patient_id = p_res.json()["id"]

    client.patch(f"/api/patients/{patient_id}", json={"phone": "+92-300-111-2222"})
    client.get(f"/api/patients/{patient_id}/export")
    client.delete(f"/api/patients/{patient_id}")

    # 2. Inspect audit_log table and verify complete records
    with main.connect() as db:
        rows = db.execute(
            "SELECT id, actor_username, action, target_type, target_id, details, created_at FROM audit_log WHERE target_id=? ORDER BY id ASC",
            (str(patient_id),)
        ).fetchall()

        actions = [r["action"] for r in rows]
        assert "patient.create" in actions
        assert "patient.update" in actions
        assert "patient.export" in actions
        assert "patient.delete" in actions

        for r in rows:
            assert r["actor_username"] == "etdadmin"
            assert r["target_type"] == "patient"
            assert r["target_id"] == str(patient_id)
            assert r["created_at"] is not None
