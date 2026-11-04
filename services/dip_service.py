"""Classical digital-image-processing helpers. Otsu tissue segmentation is the first technique."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from PIL import Image

from services.ai_engine import validate_roi_dimensions

ROOT_DIR = Path(__file__).resolve().parents[2]
DIP_DIR = Path(__file__).resolve().parents[1] / "storage" / "dip"
RESULT_ID = re.compile(r"[a-f0-9]{32}\Z")
MIN_ROI = 64
MAX_ROI = 2048
MAX_PROCESSING_PIXELS = 8_000_000
ALLOWED_KERNELS = {1, 3, 5, 7, 9}
BLANK_STD = 2.0
DIP_DIR.mkdir(parents=True, exist_ok=True)


def choose_processing_level(level_dimensions, max_pixels=MAX_PROCESSING_PIXELS):
    """Use the most detailed pyramid level that stays within the pixel budget."""
    if not level_dimensions:
        raise ValueError("Slide has no readable pyramid levels")
    for level, size in enumerate(level_dimensions):
        width, height = int(size[0]), int(size[1])
        if width > 0 and height > 0 and width * height <= max_pixels:
            return level
    width, height = int(level_dimensions[-1][0]), int(level_dimensions[-1][1])
    raise ValueError(
        f"The smallest slide overview is {width}×{height}, above the {max_pixels:,} pixel processing limit"
    )


def validate_dip_roi(x, y, width, height, slide_width, slide_height):
    valid, error = validate_roi_dimensions(
        width, height, slide_width, slide_height, x, y, min_dim=MIN_ROI, max_dim=MAX_ROI,
    )
    if not valid:
        raise ValueError(error)


def segment_otsu(image, gaussian_kernel: int = 5) -> dict:
    """Segment darker tissue from a brighter background. The source array is not modified."""
    kernel = int(gaussian_kernel)
    if kernel not in ALLOWED_KERNELS:
        raise ValueError("Gaussian kernel must be an odd size from 1 to 9")
    pixels = np.asarray(image)
    if pixels.ndim == 2:
        rgb = cv2.cvtColor(pixels, cv2.COLOR_GRAY2RGB)
    elif pixels.ndim == 3 and pixels.shape[2] == 4:
        rgb = cv2.cvtColor(pixels, cv2.COLOR_RGBA2RGB)
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        rgb = pixels
    else:
        raise ValueError("Segmentation expects a grayscale or RGB image")
    rgb = np.ascontiguousarray(rgb)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if kernel > 1:
        gray = cv2.GaussianBlur(gray, (kernel, kernel), 0)

    if gray.size == 0 or float(gray.std()) < BLANK_STD:
        mask = np.zeros(gray.shape, dtype=np.uint8)
        threshold = int(round(float(gray.mean()))) if gray.size else 0
    else:
        threshold, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        threshold = int(threshold)
        foreground = mask == 255
        background = ~foreground
        if foreground.any() and background.any() and float(gray[foreground].mean()) > float(gray[background].mean()):
            mask = cv2.bitwise_not(mask)

    coverage = round(float((mask == 255).mean()) * 100.0, 2) if mask.size else 0.0
    overlay = tissue_overlay(mask)
    return {
        "algorithm": "otsu",
        "threshold": threshold,
        "tissue_coverage_percent": coverage,
        "mask": mask,
        "overlay": overlay,
        "parameters": {"gaussian_kernel": kernel, "polarity": "dark_foreground"},
        "processing_width": int(gray.shape[1]),
        "processing_height": int(gray.shape[0]),
    }


def tissue_overlay(mask: np.ndarray) -> np.ndarray:
    overlay = np.zeros((*mask.shape, 4), dtype=np.uint8)
    overlay[mask == 255] = (32, 148, 124, 150)
    return overlay


def binary_preview(mask: np.ndarray) -> np.ndarray:
    """White is foreground tissue and black is classified background."""
    preview = np.zeros((*mask.shape, 3), dtype=np.uint8)
    preview[mask == 255] = 255
    return preview


def contour_overlay(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Outline tissue and interior holes. Tiny regions are omitted as noise."""
    canvas = np.zeros((*mask.shape, 4), dtype=np.uint8)
    if mask.size == 0 or not (mask == 255).any():
        return canvas, 0
    contours, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    min_area = max(24.0, float(mask.size) * 0.0002)
    kept = [contour for contour in contours if cv2.contourArea(contour) >= min_area]
    if not kept:
        return canvas, 0
    thickness = max(1, int(round(min(mask.shape) / 400)))
    color = np.zeros(mask.shape[:2], dtype=np.uint8)
    drawn = cv2.cvtColor(color, cv2.COLOR_GRAY2BGR)
    cv2.drawContours(drawn, kept, -1, (48, 176, 255), thickness)
    alpha = np.any(drawn > 0, axis=2)
    canvas[alpha, 0] = drawn[alpha, 2]
    canvas[alpha, 1] = drawn[alpha, 1]
    canvas[alpha, 2] = drawn[alpha, 0]
    canvas[alpha, 3] = 255
    return canvas, len(kept)


