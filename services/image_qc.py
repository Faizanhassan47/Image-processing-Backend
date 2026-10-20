"""Deterministic Image Quality Control (QC) Engine.

Executes purely deterministic computer-vision metrics using OpenCV and NumPy (no deep learning required):
1. Focus / Sharpness Scoring: Variance of the Laplacian filter (σ²(∇²I)).
2. Tissue Area vs. Glass Background: Otsu's optimal thresholding to detect blank or empty slides.
3. Exposure Analysis: Mean luminance & histogram saturation to flag overexposed or underexposed scans.

Conforms to Section 5.1 & 5.6 (Quality Control) of the PRS.
"""

from pathlib import Path
from typing import Any
import cv2
import numpy as np
from PIL import Image


def compute_deterministic_qc(
    image_input: Path | np.ndarray | Image.Image,
    min_sharpness_threshold: float = 85.0,
    min_tissue_ratio_threshold: float = 0.015,
) -> dict[str, Any]:
    """
    Analyzes an image or overview thumbnail for deterministic optical and preparation quality.

    Returns:
        dict: Metrics including sharpness score, tissue area percentage, exposure status,
              and overall QC outcome ('Passed' or 'Flagged').
    """
    # 1. Load image into BGR/Grayscale numpy array
    if isinstance(image_input, (str, Path)):
        img_bgr = cv2.imread(str(image_input))
        if img_bgr is None:
            # Fallback to PIL in case of special format
            pil_img = Image.open(str(image_input)).convert("RGB")
            img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    elif isinstance(image_input, Image.Image):
        img_bgr = cv2.cvtColor(np.array(image_input.convert("RGB")), cv2.COLOR_RGB2BGR)
    elif isinstance(image_input, np.ndarray):
        img_bgr = image_input if len(image_input.shape) == 3 else cv2.cvtColor(image_input, cv2.COLOR_GRAY2BGR)
    else:
        raise ValueError(f"Unsupported image input type: {type(image_input)}")

    if img_bgr is None or img_bgr.size == 0:
        return {
            "qc_status": "Failed",
            "flags": ["Image data is unreadable or empty"],
            "sharpness_score": 0.0,
            "tissue_percent": 0.0,
        }

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    total_pixels = gray.size

    # 2. Sharpness / Focus Analysis via Laplacian Variance
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness_score = round(float(laplacian.var()), 2)
    is_sharp = sharpness_score >= min_sharpness_threshold

    # 3. Tissue vs. Glass Detection via Otsu Thresholding
    # On H&E and pathology slides, tissue is darker and glass background is near-white (>215)
    # Apply Otsu inverse threshold or threshold against glass intensity
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    tissue_pixels = int(cv2.countNonZero(thresh))
    tissue_ratio = tissue_pixels / total_pixels
    tissue_percent = round(tissue_ratio * 100.0, 2)
    has_tissue = tissue_ratio >= min_tissue_ratio_threshold

    # 4. Exposure & Luminance Analysis
    mean_luminance = round(float(np.mean(gray)), 1)
    overexposed_pixels = int(np.sum(gray >= 250))
    underexposed_pixels = int(np.sum(gray <= 15))
    overexposed_pct = round((overexposed_pixels / total_pixels) * 100.0, 2)
    underexposed_pct = round((underexposed_pixels / total_pixels) * 100.0, 2)

    # 5. Evaluate Flags and Overall Decision
    flags = []
    if not is_sharp:
        flags.append(f"Low focus / blur detected (Sharpness score: {sharpness_score} < {min_sharpness_threshold})")
    if not has_tissue:
        flags.append(f"Insufficient tissue detected ({tissue_percent}% of slide area; possible blank or edge scan)")
    if overexposed_pct > 75.0 and tissue_percent < 1.0:
        flags.append(f"Severe overexposure / excessive blank glass ({overexposed_pct}% saturated pixels)")
    if underexposed_pct > 65.0:
        flags.append(f"Severe underexposure / dark image ({underexposed_pct}% black pixels)")

    qc_status = "Passed" if not flags else "Flagged"

    return {
        "qc_status": qc_status,
        "is_sharp": is_sharp,
        "sharpness_score": sharpness_score,
        "sharpness_threshold": min_sharpness_threshold,
        "tissue_percent": tissue_percent,
        "has_tissue": has_tissue,
        "mean_luminance": mean_luminance,
        "overexposed_percent": overexposed_pct,
        "underexposed_percent": underexposed_pct,
        "flags": flags,
    }
