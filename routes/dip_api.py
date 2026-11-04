"""ROI digital-image-processing routes. Otsu tissue segmentation is the first technique."""
from contextlib import closing
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
import numpy as np
from fastapi.responses import Response
from pydantic import BaseModel, Field
import openslide

from core.security import AI_READ_ROLES, AI_RUN_ROLES
from services.dip_service import (
    choose_processing_level,
    load_segmentation_result,
    apply_clahe,
    clahe_tile_png,
    luminance_stats,
    detect_canny,
    measure,
    save_segmentation_result,
    segment_otsu,
    validate_dip_roi,
    visualization_png,
)

logger = logging.getLogger("pathology.dip")


class DipRoi(BaseModel):
    x: int
    y: int
    width: int
    height: int


class DipParameters(BaseModel):
    gaussian_kernel: int = Field(default=5)
    low_threshold: int = Field(default=50)
    high_threshold: int = Field(default=150)
    clip_limit: float = Field(default=2.0)
    tile_grid_size: int = Field(default=8)


class DipSegmentationRequest(BaseModel):
    algorithm: str
    mode: str = "roi"
    roi: DipRoi | None = None
    parameters: DipParameters = Field(default_factory=DipParameters)


def create_dip_router(connect_func, require_roles_func, get_slide_func, get_case_func, stored_path_func, require_assigned_case_func):
    router = APIRouter(tags=["Digital Image Processing"])

    def _authorized_slide(slide_id, current_user):
        slide_row = get_slide_func(slide_id)
        case_row = get_case_func(slide_row["case_id"])
        require_assigned_case_func(case_row, current_user)
        return slide_row, case_row

    @router.post("/api/slides/{slide_id}/dip/segmentation")
    def run_segmentation(
        slide_id: int,
        request: DipSegmentationRequest,
        current_user: dict = Depends(require_roles_func(AI_RUN_ROLES)),
    ):
        if request.algorithm not in {"otsu", "canny", "clahe"}:
            raise HTTPException(400, "DIP supports otsu, canny, and clahe")
        slide_row, case_row = _authorized_slide(slide_id, current_user)
        suffix = str(slide_row.get("filename") or "").lower()
        if not suffix.endswith((".svs", ".tif", ".tiff")):
            raise HTTPException(400, "Otsu segmentation supports SVS and TIFF slides opened with OpenSlide")
        slide_path = stored_path_func(slide_row["filename"])
        if not slide_path.is_file():
            raise HTTPException(404, "Slide file is missing on disk")
        try:
            slide = openslide.OpenSlide(str(slide_path))
        except Exception as exc:
            raise HTTPException(500, f"Cannot open slide with OpenSlide: {exc}") from exc
        mode = request.mode or "roi"
        if mode not in {"roi", "whole_slide"}:
            slide.close()
            raise HTTPException(400, "Processing mode must be roi or whole_slide")
        clahe_crop = None
        try:
            slide_width, slide_height = slide.dimensions
            if mode == "whole_slide":
                try:
                    level = choose_processing_level(slide.level_dimensions)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                level_width, level_height = slide.level_dimensions[level]
                region = slide.read_region((0, 0), level, (level_width, level_height)).convert("RGB")
                bounds = {"x": 0, "y": 0, "width": slide_width, "height": slide_height, "level": 0}
                processing = {
                    "level": level,
                    "downsample": float(slide.level_downsamples[level]),
                    "gaussian_kernel": request.parameters.gaussian_kernel,
                }
            else:
                if request.roi is None:
                    raise HTTPException(400, "Selected ROI segmentation requires a rectangle")
                roi = request.roi
                try:
                    validate_dip_roi(roi.x, roi.y, roi.width, roi.height, slide_width, slide_height)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                pad = 64 if request.algorithm == "clahe" else 0
                read_x = max(0, roi.x - pad)
                read_y = max(0, roi.y - pad)
                read_right = min(slide_width, roi.x + roi.width + pad)
                read_bottom = min(slide_height, roi.y + roi.height + pad)
                region = slide.read_region((read_x, read_y), 0, (read_right - read_x, read_bottom - read_y)).convert("RGB")
                clahe_crop = (roi.x - read_x, roi.y - read_y, roi.width, roi.height) if pad else None
                bounds = {"x": roi.x, "y": roi.y, "width": roi.width, "height": roi.height, "level": 0}
                processing = {"level": 0, "downsample": 1.0, "gaussian_kernel": request.parameters.gaussian_kernel}
        finally:
            slide.close()

        def process():
            if request.algorithm == "clahe":
                source = np.array(region)
                result = apply_clahe(source, request.parameters.clip_limit, request.parameters.tile_grid_size)
                if clahe_crop:
                    crop_x, crop_y, crop_width, crop_height = clahe_crop
                    source = source[crop_y:crop_y + crop_height, crop_x:crop_x + crop_width]
                    enhanced = result["image"][crop_y:crop_y + crop_height, crop_x:crop_x + crop_width]
                    comparison = source.copy()
                    midpoint = comparison.shape[1] // 2
                    comparison[:, midpoint:] = enhanced[:, midpoint:]
                    original_mean, original_std = luminance_stats(source)
                    enhanced_mean, enhanced_std = luminance_stats(enhanced)
                    result.update({
                        "image": enhanced,
                        "comparison": comparison,
                        "processing_width": int(enhanced.shape[1]),
                        "processing_height": int(enhanced.shape[0]),
                        "original_luminance_mean": original_mean,
                        "original_luminance_std": original_std,
                        "enhanced_luminance_mean": enhanced_mean,
                        "enhanced_luminance_std": enhanced_std,
                    })
                return result
            if request.algorithm == "canny":
                return detect_canny(
                    region,
                    request.parameters.gaussian_kernel,
                    request.parameters.low_threshold,
                    request.parameters.high_threshold,
                )
            return segment_otsu(region, request.parameters.gaussian_kernel)

        try:
            segmentation, elapsed_ms = measure(process)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        record = save_segmentation_result(
            slide_id=slide_id,
            roi=bounds,
            segmentation=segmentation,
            elapsed_ms=elapsed_ms,
            mode=mode,
            processing=processing,
        )
        try:
            with closing(connect_func()) as db:
                db.execute(
                    "INSERT INTO audit_log (actor_username,action,target_type,target_id,details) VALUES (?,?,?,?,?)",
                    (current_user["username"], f"dip.{request.algorithm}.requested", "slide", str(slide_id),
                     json.dumps({"result_id": record["id"], "case_id": case_row["id"], "roi": record["roi"]})),
                )
                db.commit()
        except Exception:
            logger.exception("Could not write DIP audit record for slide %s", slide_id)
        return record

    def _view(result_id: str, view: str, current_user):
        try:
            record, folder = load_segmentation_result(result_id)
            payload = visualization_png(folder, view)
        except FileNotFoundError:
            raise HTTPException(404, "Segmentation result not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _authorized_slide(int(record["slide_id"]), current_user)
        return Response(payload, media_type="image/png")

    @router.get("/api/dip/results/{result_id}/overlay")
    def get_overlay(result_id: str, current_user=Depends(require_roles_func(AI_READ_ROLES))):
        return _view(result_id, "overlay", current_user)

    @router.get("/api/dip/results/{result_id}/mask")
    def get_mask(result_id: str, current_user=Depends(require_roles_func(AI_READ_ROLES))):
        return _view(result_id, "mask", current_user)

    @router.get("/api/dip/results/{result_id}/contours")
    def get_contours(result_id: str, current_user=Depends(require_roles_func(AI_READ_ROLES))):
        return _view(result_id, "contours", current_user)

    @router.get("/api/slides/{slide_id}/dip/clahe-tile")
    def clahe_tile(
        slide_id: int,
        x: int = Query(ge=0),
        y: int = Query(ge=0),
        width: int = Query(ge=1, le=8192),
        height: int = Query(ge=1, le=8192),
        clip_limit: float = Query(ge=1.0, le=5.0),
        tile_grid_size: int = Query(),
        current_user: dict = Depends(require_roles_func(AI_READ_ROLES)),
    ):
        if tile_grid_size not in {4, 8, 16}:
            raise HTTPException(400, "CLAHE tile grid size must be 4, 8, or 16")
        slide_row, _case_row = _authorized_slide(slide_id, current_user)
        slide_path = stored_path_func(slide_row["filename"])
        if not slide_path.is_file():
            raise HTTPException(404, "Slide file is missing on disk")
        try:
            slide = openslide.OpenSlide(str(slide_path))
        except Exception as exc:
            raise HTTPException(500, f"Cannot open slide with OpenSlide: {exc}") from exc
        try:
            payload = clahe_tile_png(
                slide,
                x=x,
                y=y,
                width=width,
                height=height,
                clip_limit=clip_limit,
                tile_grid_size=tile_grid_size,
                cache_key=(int(slide_id), int(slide_path.stat().st_mtime), x, y, width, height, float(clip_limit), tile_grid_size),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            slide.close()
        return Response(payload, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    @router.get("/api/dip/results/{result_id}/enhanced")
    def get_enhanced(result_id: str, current_user=Depends(require_roles_func(AI_READ_ROLES))):
        return _view(result_id, "enhanced", current_user)

    @router.get("/api/dip/results/{result_id}/comparison")
    def get_comparison(result_id: str, current_user=Depends(require_roles_func(AI_READ_ROLES))):
        return _view(result_id, "comparison", current_user)

    return router
