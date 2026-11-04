import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from services.dip_service import apply_clahe, binary_preview, choose_processing_level, choose_region_level, contour_overlay, detect_canny, load_segmentation_result, render_visualization, segment_otsu, tissue_overlay, validate_canny_thresholds, validate_dip_roi


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_otsu_segments_dark_foreground_on_white_background():
    image = np.full((128, 128, 3), 245, dtype=np.uint8)
    image[32:96, 32:96] = (40, 30, 50)
    result = segment_otsu(image, gaussian_kernel=5)
    assert result["threshold"] > 0
    assert result["mask"][64, 64] == 255
    assert result["mask"][4, 4] == 0
    assert result["overlay"][4, 4, 3] == 0
    assert result["overlay"][64, 64, 3] > 0
    assert 10 < result["tissue_coverage_percent"] < 40


def test_blank_white_image_is_not_all_tissue():
    image = np.full((80, 80, 3), 255, dtype=np.uint8)
    result = segment_otsu(image, gaussian_kernel=1)
    assert result["tissue_coverage_percent"] == 0
    assert not result["mask"].any()
    assert not result["overlay"][..., 3].any()


def test_otsu_output_is_deterministic():
    image = np.full((96, 96, 3), 230, dtype=np.uint8)
    image[20:70, 10:60] = (25, 20, 35)
    first = segment_otsu(image, gaussian_kernel=3)
    second = segment_otsu(image, gaussian_kernel=3)
    assert first["threshold"] == second["threshold"]
    assert np.array_equal(first["mask"], second["mask"])


def test_negative_roi_is_rejected():
    with pytest.raises(ValueError):
        validate_dip_roi(-1, 0, 128, 128, 4000, 4000)


def test_oversized_roi_is_rejected():
    with pytest.raises(ValueError):
        validate_dip_roi(0, 0, 2049, 128, 8000, 8000)


def test_roi_outside_slide_is_rejected():
    with pytest.raises(ValueError):
        validate_dip_roi(3900, 0, 200, 128, 4000, 4000)


def test_result_ids_cannot_traverse_directories():
    with pytest.raises(FileNotFoundError):
        load_segmentation_result("../secret")


def test_segmentation_requires_authentication(client):
    response = client.post("/api/slides/1/dip/segmentation", json={
        "algorithm": "otsu",
        "roi": {"x": 0, "y": 0, "width": 128, "height": 128},
        "parameters": {"gaussian_kernel": 5},
    })
    assert response.status_code == 401
    canny = client.post("/api/slides/1/dip/segmentation", json={
        "algorithm": "canny",
        "mode": "whole_slide",
        "parameters": {"gaussian_kernel": 5, "low_threshold": 50, "high_threshold": 150},
    })
    assert canny.status_code == 401
    clahe = client.post("/api/slides/1/dip/segmentation", json={
        "algorithm": "clahe",
        "mode": "roi",
        "roi": {"x": 0, "y": 0, "width": 128, "height": 128},
        "parameters": {"clip_limit": 2.0, "tile_grid_size": 8},
    })
    assert clahe.status_code == 401


def test_disconnected_fragments_are_retained():
    image = np.full((160, 160, 3), 250, dtype=np.uint8)
    image[10:40, 10:40] = (30, 20, 25)
    image[110:145, 100:140] = (35, 25, 30)
    result = segment_otsu(image, gaussian_kernel=3)
    assert result["mask"][20, 20] == 255
    assert result["mask"][125, 120] == 255
    assert result["mask"][80, 80] == 0
    assert result["tissue_coverage_percent"] == round(float(result["mask"].astype(bool).mean() * 100), 2)


def test_gaussian_kernel_changes_the_mask():
    image = np.full((128, 128, 3), 240, dtype=np.uint8)
    image[60:68, 60:68] = (20, 15, 25)
    light = segment_otsu(image, gaussian_kernel=3)
    strong = segment_otsu(image, gaussian_kernel=9)
    assert light["parameters"]["gaussian_kernel"] == 3
    assert strong["parameters"]["gaussian_kernel"] == 9
    assert not np.array_equal(light["mask"], strong["mask"])


