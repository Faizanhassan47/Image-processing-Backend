"""Regression coverage for the two DICOM viewer blockers.

These tests do not apply the SQL migration to a live database.
"""

from pathlib import Path

import pytest

from routes.cases_api import slide_dzi_url
from services.dicom_reader import stage_series
from services.image_validation import validate_image_file


def test_slide_dzi_url_for_raster_svs_and_dicom():
    assert slide_dzi_url(12, "raster") is None
    assert slide_dzi_url(12, "wsi") == "/api/slides/12/slide.dzi"
    assert slide_dzi_url(12, "dicom-wsi") == "/api/slides/12/slide.dzi"
    assert slide_dzi_url(12, None) is None
    assert slide_dzi_url(12, "unknown") is None


def test_additive_migration_permits_dicom_wsi_without_dropping_slides():
    sql = Path(__file__).resolve().parents[1].joinpath(
        "sql", "20261106_slides_dicom_wsi_file_type.sql"
    ).read_text(encoding="utf-8")
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "dicom-wsi" in sql
    assert "CHECK (file_type IN ('raster', 'wsi', 'dicom-wsi'))" in sql
    assert "CK_slides_file_type" in sql


def test_dicom_series_stage_path_is_openable_under_uploads(tmp_path: Path):
    openslide = pytest.importorskip("openslide")
    from openslide import deepzoom
    from services.dicom_wsi_sample import write_synthetic_wsi

    source = write_synthetic_wsi(tmp_path / "source")
    validation = validate_image_file(source, openslide)
    assert validation["file_type"] == "dicom-wsi"

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    stored_name = stage_series(validation, uploads)
    assert "/" in stored_name
    assert ".." not in Path(stored_name).parts

    anchor = (uploads / stored_name).resolve()
    assert uploads.resolve() in anchor.parents
    assert anchor.is_file()

    with openslide.OpenSlide(str(anchor)) as slide:
        dzi = deepzoom.DeepZoomGenerator(slide, tile_size=254, overlap=1)
        assert dzi.level_count >= 1
        tile = dzi.get_tile(dzi.level_count - 1, (0, 0))
        assert tile.size[0] > 0 and tile.size[1] > 0


def test_case_payload_urls_match_viewer_expectations():
    """Mirror serialize_slide URL rules used by the case API payload."""
    rows = [
        {"id": 1, "file_type": "raster"},
        {"id": 2, "file_type": "wsi"},
        {"id": 3, "file_type": "dicom-wsi"},
    ]
    payloads = []
    for row in rows:
        base = f"/api/slides/{row['id']}"
        payloads.append({
            "image_url": f"{base}/image" if row["file_type"] == "raster" else None,
            "thumbnail_url": f"{base}/thumbnail",
            "dzi_url": slide_dzi_url(row["id"], row["file_type"]),
        })

    assert payloads[0] == {
        "image_url": "/api/slides/1/image",
        "thumbnail_url": "/api/slides/1/thumbnail",
        "dzi_url": None,
    }
    assert payloads[1]["dzi_url"] == "/api/slides/2/slide.dzi"
    assert payloads[1]["image_url"] is None
    assert payloads[2]["dzi_url"] == "/api/slides/3/slide.dzi"
    assert payloads[2]["image_url"] is None
