"""
Image Registration Service for Digital Pathology Whole-Slide Imaging.
Provides mathematical algorithms for:
- Manual Landmark Transformations (Translation, Rigid, Similarity, Affine)
- Landmark validation & degeneracy rejection
- Alignment residual error computation (mean, max, inliers)
- Downsampled thumbnail feature extraction and automatic registration (ORB / AKAZE)
- Outlier rejection via RANSAC
- Physical coordinate transformation and calibration handling
"""

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class RegistrationError(Exception):
    """Raised when landmark configuration or registration estimation fails."""
    pass


def validate_landmarks(landmarks: List[Dict[str, Any]], min_count: int = 1) -> Tuple[np.ndarray, np.ndarray]:
    """
    Validates and extracts reference and moving coordinates from landmark pairs.
    Returns (ref_pts, mov_pts) as float32 arrays of shape (N, 2).
    """
    if len(landmarks) < min_count:
        raise RegistrationError(f"At least {min_count} landmark pair(s) required for this transformation type.")

    ref_coords = []
    mov_coords = []

    for idx, lm in enumerate(landmarks):
        rx, ry = lm.get("ref_x"), lm.get("ref_y")
        mx, my = lm.get("mov_x"), lm.get("mov_y")

        if any(v is None for v in (rx, ry, mx, my)):
            raise RegistrationError(f"Landmark pair #{idx + 1} has incomplete coordinates.")

        try:
            rx, ry, mx, my = float(rx), float(ry), float(mx), float(my)
        except (ValueError, TypeError):
            raise RegistrationError(f"Landmark pair #{idx + 1} contains non-numeric coordinate values.")

        if not all(math.isfinite(v) for v in (rx, ry, mx, my)):
            raise RegistrationError(f"Landmark pair #{idx + 1} contains non-finite coordinates.")

        ref_coords.append([rx, ry])
        mov_coords.append([mx, my])

    ref_arr = np.array(ref_coords, dtype=np.float32)
    mov_arr = np.array(mov_coords, dtype=np.float32)

    # Check for duplicate or collinear points if N >= 3
    if len(ref_arr) >= 3:
        # Check variance in coordinates
        ref_std = np.std(ref_arr, axis=0)
        mov_std = np.std(mov_arr, axis=0)
        if ref_std[0] < 1e-4 and ref_std[1] < 1e-4:
            raise RegistrationError("Reference landmarks are degenerate (all points identical or virtually identical).")
        if mov_std[0] < 1e-4 and mov_std[1] < 1e-4:
            raise RegistrationError("Moving landmarks are degenerate (all points identical or virtually identical).")

    return ref_arr, mov_arr


