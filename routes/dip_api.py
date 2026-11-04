"""Digital image processing on a slide region: Otsu, Canny, and CLAHE."""

from pathlib import Path
import time
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field


class DipRequest(BaseModel):
    algorithm: str
    mode: str = "roi"
    roi: dict | None = None
    parameters: dict = Field(default_factory=dict)


def create_dip_router(get_current_user_fn, get_slide_fn, stored_path_fn):
    router = APIRouter(tags=["Digital image processing"])
    storage = Path(__file__).resolve().parents[1] / "storage" / "dip"
    storage.mkdir(parents=True, exist_ok=True)

    def _read_region(slide_row, body: DipRequest):
        path = stored_path_fn(slide_row["filename"])
        if not path.is_file():
            raise HTTPException(404, "Slide file is missing on disk")
        try:
            import openslide
            slide = openslide.OpenSlide(str(path))
        except Exception:
            slide = None
        if slide is not None:
            try:
                full_w, full_h = slide.level_dimensions[0]
                if body.mode == "whole_slide":
                    level = slide.level_count - 1
                    for index, (width, height) in enumerate(slide.level_dimensions):
                        if max(width, height) <= 2048:
                            level = index
                            break
                    width, height = slide.level_dimensions[level]
                    image = slide.read_region((0, 0), level, (width, height)).convert("RGB")
                    return np.array(image), {"x": 0, "y": 0, "width": full_w, "height": full_h}
                roi = body.roi or {}
                x, y = int(roi.get("x", 0)), int(roi.get("y", 0))
                width, height = int(roi.get("width", 0)), int(roi.get("height", 0))
                if width < 64 or height < 64 or width > 2048 or height > 2048:
                    raise HTTPException(400, "ROI must be between 64 and 2048 pixels on each side")
                if x < 0 or y < 0 or x + width > full_w or y + height > full_h:
                    raise HTTPException(400, "ROI extends beyond slide boundaries")
                image = slide.read_region((x, y), 0, (width, height)).convert("RGB")
                return np.array(image), {"x": x, "y": y, "width": width, "height": height}
            finally:
                slide.close()
        with Image.open(path) as raw:
            rgb = raw.convert("RGB")
            full_w, full_h = rgb.size
            if body.mode == "whole_slide":
                rgb.thumbnail((2048, 2048))
                return np.array(rgb), {"x": 0, "y": 0, "width": full_w, "height": full_h}
            roi = body.roi or {}
            x, y = int(roi.get("x", 0)), int(roi.get("y", 0))
            width, height = int(roi.get("width", 0)), int(roi.get("height", 0))
            if x < 0 or y < 0 or x + width > full_w or y + height > full_h:
                raise HTTPException(400, "ROI extends beyond slide boundaries")
            return np.array(rgb.crop((x, y, x + width, y + height))), {"x": x, "y": y, "width": width, "height": height}

    def _save(name, image):
        target = storage / name
        Image.fromarray(image).save(target, format="PNG")
        return target.name

    @router.post("/api/slides/{slide_id}/dip/segmentation")
    def run_segmentation(slide_id: int, body: DipRequest, current_user: dict = Depends(get_current_user_fn)):
        del current_user
        slide_row = get_slide_fn(slide_id)
        started = time.perf_counter()
        rgb, roi = _read_region(slide_row, body)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        kernel = int(body.parameters.get("gaussian_kernel") or 5)
        if kernel % 2 == 0:
            kernel += 1
        kernel = min(max(kernel, 1), 31)
        token = uuid.uuid4().hex[:12]
        algorithm = body.algorithm
        views = {}
        payload = {
            "algorithm": algorithm,
            "mode": body.mode,
            "roi": roi,
            "parameters": body.parameters,
            "processing": {"width": int(rgb.shape[1]), "height": int(rgb.shape[0])},
        }
        if algorithm == "otsu":
            blurred = cv2.GaussianBlur(gray, (kernel, kernel), 0) if kernel > 1 else gray
            threshold, mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            overlay = rgb.copy()
            overlay[mask > 0] = (overlay[mask > 0] * 0.45 + np.array([40, 180, 90]) * 0.55).astype(np.uint8)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            drawn = rgb.copy()
            cv2.drawContours(drawn, contours, -1, (40, 180, 90), 2)
            views = {
                "overlay": _save(f"{token}-overlay.png", overlay),
                "mask": _save(f"{token}-mask.png", cv2.cvtColor(mask, cv2.COLOR_GRAY2RGB)),
                "contours": _save(f"{token}-contours.png", drawn),
            }
            tissue = int(np.count_nonzero(mask))
            total = int(mask.size)
            payload.update({
                "threshold": round(float(threshold), 2),
                "tissue_coverage_percent": round(100 * tissue / max(total, 1), 2),
                "foreground_pixel_count": tissue,
                "background_pixel_count": total - tissue,
                "contour_count": len(contours),
                "accuracy_note": "Otsu separates darker tissue from a lighter background on this region. It is a research view, not a diagnosis.",
            })
        elif algorithm == "canny":
            low = float(body.parameters.get("low_threshold") or 50)
            high = float(body.parameters.get("high_threshold") or 150)
            if low >= high:
                raise HTTPException(400, "The low threshold must be lower than the high threshold")
            blurred = cv2.GaussianBlur(gray, (kernel, kernel), 0) if kernel > 1 else gray
            edges = cv2.Canny(blurred, low, high)
            overlay = rgb.copy()
            overlay[edges > 0] = (220, 70, 70)
            views = {
                "overlay": _save(f"{token}-overlay.png", overlay),
                "mask": _save(f"{token}-mask.png", cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)),
            }
            edge_count = int(np.count_nonzero(edges))
            payload.update({
                "edge_pixel_count": edge_count,
                "edge_density_percent": round(100 * edge_count / max(int(edges.size), 1), 2),
            })
        elif algorithm == "clahe":
            clip = float(body.parameters.get("clip_limit") or 2)
            grid = int(body.parameters.get("tile_grid_size") or 8)
            grid = min(max(grid, 2), 32)
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            lightness, a, b = cv2.split(lab)
            enhanced_l = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid)).apply(lightness)
            enhanced = cv2.cvtColor(cv2.merge((enhanced_l, a, b)), cv2.COLOR_LAB2RGB)
            side = np.concatenate((rgb, enhanced), axis=1)
            views = {
                "enhanced": _save(f"{token}-enhanced.png", enhanced),
                "comparison": _save(f"{token}-comparison.png", side),
            }
            payload.update({
                "original_luminance_mean": round(float(lightness.mean()), 2),
                "enhanced_luminance_mean": round(float(enhanced_l.mean()), 2),
                "original_luminance_std": round(float(lightness.std()), 2),
                "enhanced_luminance_std": round(float(enhanced_l.std()), 2),
            })
        else:
            raise HTTPException(400, "Unsupported image processing algorithm")
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        payload["views"] = {name: f"/api/slides/{slide_id}/dip/images/{filename}" for name, filename in views.items()}
        payload["overlay_url"] = payload["views"].get("overlay") or payload["views"].get("enhanced")
        return payload

    @router.get("/api/slides/{slide_id}/dip/images/{filename}")
    def dip_image(slide_id: int, filename: str, current_user: dict = Depends(get_current_user_fn)):
        del slide_id, current_user
        if "/" in filename or "\\" in filename or not filename.endswith(".png"):
            raise HTTPException(400, "Invalid image name")
        path = (storage / filename).resolve()
        if path.parent != storage.resolve() or not path.is_file():
            raise HTTPException(404, "Processing image not found")
        return FileResponse(path, media_type="image/png")

    return router
