"""FastAPI router for HoVer-Net AI analysis endpoints.

Exposes secure endpoints for triggering ROI analysis, fetching past analyses,
retrieving overlay and ROI assets, and downloading full-color PDF reports.
"""

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import uuid

import openslide
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ai_engine import (
    AI_GALLERY_DIR,
    AI_REPORT_DIR,
    AI_ROI_DIR,
    DISCLAIMER_TEXT,
    MODEL_NAME,
    MODEL_VERSION,
    analyze_nuclei,
    build_color_ai_pdf,
    calculate_nuclear_density,
    create_overlay_image,
    extract_roi,
    generate_nucleus_gallery,
    hovernet_manager,
    save_ai_analysis_record,
    save_roi_image,
    summarize_classification_scores,
)

logger = logging.getLogger("pathology.ai_api")


class AIAnalysisRequest(BaseModel):
    x: int | None = None
    y: int | None = None
    width: int = Field(default=256, gt=0, le=4096)
    height: int = Field(default=256, gt=0, le=4096)
    level: int = 0


def create_ai_router(connect_func, require_roles_func, get_current_user_func, get_slide_func, get_case_func, stored_path_func):
    router = APIRouter(tags=["AI Analysis"])

    # Permitted roles for viewing/running AI
    ai_run_roles = ["Admin", "Pathologist", "Technician", "AI Analyst"]
    ai_read_roles = ["Admin", "Pathologist", "Technician", "Viewer", "AI Analyst"]

    def _get_analysis_row(db, analysis_id: int):
        row = db.execute("SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"AI analysis #{analysis_id} not found")
        return row

    @router.post("/api/slides/{slide_id}/ai/analyses", status_code=201)
    def run_slide_ai_analysis(
        slide_id: int,
        req: AIAnalysisRequest,
        current_user: dict = Depends(require_roles_func(ai_run_roles)),
    ):
        slide_row = get_slide_func(slide_id)
        case_row = get_case_func(slide_row["case_id"])

        slide_path = stored_path_func(slide_row["filename"])
        if not slide_path.is_file():
            raise HTTPException(404, f"Slide file '{slide_row['filename']}' is missing on disk.")

        try:
            slide = openslide.OpenSlide(str(slide_path))
        except Exception as exc:
            logger.error("Failed to open slide %s with OpenSlide: %s", slide_id, exc)
            raise HTTPException(500, f"Cannot open slide with OpenSlide: {exc}")

        try:
            # Physical calibration (microns per pixel)
            def read_mpp(key):
                try:
                    val = float(slide.properties.get(key))
                    return val if math.isfinite(val) and val > 0 else None
                except Exception:
                    return None

            import math
            mpp_x = read_mpp(openslide.PROPERTY_NAME_MPP_X)
            mpp_y = read_mpp(openslide.PROPERTY_NAME_MPP_Y)

            # Check database for calibrated physical scale if not in slide properties
            with closing(connect_func()) as db:
                cal_row = db.execute(
                    "SELECT TOP 1 pixel_size_microns FROM image_versions WHERE slide_id = ? ORDER BY version DESC",
                    (slide_id,),
                ).fetchone()
                if cal_row and cal_row.get("pixel_size_microns"):
                    if mpp_x is None:
                        mpp_x = float(cal_row["pixel_size_microns"])
                    if mpp_y is None:
                        mpp_y = float(cal_row["pixel_size_microns"])

            # 1. Extract ROI
            level0_x, level0_y, w, h, roi_array, auto_detected, tissue_pct = extract_roi(
                slide=slide,
                x=req.x,
                y=req.y,
                width=req.width,
                height=req.height,
                level=req.level,
            )

            job_id_str = uuid.uuid4().hex[:12]
            roi_filename = f"roi_s{slide_id}_{job_id_str}.png"
            roi_path = save_roi_image(roi_array, roi_filename)

            # 2. Run HoVer-Net Inference
            try:
                inference_result = hovernet_manager.run_inference(roi_path)
            except Exception as inf_exc:
                logger.error("HoVer-Net inference error on slide %s: %s", slide_id, inf_exc)
                raise HTTPException(500, f"HoVer-Net inference failed: {inf_exc}")

            nuclei = inference_result.get("nuc", {})
            if not isinstance(nuclei, dict):
                nuclei = {}

            # 3. Analyze Nuclei & Morphology
            analysis_data = analyze_nuclei(nuclei, mpp_x, mpp_y)
            analysis_data["nuclear_density_per_mm2"] = calculate_nuclear_density(
                analysis_data["total_nuclei"], w, h, mpp_x, mpp_y
            )

            # 4. Classification Confidence Statistics
            score_stats = summarize_classification_scores(nuclei, review_threshold=0.70)

            # 5. Colored Overlay
            overlay_filename = f"overlay_s{slide_id}_{job_id_str}.png"
            overlay_path, valid_cnts, class_cnts = create_overlay_image(roi_path, nuclei, overlay_filename)

            # 6. Nucleus Gallery
            gallery_data = generate_nucleus_gallery(
                roi_path=roi_path,
                nuclei=nuclei,
                analysis_id=job_id_str,
                roi_x=level0_x,
                roi_y=level0_y,
                mpp_x=mpp_x,
                mpp_y=mpp_y,
            )

            # 7. Generate Full-Color PDF Report
            report_filename = f"ai_report_s{slide_id}_{job_id_str}.pdf"
            report_path = AI_REPORT_DIR / report_filename
            roi_meta = {
                "x": level0_x,
                "y": level0_y,
                "width": w,
                "height": h,
                "level": req.level,
                "auto_detected": auto_detected,
                "tissue_percentage": tissue_pct,
            }

            try:
                build_color_ai_pdf(
                    output_path=report_path,
                    slide_id=slide_id,
                    case_identifier=case_row["case_id"],
                    roi_path=roi_path,
                    overlay_path=overlay_path,
                    analysis=analysis_data,
                    score_stats=score_stats,
                    gallery=gallery_data,
                    roi_info=roi_meta,
                    model_name=f"{MODEL_NAME} ({MODEL_VERSION})",
                )
            except Exception as pdf_exc:
                logger.warning("PDF report generation encountered an issue: %s", pdf_exc)
                report_filename = None

            # 8. Persist to Database
            analysis_id = save_ai_analysis_record(
                connect_func=connect_func,
                slide_row=slide_row,
                case_row=case_row,
                user_username=current_user["username"],
                roi_info=roi_meta,
                analysis_data=analysis_data,
                score_stats=score_stats,
                nuclei_dict=nuclei,
                gallery_data=gallery_data,
                roi_filename=roi_filename,
                overlay_filename=overlay_filename,
                report_filename=report_filename,
            )

            # Update gallery URLs with real integer analysis ID
            for cells in gallery_data.get("groups", {}).values():
                for cell in cells:
                    cell["image_url"] = f"/api/ai/analyses/{analysis_id}/gallery/{cell['filename']}"

            # Format compact response
            return {
                "id": analysis_id,
                "slide_id": slide_id,
                "case_id": case_row["id"],
                "case_identifier": case_row["case_id"],
                "model_name": MODEL_NAME,
                "model_version": MODEL_VERSION,
                "status": "Completed",
                "disclaimer": DISCLAIMER_TEXT,
                "roi": {
                    "x": level0_x,
                    "y": level0_y,
                    "width": w,
                    "height": h,
                    "auto_detected": auto_detected,
                    "tissue_percentage": tissue_pct,
                    "image_url": f"/api/ai/analyses/{analysis_id}/roi.png",
                },
                "analysis": analysis_data,
                "confidence_statistics": score_stats,
                "overlay": {
                    "image_url": f"/api/ai/analyses/{analysis_id}/overlay.png",
                    "valid_contours": valid_cnts,
                },
                "nucleus_gallery": gallery_data,
                "report_url": f"/api/ai/analyses/{analysis_id}/report.pdf" if report_filename else None,
                "predictions": {
                    str(nid): {
                        "id": str(nid),
                        "type": nuc.get("type", 0),
                        "type_prob": round(float(nuc["type_prob"]), 4) if nuc.get("type_prob") is not None else None,
                        "centroid": [round(float(c), 2) for c in nuc.get("centroid", [])],
                        "contour": nuc.get("contour"),
                        "box": nuc.get("box"),
                    }
                    for nid, nuc in nuclei.items()
                    if isinstance(nuc, dict)
                },
            }

        finally:
            slide.close()

    @router.get("/api/slides/{slide_id}/ai/analyses")
    def list_slide_ai_analyses(
        slide_id: int,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        get_slide_func(slide_id)
        with closing(connect_func()) as db:
            rows = db.execute(
                """SELECT id, slide_id, case_id, requested_by, model_name, model_version, status,
                          roi_x, roi_y, roi_width, roi_height, roi_level,
                          total_nuclei, neoplastic_count, inflammatory_count, connective_count,
                          necrotic_count, non_neoplastic_count, no_label_count,
                          nuclear_density, avg_confidence,
                          summary_json, roi_image_filename, overlay_image_filename, report_pdf_filename,
                          disclaimer, created_at, completed_at
                   FROM ai_analyses
                   WHERE slide_id = ?
                   ORDER BY id DESC""",
                (slide_id,),
            ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            if d.get("summary_json"):
                try:
                    d["summary"] = json.loads(d.pop("summary_json"))
                except Exception:
                    d["summary"] = None
            d["roi_image_url"] = f"/api/ai/analyses/{d['id']}/roi.png" if d.get("roi_image_filename") else None
            d["overlay_image_url"] = f"/api/ai/analyses/{d['id']}/overlay.png" if d.get("overlay_image_filename") else None
            d["report_url"] = f"/api/ai/analyses/{d['id']}/report.pdf" if d.get("report_pdf_filename") else None
            out.append(d)
        return out

    @router.get("/api/ai/analyses/{analysis_id}")
    def get_ai_analysis_details(
        analysis_id: int,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)

        d = dict(row)
        summary = json.loads(d.pop("summary_json") or "{}")
        predictions = json.loads(d.pop("predictions_json") or "{}")
        gallery = json.loads(d.pop("gallery_json") or "{}")

        return {
            "id": d["id"],
            "slide_id": d["slide_id"],
            "case_id": d["case_id"],
            "requested_by": d["requested_by"],
            "model_name": d["model_name"],
            "model_version": d["model_version"],
            "status": d["status"],
            "disclaimer": d["disclaimer"],
            "created_at": d["created_at"],
            "completed_at": d["completed_at"],
            "roi": {
                "x": d["roi_x"],
                "y": d["roi_y"],
                "width": d["roi_width"],
                "height": d["roi_height"],
                "level": d["roi_level"],
                "image_url": f"/api/ai/analyses/{analysis_id}/roi.png",
            },
            "overlay": {
                "image_url": f"/api/ai/analyses/{analysis_id}/overlay.png",
            },
            "counts": {
                "total": d["total_nuclei"],
                "neoplastic": d["neoplastic_count"],
                "inflammatory": d["inflammatory_count"],
                "connective": d["connective_count"],
                "necrotic": d["necrotic_count"],
                "non_neoplastic": d["non_neoplastic_count"],
                "no_label": d["no_label_count"],
            },
            "nuclear_density": d["nuclear_density"],
            "avg_confidence": d["avg_confidence"],
            "summary": summary,
            "predictions": predictions,
            "nucleus_gallery": gallery,
            "report_url": f"/api/ai/analyses/{analysis_id}/report.pdf" if d.get("report_pdf_filename") else None,
        }

    @router.get("/api/ai/analyses/{analysis_id}/roi.png")
    def get_analysis_roi_image(
        analysis_id: int,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)

        filename = row.get("roi_image_filename")
        if not filename:
            raise HTTPException(404, "Analysis has no saved ROI image")

        path = AI_ROI_DIR / filename
        if not path.is_file():
            raise HTTPException(404, "ROI image file not found on server")

        return FileResponse(path, media_type="image/png")

    @router.get("/api/ai/analyses/{analysis_id}/overlay.png")
    def get_analysis_overlay_image(
        analysis_id: int,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)

        filename = row.get("overlay_image_filename")
        if not filename:
            raise HTTPException(404, "Analysis has no saved overlay image")

        path = AI_ROI_DIR / filename
        if not path.is_file():
            raise HTTPException(404, "Overlay image file not found on server")

        return FileResponse(path, media_type="image/png")

    @router.get("/api/ai/analyses/{analysis_id}/gallery/{filename}")
    def get_analysis_gallery_crop(
        analysis_id: int,
        filename: str,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        if not re.fullmatch(r"type_[0-5]_[0-9]{2}\.png", filename):
            raise HTTPException(400, "Invalid crop thumbnail identifier")

        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)

        gallery = json.loads(row.get("gallery_json") or "{}")
        gallery_id = gallery.get("gallery_id")
        if not gallery_id or not re.fullmatch(r"[0-9a-f]{32}", gallery_id):
            raise HTTPException(404, "No gallery exists for this analysis")

        path = (AI_GALLERY_DIR / gallery_id / filename).resolve()
        if not path.is_file() or AI_GALLERY_DIR not in path.parents:
            raise HTTPException(404, "Gallery crop image not found")

        return FileResponse(path, media_type="image/png")

    @router.get("/api/ai/analyses/{analysis_id}/report.pdf")
    def download_ai_report_pdf(
        analysis_id: int,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)
            case_row = get_case_func(row["case_id"])

        filename = row.get("report_pdf_filename")
        if not filename:
            raise HTTPException(404, "No PDF report is available for this analysis")

        path = AI_REPORT_DIR / filename
        if not path.is_file():
            raise HTTPException(404, "PDF report file not found on server")

        case_code = re.sub(r"[^\w\-]", "_", str(case_row["case_id"]))
        download_name = f"ai_report_case_{case_code}_analysis_{analysis_id}.pdf"
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={download_name}"},
        )

    return router