def compute_residuals(
    ref_pts: np.ndarray,
    mov_pts: np.ndarray,
    matrix_3x3: np.ndarray,
    pixel_size_microns: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes registration quality metrics:
    - mean residual error (pixels)
    - max residual error (pixels)
    - root mean square error (RMSE)
    - physical calibration distance (microns) if pixel_size_microns is provided
    """
    n_pts = len(ref_pts)
    if n_pts == 0:
        return {
            "landmark_count": 0,
            "mean_residual_px": 0.0,
            "max_residual_px": 0.0,
            "rmse_px": 0.0,
            "calibrated": False,
        }

    # Transform moving points using matrix_3x3: [x_ref, y_ref, 1]^T = M * [x_mov, y_mov, 1]^T
    ones = np.ones((n_pts, 1), dtype=np.float32)
    mov_homogeneous = np.hstack([mov_pts, ones])  # (N, 3)
    transformed = (matrix_3x3 @ mov_homogeneous.T).T  # (N, 3)

    pred_x = transformed[:, 0] / np.where(np.abs(transformed[:, 2]) > 1e-8, transformed[:, 2], 1.0)
    pred_y = transformed[:, 1] / np.where(np.abs(transformed[:, 2]) > 1e-8, transformed[:, 2], 1.0)
    pred_pts = np.column_stack([pred_x, pred_y])

    # Euclidean distance between true ref and predicted ref points
    diffs = ref_pts - pred_pts
    residuals = np.sqrt(np.sum(diffs ** 2, axis=1))

    mean_err = float(np.mean(residuals))
    max_err = float(np.max(residuals))
    rmse = float(np.sqrt(np.mean(residuals ** 2)))

    metrics = {
        "landmark_count": int(n_pts),
        "mean_residual_px": round(mean_err, 3),
        "max_residual_px": round(max_err, 3),
        "rmse_px": round(rmse, 3),
        "calibrated": bool(pixel_size_microns and pixel_size_microns > 0),
    }

    if metrics["calibrated"] and pixel_size_microns:
        metrics["mean_residual_um"] = round(mean_err * pixel_size_microns, 2)
        metrics["max_residual_um"] = round(max_err * pixel_size_microns, 2)
        metrics["rmse_um"] = round(rmse * pixel_size_microns, 2)
        metrics["pixel_size_microns"] = pixel_size_microns

    return metrics


def estimate_manual_registration(
    transform_type: str,
    landmarks: List[Dict[str, Any]],
    pixel_size_microns: Optional[float] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Computes transformation matrix M (3x3 homogeneous) mapping moving coordinates -> reference coordinates:
    p_ref = M * p_moving
    """
    transform_type = transform_type.lower().strip()

    if transform_type == "translation":
        ref_pts, mov_pts = validate_landmarks(landmarks, min_count=1)
        # Translation vector is mean(ref - mov)
        t = np.mean(ref_pts - mov_pts, axis=0)
        M = np.array([
            [1.0, 0.0, float(t[0])],
            [0.0, 1.0, float(t[1])],
            [0.0, 0.0, 1.0],
        ], dtype=np.float64)

    elif transform_type == "rigid":
        ref_pts, mov_pts = validate_landmarks(landmarks, min_count=2)
        # Rigid transformation (Euclidean: rotation + translation, no scaling)
        # M_2x3 = cv2.estimateAffinePartial2D with force scale = 1.0, or SVD Kabsch
        # Calculate centroids
        cent_mov = np.mean(mov_pts, axis=0)
        cent_ref = np.mean(ref_pts, axis=0)
        mov_centered = mov_pts - cent_mov
        ref_centered = ref_pts - cent_ref

        # Covariance matrix H = mov_centered^T * ref_centered
        H = mov_centered.T @ ref_centered
        U, S, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T

        # Handle reflection
        if np.linalg.det(R) < 0:
            Vt[-1, :] *= -1
            R = Vt.T @ U.T

        t = cent_ref - R @ cent_mov
        M = np.array([
            [float(R[0, 0]), float(R[0, 1]), float(t[0])],
            [float(R[1, 0]), float(R[1, 1]), float(t[1])],
            [0.0, 0.0, 1.0],
        ], dtype=np.float64)

    elif transform_type == "similarity":
        ref_pts, mov_pts = validate_landmarks(landmarks, min_count=2)
        # Similarity transformation: translation, rotation, and uniform scale
        # cv2.estimateAffinePartial2D estimates [s*R | t]
        M_2x3, inliers = cv2.estimateAffinePartial2D(mov_pts, ref_pts, method=cv2.LMEDS)
        if M_2x3 is None:
            # Fallback to least squares similarity
            cent_mov = np.mean(mov_pts, axis=0)
            cent_ref = np.mean(ref_pts, axis=0)
            mov_c = mov_pts - cent_mov
            ref_c = ref_pts - cent_ref
            norm_mov = np.sum(mov_c ** 2)
            if norm_mov < 1e-8:
                raise RegistrationError("Moving landmarks are too close to compute scaling.")
            H = mov_c.T @ ref_c
            U, S, Vt = np.linalg.svd(H)
            R = Vt.T @ U.T
            if np.linalg.det(R) < 0:
                Vt[-1, :] *= -1
                R = Vt.T @ U.T
            scale = np.sum(S) / norm_mov
            sR = scale * R
            t = cent_ref - sR @ cent_mov
            M_2x3 = np.hstack([sR, t.reshape(2, 1)])

        M = np.vstack([M_2x3, [0.0, 0.0, 1.0]]).astype(np.float64)

    elif transform_type == "affine":
        ref_pts, mov_pts = validate_landmarks(landmarks, min_count=3)
        # Affine transformation: 6 degrees of freedom (translation, rotation, scale x/y, shear)
        if len(mov_pts) == 3:
            M_2x3 = cv2.getAffineTransform(mov_pts.astype(np.float32), ref_pts.astype(np.float32))
        else:
            M_2x3, inliers = cv2.estimateAffine2D(mov_pts, ref_pts, method=cv2.RANSAC, ransacReprojThreshold=50.0)
            if M_2x3 is None:
                # Least-squares fallback
                pad_mov = np.hstack([mov_pts, np.ones((len(mov_pts), 1), dtype=np.float32)])
                sol, _, _, _ = np.linalg.lstsq(pad_mov, ref_pts, rcond=None)
                M_2x3 = sol.T

        M = np.vstack([M_2x3, [0.0, 0.0, 1.0]]).astype(np.float64)

    else:
        raise RegistrationError(f"Unsupported transformation type '{transform_type}'. Must be one of: translation, rigid, similarity, affine.")

    metrics = compute_residuals(ref_pts, mov_pts, M, pixel_size_microns)
    metrics["transform_type"] = transform_type
    metrics["method"] = "manual"

    return M, metrics


def load_downsampled_image(
    slide_row: Dict[str, Any],
    file_path: Path,
    openslide_module: Any,
    max_dimension: int = 2048,
) -> Tuple[np.ndarray, float]:
    """
    Loads a downsampled RGB image of a whole-slide image or raster file for image registration.
    Returns: (rgb_array, downsample_factor) where downsample_factor = slide_level0_dim / downsampled_dim.
    """
    file_type = slide_row.get("file_type")
    orig_w = int(slide_row.get("width") or 0)
    orig_h = int(slide_row.get("height") or 0)

    if file_type == "wsi" and openslide_module is not None:
        try:
            with openslide_module.OpenSlide(str(file_path)) as osr:
                w, h = osr.dimensions
                target_factor = max(1.0, max(w, h) / float(max_dimension))
                
                # Find best level in slide
                best_level = osr.get_best_level_for_downsample(target_factor)
                level_downsample = osr.level_downsamples[best_level]
                level_dims = osr.level_dimensions[best_level]
                
                pil_img = osr.read_region((0, 0), best_level, level_dims).convert("RGB")
                
                # Resize if still larger than max_dimension
                if max(pil_img.size) > max_dimension:
                    scale = max_dimension / float(max(pil_img.size))
                    new_size = (max(1, int(pil_img.size[0] * scale)), max(1, int(pil_img.size[1] * scale)))
                    pil_img = pil_img.resize(new_size, Image.Resampling.BILINEAR)
                
                arr = np.array(pil_img)
                factor_x = orig_w / float(arr.shape[1]) if arr.shape[1] > 0 else 1.0
                factor_y = orig_h / float(arr.shape[0]) if arr.shape[0] > 0 else 1.0
                return arr, (factor_x + factor_y) / 2.0
        except Exception as exc:
            logger.warning("OpenSlide downsample load failed (%s), falling back to thumbnail/Pillow", exc)

    # Raster or OpenSlide fallback
    with Image.open(str(file_path)) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dimension:
            scale = max_dimension / float(max(w, h))
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.Resampling.BILINEAR)
        arr = np.array(img)
        actual_w = orig_w if orig_w > 0 else w
        factor = float(actual_w) / float(arr.shape[1]) if arr.shape[1] > 0 else 1.0
        return arr, factor