def test_views_share_one_mask_and_blank_masks_have_no_contours():
    image = np.full((160, 160, 3), 250, dtype=np.uint8)
    image[12:48, 12:48] = (28, 18, 24)
    image[100:150, 90:145] = (34, 22, 28)
    result = segment_otsu(image, gaussian_kernel=5)
    mask = result["mask"]
    overlay = tissue_overlay(mask)
    preview = binary_preview(mask)
    contours, count = contour_overlay(mask)
    assert preview[30, 30, 0] == 255
    assert preview[4, 4, 0] == 0
    assert overlay[30, 30, 3] > 0
    assert overlay[4, 4, 3] == 0
    assert contours[4, 4, 3] == 0
    assert count >= 2
    assert np.array_equal(render_visualization(mask, "overlay")[..., 3] > 0, mask == 255)
    assert result["tissue_coverage_percent"] == round(float((mask == 255).mean() * 100), 2)
    blank = segment_otsu(np.full((80, 80, 3), 255, dtype=np.uint8), gaussian_kernel=1)
    empty, empty_count = contour_overlay(blank["mask"])
    assert empty_count == 0
    assert not empty[..., 3].any()


def test_processing_level_uses_the_finest_level_inside_the_pixel_budget():
    levels = [(40000, 30000), (4000, 3000), (1000, 750)]
    assert choose_processing_level(levels, max_pixels=2_000_000) == 2
    assert choose_processing_level([(1000, 800)], max_pixels=2_000_000) == 0
    with pytest.raises(ValueError, match="processing limit"):
        choose_processing_level([(5000, 5000)], max_pixels=2_000_000)


def test_canny_finds_edges_and_reports_density():
    image = np.full((80, 80, 3), 240, dtype=np.uint8)
    image[20:60, 20:60] = 20
    result = detect_canny(image, gaussian_kernel=5, low_threshold=50, high_threshold=150)
    assert result["mask"][40, 20] == 255 or result["edge_pixel_count"] > 0
    assert result["edge_density_percent"] == round(float((result["mask"] == 255).mean() * 100), 2)
    overlay = render_visualization(result["mask"], "overlay", "canny")
    preview = render_visualization(result["mask"], "mask", "canny")
    assert np.array_equal(overlay[..., 3] > 0, result["mask"] == 255)
    assert np.array_equal(preview[..., 0] == 255, result["mask"] == 255)
    assert result["parameters"]["gaussian_kernel"] == 5
    blank = detect_canny(np.full((64, 64, 3), 200, dtype=np.uint8), 5, 50, 150)
    assert blank["edge_pixel_count"] == 0
    with pytest.raises(ValueError):
        validate_canny_thresholds(150, 50)
    with pytest.raises(ValueError):
        detect_canny(image, gaussian_kernel=4, low_threshold=50, high_threshold=150)


def test_clahe_keeps_dimensions_and_does_not_change_the_source():
    image = np.zeros((64, 80, 3), dtype=np.uint8)
    image[:, :40] = (180, 140, 160)
    image[:, 40:] = (40, 30, 50)
    original = image.copy()
    result = apply_clahe(image, clip_limit=2.0, tile_grid_size=8)
    assert result["image"].shape == image.shape
    assert result["image"].dtype == np.uint8
    assert result["comparison"].shape == image.shape
    assert np.array_equal(image, original)
    assert np.array_equal(result["comparison"][:, :40], original[:, :40])
    assert result["original_luminance_mean"] != result["enhanced_luminance_mean"] or result["enhanced_luminance_std"] >= 0
    flat = apply_clahe(np.full((32, 32, 3), 128, dtype=np.uint8), 2.0, 8)
    assert flat["image"].shape == (32, 32, 3)
    with pytest.raises(ValueError):
        apply_clahe(image, 0.5, 8)
    with pytest.raises(ValueError):
        apply_clahe(image, 2.0, 3)


def test_clahe_tile_level_tracks_requested_resolution():
    downsamples = [1, 4, 16, 64]
    assert choose_region_level(downsamples, 512, 512) == 0
    assert choose_region_level(downsamples, 4096, 4096) == 2
    with pytest.raises(ValueError):
        choose_region_level(downsamples, 9000, 100)


def test_overlay_requires_authentication(client):
    result_id = "ab" * 16
    for view in ("overlay", "mask", "contours"):
        response = client.get(f"/api/dip/results/{result_id}/{view}")
        assert response.status_code == 401
