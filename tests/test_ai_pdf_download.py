"""Regression tests for AI PDF report download authentication and functionality."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import jwt
from fastapi.testclient import TestClient

from main import app
from core.config import SECRET_KEY, ALGORITHM
from core.database import connect
from services.ai_engine import AI_REPORT_DIR


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def existing_analysis_id():
    """Find an existing analysis in the database, or create a mock analysis record for testing."""
    with connect() as db:
        row = db.execute("SELECT id FROM ai_analyses ORDER BY id DESC").fetchone()
        if row:
            return row["id"]
        # If no analysis exists, look for a case and slide
        case_row = db.execute("SELECT id FROM cases ORDER BY id DESC").fetchone()
        if not case_row:
            db.execute("INSERT INTO cases (case_id, description, status) VALUES ('test_case', 'Test Case', 'New')")
            db.commit()
            case_row = db.execute("SELECT id FROM cases WHERE case_id = 'test_case'").fetchone()

        slide_row = db.execute("SELECT id FROM slides ORDER BY id DESC").fetchone()
        if not slide_row:
            db.execute("INSERT INTO slides (case_id, label, filename, status) VALUES (?, 'Test Slide', 'test.svs', 'ready')", (case_row["id"],))
            db.commit()
            slide_row = db.execute("SELECT id FROM slides WHERE case_id = ?", (case_row["id"],)).fetchone()

        db.execute(
            """INSERT INTO ai_analyses (
                slide_id, case_id, roi_x, roi_y, roi_width, roi_height, roi_level,
                total_nuclei, nuclear_density, summary_json, gallery_json,
                model_name, model_version, requested_by
            ) VALUES (?, ?, 0, 0, 256, 256, 0, 10, 100.0, '{}', '{}', 'HoVer-Net', 'PanNuke', 'admin')""",
            (slide_row["id"], case_row["id"]),
        )
        db.commit()
        new_row = db.execute("SELECT id FROM ai_analyses ORDER BY id DESC").fetchone()
        return new_row["id"]


def _make_token(username: str, role: str, expired: bool = False, wrong_secret: bool = False) -> str:
    delta = timedelta(hours=-1) if expired else timedelta(hours=1)
    exp = datetime.now(timezone.utc) + delta
    secret = "wrong-secret-key-123456789012345" if wrong_secret else SECRET_KEY
    return jwt.encode({"sub": username, "role": role, "exp": exp}, secret, algorithm=ALGORITHM)


def test_download_ai_report_pdf_unauthenticated(client, existing_analysis_id):
    """Unauthenticated requests (no cookies or auth headers) must return 401."""
    response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
    assert response.status_code == 401
    assert "detail" in response.json()
    assert response.json()["detail"] == "Not authenticated"


def test_download_ai_report_pdf_expired_token_cookie(client, existing_analysis_id):
    """Requests with an expired session token cookie must return 401 with descriptive detail."""
    expired_token = _make_token("adyan", "System Administrator", expired=True)
    client.cookies.set("access_token", expired_token)
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired authentication token"
    finally:
        client.cookies.clear()


def test_download_ai_report_pdf_expired_token_header(client, existing_analysis_id):
    """Requests with an expired Bearer token in Authorization header must return 401."""
    expired_token = _make_token("adyan", "System Administrator", expired=True)
    response = client.get(
        f"/api/ai/analyses/{existing_analysis_id}/report.pdf",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired authentication token"


def test_download_ai_report_pdf_invalid_signature(client, existing_analysis_id):
    """Requests with an invalid token signature must return 401."""
    bad_token = _make_token("adyan", "System Administrator", wrong_secret=True)
    client.cookies.set("access_token", bad_token)
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid token"
    finally:
        client.cookies.clear()


def test_download_ai_report_pdf_unauthorized_role(client, existing_analysis_id):
    """Users with unpermitted roles must return 403 Forbidden."""
    from core.security import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 999,
        "username": "guest_user",
        "display_name": "Guest User",
        "role": "Guest",
        "is_active": 1,
    }
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 403
        assert response.json()["detail"] == "Not enough permissions"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_download_ai_report_pdf_authenticated_admin_cookie_success(client, existing_analysis_id):
    """Authenticated administrator via access_token cookie receives valid PDF binary."""
    valid_token = _make_token("adyan", "System Administrator")
    client.cookies.set("access_token", valid_token)
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert "Content-Disposition" in response.headers or "content-disposition" in response.headers
        disposition = response.headers.get("content-disposition", "")
        assert f"analysis_{existing_analysis_id}.pdf" in disposition
        # Verify valid PDF magic header (%PDF-)
        assert response.content[:5] == b"%PDF-"
        assert len(response.content) > 100
    finally:
        client.cookies.clear()


def test_download_ai_report_pdf_authenticated_bearer_header_success(client, existing_analysis_id):
    """Authenticated request via Authorization: Bearer header receives valid PDF binary."""
    valid_token = _make_token("adyan", "System Administrator")
    response = client.get(
        f"/api/ai/analyses/{existing_analysis_id}/report.pdf",
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"


def test_download_ai_report_pdf_pathologist_success(client, existing_analysis_id):
    """Users with Pathologist role are authorized to download AI PDF."""
    from core.security import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 100,
        "username": "pathologist_jane",
        "display_name": "Dr. Jane",
        "role": "Pathologist",
        "is_active": 1,
    }
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content[:5] == b"%PDF-"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_download_ai_report_pdf_lab_manager_success(client, existing_analysis_id):
    """Users with Laboratory Manager role are authorized to download AI PDF."""
    from core.security import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 101,
        "username": "lab_mgr",
        "display_name": "Lab Manager",
        "role": "Laboratory Manager",
        "is_active": 1,
    }
    try:
        response = client.get(f"/api/ai/analyses/{existing_analysis_id}/report.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content[:5] == b"%PDF-"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_download_ai_report_pdf_nonexistent_analysis(client):
    """Authenticated request for non-existent analysis ID returns 404."""
    valid_token = _make_token("adyan", "System Administrator")
    response = client.get(
        "/api/ai/analyses/999999/report.pdf",
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
