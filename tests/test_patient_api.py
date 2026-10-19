import json

import main


def patient_payload(**overrides):
    value = {
        "cnic": "3520112345671",
        "medical_record_number": "MRN-1001",
        "external_id": "EXT-1001",
        "first_name": "Amina",
        "last_name": "Khan",
        "date_of_birth": "1984-06-12",
        "sex": "Female",
        "status": "Active",
        "additional_details": {"preferredLanguage": "Urdu"},
    }
    value.update(overrides)
    return value


def test_patient_registration_search_update_and_duplicate(client):
    created = client.post("/api/patients", json=patient_payload())
    assert created.status_code == 201, created.text
    patient = created.json()
    assert patient["patient_identifier"].startswith("PAT-")
    assert patient["age"] is not None

    duplicate = client.post("/api/patients", json=patient_payload(first_name="Different"))
    assert duplicate.status_code == 409

    listing = client.get("/api/patients", params={"search": "MRN-1001"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    updated = client.patch(f"/api/patients/{patient['id']}", json={"phone": "+92-300-555-0101"})
    assert updated.status_code == 200
    assert updated.json()["phone"] == "+92-300-555-0101"


def test_cnic_is_required_valid_unique_and_searchable(client):
    missing = patient_payload(); missing.pop("cnic")
    assert client.post("/api/patients", json=missing).status_code == 422
    assert client.post("/api/patients", json=patient_payload(cnic="12345")).status_code == 422
    assert client.post("/api/patients", json=patient_payload(cnic="35201-123456789")).status_code == 422

    created = client.post("/api/patients", json=patient_payload(cnic="35201-1234567-1"))
    assert created.status_code == 201, created.text
    assert created.json()["cnic"] == "3520112345671"

    repeated = client.post("/api/patients", json=patient_payload(cnic="3520112345671", medical_record_number="MRN-OTHER", external_id="EXT-OTHER"))
    assert repeated.status_code == 409

    assert client.get("/api/patients", params={"search": "3520112345671"}).json()["total"] == 1
    assert client.get("/api/patients/duplicates", params={"cnic": "35201-1234567-1"}).json()[0]["id"] == created.json()["id"]

    other = client.post("/api/patients", json=patient_payload(cnic="3520198765432", medical_record_number="MRN-3", external_id="EXT-3")).json()
    clash = client.patch(f"/api/patients/{other['id']}", json={"cnic": "3520112345671"})
    assert clash.status_code == 409


def test_medical_history_is_validated_and_revisioned(client):
    patient_id = client.post("/api/patients", json=patient_payload()).json()["id"]
    history = {
        "allergies": ["Penicillin"],
        "chronicConditions": ["Hypertension"],
        "medications": ["Amlodipine"],
        "additionalInformation": {"smokingStatus": "Never"},
        "change_reason": "Initial history",
    }
    saved = client.put(f"/api/patients/{patient_id}/medical-history", json=history)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision_number"] == 1
    history["allergies"].append("Latex")
    history["change_reason"] = "Allergy confirmed"
    assert client.put(f"/api/patients/{patient_id}/medical-history", json=history).json()["revision_number"] == 2
    loaded = client.get(f"/api/patients/{patient_id}/medical-history").json()
    assert loaded["history"]["allergies"] == ["Penicillin", "Latex"]
    assert len(loaded["revisions"]) == 2

    invalid = client.put(f"/api/patients/{patient_id}/medical-history", json={"allergies": "not-an-array", "change_reason": "invalid type"})
    assert invalid.status_code == 422


def test_case_test_report_slide_timeline_and_export(client, png_file):
    patient_id = client.post("/api/patients", json=patient_payload()).json()["id"]
    case_response = client.post("/api/cases", data={"case_id": "PAT-CASE-001", "description": "Biopsy"}, files={"image": ("biopsy.png", png_file, "image/png")})
    assert case_response.status_code == 201, case_response.text
    case_id = case_response.json()["id"]
    linked = client.post(f"/api/patients/{patient_id}/cases", json={"case_id": case_id, "accession_number": "ACC-001", "priority": "Routine"})
    assert linked.status_code == 201, linked.text
    other_patient_id = client.post("/api/patients", json=patient_payload(cnic="3520198765432", medical_record_number="MRN-2002", external_id="EXT-2002")).json()["id"]
    cross_patient = client.post(f"/api/patients/{other_patient_id}/cases", json={"case_id": case_id, "accession_number": "ACC-OTHER", "priority": "Routine"})
    assert cross_patient.status_code == 409

    report = client.post(f"/api/cases/{case_id}/reports")
    assert report.status_code == 201
    finalized = client.post(f"/api/reports/{report.json()['id']}/finalize")
    assert finalized.status_code == 200

    test = client.post(f"/api/patients/{patient_id}/tests", json={
        "test_identifier": "TEST-001", "test_name": "Histopathology", "test_category": "Pathology",
        "case_id": case_id, "report_id": report.json()["id"], "status": "Final",
        "summary_result": "Recorded pathology result", "results": [{"analyte_name": "Diagnosis", "result_value": "Recorded finding"}],
    })
    assert test.status_code == 201, test.text
    assert client.get(f"/api/patients/{patient_id}/cases").json()["total"] == 1
    assert client.get(f"/api/patients/{patient_id}/tests").json()["total"] == 1
    assert len(client.get(f"/api/patients/{patient_id}/reports").json()) == 1
    assert len(client.get(f"/api/patients/{patient_id}/slides").json()) == 1

    timeline = client.get(f"/api/patients/{patient_id}/timeline").json()["items"]
    assert timeline == sorted(timeline, key=lambda event: event["event_at"], reverse=True)
    assert {event["event_type"] for event in timeline} >= {"Patient registration", "Case created", "Slide uploaded", "Report finalized", "Test recorded"}

    exported = client.get(f"/api/patients/{patient_id}/export")
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == "1.0"
    assert exported.json()["reports"][0]["status"] == "Finalized"

    # Patient import only accepts demographics/history and cannot overwrite reports.
    document = exported.json()
    document["reports"][0]["status"] = "Draft"
    preview = client.post("/api/patients/import", json={"document": document, "confirm": False})
    assert preview.status_code == 200
    assert "reports" in preview.json()["preview"]["ignored_sections"]
    confirmed = client.post("/api/patients/import", json={"document": document, "confirm": True})
    assert confirmed.status_code == 200
    with main.connect() as db:
        assert db.execute("SELECT status FROM reports WHERE id=?", (report.json()["id"],)).fetchone()["status"] == "Finalized"


def test_notes_documents_profile_and_access_control(client):
    patient_id = client.post("/api/patients", json=patient_payload()).json()["id"]
    note = client.post(f"/api/patients/{patient_id}/notes", json={"note_type": "Clinical", "note_text": "De-identified research note"})
    assert note.status_code == 201
    revised = client.patch(f"/api/patients/{patient_id}/notes/{note.json()['id']}", json={"note_text": "Revised de-identified note", "change_reason": "Clarified wording"})
    assert revised.status_code == 200
    assert revised.json()["revision_number"] == 2

    uploaded = client.post(f"/api/patients/{patient_id}/documents", data={"document_type": "Referral", "description": "Referral note"}, files={"file": ("referral.txt", b"De-identified referral", "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    download = client.get(f"/api/patients/{patient_id}/documents/{uploaded.json()['id']}/download")
    assert download.status_code == 200
    assert download.content == b"De-identified referral"
    profile = client.get(f"/api/patients/{patient_id}/complete-profile")
    assert profile.status_code == 200
    assert profile.json()["patient"]["medical_record_number"] == "MRN-1001"

    client.cookies.clear()
    assert client.get(f"/api/patients/{patient_id}").status_code == 401


def test_invalid_import_does_not_create_patient(client):
    before = client.get("/api/patients").json()["total"]
    invalid = client.post("/api/patients/import", json={"document": {"schema_version": "99.0", "patient": {}}, "confirm": True})
    assert invalid.status_code == 422
    assert client.get("/api/patients").json()["total"] == before


def test_only_system_admin_can_delete_patient(client, png_file):
    # 1. Create users for testing role access.
    with main.connect() as db:
        pw = main.get_password_hash("password123")
        db.execute("INSERT INTO users (username, display_name, role, password_hash, is_active) VALUES (?, ?, ?, ?, 1)", ("pathologist", "Dr. Path", "Pathologist", pw))
        db.execute("INSERT INTO users (username, display_name, role, password_hash, is_active) VALUES (?, ?, ?, ?, 1)", ("etdadmin", "Protected Admin", "System Administrator", pw))
        db.commit()

    # 2. Create patient with child data: case, test, notes, documents
    created = client.post("/api/patients", json=patient_payload()).json()
    patient_id = created["id"]

    case_res = client.post("/api/cases", data={"case_id": "PAT-DEL-CASE-1", "description": "Biopsy to preserve"}, files={"image": ("biopsy.png", png_file, "image/png")})
    assert case_res.status_code == 201
    case_id = case_res.json()["id"]

    client.post(f"/api/patients/{patient_id}/cases", json={"case_id": case_id, "accession_number": "ACC-DEL-01", "priority": "Routine"})
    client.post(f"/api/patients/{patient_id}/notes", json={"note_type": "Clinical", "note_text": "Note to be deleted"})
    client.post(f"/api/patients/{patient_id}/documents", data={"document_type": "Referral", "description": "Ref doc"}, files={"file": ("doc.txt", b"Document content", "text/plain")})

    # Non-admin user cannot delete (e.g., Pathologist role)
    pathologist_token = main.create_access_token({"sub": "pathologist", "role": "Pathologist"})
    client.cookies.set("access_token", pathologist_token)
    forbidden = client.delete(f"/api/patients/{patient_id}")
    assert forbidden.status_code == 403

    # Protected System Administrator can delete.
    system_admin_token = main.create_access_token({"sub": "etdadmin", "role": "System Administrator"})
    client.cookies.set("access_token", system_admin_token)
    delete_res = client.delete(f"/api/patients/{patient_id}")
    assert delete_res.status_code == 200, delete_res.text
    assert delete_res.json()["message"] == "Patient profile deleted successfully"

    # Verify patient is gone
    get_res = client.get(f"/api/patients/{patient_id}")
    assert get_res.status_code == 404

    # Verify case still exists and patient_id in accessions was dissociated (NULL)
    with main.connect() as db:
        accession = db.execute("SELECT patient_id FROM accessions WHERE case_id=?", (case_id,)).fetchone()
        assert accession is not None
        assert accession["patient_id"] is None
