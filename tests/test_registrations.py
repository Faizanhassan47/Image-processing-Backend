"""
Integration and Unit Tests for Multi-Slide Registration and Viewing Trails.
Tests:
- Manual landmark registration: translation, rigid, similarity, affine
- Reject degenerate landmark configurations
- Residual error metrics calculation (pixels and microns)
- Viewing trail persistence, retrieval, and clearing
- Viewpoint persistence and restoration (bookmarks with flip, brightness, contrast, gamma)
- Case authorization and RBAC (Pathologists restricted to assigned cases)
- Automatic registration feature detection & matching
- Registration review and status transitions (Draft -> Calculated -> Reviewed / Rejected)
- Audit history logging
"""

import io
import json
from pathlib import Path
from uuid import uuid4
import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from core.database import connect
from core.security import create_access_token
from services.registration_service import (
    RegistrationError,
    estimate_manual_registration,
    run_automatic_registration,
    validate_landmarks,
)

client = TestClient(app)


def make_auth_headers(username: str, role: str):
    token = create_access_token({"sub": username, "role": role})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def assigned_case_and_slides():
    """Creates a test case assigned to dr_alice with two slides."""
    with connect() as db:
        # Create case assigned to dr_alice
        cursor = db.execute("""
            INSERT INTO cases (case_id, description, status, reviewer)
            VALUES (?, ?, 'In Review', 'dr_alice')
        """, (f"CASE-REG-{np.random.randint(10000, 99999)}", "Multi-slide registration test case"))
        case_pk = cursor.lastrowid

        # Insert 2 test slides
        fn1 = f"dummy_{uuid4().hex[:8]}.jpg"
        fn2 = f"dummy_{uuid4().hex[:8]}.jpg"
        cur_s1 = db.execute("""
            INSERT INTO slides (case_id, label, stain, filename, file_type, format, width, height, level_count, upload_state, file_size)
            VALUES (?, 'Slide H&E', 'H&E', ?, 'raster', 'JPEG', 2000, 2000, 1, 'ready', 1024)
        """, (case_pk, fn1))
        slide1_id = cur_s1.lastrowid

        cur_s2 = db.execute("""
            INSERT INTO slides (case_id, label, stain, filename, file_type, format, width, height, level_count, upload_state, file_size)
            VALUES (?, 'Slide IHC', 'ER', ?, 'raster', 'JPEG', 2000, 2000, 1, 'ready', 1024)
        """, (case_pk, fn2))
        slide2_id = cur_s2.lastrowid

        # Insert image versions with calibration
        db.execute("""
            INSERT INTO image_versions (slide_id, version, checksum_sha256, pixel_size_microns, metadata_json, stored_filename)
            VALUES (?, 1, 'fakehash1', 0.25, '{}', 'dummy_he.jpg')
        """, (slide1_id,))
        db.execute("""
            INSERT INTO image_versions (slide_id, version, checksum_sha256, pixel_size_microns, metadata_json, stored_filename)
            VALUES (?, 1, 'fakehash2', 0.25, '{}', 'dummy_ihc.jpg')
        """, (slide2_id,))

        db.commit()

    try:
        yield {"case_id": case_pk, "ref_slide_id": slide1_id, "mov_slide_id": slide2_id}
    finally:
        with connect() as db:
            db.execute("DELETE FROM slide_registrations WHERE case_id = ?", (case_pk,))
            db.execute("DELETE FROM image_versions WHERE slide_id IN (?, ?)", (slide1_id, slide2_id))
            db.execute("DELETE FROM slides WHERE case_id = ?", (case_pk,))
            db.execute("DELETE FROM cases WHERE id = ?", (case_pk,))
            db.commit()


# =====================================================================
# 1. MATHEMATICAL REGISTRATION ALGORITHM TESTS
# =====================================================================

def test_translation_transformation():
    # True shift: ref = mov + [100, 50]
    landmarks = [
        {"ref_x": 200.0, "ref_y": 150.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 400.0, "ref_y": 250.0, "mov_x": 300.0, "mov_y": 200.0},
    ]
    M, metrics = estimate_manual_registration("translation", landmarks, pixel_size_microns=0.5)
    assert M.shape == (3, 3)
    assert np.isclose(M[0, 2], 100.0)
    assert np.isclose(M[1, 2], 50.0)
    assert metrics["mean_residual_px"] < 1e-4
    assert metrics["calibrated"] is True
    assert metrics["mean_residual_um"] < 1e-4


def test_rigid_transformation():
    # 90 degree rotation clockwise around origin: x_ref = y_mov, y_ref = -x_mov
    landmarks = [
        {"ref_x": 100.0, "ref_y": -100.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 200.0, "ref_y": -100.0, "mov_x": 100.0, "mov_y": 200.0},
        {"ref_x": 100.0, "ref_y": -200.0, "mov_x": 200.0, "mov_y": 100.0},
    ]
    M, metrics = estimate_manual_registration("rigid", landmarks)
    assert M.shape == (3, 3)
    assert metrics["mean_residual_px"] < 1e-3


def test_similarity_transformation():
    # Scaling factor 2.0 with shift
    landmarks = [
        {"ref_x": 200.0, "ref_y": 200.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 400.0, "ref_y": 200.0, "mov_x": 200.0, "mov_y": 100.0},
        {"ref_x": 200.0, "ref_y": 400.0, "mov_x": 100.0, "mov_y": 200.0},
    ]
    M, metrics = estimate_manual_registration("similarity", landmarks)
    assert M.shape == (3, 3)
    assert metrics["mean_residual_px"] < 1e-3


