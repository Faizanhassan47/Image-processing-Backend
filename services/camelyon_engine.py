"""Isolated CAMELYON16 ROI inference and SQL persistence (research only)."""
from contextlib import closing
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event

ROOT_DIR = Path(__file__).resolve().parents[2]
CAMELYON_ROOT = ROOT_DIR / "camelyon"
CAMELYON_PYTHON = Path(os.getenv("CAMELYON_PYTHON", str(CAMELYON_ROOT / ".venv" / "Scripts" / "python.exe")))
CAMELYON_WORKER = Path(os.getenv("CAMELYON_WORKER", str(CAMELYON_ROOT / "camelyon_worker.py")))
CAMELYON_WORKDIR = Path(os.getenv("CAMELYON_WORKDIR", str(CAMELYON_ROOT)))
CAMELYON_OUTPUT_DIR = Path(os.getenv("CAMELYON_OUTPUT_DIR", str(ROOT_DIR / "backend" / "storage" / "ai" / "camelyon")))
CAMELYON_TIMEOUT = int(os.getenv("CAMELYON_TIMEOUT_SECONDS", "1800"))
MODEL_NAME = "MONAI pathology_tumor_detection"
FEATURE_CODE = "CAMELYON_TUMOR_DETECTION"
MODEL_VERSION = "CAMELYON16 pretrained"
PATCH_SIZE = 224
MAX_ROI_SIZE = 4480
DISCLAIMER = "Research Use Only — Not for Clinical Diagnosis. H&E lymph-node tissue only; unvalidated patch scores."


def calculate_camelyon_patches(width: int, height: int) -> int:
    if not (PATCH_SIZE <= width <= MAX_ROI_SIZE and PATCH_SIZE <= height <= MAX_ROI_SIZE):
        raise ValueError(f"CAMELYON ROI must be {PATCH_SIZE} to {MAX_ROI_SIZE} pixels per dimension")
    return (width // PATCH_SIZE) * (height // PATCH_SIZE)


def _read_progress(output_dir: Path) -> dict | None:
    path = output_dir / "progress.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run_worker(cmd, output_dir: Path, cancel_event: Event | None, progress_callback=None):
    env = os.environ.copy()
    env.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(CAMELYON_WORKDIR), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    try:
        while True:
            progress = _read_progress(output_dir)
            if progress and progress_callback is not None:
                progress_callback(progress)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("CAMELYON analysis cancelled")
            remaining = CAMELYON_TIMEOUT - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError("CAMELYON inference timed out")
            try:
                stdout, stderr = proc.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except (InterruptedError, TimeoutError):
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"CAMELYON worker failed ({proc.returncode}): {(stderr or '')[-1500:]}")
    return time.monotonic() - start


def run_camelyon_inference(*, slide_path: Path, x: int, y: int, width: int, height: int,
                           job_id: str, cancel_event: Event | None = None, progress_callback=None) -> dict:
    for path, kind in ((CAMELYON_PYTHON, 'file'), (CAMELYON_WORKER, 'file'), (CAMELYON_WORKDIR, 'dir')):
        if not (path.is_file() if kind == 'file' else path.is_dir()):
            raise RuntimeError(f"Missing CAMELYON runtime dependency: {path}")
    slide_path = Path(slide_path).resolve()
    if not slide_path.is_file():
        raise FileNotFoundError(slide_path)
    if x < 0 or y < 0:
        raise ValueError("Negative ROI coordinates")
    expected = calculate_camelyon_patches(width, height)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
        raise ValueError("Invalid job identifier")
    output_dir = (CAMELYON_OUTPUT_DIR / job_id).resolve()
    if output_dir.parent != CAMELYON_OUTPUT_DIR.resolve():
        raise ValueError("Invalid output location")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CAMELYON analysis cancelled")
    output_dir.mkdir(parents=True, exist_ok=False)
    cmd = [str(CAMELYON_PYTHON), str(CAMELYON_WORKER),
           '--slide', str(slide_path), '--x', str(x), '--y', str(y),
           '--width', str(width), '--height', str(height), '--output', str(output_dir)]
    elapsed = _run_worker(cmd, output_dir, cancel_event, progress_callback)
    result_file = output_dir / 'results.json'
    if not result_file.is_file():
        raise RuntimeError("CAMELYON missing results.json")
    result = json.loads(result_file.read_text(encoding='utf-8'))
    if result.get('status') != 'completed' or result.get('patch_count') != expected:
        raise RuntimeError("Invalid CAMELYON result")
    for name in ('original_region.png', 'probability_grid.png', 'heatmap_overlay.png'):
        if not (output_dir / name).is_file():
            raise RuntimeError(f"CAMELYON missing {name}")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CAMELYON analysis cancelled")
    result.update(job_id=job_id, feature_code=FEATURE_CODE, model_version=MODEL_VERSION,
                  elapsed_seconds=round(elapsed, 2), output_dir=str(output_dir))
    return result


