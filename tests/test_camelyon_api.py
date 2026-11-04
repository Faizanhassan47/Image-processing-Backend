"""CAMELYON FastAPI routing and security regression tests."""

import pytest
from fastapi.testclient import TestClient

from main import app
from services.camelyon_engine import (
    FEATURE_CODE,
    MODEL_NAME,
    PATCH_SIZE,
    MAX_ROI_SIZE,
    calculate_camelyon_patches,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_camelyon_feature_code():
    assert FEATURE_CODE == "CAMELYON_TUMOR_DETECTION"
    assert MODEL_NAME == "MONAI pathology_tumor_detection"


def test_camelyon_patch_count():
    assert calculate_camelyon_patches(2240, 2240) == 100


def test_camelyon_small_roi_rejected():
    with pytest.raises(ValueError):
        calculate_camelyon_patches(100, 100)


def test_camelyon_oversized_roi_rejected():
    with pytest.raises(ValueError):
        calculate_camelyon_patches(MAX_ROI_SIZE + 1, PATCH_SIZE)


def test_camelyon_analysis_endpoint_registered():
    paths = app.openapi()["paths"]

    endpoint = "/api/slides/{slide_id}/ai/analyses"

    assert endpoint in paths
    assert "post" in paths[endpoint]


def test_camelyon_job_endpoint_registered():
    paths = app.openapi()["paths"]

    assert "/api/ai/jobs/{job_id}" in paths
    assert "get" in paths["/api/ai/jobs/{job_id}"]

    assert "/api/ai/jobs/{job_id}/cancel" in paths
    assert "post" in paths["/api/ai/jobs/{job_id}/cancel"]

    assert "/api/ai/analyses/{analysis_id}/overlay.png" in paths
    assert "get" in paths["/api/ai/analyses/{analysis_id}/overlay.png"]


def test_camelyon_analysis_requires_authentication(client):
    response = client.post(
        "/api/slides/6111/ai/analyses",
        json={
            "feature_code": FEATURE_CODE,
            "x": 0,
            "y": 0,
            "width": 224,
            "height": 224,
            "level": 0,
        },
    )

    assert response.status_code == 401


def test_camelyon_whole_slide_request_requires_authentication(client):
    response = client.post(
        "/api/slides/6111/ai/analyses",
        json={
            "feature_code": FEATURE_CODE,
            "auto_tissue": True,
            "run_in_background": True,
            "level": 0,
        },
    )

    assert response.status_code == 401


def test_camelyon_job_requires_authentication(client):
    response = client.get("/api/ai/jobs/job-test-camelyon")

    assert response.status_code == 401


def test_camelyon_overlay_requires_authentication(client):
    response = client.get("/api/ai/analyses/999999/overlay.png")

    assert response.status_code == 401
