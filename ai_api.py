"""FastAPI router for HoVer-Net AI analysis endpoints.

Exposes secure endpoints for triggering ROI analysis, fetching past analyses,
retrieving overlay and ROI assets, and downloading full-color PDF reports.
"""

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

import openslide
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ai_engine import (
    AI_GALLERY_DIR,
    AI_REPORT_DIR,
    AI_ROI_DIR,
    DISCLAIMER_TEXT,
    MODEL_NAME,
    MODEL_VERSION,
    AIJob,
    ai_job_manager,
    analyze_nuclei,
    build_color_ai_pdf,
    calculate_hovernet_patches,
    calculate_nuclear_density,
    create_overlay_image,
    extract_roi,
    format_nucleus_predictions,
    generate_nucleus_gallery,
    hovernet_manager,
    save_ai_analysis_record,
    save_roi_image,
    summarize_classification_scores,
    validate_roi_dimensions,
)

logger = logging.getLogger("pathology.ai_api")


class AIAnalysisRequest(BaseModel):
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    roi_x: int | None = None
    roi_y: int | None = None
    roi_width: int | None = None
    roi_height: int | None = None
    level: int = 0
    auto_tissue: bool = False
    run_in_background: bool | None = None


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

    def _execute_pipeline(
        slide_row: dict,
        case_row: dict,
        current_username: str,
        eff_x: int | None,
        eff_y: int | None,
        eff_w: int,
        eff_h: int,
        level: int,
        auto_tissue: bool,
        slide: openslide.OpenSlide,
        job: AIJob | None = None,
    ) -> dict:
        total_start_time = time.time()
        slide_id = slide_row["id"]
        if job:
            job.status = "processing"
            job.started_at = datetime.now(timezone.utc).isoformat()
            job.start_time = total_start_time

        # 1. Physical calibration (microns per pixel)
        def read_mpp(key):
            try:
                val = float(slide.properties.get(key))
                return val if math.isfinite(val) and val > 0 else None
            except Exception:
                return None

        mpp_x = read_mpp(openslide.PROPERTY_NAME_MPP_X)
        mpp_y = read_mpp(openslide.PROPERTY_NAME_MPP_Y)

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

        # 2. Extract ROI
        roi_start = time.time()
        level0_x, level0_y, w, h, roi_array, auto_detected, tissue_pct = extract_roi(
            slide=slide,
            x=eff_x,
            y=eff_y,
            width=eff_w,
            height=eff_h,
            level=level,
        )
        roi_elapsed = time.time() - roi_start

        job_id_str = job.job_id if job else uuid.uuid4().hex[:12]
        roi_filename = f"roi_s{slide_id}_{job_id_str}.png"
        roi_path = save_roi_image(roi_array, roi_filename)

        # 3. Compute patch count
        patch_info = calculate_hovernet_patches(w, h)
        total_patches = patch_info["total_patches"]
        if job:
            job.total_patches = total_patches
            job.estimated_seconds = patch_info["estimated_seconds"]

        # 4. Progress callback & cancellation event
        def on_progress(p: dict):
            if job:
                job.processed_patches = p.get("processed_patches", job.processed_patches)
                job.total_patches = p.get("total_patches", job.total_patches)
                job.percent = p.get("percent", job.percent)
                job.elapsed_str = p.get("elapsed_str", job.elapsed_str)
                job.remaining_str = p.get("remaining_str", job.remaining_str)
                if job.start_time:
                    job.elapsed_seconds = time.time() - job.start_time

        cancel_ev = job.cancel_event if job else None

        # 5. Run HoVer-Net inference
        infer_start = time.time()
        logger.info("HoVer-Net inference starting on %s (patches: %d)...", roi_path.name, total_patches)
        try:
            inference_result = hovernet_manager.run_inference(
                image_path=roi_path,
                expected_patches=total_patches,
                progress_callback=on_progress if job else None,
                cancel_event=cancel_ev,
            )
        except InterruptedError:
            if job:
                job.status = "cancelled"
                job.error = "Analysis cancelled by user."
                job.completed_at = datetime.now(timezone.utc).isoformat()
            raise
        except TimeoutError as exc:
            if job:
                job.status = "timeout"
                job.error = str(exc)
                job.completed_at = datetime.now(timezone.utc).isoformat()
            raise
        except Exception as inf_exc:
            logger.error("HoVer-Net inference error on slide %s: %s", slide_id, inf_exc)
            if job:
                job.status = "failed"
                job.error = str(inf_exc)
                job.completed_at = datetime.now(timezone.utc).isoformat()
            raise

        infer_elapsed = time.time() - infer_start
        logger.info("HoVer-Net inference completed in %.2fs", infer_elapsed)

        nuclei = inference_result.get("nuc", {})
        if not isinstance(nuclei, dict):
            nuclei = {}

        # 6. Analyze Nuclei & Morphology
        analysis_data = analyze_nuclei(nuclei, mpp_x, mpp_y)
        analysis_data["nuclear_density_per_mm2"] = calculate_nuclear_density(
            analysis_data["total_nuclei"], w, h, mpp_x, mpp_y
        )

        formatted_nuclei = format_nucleus_predictions(
            nuclei_dict=nuclei,
            level0_x=level0_x,
            level0_y=level0_y,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
        )

        score_stats = summarize_classification_scores(nuclei, review_threshold=0.70)

        overlay_filename = f"overlay_s{slide_id}_{job_id_str}.png"
        overlay_path, valid_cnts, class_cnts = create_overlay_image(roi_path, nuclei, overlay_filename)

        gallery_data = generate_nucleus_gallery(
            roi_path=roi_path,
            nuclei=nuclei,
            analysis_id=job_id_str,
            roi_x=level0_x,
            roi_y=level0_y,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
        )

        report_filename = f"ai_report_s{slide_id}_{job_id_str}.pdf"
        report_path = AI_REPORT_DIR / report_filename
        roi_meta = {
            "x": level0_x,
            "y": level0_y,
            "width": w,
            "height": h,
            "level": level,
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

        analysis_id = save_ai_analysis_record(
            connect_func=connect_func,
            slide_row=slide_row,
            case_row=case_row,
            user_username=current_username,
            roi_info=roi_meta,
            analysis_data=analysis_data,
            score_stats=score_stats,
            nuclei_dict=nuclei,
            gallery_data=gallery_data,
            roi_filename=roi_filename,
            overlay_filename=overlay_filename,
            report_filename=report_filename,
        )

        for cells in gallery_data.get("groups", {}).values():
            for cell in cells:
                cell["image_url"] = f"/api/ai/analyses/{analysis_id}/gallery/{cell['filename']}"

        total_elapsed = time.time() - (job.start_time if job and job.start_time else total_start_time)

        response = {
            "id": analysis_id,
            "slide_id": slide_id,
            "case_id": case_row["id"],
            "case_identifier": case_row["case_id"],
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "status": "completed",
            "disclaimer": DISCLAIMER_TEXT,
            "total_nuclei": analysis_data.get("total_nuclei", 0),
            "classification_breakdown": analysis_data.get("classification_breakdown", {}),
            "nuclear_density": analysis_data.get("nuclear_density_per_mm2"),
            "confidence_summary": {
                "mean": score_stats.get("overall", {}).get("mean") if score_stats.get("overall") else None,
                "median": score_stats.get("overall", {}).get("median") if score_stats.get("overall") else None,
                "minimum": score_stats.get("overall", {}).get("minimum") if score_stats.get("overall") else None,
                "maximum": score_stats.get("overall", {}).get("maximum") if score_stats.get("overall") else None,
            },
            "morphology": analysis_data.get("morphology", {}),
            "nuclei": formatted_nuclei,
            "elapsed_seconds": round(infer_elapsed, 2),
            "inference_duration": round(infer_elapsed, 2),
            "total_duration": round(total_elapsed, 2),
            "roi_x": level0_x,
            "roi_y": level0_y,
            "roi_width": w,
            "roi_height": h,
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
                str(n["id"]): n for n in formatted_nuclei
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        if job:
            job.status = "completed"
            job.analysis_id = analysis_id
            job.result = response
            job.percent = 100
            job.processed_patches = total_patches
            job.completed_at = datetime.now(timezone.utc).isoformat()

        return response

    @router.get("/api/slides/{slide_id}/ai/estimate")
    def estimate_ai_analysis_patches(
        slide_id: int,
        width: int = 256,
        height: int = 256,
        x: int | None = None,
        y: int | None = None,
        level: int = 0,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        slide_row = get_slide_func(slide_id)
        slide_path = stored_path_func(slide_row["filename"])
        slide = openslide.OpenSlide(str(slide_path))
        try:
            full_w, full_h = slide.level_dimensions[0]
            valid, err = validate_roi_dimensions(width, height, full_w, full_h, x, y)
            patch_data = calculate_hovernet_patches(width, height)
            small_limit = int(os.getenv("HOVERNET_SMALL_ROI_PATCHES", "25"))
            return {
                "valid": valid,
                "error": err,
                "width": width,
                "height": height,
                "total_patches": patch_data["total_patches"],
                "tqdm_total": patch_data["tqdm_total"],
                "estimated_seconds": patch_data["estimated_seconds"],
                "is_large": patch_data["total_patches"] > small_limit,
            }
        finally:
            slide.close()

    @router.get("/api/ai/jobs/{job_id}")
    def get_ai_job_progress(
        job_id: str,
        _=Depends(require_roles_func(ai_read_roles)),
    ):
        job = ai_job_manager.get_job(job_id)
        if not job:
            raise HTTPException(404, f"AI Job '{job_id}' not found.")
        return job.to_dict()

    @router.post("/api/ai/jobs/{job_id}/cancel")
    def cancel_ai_job_endpoint(
        job_id: str,
        current_user: dict = Depends(require_roles_func(ai_run_roles)),
    ):
        job = ai_job_manager.get_job(job_id)
        if not job:
            raise HTTPException(404, f"AI Job '{job_id}' not found.")
        success = ai_job_manager.cancel_job(job_id)
        return {"job_id": job_id, "cancelled": success, "status": job.status}

    @router.post("/api/slides/{slide_id}/ai/analyses")
    def run_slide_ai_analysis(
        slide_id: int,
        req: AIAnalysisRequest,
        response: Response,
        current_user: dict = Depends(require_roles_func(ai_run_roles)),
    ):
        slide_row = get_slide_func(slide_id)
        case_row = get_case_func(slide_row["case_id"])

        slide_path = stored_path_func(slide_row["filename"])
        if not slide_path.is_file():
            raise HTTPException(404, f"Slide file '{slide_row['filename']}' is missing on disk.")

        eff_x = req.x if req.x is not None else req.roi_x
        eff_y = req.y if req.y is not None else req.roi_y
        eff_w = req.width if req.width is not None else (req.roi_width if req.roi_width is not None else 256)
        eff_h = req.height if req.height is not None else (req.roi_height if req.roi_height is not None else 256)
        if req.auto_tissue:
            eff_x = None
            eff_y = None

        try:
            slide = openslide.OpenSlide(str(slide_path))
        except Exception as exc:
            logger.error("Failed to open slide %s with OpenSlide: %s", slide_id, exc)
            raise HTTPException(500, f"Cannot open slide with OpenSlide: {exc}")

        full_w, full_h = slide.level_dimensions[0]

        # 1. ROI Dimension Validation before inference
        if not req.auto_tissue:
            valid, err = validate_roi_dimensions(eff_w, eff_h, full_w, full_h, eff_x, eff_y)
            if not valid:
                slide.close()
                raise HTTPException(status_code=400, detail=err)

        # 2. Calculate patch requirements
        patch_info = calculate_hovernet_patches(eff_w, eff_h)
        total_patches = patch_info["total_patches"]
        small_limit = int(os.getenv("HOVERNET_SMALL_ROI_PATCHES", "25"))
        is_large = total_patches > small_limit

        # 3. Route to Background Job if large ROI or explicitly requested
        if is_large or req.run_in_background:
            job = ai_job_manager.create_job(
                slide_id=slide_id,
                case_id=case_row["id"],
                requested_by=current_user["username"],
                roi_info={
                    "x": eff_x,
                    "y": eff_y,
                    "width": eff_w,
                    "height": eff_h,
                    "level": req.level,
                    "auto_tissue": req.auto_tissue,
                },
                total_patches=total_patches,
                estimated_seconds=patch_info["estimated_seconds"],
            )

            def _bg_runner():
                try:
                    _execute_pipeline(
                        slide_row=slide_row,
                        case_row=case_row,
                        current_username=current_user["username"],
                        eff_x=eff_x,
                        eff_y=eff_y,
                        eff_w=eff_w,
                        eff_h=eff_h,
                        level=req.level,
                        auto_tissue=req.auto_tissue,
                        slide=slide,
                        job=job,
                    )
                except Exception as exc:
                    logger.exception("Background AI inference job %s failed: %s", job.job_id, exc)
                finally:
                    try:
                        slide.close()
                    except Exception:
                        pass

            t = threading.Thread(target=_bg_runner, daemon=True, name=f"AIJobWorker-{job.job_id}")
            t.start()

            response.status_code = status.HTTP_202_ACCEPTED
            return {
                "job_id": job.job_id,
                "id": job.job_id,
                "status": "processing",
                "is_background": True,
                "total_patches": total_patches,
                "estimated_seconds": patch_info["estimated_seconds"],
                "message": f"AI analysis started in background ({total_patches} patches).",
            }

        # 4. Small ROI: Fast Synchronous Workflow
        try:
            result = _execute_pipeline(
                slide_row=slide_row,
                case_row=case_row,
                current_username=current_user["username"],
                eff_x=eff_x,
                eff_y=eff_y,
                eff_w=eff_w,
                eff_h=eff_h,
                level=req.level,
                auto_tissue=req.auto_tissue,
                slide=slide,
                job=None,
            )
            response.status_code = status.HTTP_200_OK
            return result
        except InterruptedError:
            raise HTTPException(status_code=409, detail="AI analysis was cancelled.")
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"AI analysis timed out: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.exception("Synchronous AI analysis failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"HoVer-Net inference failed: {exc}")
        finally:
            try:
                slide.close()
            except Exception:
                pass

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
            summary = None
            if d.get("summary_json"):
                try:
                    summary = json.loads(d.pop("summary_json"))
                except Exception:
                    summary = None
            d["summary"] = summary
            d["status"] = (d.get("status") or "completed").lower()
            d["classification_breakdown"] = (summary and summary.get("classification_breakdown")) or {
                "Neoplastic": d.get("neoplastic_count", 0),
                "Inflammatory": d.get("inflammatory_count", 0),
                "Connective": d.get("connective_count", 0),
                "Necrotic": d.get("necrotic_count", 0),
                "Non-neoplastic": d.get("non_neoplastic_count", 0),
                "No label": d.get("no_label_count", 0),
            }
            d["morphology"] = (summary and summary.get("morphology")) or {}
            d["confidence_summary"] = {"mean": d.get("avg_confidence")}
            d["roi"] = {
                "x": d.get("roi_x"),
                "y": d.get("roi_y"),
                "width": d.get("roi_width"),
                "height": d.get("roi_height"),
                "level": d.get("roi_level"),
            }
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

        nuclei_list = list(predictions.values())
        breakdown = summary.get("classification_breakdown") or {
            "Neoplastic": d.get("neoplastic_count", 0),
            "Inflammatory": d.get("inflammatory_count", 0),
            "Connective": d.get("connective_count", 0),
            "Necrotic": d.get("necrotic_count", 0),
            "Non-neoplastic": d.get("non_neoplastic_count", 0),
            "No label": d.get("no_label_count", 0),
        }
        morph = summary.get("morphology", {})

        return {
            "id": d["id"],
            "slide_id": d["slide_id"],
            "case_id": d["case_id"],
            "requested_by": d["requested_by"],
            "model_name": d["model_name"],
            "model_version": d["model_version"],
            "status": "completed",
            "disclaimer": d["disclaimer"],
            "created_at": d["created_at"],
            "completed_at": d["completed_at"],
            "total_nuclei": d["total_nuclei"],
            "classification_breakdown": breakdown,
            "nuclear_density": d["nuclear_density"],
            "confidence_summary": summary.get("confidence_statistics", {}).get("overall") or {"mean": d["avg_confidence"]},
            "morphology": morph,
            "nuclei": nuclei_list,
            "roi_x": d["roi_x"],
            "roi_y": d["roi_y"],
            "roi_width": d["roi_width"],
            "roi_height": d["roi_height"],
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

    @router.post("/api/ai/analyses/{analysis_id}/report")
    def generate_analysis_report_endpoint(
        analysis_id: int,
        current_user: dict = Depends(require_roles_func(ai_run_roles)),
    ):
        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)
            case_row = get_case_func(row["case_id"])

        summary = json.loads(row.get("summary_json") or "{}")
        gallery = json.loads(row.get("gallery_json") or "{}")
        roi_meta = {
            "x": row["roi_x"],
            "y": row["roi_y"],
            "width": row["roi_width"],
            "height": row["roi_height"],
            "level": row["roi_level"],
        }
        roi_path = AI_ROI_DIR / (row.get("roi_image_filename") or "")
        overlay_path = AI_ROI_DIR / (row.get("overlay_image_filename") or "")

        report_filename = f"ai_report_s{row['slide_id']}_{analysis_id}_{uuid.uuid4().hex[:8]}.pdf"
        report_path = AI_REPORT_DIR / report_filename

        build_color_ai_pdf(
            output_path=report_path,
            slide_id=row["slide_id"],
            case_identifier=case_row["case_id"],
            roi_path=roi_path,
            overlay_path=overlay_path,
            analysis={
                "total_nuclei": row["total_nuclei"],
                "type_summary": summary.get("type_summary", {}),
                "morphology": summary.get("morphology", {}),
                "nuclear_density_per_mm2": row["nuclear_density"],
            },
            score_stats=summary.get("confidence_statistics", {}),
            gallery=gallery,
            roi_info=roi_meta,
            model_name=f"{row['model_name']} ({row['model_version']})",
        )

        with closing(connect_func()) as db:
            db.execute("UPDATE ai_analyses SET report_pdf_filename = ? WHERE id = ?", (report_filename, analysis_id))
            db.commit()

        return {
            "success": True,
            "report_filename": report_filename,
            "report_url": f"/api/ai/analyses/{analysis_id}/report.pdf",
        }

    @router.get("/api/ai/analyses/{analysis_id}/report.pdf")
    def download_ai_report_pdf(
        analysis_id: int,
        request: Request,
        token: str | None = None,
    ):
        # Authenticate via either header/cookie or query token
        auth_token = token or request.cookies.get("access_token")
        if not auth_token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                auth_token = auth_header.split(" ")[1]

        if auth_token:
            try:
                import jwt
                from main import SECRET_KEY, ALGORITHM
                jwt.decode(auth_token, SECRET_KEY, algorithms=[ALGORITHM])
            except Exception:
                raise HTTPException(401, "Invalid or expired authentication token")
        else:
            try:
                get_current_user_func(request)
            except Exception:
                raise HTTPException(401, "Authentication required to download AI report")

        with closing(connect_func()) as db:
            row = _get_analysis_row(db, analysis_id)
            case_row = get_case_func(row["case_id"])

        filename = row.get("report_pdf_filename")
        if not filename or not (AI_REPORT_DIR / filename).is_file():
            summary = json.loads(row.get("summary_json") or "{}")
            gallery = json.loads(row.get("gallery_json") or "{}")
            roi_meta = {
                "x": row["roi_x"],
                "y": row["roi_y"],
                "width": row["roi_width"],
                "height": row["roi_height"],
                "level": row["roi_level"],
            }
            roi_path = AI_ROI_DIR / (row.get("roi_image_filename") or "")
            overlay_path = AI_ROI_DIR / (row.get("overlay_image_filename") or "")
            filename = f"ai_report_s{row['slide_id']}_{analysis_id}.pdf"
            report_path = AI_REPORT_DIR / filename

            build_color_ai_pdf(
                output_path=report_path,
                slide_id=row["slide_id"],
                case_identifier=case_row["case_id"],
                roi_path=roi_path,
                overlay_path=overlay_path,
                analysis={
                    "total_nuclei": row["total_nuclei"],
                    "type_summary": summary.get("type_summary", {}),
                    "morphology": summary.get("morphology", {}),
                    "nuclear_density_per_mm2": row["nuclear_density"],
                },
                score_stats=summary.get("confidence_statistics", {}),
                gallery=gallery,
                roi_info=roi_meta,
                model_name=f"{row['model_name']} ({row['model_version']})",
            )
            with closing(connect_func()) as db:
                db.execute("UPDATE ai_analyses SET report_pdf_filename = ? WHERE id = ?", (filename, analysis_id))
                db.commit()

        path = AI_REPORT_DIR / filename
        case_code = re.sub(r"[^\w\-]", "_", str(case_row["case_id"]))
        download_name = f"ai_report_case_{case_code}_analysis_{analysis_id}.pdf"
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={download_name}"},
        )

    return router