def render_visualization(mask: np.ndarray, view: str, algorithm: str = "otsu") -> np.ndarray:
    if algorithm == "canny":
        if view == "overlay":
            overlay = np.zeros((*mask.shape, 4), dtype=np.uint8)
            overlay[mask == 255] = (0, 196, 220, 255)
            return overlay
        if view == "mask":
            return binary_preview(mask)
        raise ValueError("Canny visualizations are overlay and mask")
    if view == "overlay":
        return tissue_overlay(mask)
    if view == "mask":
        return binary_preview(mask)
    if view == "contours":
        image, _ = contour_overlay(mask)
        return image
    raise ValueError("Visualization must be overlay, mask, or contours")


def validate_canny_thresholds(low_threshold, high_threshold) -> tuple[int, int]:
    low = int(low_threshold)
    high = int(high_threshold)
    if not (0 <= low < high <= 255):
        raise ValueError("Canny thresholds must satisfy 0 <= low threshold < high threshold <= 255")
    return low, high


def detect_canny(image, gaussian_kernel: int = 5, low_threshold: int = 50, high_threshold: int = 150) -> dict:
    """Detect intensity edges. The source array is not modified."""
    kernel = int(gaussian_kernel)
    if kernel not in {3, 5, 7, 9}:
        raise ValueError("Gaussian kernel must be 3, 5, 7, or 9")
    low, high = validate_canny_thresholds(low_threshold, high_threshold)
    pixels = np.asarray(image)
    if pixels.ndim == 2:
        gray = pixels
    elif pixels.ndim == 3 and pixels.shape[2] == 4:
        gray = cv2.cvtColor(pixels, cv2.COLOR_RGBA2GRAY)
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        gray = cv2.cvtColor(np.ascontiguousarray(pixels), cv2.COLOR_RGB2GRAY)
    else:
        raise ValueError("Canny expects a grayscale or RGB image")
    gray = np.ascontiguousarray(gray)
    blurred = cv2.GaussianBlur(gray, (kernel, kernel), 0)
    edges = cv2.Canny(blurred, low, high, apertureSize=3, L2gradient=True)
    edge_pixels = int((edges == 255).sum())
    density = round((edge_pixels / edges.size) * 100.0, 2) if edges.size else 0.0
    return {
        "algorithm": "canny",
        "mask": edges,
        "edge_pixel_count": edge_pixels,
        "edge_density_percent": density,
        "parameters": {"gaussian_kernel": kernel, "low_threshold": low, "high_threshold": high},
        "processing_width": int(gray.shape[1]),
        "processing_height": int(gray.shape[0]),
    }


TILE_MAX_SIDE = 512
TILE_HALO = 48
TILE_CACHE_LIMIT = 48
_TILE_CACHE: dict = {}
_TILE_ORDER: list = []
_TILE_LOCK = Lock()


def choose_region_level(level_downsamples, width: int, height: int, max_side: int = TILE_MAX_SIDE) -> int:
    """Finest pyramid level whose read of this level-0 region stays within max_side."""
    if width < 1 or height < 1:
        raise ValueError("Tile is empty")
    if width > 8192 or height > 8192:
        raise ValueError("Requested region is too large for one enhancement tile")
    if not level_downsamples:
        raise ValueError("Slide has no readable pyramid levels")
    for level, downsample in enumerate(level_downsamples):
        factor = float(downsample) or 1.0
        if width / factor <= max_side and height / factor <= max_side:
            return level
    raise ValueError("This region cannot be enhanced within the tile size limit")