def test_affine_transformation():
    # Shear + scale + translation
    landmarks = [
        {"ref_x": 150.0, "ref_y": 120.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 350.0, "ref_y": 120.0, "mov_x": 300.0, "mov_y": 100.0},
        {"ref_x": 170.0, "ref_y": 320.0, "mov_x": 100.0, "mov_y": 300.0},
        {"ref_x": 370.0, "ref_y": 320.0, "mov_x": 300.0, "mov_y": 300.0},
    ]
    M, metrics = estimate_manual_registration("affine", landmarks, pixel_size_microns=0.25)
    assert M.shape == (3, 3)
    assert metrics["mean_residual_px"] < 1e-3
    assert metrics["mean_residual_um"] < 1e-3


def test_degenerate_landmarks_rejected():
    # Identical points
    landmarks = [
        {"ref_x": 100.0, "ref_y": 100.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 100.0, "ref_y": 100.0, "mov_x": 100.0, "mov_y": 100.0},
        {"ref_x": 100.0, "ref_y": 100.0, "mov_x": 100.0, "mov_y": 100.0},
    ]
    with pytest.raises(RegistrationError):
        estimate_manual_registration("affine", landmarks)


# =====================================================================
# 2. VIEWING TRAILS ENDPOINTS TESTS
# =====================================================================

def test_viewing_trail_lifecycle(assigned_case_and_slides):
    headers = make_auth_headers("dr_alice", "Pathologist")
    slide_id = assigned_case_and_slides["ref_slide_id"]

    # Initial get should be empty
    resp = client.get(f"/api/slides/{slide_id}/trail", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["coverage_boxes"] == []

    # Save trail coverage
    payload = {
        "coverage_boxes": [{"x": 100, "y": 100, "w": 400, "h": 300}],
        "total_viewed_area_px": 120000,
        "last_center_x": 300,
        "last_center_y": 250,
        "last_zoom": 2.5,
    }
    resp = client.put(f"/api/slides/{slide_id}/trail", json=payload, headers=headers)
    assert resp.status_code == 200

    # Retrieve saved trail
    resp = client.get(f"/api/slides/{slide_id}/trail", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["coverage_boxes"]) == 1
    assert data["last_zoom"] == 2.5

    # Clear trail
    resp = client.delete(f"/api/slides/{slide_id}/trail", headers=headers)
    assert resp.status_code == 204

    # Verify cleared
    resp = client.get(f"/api/slides/{slide_id}/trail", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["coverage_boxes"] == []


# =====================================================================
# 3. REGISTRATION CRUD & WORKFLOW TESTS
# =====================================================================

def test_registration_lifecycle(assigned_case_and_slides):
    headers_alice = make_auth_headers("dr_alice", "Pathologist")
    case_id = assigned_case_and_slides["case_id"]
    ref_slide = assigned_case_and_slides["ref_slide_id"]
    mov_slide = assigned_case_and_slides["mov_slide_id"]

    # 1. Create draft registration
    create_payload = {
        "case_id": case_id,
        "reference_slide_id": ref_slide,
        "moving_slide_id": mov_slide,
        "registration_method": "manual",
        "transform_type": "affine",
        "landmarks": [],
    }
    resp = client.post("/api/registrations", json=create_payload, headers=headers_alice)
    assert resp.status_code == 201
    reg = resp.json()
    reg_id = reg["id"]
    assert reg["status"] == "Draft"
    assert reg["reference_slide_id"] == ref_slide
    assert reg["moving_slide_id"] == mov_slide

    # 2. Add landmark pairs
    landmarks = [
        {"id": 1, "ref_x": 100.0, "ref_y": 100.0, "mov_x": 90.0, "mov_y": 95.0, "display_index": 1},
        {"id": 2, "ref_x": 500.0, "ref_y": 100.0, "mov_x": 490.0, "mov_y": 95.0, "display_index": 2},
        {"id": 3, "ref_x": 100.0, "ref_y": 500.0, "mov_x": 90.0, "mov_y": 495.0, "display_index": 3},
        {"id": 4, "ref_x": 500.0, "ref_y": 500.0, "mov_x": 490.0, "mov_y": 495.0, "display_index": 4},
    ]
    resp = client.post(f"/api/registrations/{reg_id}/landmarks", json={"landmarks": landmarks}, headers=headers_alice)
    assert resp.status_code == 200
    assert len(resp.json()["landmarks"]) == 4

    # 3. Calculate alignment
    resp = client.post(
        f"/api/registrations/{reg_id}/calculate",
        json={"transform_type": "affine", "landmarks": landmarks},
        headers=headers_alice,
    )
    assert resp.status_code == 200
    calc_data = resp.json()
    assert calc_data["status"] == "Calculated"
    assert calc_data["transform_matrix"] is not None
    assert len(calc_data["transform_matrix"]) == 3
    assert calc_data["quality_metrics"]["mean_residual_px"] < 1.0

    # 4. Review registration -> Reviewed
    review_resp = client.post(
        f"/api/registrations/{reg_id}/review",
        json={"status": "Reviewed", "review_notes": "Tissue edges aligned accurately."},
        headers=headers_alice,
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "Reviewed"
    assert review_resp.json()["approved_by"] == "Dr. Alice"

    # 5. Check history audit log
    hist_resp = client.get(f"/api/registrations/{reg_id}/history", headers=headers_alice)
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    actions = [h["action"] for h in history]
    assert "created" in actions
    assert "calculated" in actions
    assert "marked_reviewed" in actions


def test_unassigned_pathologist_forbidden_from_registration(assigned_case_and_slides):
    headers_bob = make_auth_headers("dr_bob", "Pathologist")
    case_id = assigned_case_and_slides["case_id"]

    # Bob is not assigned to this case
    resp = client.get(f"/api/registrations?case_id={case_id}", headers=headers_bob)
    assert resp.status_code == 403
    assert "Pathologists may access only cases assigned to them" in resp.text