def run_automatic_registration(
    ref_slide_row: Dict[str, Any],
    ref_file_path: Path,
    mov_slide_row: Dict[str, Any],
    mov_file_path: Path,
    openslide_module: Any,
    transform_type: str = "affine",
    max_dimension: int = 1600,
    pixel_size_microns: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Executes automatic feature-based multi-slide tissue registration:
    1. Loads normalized downsampled RGB images.
    2. Converts to tissue-optimized grayscale / CLAHE contrast enhancement.
    3. Detects features via AKAZE / ORB.
    4. Matches descriptors and rejects outliers with Lowe's ratio test and RANSAC.
    5. Scales the estimated transformation back to Level-0 image coordinates.
    6. Returns proposed matrix, sample landmark pairs for visual verification, and quality metrics.
    """
    transform_type = transform_type.lower().strip()
    if transform_type not in ("translation", "rigid", "similarity", "affine"):
        transform_type = "affine"

    # Step 1: Load downsampled image pairs
    ref_rgb, ref_scale = load_downsampled_image(ref_slide_row, ref_file_path, openslide_module, max_dimension)
    mov_rgb, mov_scale = load_downsampled_image(mov_slide_row, mov_file_path, openslide_module, max_dimension)

    # Step 2: Convert to grayscale and apply CLAHE to balance H&E / IHC stain differences
    ref_gray = cv2.cvtColor(ref_rgb, cv2.COLOR_RGB2GRAY)
    mov_gray = cv2.cvtColor(mov_rgb, cv2.COLOR_RGB2GRAY)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    ref_enhanced = clahe.apply(ref_gray)
    mov_enhanced = clahe.apply(mov_gray)

    # Step 3: Feature Detection (try AKAZE first for stain invariance, fallback to ORB)
    kps_ref, des_ref = None, None
    kps_mov, des_mov = None, None

    try:
        akaze = cv2.AKAZE_create(threshold=0.001)
        kps_ref, des_ref = akaze.detectAndCompute(ref_enhanced, None)
        kps_mov, des_mov = akaze.detectAndCompute(mov_enhanced, None)
    except Exception as exc:
        logger.info("AKAZE detection error (%s), using ORB", exc)

    if des_ref is None or des_mov is None or len(kps_ref) < 8 or len(kps_mov) < 8:
        orb = cv2.ORB_create(nfeatures=4000, fastThreshold=10)
        kps_ref, des_ref = orb.detectAndCompute(ref_enhanced, None)
        kps_mov, des_mov = orb.detectAndCompute(mov_enhanced, None)

    if des_ref is None or des_mov is None or len(kps_ref) < 4 or len(kps_mov) < 4:
        raise RegistrationError("Insufficient reliable tissue features detected in slide pair for automatic alignment.")

    # Step 4: Feature matching with Lowe's ratio test
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw_matches = matcher.knnMatch(des_mov, des_ref, k=2)

    good_matches = []
    for m_pair in raw_matches:
        if len(m_pair) == 2:
            m, n = m_pair
            if m.distance < 0.78 * n.distance:
                good_matches.append(m)

    min_required_matches = {"translation": 2, "rigid": 3, "similarity": 4, "affine": 4}[transform_type]
    if len(good_matches) < min_required_matches:
        raise RegistrationError(
            f"Could not find sufficient corresponding landmarks ({len(good_matches)} found, minimum {min_required_matches} required). "
            "Please use Manual Landmark Registration for this slide pair."
        )

    # Step 5: Extract matching coordinates (in downsampled space)
    mov_pts_down = np.float32([kps_mov[m.queryIdx].pt for m in good_matches])
    ref_pts_down = np.float32([kps_ref[m.trainIdx].pt for m in good_matches])

    # Convert coordinates directly to Level-0 coordinates
    mov_pts_l0 = mov_pts_down * mov_scale
    ref_pts_l0 = ref_pts_down * ref_scale

    # Step 6: Estimate transformation on Level-0 coordinates with RANSAC
    inlier_mask = None
    if transform_type == "translation":
        # Median translation across all matches
        diff = ref_pts_l0 - mov_pts_l0
        t = np.median(diff, axis=0)
        M = np.array([
            [1.0, 0.0, float(t[0])],
            [0.0, 1.0, float(t[1])],
            [0.0, 0.0, 1.0],
        ], dtype=np.float64)
        inlier_mask = np.ones(len(good_matches), dtype=np.uint8)

    elif transform_type in ("rigid", "similarity"):
        M_2x3, inliers = cv2.estimateAffinePartial2D(
            mov_pts_l0,
            ref_pts_l0,
            method=cv2.RANSAC,
            ransacReprojThreshold=150.0,
            maxIters=3000,
            confidence=0.99,
        )
        if M_2x3 is None:
            raise RegistrationError("RANSAC failed to find a consistent geometric transformation between the slides.")
        M = np.vstack([M_2x3, [0.0, 0.0, 1.0]]).astype(np.float64)
        inlier_mask = inliers.ravel() if inliers is not None else np.ones(len(good_matches), dtype=np.uint8)

    else:  # affine
        M_2x3, inliers = cv2.estimateAffine2D(
            mov_pts_l0,
            ref_pts_l0,
            method=cv2.RANSAC,
            ransacReprojThreshold=200.0,
            maxIters=3000,
            confidence=0.99,
        )
        if M_2x3 is None:
            raise RegistrationError("RANSAC failed to converge on an affine transformation.")
        M = np.vstack([M_2x3, [0.0, 0.0, 1.0]]).astype(np.float64)
        inlier_mask = inliers.ravel() if inliers is not None else np.ones(len(good_matches), dtype=np.uint8)

    # Filter by inliers
    inlier_indices = np.where(inlier_mask.astype(bool))[0]
    n_inliers = len(inlier_indices)
    if n_inliers < min_required_matches:
        raise RegistrationError(
            f"Only {n_inliers} inlier matches survived RANSAC outlier rejection. Alignment cannot be confirmed."
        )

    inlier_ref_l0 = ref_pts_l0[inlier_indices]
    inlier_mov_l0 = mov_pts_l0[inlier_indices]

    metrics = compute_residuals(inlier_ref_l0, inlier_mov_l0, M, pixel_size_microns)
    metrics["total_matches"] = int(len(good_matches))
    metrics["inlier_count"] = int(n_inliers)
    metrics["inlier_ratio"] = round(float(n_inliers) / float(len(good_matches)), 3)
    metrics["transform_type"] = transform_type
    metrics["method"] = "automatic"

    # Select up to 10 representative inlier landmark pairs for manual review/display
    step = max(1, len(inlier_indices) // 10)
    selected_inliers = inlier_indices[::step][:10]

    proposed_landmarks = []
    for rank, idx in enumerate(selected_inliers, start=1):
        proposed_landmarks.append({
            "id": rank,
            "display_index": rank,
            "label": f"Auto Point #{rank}",
            "ref_x": round(float(ref_pts_l0[idx][0]), 2),
            "ref_y": round(float(ref_pts_l0[idx][1]), 2),
            "mov_x": round(float(mov_pts_l0[idx][0]), 2),
            "mov_y": round(float(mov_pts_l0[idx][1]), 2),
            "is_auto": True,
        })

    return {
        "transform_type": transform_type,
        "registration_method": "automatic",
        "transform_matrix": M.tolist(),
        "quality_metrics": metrics,
        "landmarks": proposed_landmarks,
    }
