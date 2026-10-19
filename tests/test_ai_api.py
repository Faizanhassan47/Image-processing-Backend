"""Test AI analysis API endpoints."""

import json
from ai_engine import save_ai_analysis_record, build_color_ai_pdf, AI_REPORT_DIR
import main


def _setup_case_and_slide(client):
    with main.connect() as db:
        c_cur = db.execute("INSERT INTO cases (case_id, description, status) VALUES ('CASE-AI-TEST', 'AI Test Case', 'New')")
        case_id = c_cur.lastrowid
        s_cur = db.execute(
            """INSERT INTO slides (case_id, label, filename, file_type, format, file_size, width, height, upload_state)
               VALUES (?, 'Slide AI Test', 'mock_ai_slide.png', 'raster', 'PNG', 1024, 512, 512, 'ready')""",
            (case_id,)
        )
        slide_id = s_cur.lastrowid
        db.commit()
    return case_id, slide_id


def test_ai_analysis_unauthenticated(client):
    anonymous = type(client)(main.app)
    response = anonymous.post("/api/slides/1/ai/analyses", json={"width": 256, "height": 256})
    assert response.status_code == 401


def test_ai_analysis_slide_not_found(client):
    response = client.post("/api/slides/999999/ai/analyses", json={"width": 256, "height": 256})
    assert response.status_code == 404


def test_ai_analyses_list_and_details(client):
    case_id, slide_id = _setup_case_and_slide(client)

    # Empty list initially for this slide
    res = client.get(f"/api/slides/{slide_id}/ai/analyses")
    assert res.status_code == 200
    assert res.json() == []

    # Insert a mock analysis record
    slide_row = main.get_slide(slide_id)
    case_row = main.get_case(case_id)
    roi_meta = {"x": 100, "y": 100, "width": 256, "height": 256, "level": 0}
    analysis_data = {
        "total_nuclei": 25,
        "type_counts": {1: 20, 2: 3, 3: 2},
        "type_summary": {
            "Neoplastic": {"count": 20, "percentage": 80.0},
            "Inflammatory": {"count": 3, "percentage": 12.0},
            "Connective": {"count": 2, "percentage": 8.0},
        },
        "average_type_confidence": 0.8842,
        "nuclear_density_per_mm2": 450.5,
        "morphology": {"mean_area_um2": 45.2, "mean_circularity": 0.82},
    }
    score_stats = {
        "scored_nuclei": 25,
        "overall": {"mean": 0.8842, "median": 0.91, "minimum": 0.72, "maximum": 0.98},
        "low_score_count": 0,
        "by_type": {},
    }
    nuclei_dict = {
        "1": {"type": 1, "type_prob": 0.95, "centroid": [128, 128], "contour": [[120, 120], [136, 120], [136, 136], [120, 136]]}
    }
    gallery_data = {"gallery_id": "0123456789abcdef0123456789abcdef", "total_gallery_images": 1, "groups": {"Neoplastic": []}}

    aid = save_ai_analysis_record(
        main.connect,
        slide_row,
        case_row,
        "admin",
        roi_meta,
        analysis_data,
        score_stats,
        nuclei_dict,
        gallery_data,
        "roi_test.png",
        "overlay_test.png",
        "report_test.pdf",
    )
    assert aid > 0

    # Test list endpoint
    list_res = client.get(f"/api/slides/{slide_id}/ai/analyses")
    assert list_res.status_code == 200
    analyses = list_res.json()
    assert len(analyses) == 1
    assert analyses[0]["id"] == aid
    assert analyses[0]["total_nuclei"] == 25
    assert analyses[0]["disclaimer"] == "Research Use Only — Not for Clinical Diagnosis"

    # Test detail endpoint
    det_res = client.get(f"/api/ai/analyses/{aid}")
    assert det_res.status_code == 200
    detail = det_res.json()
    assert detail["id"] == aid
    assert detail["counts"]["total"] == 25
    assert detail["counts"]["neoplastic"] == 20
    assert "predictions" in detail
    assert "1" in detail["predictions"]
    assert detail["predictions"]["1"]["type_prob"] == 0.95


def test_ai_analysis_get_nonexistent(client):
    res = client.get("/api/ai/analyses/9999999")
    assert res.status_code == 404