def clahe_tile_png(slide, *, x: int, y: int, width: int, height: int, clip_limit: float, tile_grid_size: int, cache_key: tuple) -> bytes:
    """Enhance one level-0 rectangle. CLAHE's tile grid applies to the padded read, then the halo is cropped off."""
    from io import BytesIO
    with _TILE_LOCK:
        cached = _TILE_CACHE.get(cache_key)
        if cached is not None:
            _TILE_ORDER.remove(cache_key)
            _TILE_ORDER.append(cache_key)
            return cached
    slide_width, slide_height = slide.dimensions
    if x < 0 or y < 0 or width < 1 or height < 1 or x + width > slide_width or y + height > slide_height:
        raise ValueError("Tile is outside the slide")
    level = choose_region_level(slide.level_downsamples, width, height)
    downsample = float(slide.level_downsamples[level]) or 1.0
    level_width, level_height = slide.level_dimensions[level]
    origin_x = int(x / downsample)
    origin_y = int(y / downsample)
    read_width = max(1, int(round(width / downsample)))
    read_height = max(1, int(round(height / downsample)))
    left = max(0, origin_x - TILE_HALO)
    top = max(0, origin_y - TILE_HALO)
    right = min(level_width, origin_x + read_width + TILE_HALO)
    bottom = min(level_height, origin_y + read_height + TILE_HALO)
    region = slide.read_region((int(left * downsample), int(top * downsample)), level, (right - left, bottom - top)).convert("RGB")
    enhanced = apply_clahe(np.array(region), clip_limit, tile_grid_size)["image"]
    crop_x = origin_x - left
    crop_y = origin_y - top
    cropped = enhanced[crop_y:crop_y + read_height, crop_x:crop_x + read_width]
    buffer = BytesIO()
    Image.fromarray(cropped, mode="RGB").save(buffer, format="PNG")
    payload = buffer.getvalue()
    with _TILE_LOCK:
        _TILE_CACHE[cache_key] = payload
        _TILE_ORDER.append(cache_key)
        while len(_TILE_ORDER) > TILE_CACHE_LIMIT:
            _TILE_CACHE.pop(_TILE_ORDER.pop(0), None)
    return payload


def luminance_stats(rgb: np.ndarray) -> tuple[float, float]:
    lightness = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2LAB)[:, :, 0].astype(np.float64)
    return round(float(lightness.mean()), 2), round(float(lightness.std()), 2)


def apply_clahe(image, clip_limit: float = 2.0, tile_grid_size: int = 8) -> dict:
    """Enhance local contrast on the LAB lightness channel. The source array is not modified."""
    clip = float(clip_limit)
    tile = int(tile_grid_size)
    if not 1.0 <= clip <= 5.0:
        raise ValueError("CLAHE clip limit must be between 1.0 and 5.0")
    if tile not in {4, 8, 16}:
        raise ValueError("CLAHE tile grid size must be 4, 8, or 16")
    pixels = np.asarray(image)
    if pixels.ndim == 3 and pixels.shape[2] == 4:
        rgb = cv2.cvtColor(pixels, cv2.COLOR_RGBA2RGB)
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        rgb = np.array(pixels, copy=True)
    else:
        raise ValueError("CLAHE expects an RGB image")
    if rgb.dtype != np.uint8:
        raise ValueError("CLAHE expects a uint8 image")
    source = np.array(rgb, copy=True)
    original_mean, original_std = luminance_stats(source)
    lab = cv2.cvtColor(source, cv2.COLOR_RGB2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    enhanced_l = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tile, tile)).apply(lightness)
    enhanced = cv2.cvtColor(cv2.merge([enhanced_l, a_channel, b_channel]), cv2.COLOR_LAB2RGB)
    enhanced_mean, enhanced_std = luminance_stats(enhanced)
    comparison = source.copy()
    midpoint = comparison.shape[1] // 2
    comparison[:, midpoint:] = enhanced[:, midpoint:]
    return {
        "algorithm": "clahe",
        "image": enhanced,
        "comparison": comparison,
        "parameters": {"clip_limit": clip, "tile_grid_size": tile},
        "processing_width": int(enhanced.shape[1]),
        "processing_height": int(enhanced.shape[0]),
        "original_luminance_mean": original_mean,
        "original_luminance_std": original_std,
        "enhanced_luminance_mean": enhanced_mean,
        "enhanced_luminance_std": enhanced_std,
    }


