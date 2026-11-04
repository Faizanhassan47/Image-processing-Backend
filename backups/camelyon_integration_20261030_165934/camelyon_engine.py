"""Isolated CAMELYON inference service for the pathology backend."""

import json
import os
import subprocess
import time
from pathlib import Path
from threading import Event


ROOT_DIR = Path(__file__).resolve().parents[2]
CAMELYON_ROOT = ROOT_DIR / "camelyon"

CAMELYON_PYTHON = Path(os.getenv(
    "CAMELYON_PYTHON",
    str(CAMELYON_ROOT / ".venv" / "Scripts" / "python.exe"),
))

CAMELYON_WORKER = Path(os.getenv(
    "CAMELYON_WORKER",
    str(CAMELYON_ROOT / "camelyon_worker.py"),
))

CAMELYON_WORKDIR = Path(os.getenv(
    "CAMELYON_WORKDIR",
    str(CAMELYON_ROOT),
))

CAMELYON_OUTPUT_DIR = Path(os.getenv(
    "CAMELYON_OUTPUT_DIR",
    str(ROOT_DIR / "backend" / "storage" / "ai" / "camelyon"),
))

CAMELYON_TIMEOUT = int(os.getenv("CAMELYON_TIMEOUT_SECONDS", "1800"))

MODEL_NAME = "MONAI pathology_tumor_detection"
FEATURE_CODE = "CAMELYON_TUMOR_DETECTION"
MODEL_VERSION = "CAMELYON16 pretrained"
PATCH_SIZE = 224
MAX_ROI_SIZE = 4480


def calculate_camelyon_patches(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        raise ValueError("ROI dimensions must be positive")

    if width > MAX_ROI_SIZE or height > MAX_ROI_SIZE:
        raise ValueError("CAMELYON ROI exceeds maximum dimensions")

    count = (width // PATCH_SIZE) * (height // PATCH_SIZE)

    if count < 1:
        raise ValueError("CAMELYON ROI must be at least 224x224")

    return count


def run_camelyon_inference(
    *,
    slide_path: Path,
    x: int,
    y: int,
    width: int,
    height: int,
    job_id: str,
    cancel_event: Event | None = None,
) -> dict:
    """Execute CAMELYON in an isolated, single-thread CPU subprocess."""

    if not CAMELYON_PYTHON.is_file():
        raise RuntimeError(f"CAMELYON Python missing: {CAMELYON_PYTHON}")

    if not CAMELYON_WORKER.is_file():
        raise RuntimeError(f"CAMELYON worker missing: {CAMELYON_WORKER}")

    if not CAMELYON_WORKDIR.is_dir():
        raise RuntimeError(f"CAMELYON workdir missing: {CAMELYON_WORKDIR}")

    slide_path = Path(slide_path).resolve()

    if not slide_path.is_file():
        raise FileNotFoundError(slide_path)

    if x < 0 or y < 0:
        raise ValueError("Negative ROI coordinates")

    patch_count = calculate_camelyon_patches(width, height)

    # The caller must supply a server-generated job identifier.
    if not job_id or not all(
        c.isascii() and (c.isalnum() or c in "-_")
        for c in job_id
    ):
        raise ValueError("Invalid job identifier")

    output_dir = (CAMELYON_OUTPUT_DIR / job_id).resolve()
    output_root = CAMELYON_OUTPUT_DIR.resolve()

    if not output_dir.is_relative_to(output_root):
        raise ValueError("Invalid output directory")

    if output_dir.exists():
        raise FileExistsError(f"CAMELYON job output exists: {job_id}")

    output_dir.mkdir(parents=True)

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"

    command = [
        str(CAMELYON_PYTHON),
        str(CAMELYON_WORKER),
        "--slide", str(slide_path),
        "--x", str(x),
        "--y", str(y),
        "--width", str(width),
        "--height", str(height),
        "--output", str(output_dir),
    ]

    start = time.monotonic()

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CAMELYON analysis cancelled")

    process = subprocess.Popen(
        command,
        cwd=str(CAMELYON_WORKDIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("CAMELYON analysis cancelled")

            remaining = CAMELYON_TIMEOUT - (time.monotonic() - start)

            if remaining <= 0:
                raise TimeoutError("CAMELYON inference timed out")

            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.5, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue

    except (InterruptedError, TimeoutError):
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise

    if process.returncode != 0:
        raise RuntimeError(
            f"CAMELYON worker exited with code {process.returncode}: "
            f"{stderr[-2000:]}"
        )

    result_path = output_dir / "results.json"

    if not result_path.is_file():
        raise RuntimeError("CAMELYON worker did not produce results.json")

    result = json.loads(result_path.read_text(encoding="utf-8"))

    if result.get("status") != "completed":
        raise RuntimeError("CAMELYON worker returned incomplete results")

    if result.get("patch_count") != patch_count:
        raise RuntimeError("CAMELYON patch count mismatch")

    for filename in (
        "original_region.png",
        "probability_grid.png",
        "heatmap_overlay.png",
    ):
        if not (output_dir / filename).is_file():
            raise RuntimeError(f"Missing CAMELYON output: {filename}")

    result["job_id"] = job_id
    result["feature_code"] = FEATURE_CODE
    result["model_version"] = MODEL_VERSION
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    result["output_dir"] = str(output_dir)

    return result