def run_camelyon_whole_slide(*, slide_path: Path, job_id: str, cancel_event: Event | None = None,
                             progress_callback=None) -> dict:
    for path, kind in ((CAMELYON_PYTHON, 'file'), (CAMELYON_WORKER, 'file'), (CAMELYON_WORKDIR, 'dir')):
        if not (path.is_file() if kind == 'file' else path.is_dir()):
            raise RuntimeError(f"Missing CAMELYON runtime dependency: {path}")
    slide_path = Path(slide_path).resolve()
    if not slide_path.is_file():
        raise FileNotFoundError(slide_path)
    if slide_path.suffix.lower() not in {'.svs', '.tif', '.tiff'}:
        raise ValueError("Whole-slide CAMELYON supports SVS and TIFF slides")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
        raise ValueError("Invalid job identifier")
    output_dir = (CAMELYON_OUTPUT_DIR / job_id).resolve()
    if output_dir.parent != CAMELYON_OUTPUT_DIR.resolve():
        raise ValueError("Invalid output location")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CAMELYON analysis cancelled")
    output_dir.mkdir(parents=True, exist_ok=False)
    cmd = [str(CAMELYON_PYTHON), str(CAMELYON_WORKER),
           '--slide', str(slide_path), '--whole-slide', '--output', str(output_dir)]
    elapsed = _run_worker(cmd, output_dir, cancel_event, progress_callback)
    result_file = output_dir / 'results.json'
    if not result_file.is_file():
        raise RuntimeError("CAMELYON missing results.json")
    result = json.loads(result_file.read_text(encoding='utf-8'))
    if result.get('status') != 'completed' or result.get('analysis_mode') != 'whole_slide':
        raise RuntimeError("Invalid CAMELYON whole-slide result")
    if not result.get('patch_count'):
        raise RuntimeError("CAMELYON whole-slide result contains no patches")
    for name in ('original_region.png', 'probability_grid.png', 'heatmap_overlay.png'):
        if not (output_dir / name).is_file():
            raise RuntimeError(f"CAMELYON missing {name}")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("CAMELYON analysis cancelled")
    result.update(job_id=job_id, feature_code=FEATURE_CODE, model_version=MODEL_VERSION,
                  elapsed_seconds=round(elapsed, 2), output_dir=str(output_dir))
    return result


def save_camelyon_analysis_record(*, connect_func, slide_row: dict, case_row: dict,
                                  requested_by: str, result: dict, roi_image_dir: Path) -> int:
    """Store patch scores and copy images into the existing authorized ROI image service."""
    roi = result['roi']
    job_id = result['job_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id):
        raise ValueError('Invalid job identifier')
    source = (CAMELYON_OUTPUT_DIR / job_id).resolve()
    if source.parent != CAMELYON_OUTPUT_DIR.resolve():
        raise ValueError('Invalid CAMELYON image directory')
    roi_image_dir.mkdir(parents=True, exist_ok=True)
    roi_filename = f'camelyon_roi_{job_id}.png'
    overlay_filename = f'camelyon_overlay_{job_id}.png'
    shutil.copyfile(source / 'original_region.png', roi_image_dir / roi_filename)
    shutil.copyfile(source / 'heatmap_overlay.png', roi_image_dir / overlay_filename)
    summary = {
        'feature_code': FEATURE_CODE, 'model_name': MODEL_NAME,
        'model_version': MODEL_VERSION, 'patch_count': result['patch_count'],
        'min_score': result['min_score'], 'max_score': result['max_score'],
        'mean_score': result['mean_score'], 'checkpoint_sha256': result['checkpoint_sha256'],
        'mpp_x': result.get('mpp_x'), 'mpp_y': result.get('mpp_y'),
        'coordinate_system': 'level_0_pixels', 'job_id': job_id,
        'analysis_mode': result.get('analysis_mode', 'roi'),
        'eligible_patch_count': result.get('eligible_patch_count', result.get('patch_count')),
        'skipped_patch_count': result.get('skipped_patch_count', 0),
        'failed_patch_count': result.get('failed_patch_count', 0),
        'partial_coverage': bool(result.get('partial_coverage')),
        'coverage_fraction': result.get('coverage_fraction', 1.0),
        'tissue_parameters': result.get('tissue_parameters'),
        'heatmap_frame': result.get('heatmap_frame') or roi,
        'resolution_note': result.get('resolution_note'),
        'research_only': True, 'disclaimer': DISCLAIMER,
    }
    predictions = {'patches': result['patches'], 'patch_size': PATCH_SIZE,
                   'coordinate_system': 'level_0_pixels'}
    with closing(connect_func()) as db:
        cursor = db.execute("""INSERT INTO ai_analyses (
            slide_id, case_id, requested_by, model_name, model_version, status,
            roi_x, roi_y, roi_width, roi_height, roi_level,
            total_nuclei, neoplastic_count, inflammatory_count, connective_count,
            necrotic_count, non_neoplastic_count, no_label_count,
            nuclear_density, avg_confidence, summary_json, predictions_json, gallery_json,
            roi_image_filename, overlay_image_filename, report_pdf_filename, disclaimer, completed_at
        ) VALUES (
            ?, ?, ?, ?, ?, 'Completed',
            ?, ?, ?, ?, 0,
            0, 0, 0, 0, 0, 0, 0,
            NULL, ?, ?, ?, ?, ?, ?, NULL, ?, SYSUTCDATETIME()
        )""", (
            slide_row['id'], case_row['id'], requested_by, MODEL_NAME, MODEL_VERSION,
            roi['x'], roi['y'], roi['width'], roi['height'],
            result['mean_score'], json.dumps(summary), json.dumps(predictions), json.dumps({}),
            roi_filename, overlay_filename, DISCLAIMER,
        ))
        analysis_id = cursor.lastrowid
        db.commit()
    return analysis_id