def save_segmentation_result(*, slide_id: int, roi: dict, segmentation: dict, elapsed_ms: float, mode: str = "roi", processing: dict | None = None) -> dict:
    result_id = uuid.uuid4().hex
    folder = (DIP_DIR / result_id).resolve()
    if folder.parent != DIP_DIR.resolve():
        raise ValueError("Invalid segmentation storage location")
    folder.mkdir(parents=True, exist_ok=False)
    algorithm = segmentation.get("algorithm", "otsu")
    mask = None
    if algorithm == "clahe":
        Image.fromarray(segmentation["image"], mode="RGB").save(folder / "enhanced.png")
        Image.fromarray(segmentation["comparison"], mode="RGB").save(folder / "comparison.png")
    else:
        mask = segmentation["mask"]
        Image.fromarray(mask, mode="L").save(folder / "mask.png")
    record = {
        "id": result_id,
        "slide_id": int(slide_id),
        "algorithm": algorithm,
        "mode": mode,
        "roi": roi,
        "processing": {
            "width": segmentation.get("processing_width"),
            "height": segmentation.get("processing_height"),
            **(processing or {}),
        },
        "parameters": segmentation["parameters"],
        "elapsed_ms": round(float(elapsed_ms), 2),
        "overlay_url": f"/api/dip/results/{result_id}/overlay",
        "research_only": True,
    }
    if algorithm == "clahe":
        record.update({
            "original_luminance_mean": segmentation["original_luminance_mean"],
            "original_luminance_std": segmentation["original_luminance_std"],
            "enhanced_luminance_mean": segmentation["enhanced_luminance_mean"],
            "enhanced_luminance_std": segmentation["enhanced_luminance_std"],
            "overlay_url": f"/api/dip/results/{result_id}/enhanced",
            "views": {
                "enhanced": f"/api/dip/results/{result_id}/enhanced",
                "comparison": f"/api/dip/results/{result_id}/comparison",
            },
            "disclaimer": "Research Use Only. CLAHE changes local contrast. It is not a diagnostic improvement.",
        })
    elif algorithm == "canny":
        record.update({
            "edge_pixel_count": int(segmentation["edge_pixel_count"]),
            "edge_density_percent": segmentation["edge_density_percent"],
            "views": {
                "overlay": f"/api/dip/results/{result_id}/overlay",
                "mask": f"/api/dip/results/{result_id}/mask",
            },
            "disclaimer": "Research Use Only. Canny marks intensity transitions. It is not a tissue, cell, or tumor boundary.",
        })
    else:
        _, contour_count = contour_overlay(mask)
        foreground = int((mask == 255).sum())
        record.update({
            "threshold": segmentation["threshold"],
            "tissue_coverage_percent": segmentation["tissue_coverage_percent"],
            "foreground_pixel_count": foreground,
            "background_pixel_count": int(mask.size - foreground),
            "contour_count": contour_count,
            "views": {
                "overlay": f"/api/dip/results/{result_id}/overlay",
                "mask": f"/api/dip/results/{result_id}/mask",
                "contours": f"/api/dip/results/{result_id}/contours",
            },
            "accuracy_note": "No ground-truth tissue mask is available, so segmentation accuracy is not independently validated.",
            "disclaimer": "Research Use Only. Otsu separates intensity groups; it is not tumor, cell, or cancer-boundary detection.",
        })
    (folder / "meta.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def load_segmentation_result(result_id: str) -> tuple[dict, Path]:
    if not RESULT_ID.fullmatch(result_id or ""):
        raise FileNotFoundError(result_id)
    folder = (DIP_DIR / result_id).resolve()
    if folder.parent != DIP_DIR.resolve() or not (folder / "meta.json").is_file():
        raise FileNotFoundError(result_id)
    if not any((folder / name).is_file() for name in ("mask.png", "overlay.png", "enhanced.png")):
        raise FileNotFoundError(result_id)
    record = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return record, folder


def visualization_png(folder: Path, view: str) -> bytes:
    from io import BytesIO
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8")) if (folder / "meta.json").is_file() else {}
    if meta.get("algorithm") == "clahe":
        name = "comparison.png" if view == "comparison" else "enhanced.png"
        if view not in {"enhanced", "comparison", "overlay"}:
            raise FileNotFoundError(view)
        path = folder / name
        if not path.is_file():
            raise FileNotFoundError(view)
        return path.read_bytes()
    mask_path = folder / "mask.png"
    if mask_path.is_file():
        mask = np.array(Image.open(mask_path))
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8")) if (folder / "meta.json").is_file() else {}
        image = render_visualization(mask, view, meta.get("algorithm", "otsu"))
        mode = "RGBA" if image.ndim == 3 and image.shape[2] == 4 else "RGB"
    elif view == "overlay" and (folder / "overlay.png").is_file():
        return (folder / "overlay.png").read_bytes()
    else:
        raise FileNotFoundError(view)
    buffer = BytesIO()
    Image.fromarray(image, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


def measure(function):
    started = time.perf_counter()
    value = function()
    return value, (time.perf_counter() - started) * 1000.0
