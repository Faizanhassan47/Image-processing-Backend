"""
KongNet-Det AI Engine for Digital Pathology Platform.

Manages persistent KongNet-Det worker process, mitotic candidate detection,
spatial coordinates transformation, candidate gallery crop extraction,
annotated ROI overlay rendering, ReportLab color PDF report generation,
and SQL persistence.

Research Use Only — Not for Clinical Diagnosis.
"""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("pathology.kongnet")

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent

DEFAULT_AI_STORAGE = BACKEND_DIR / "storage" / "ai"
AI_STORAGE = Path(os.getenv("AI_STORAGE_DIR", str(DEFAULT_AI_STORAGE)))
if not AI_STORAGE.is_absolute():
    AI_STORAGE = BACKEND_DIR / AI_STORAGE

AI_ROI_DIR = AI_STORAGE / "rois"
AI_GALLERY_DIR = AI_STORAGE / "galleries"
AI_REPORT_DIR = AI_STORAGE / "reports"

for _dir in (AI_STORAGE, AI_ROI_DIR, AI_GALLERY_DIR, AI_REPORT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# Path to python environment with PyTorch and KongNet dependencies
KONGNET_PYTHON = Path(
    os.getenv(
        "KONGNET_PYTHON",
        r"E:\pathology-ai-models\kongnet\.venv\Scripts\python.exe",
    )
)
KONGNET_WORKER = Path(
    os.getenv(
        "KONGNET_WORKER",
        str(ROOT_DIR / "ai_models" / "kongnet" / "kongnet_worker.py"),
    )
)
KONGNET_WORKDIR = Path(
    os.getenv(
        "KONGNET_WORKDIR",
        str(ROOT_DIR / "ai_models" / "kongnet"),
    )
)

MODEL_NAME = "KongNet-Det"
MODEL_VERSION = "MIDOG-1"
FEATURE_CODE = "MITOTIC_DETECTION"
DISCLAIMER_TEXT = "Research Use Only — Not for Clinical Diagnosis"

# Palette for PDF and Visualization
COLOR_NAVY_DARK = colors.HexColor("#0F2042")
COLOR_NAVY_PRIMARY = colors.HexColor("#1A365D")
COLOR_NAVY_ACCENT = colors.HexColor("#0284C7")
COLOR_TEXT_PRIMARY = colors.HexColor("#0F172A")
COLOR_TEXT_SECONDARY = colors.HexColor("#475569")
COLOR_TEXT_MUTED = colors.HexColor("#64748B")
COLOR_BG_CARD = colors.HexColor("#F8FAFC")
COLOR_BORDER = colors.HexColor("#CBD5E1")
COLOR_BORDER_LIGHT = colors.HexColor("#E2E8F0")
COLOR_RED_ALERT = colors.HexColor("#DC2626")
COLOR_ROSE_MITOSIS = colors.HexColor("#E11D48")

PROGRESS_RE = re.compile(r"PROGRESS:\s*(\d+)/(\d+)\s*\(([\d\.]+)%\)\s*\[([0-9\.]+)s<([0-9\.]+)s\]")


# =============================================================================
# PERSISTENT KONGNET WORKER MANAGER
# =============================================================================

class KongNetWorkerManager:
    """Thread-safe singleton managing the persistent KongNet inference worker."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._process = None
                cls._instance._worker_lock = threading.Lock()
                cls._instance._start_lock = threading.Lock()
                cls._instance._stdout_queue = queue.Queue()
                cls._instance._ready_event = threading.Event()
                cls._instance._last_error = None
                cls._instance._progress_callback = None
                cls._instance._cancel_event = threading.Event()
                cls._instance._last_activity_time = time.time()
                cls._instance._metadata = {}
            return cls._instance

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def cancel_inference(self):
        """Request immediate cancellation of active inference."""
        self._cancel_event.set()
        logger.info("Cancellation requested for active KongNet inference worker.")
        self.stop_worker()
        threading.Thread(target=self.start_worker, daemon=True, name="KongNet-Recovery").start()

    def stop_worker(self):
        with self._start_lock:
            if self._process is not None:
                logger.info("Stopping KongNet persistent worker...")
                try:
                    self._process.stdin.close()
                except Exception:
                    pass
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
            self._ready_event.clear()

    def start_worker(self, timeout_seconds: int = 90):
        with self._start_lock:
            if self.is_running():
                return

            if not KONGNET_PYTHON.exists():
                raise RuntimeError(
                    f"KongNet Python interpreter not found at: {KONGNET_PYTHON}. "
                    "Configure KONGNET_PYTHON to point to the Python environment with PyTorch and KongNet."
                )
            if not KONGNET_WORKER.exists():
                raise RuntimeError(f"KongNet worker script not found at: {KONGNET_WORKER}")
            if not KONGNET_WORKDIR.exists():
                raise RuntimeError(f"KongNet workdir not found at: {KONGNET_WORKDIR}")

            logger.info("Starting KongNet persistent worker (%s)...", KONGNET_WORKER)

            while not self._stdout_queue.empty():
                try:
                    self._stdout_queue.get_nowait()
                except queue.Empty:
                    break

            self._ready_event.clear()
            self._cancel_event.clear()
            self._last_error = None
            self._last_activity_time = time.time()

            self._process = subprocess.Popen(
                [str(KONGNET_PYTHON), str(KONGNET_WORKER)],
                cwd=str(KONGNET_WORKDIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            def drain_stderr(proc):
                try:
                    for line in iter(proc.stderr.readline, ""):
                        if not line:
                            break
                        self._last_activity_time = time.time()
                        line_str = line.strip()
                        if line_str:
                            logger.info("[KongNet Worker stderr] %s", line_str)
                            if line_str == "READY":
                                self._ready_event.set()
                            elif line_str.startswith("ERROR:"):
                                self._last_error = line_str

                            m = PROGRESS_RE.search(line_str)
                            if m:
                                curr, tot, pct, elap, rem = m.groups()
                                cb = self._progress_callback
                                if cb:
                                    try:
                                        cb({
                                            "percent": int(float(pct)),
                                            "processed_patches": int(curr),
                                            "total_patches": int(tot),
                                            "elapsed_str": f"{float(elap):.1f}s",
                                            "remaining_str": f"{float(rem):.1f}s",
                                            "elapsed_seconds": float(elap),
                                            "remaining_seconds": float(rem),
                                        })
                                    except Exception:
                                        pass
                except Exception as exc:
                    logger.debug("KongNet stderr drainer ended: %s", exc)

            def drain_stdout(proc, q):
                try:
                    for line in iter(proc.stdout.readline, ""):
                        if not line:
                            break
                        q.put(line)
                except Exception as exc:
                    logger.debug("KongNet stdout reader ended: %s", exc)

            t_err = threading.Thread(target=drain_stderr, args=(self._process,), daemon=True, name="KongNet-Stderr-Drainer")
            t_err.start()

            t_out = threading.Thread(target=drain_stdout, args=(self._process, self._stdout_queue), daemon=True, name="KongNet-Stdout-Reader")
            t_out.start()

            if not self._ready_event.wait(timeout=timeout_seconds):
                code = self._process.poll()
                self.stop_worker()
                raise TimeoutError(
                    f"KongNet worker timed out waiting for READY state (exit code: {code}, error: {self._last_error})"
                )

            logger.info("KongNet persistent worker is READY.")

    def run_inference(
        self,
        request_payload: dict,
        timeout_seconds: int = 300,
        progress_callback: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict:
        """Send inference request payload to the persistent worker and parse result."""
        with self._worker_lock:
            self._cancel_event.clear()
            self._progress_callback = progress_callback

            if not self.is_running():
                logger.info("KongNet worker not running; launching now...")
                self.start_worker()

            payload = {
                "command": "infer",
                **request_payload,
            }

            try:
                line = json.dumps(payload) + "\n"
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except Exception as write_err:
                logger.error("Failed to write request to KongNet worker: %s", write_err)
                self.stop_worker()
                raise RuntimeError(f"Communication with KongNet worker failed: {write_err}")

            start_t = time.time()
            inactivity_limit = 180
            self._last_activity_time = time.time()

            while True:
                if (cancel_event and cancel_event.is_set()) or self._cancel_event.is_set():
                    logger.info("Cancellation detected in KongNet worker manager.")
                    self.cancel_inference()
                    raise InterruptedError("KongNet inference cancelled by user.")

                now = time.time()
                if now - start_t > timeout_seconds:
                    logger.error("KongNet inference overall timeout (%ds). Killing worker.", timeout_seconds)
                    self.stop_worker()
                    raise TimeoutError(f"KongNet inference timed out after {timeout_seconds}s.")

                if now - self._last_activity_time > inactivity_limit:
                    logger.error("KongNet worker inactive for %ds without progress. Killing worker.", inactivity_limit)
                    self.stop_worker()
                    raise TimeoutError(f"KongNet worker stopped responding for {inactivity_limit}s.")

                try:
                    result_line = self._stdout_queue.get(timeout=0.25)
                    data = json.loads(result_line)
                    if not isinstance(data, dict):
                        raise ValueError("Worker returned invalid non-dictionary response.")
                    if data.get("status") == "error" or "error" in data:
                        raise RuntimeError(data.get("error", "Unknown KongNet inference failure"))
                    return data
                except queue.Empty:
                    if self._process.poll() is not None:
                        exit_code = self._process.poll()
                        raise RuntimeError(f"KongNet worker process died unexpectedly (exit code: {exit_code})")
                    continue


kongnet_manager = KongNetWorkerManager()


# =============================================================================
# CALCULATION & VALIDATION UTILITIES
# =============================================================================

def calculate_kongnet_tiles(
    width: int,
    height: int,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    target_mpp: float = 0.25,
    stride_px: int = 448,
    model_input_size: int = 512,
) -> dict:
    """Calculate the exact number of tiles KongNet will process for an ROI."""
    crop_w = max(1, round(model_input_size * target_mpp / mpp_x))
    crop_h = max(1, round(model_input_size * target_mpp / mpp_y))
    stride_x = max(1, round(stride_px * target_mpp / mpp_x))
    stride_y = max(1, round(stride_px * target_mpp / mpp_y))

    def _axis_starts(length, patch_len, stride):
        if patch_len >= length:
            return [0]
        out = list(range(0, length - patch_len + 1, stride))
        if not out or out[-1] != length - patch_len:
            out.append(length - patch_len)
        return out

    xs = _axis_starts(width, crop_w, stride_x)
    ys = _axis_starts(height, crop_h, stride_y)
    total_tiles = max(1, len(xs) * len(ys))
    seconds_per_tile = float(os.getenv("KONGNET_SECONDS_PER_TILE", "0.85"))
    est_seconds = round(total_tiles * seconds_per_tile, 1)

    return {
        "width": width,
        "height": height,
        "grid_x": len(xs),
        "grid_y": len(ys),
        "total_tiles": total_tiles,
        "total_patches": total_tiles,
        "estimated_seconds": est_seconds,
    }


# =============================================================================
# OVERLAY & CANDIDATE CROPS
# =============================================================================

def create_mitotic_overlay_image(
    roi_image_path: Path,
    detections: List[Dict[str, Any]],
    output_filename: str,
) -> Tuple[Path, int]:
    """Render annotated ROI preview image with mitotic figure candidate boxes and markers."""
    roi_image_path = Path(roi_image_path)
    output_path = AI_ROI_DIR / output_filename

    img = cv2.imread(str(roi_image_path))
    if img is None:
        raise FileNotFoundError(f"Cannot read ROI image: {roi_image_path}")

    h, w = img.shape[:2]
    # Rose/Red in BGR: (72, 29, 225)
    BOX_COLOR = (72, 29, 225)
    CROSS_COLOR = (0, 0, 255)
    TEXT_COLOR = (255, 255, 255)
    BG_COLOR = (30, 20, 120)

    for det in detections:
        rc = det.get("roi_coordinates")
        if not rc:
            continue
        rx, ry = int(round(rc["x"])), int(round(rc["y"]))
        score = det.get("score", 0.0)
        det_id = det.get("id", "")

        # Draw circle and crosshair
        radius = 14
        cv2.circle(img, (rx, ry), radius, BOX_COLOR, 2, cv2.LINE_AA)
        cv2.line(img, (rx - radius - 4, ry), (rx + radius + 4, ry), CROSS_COLOR, 1, cv2.LINE_AA)
        cv2.line(img, (rx, ry - radius - 4), (rx, ry + radius + 4), CROSS_COLOR, 1, cv2.LINE_AA)

        # Label tag
        label = f"{det_id}: {score:.2f}"
        (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
        tag_x = min(w - tw - 4, max(4, rx - tw // 2))
        tag_y = max(th + 4, ry - radius - 4)

        cv2.rectangle(img, (tag_x - 2, tag_y - th - 2), (tag_x + tw + 2, tag_y + 2), BG_COLOR, -1)
        cv2.putText(img, label, (tag_x, tag_y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, TEXT_COLOR, 1, cv2.LINE_AA)

    cv2.imwrite(str(output_path), img)
    return output_path, len(detections)


def extract_candidate_crops(
    slide_or_roi_path: Path,
    detections: List[Dict[str, Any]],
    slide_id: int,
    analysis_id_str: str,
    roi_x: int = 0,
    roi_y: int = 0,
    crop_size: int = 120,
    is_wsi: bool = False,
) -> Dict[str, Any]:
    """Extract and save 120x120 crops for each detection candidate."""
    gallery_items = []
    slide_or_roi_path = Path(slide_or_roi_path)

    img_source = None
    slide_obj = None

    if is_wsi:
        try:
            import openslide
            slide_obj = openslide.OpenSlide(str(slide_or_roi_path))
        except Exception:
            slide_obj = None

    if slide_obj is None:
        try:
            img_source = Image.open(str(slide_or_roi_path)).convert("RGB")
        except Exception as e:
            logger.error("Failed to open source image for candidate crops: %s", e)
            return {"items": [], "total": 0}

    half = crop_size // 2

    for det in detections:
        det_id = det["id"]
        cx = int(round(det["center_level0"]["x"]))
        cy = int(round(det["center_level0"]["y"]))
        score = det["score"]

        crop_filename = f"cand_s{slide_id}_{analysis_id_str}_{det_id}.png"
        crop_path = AI_GALLERY_DIR / crop_filename

        try:
            if slide_obj is not None:
                x0 = max(0, cx - half)
                y0 = max(0, cy - half)
                crop_pil = slide_obj.read_region((x0, y0), 0, (crop_size, crop_size)).convert("RGB")
            elif img_source is not None:
                rx = int(round(det.get("roi_coordinates", {}).get("x", cx - roi_x)))
                ry = int(round(det.get("roi_coordinates", {}).get("y", cy - roi_y)))
                x0 = max(0, rx - half)
                y0 = max(0, ry - half)
                x1 = min(img_source.width, rx + half)
                y1 = min(img_source.height, ry + half)
                crop_pil = img_source.crop((x0, y0, x1, y1))
                if crop_pil.size != (crop_size, crop_size):
                    crop_pil = crop_pil.resize((crop_size, crop_size), Image.Resampling.BILINEAR)

            # Draw small crosshair marker in crop center
            draw_crop = crop_pil.copy()
            draw_np = np.array(draw_crop)
            ch = crop_size // 2
            cv2.circle(draw_np, (ch, ch), 8, (225, 29, 72), 1, cv2.LINE_AA)
            Image.fromarray(draw_np).save(str(crop_path), format="PNG")

            gallery_items.append({
                "id": det_id,
                "score": score,
                "x": cx,
                "y": cy,
                "roi_x": det.get("roi_coordinates", {}).get("x", cx - roi_x),
                "roi_y": det.get("roi_coordinates", {}).get("y", cy - roi_y),
                "filename": crop_filename,
                "image_url": f"/api/ai/analyses/placeholder/gallery/{crop_filename}",
            })
            det["crop_filename"] = crop_filename
        except Exception as crop_exc:
            logger.warning("Could not extract crop for detection %s: %s", det_id, crop_exc)

    if slide_obj is not None:
        slide_obj.close()

    return {
        "items": gallery_items,
        "total": len(gallery_items),
    }


# =============================================================================
# REPORTLAB PDF GENERATION
# =============================================================================

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: List[Dict[str, Any]] = []
        self.report_analysis_id = "N/A"
        self.report_slide_name = ""

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_decorations(num_pages)
            super().showPage()
        super().save()

    def _draw_decorations(self, total_pages: int):
        self.saveState()

        # Running header (pages > 1)
        if self._pageNumber > 1:
            self.setFont("Helvetica-Bold", 8)
            self.setFillColor(COLOR_NAVY_DARK)
            self.drawString(36, 756, "KongNet-Det — Mitotic Figure Detection Report")
            self.setFont("Helvetica", 8)
            self.setFillColor(COLOR_TEXT_MUTED)
            self.drawRightString(576, 756, f"Analysis: {self.report_analysis_id}")
            self.setStrokeColor(COLOR_BORDER_LIGHT)
            self.setLineWidth(0.6)
            self.line(36, 750, 576, 750)

        # Running footer (all pages)
        self.setStrokeColor(COLOR_BORDER_LIGHT)
        self.setLineWidth(0.6)
        self.line(36, 42, 576, 42)

        self.setFont("Helvetica-Bold", 7.5)
        self.setFillColor(COLOR_RED_ALERT)
        self.drawString(36, 30, "RESEARCH USE ONLY — NOT FOR CLINICAL DIAGNOSIS")

        if self.report_slide_name:
            self.setFont("Helvetica", 7.5)
            self.setFillColor(COLOR_TEXT_MUTED)
            self.drawCentredString(306, 30, f"Slide: {self.report_slide_name}")

        self.setFont("Helvetica", 8)
        self.setFillColor(COLOR_TEXT_SECONDARY)
        self.drawRightString(576, 30, f"Page {self._pageNumber} of {total_pages}")

        self.restoreState()


def build_kongnet_pdf(
    output_path: Path,
    slide_id: int,
    case_identifier: str,
    slide_filename: str,
    roi_info: dict,
    summary: dict,
    detections: list,
    gallery: dict,
    roi_image_path: Optional[Path] = None,
    overlay_image_path: Optional[Path] = None,
    analysis_id: Optional[int] = None,
):
    """Generate high-quality multi-page PDF report for KongNet-Det analysis."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=COLOR_NAVY_DARK,
        spaceAfter=3,
    )
    style_subtitle = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=COLOR_TEXT_SECONDARY,
        spaceAfter=8,
    )
    style_h2 = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=15,
        textColor=COLOR_NAVY_PRIMARY,
        spaceBefore=6,
        spaceAfter=4,
    )
    style_cell = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10.5,
        textColor=COLOR_TEXT_PRIMARY,
    )
    style_cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=style_cell,
        fontName="Helvetica-Bold",
    )
    style_disclaimer_title = ParagraphStyle(
        "DisclaimerTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=COLOR_RED_ALERT,
        spaceAfter=2,
    )
    style_disclaimer_body = ParagraphStyle(
        "DisclaimerBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=10,
        textColor=COLOR_TEXT_SECONDARY,
        alignment=TA_JUSTIFY,
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=46,
        bottomMargin=48,
    )

    story = []

    # Title & Subtitle
    story.append(Paragraph("KongNet-Det — Mitotic Figure Detection Analysis", style_title))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    analysis_tag = f"#{analysis_id}" if analysis_id else "Preliminary"
    story.append(
        Paragraph(
            f"Case: <b>{case_identifier}</b> &nbsp;|&nbsp; Slide ID: <b>{slide_id}</b> &nbsp;|&nbsp; "
            f"Analysis: <b>{analysis_tag}</b> &nbsp;|&nbsp; Generated: <b>{now_str}</b>",
            style_subtitle,
        )
    )
    story.append(HRFlowable(width="100%", thickness=1.5, color=COLOR_NAVY_ACCENT, spaceAfter=8))

    # Metadata & Parameters Table
    w = roi_info.get("width", 0)
    h = roi_info.get("height", 0)
    x = roi_info.get("x", 0)
    y = roi_info.get("y", 0)

    meta_data = [
        [
            Paragraph("<b>Specimen Case ID:</b>", style_cell),
            Paragraph(str(case_identifier), style_cell),
            Paragraph("<b>Model Architecture:</b>", style_cell),
            Paragraph("KongNet-Det (EfficientNetV2-L)", style_cell),
        ],
        [
            Paragraph("<b>Slide File:</b>", style_cell),
            Paragraph(str(slide_filename), style_cell),
            Paragraph("<b>Checkpoint / Weights:</b>", style_cell),
            Paragraph("KongNet_Det_MIDOG_1.pth (CC BY-NC-SA 4.0)", style_cell),
        ],
        [
            Paragraph("<b>Analyzed Region:</b>", style_cell),
            Paragraph(f"{w} × {h} px at ({x}, {y})", style_cell),
            Paragraph("<b>Target Resolution:</b>", style_cell),
            Paragraph(f"{summary.get('target_mpp', 0.25):.2f} µm/pixel (MPP)", style_cell),
        ],
        [
            Paragraph("<b>Analyzed Area:</b>", style_cell),
            Paragraph(f"{summary.get('analyzed_area_mm2', 0.0):.4f} mm²", style_cell),
            Paragraph("<b>Detection Threshold:</b>", style_cell),
            Paragraph(f"{summary.get('threshold', 0.50):.2f} (uncalibrated score)", style_cell),
        ],
    ]
    t_meta = Table(meta_data, colWidths=[110, 160, 120, 150])
    t_meta.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_CARD),
            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER_LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ])
    )
    story.append(t_meta)
    story.append(Spacer(1, 8))

    # Quantitative Mitotic Summary Metrics
    cand_count = summary.get("candidate_count", len(detections))
    density = summary.get("candidate_density_per_mm2", 0.0)
    exec_sec = summary.get("execution_time_seconds", 0.0)

    story.append(Paragraph("Quantitative Mitotic Assessment", style_h2))
    kpi_data = [
        [
            Paragraph(f"<font size=16 color='{COLOR_ROSE_MITOSIS.hexval()}'><b>{cand_count}</b></font><br/><b>Mitotic Candidates</b>", style_cell),
            Paragraph(f"<font size=16 color='{COLOR_NAVY_PRIMARY.hexval()}'><b>{density:.2f}</b></font><br/><b>Candidates / mm²</b>", style_cell),
            Paragraph(f"<font size=16 color='{COLOR_NAVY_DARK.hexval()}'><b>{summary.get('mean_score', 0.0):.3f}</b></font><br/><b>Mean Raw Score</b>", style_cell),
            Paragraph(f"<font size=16 color='{COLOR_TEXT_SECONDARY.hexval()}'><b>{exec_sec:.1f}s</b></font><br/><b>Inference Duration</b>", style_cell),
        ]
    ]
    t_kpi = Table(kpi_data, colWidths=[135, 135, 135, 135])
    t_kpi.setStyle(
        TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_CARD),
            ("BOX", (0, 0), (-1, -1), 1, COLOR_NAVY_ACCENT),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER_LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    story.append(t_kpi)
    story.append(Spacer(1, 10))

    # Visual Inspection Images (ROI Preview & Annotated Overlay)
    if roi_image_path and Path(roi_image_path).is_file() and overlay_image_path and Path(overlay_image_path).is_file():
        story.append(Paragraph("Region of Interest & Detection Overlays", style_h2))
        try:
            im_roi = RLImage(str(roi_image_path), width=260, height=180)
            im_overlay = RLImage(str(overlay_image_path), width=260, height=180)
            t_images = Table(
                [
                    [Paragraph("<b>Extracted ROI (H&amp;E)</b>", style_cell), Paragraph("<b>Mitotic Detection Markers</b>", style_cell)],
                    [im_roi, im_overlay],
                ],
                colWidths=[270, 270],
            )
            t_images.setStyle(
                TableStyle([
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ])
            )
            story.append(t_images)
            story.append(Spacer(1, 8))
        except Exception as img_err:
            logger.warning("Could not add ROI images to PDF: %s", img_err)

    # Candidate Gallery Grid (Up to 18 top candidates)
    gallery_items = gallery.get("items", [])
    if gallery_items:
        story.append(Paragraph(f"Mitotic Candidate Gallery (Top {min(len(gallery_items), 18)} Candidates)", style_h2))
        grid_rows = []
        curr_row = []
        num_cols = 6
        col_w = 90

        for item in gallery_items[:18]:
            crop_file = AI_GALLERY_DIR / item["filename"]
            cell_items = []
            if crop_file.is_file():
                try:
                    rl_crop = RLImage(str(crop_file), width=70, height=70)
                    cell_items.append(rl_crop)
                except Exception:
                    cell_items.append(Paragraph("Crop", style_cell))
            else:
                cell_items.append(Paragraph("N/A", style_cell))

            cell_items.append(Paragraph(f"<b>{item['id']}</b>", style_cell))
            cell_items.append(Paragraph(f"Score: <b>{item['score']:.3f}</b>", style_cell))
            curr_row.append(cell_items)

            if len(curr_row) == num_cols:
                grid_rows.append(curr_row)
                curr_row = []

        if curr_row:
            while len(curr_row) < num_cols:
                curr_row.append([Paragraph("", style_cell)])
            grid_rows.append(curr_row)

        t_gal = Table(grid_rows, colWidths=[col_w] * num_cols)
        t_gal.setStyle(
            TableStyle([
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_CARD),
                ("BOX", (0, 0), (-1, -1), 0.5, COLOR_BORDER_LIGHT),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_BORDER_LIGHT),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 1),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ])
        )
        story.append(t_gal)
        story.append(Spacer(1, 10))

    # Regulatory Notice & Model Restrictions
    story.append(HRFlowable(width="100%", thickness=0.8, color=COLOR_BORDER_LIGHT, spaceAfter=6))
    story.append(Paragraph("REGULATORY & RESEARCH USE NOTICE", style_disclaimer_title))
    notice_text = (
        "This report was generated using the KongNet-Det artificial intelligence model trained on the MIDOG dataset. "
        "Detection scores reflect raw model probability outputs [0, 1] and must NOT be interpreted as calibrated clinical "
        "accuracy percentages or definitive diagnoses. This software and its outputs are for RESEARCH USE ONLY and are "
        "NOT cleared or approved by any regulatory authority for diagnostic procedures. All mitotic candidate locations "
        "and densities must be independently verified by a licensed pathologist."
    )
    story.append(Paragraph(notice_text, style_disclaimer_body))

    # Build document
    canvas_maker = NumberedCanvas
    canvas_maker.report_analysis_id = str(analysis_id or "Preliminary")
    canvas_maker.report_slide_name = str(slide_filename)

    doc.build(story, canvasmaker=canvas_maker)
    logger.info("KongNet PDF report built successfully: %s", output_path)
    return output_path


# =============================================================================
# SQL PERSISTENCE
# =============================================================================

def save_kongnet_analysis_record(
    connect_func,
    slide_row: dict,
    case_row: dict,
    user_username: str,
    roi_info: dict,
    summary: dict,
    detections: list,
    gallery: dict,
    roi_filename: str,
    overlay_filename: str,
    report_filename: Optional[str] = None,
) -> int:
    """Inserts completed KongNet analysis into SQL Server table ai_analyses and returns ID."""
    with closing(connect_func()) as db:
        summary_payload = {
            "feature_code": FEATURE_CODE,
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "summary": summary,
            "candidate_count": summary.get("candidate_count", len(detections)),
            "analyzed_area_mm2": summary.get("analyzed_area_mm2"),
            "candidate_density_per_mm2": summary.get("candidate_density_per_mm2"),
            "threshold": summary.get("threshold"),
            "min_score": summary.get("min_score"),
            "mean_score": summary.get("mean_score"),
            "max_score": summary.get("max_score"),
            "execution_time_seconds": summary.get("execution_time_seconds"),
        }

        predictions_payload = {
            "model": MODEL_NAME,
            "version": MODEL_VERSION,
            "detections": detections,
            "total_candidates": len(detections),
        }

        cursor = db.execute(
            """INSERT INTO ai_analyses (
                slide_id, case_id, requested_by, model_name, model_version, status,
                roi_x, roi_y, roi_width, roi_height, roi_level,
                total_nuclei, neoplastic_count, inflammatory_count, connective_count,
                necrotic_count, non_neoplastic_count, no_label_count,
                nuclear_density, avg_confidence,
                summary_json, predictions_json, gallery_json,
                roi_image_filename, overlay_image_filename, report_pdf_filename,
                disclaimer, completed_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'Completed',
                ?, ?, ?, ?, ?,
                ?, 0, 0, 0,
                0, 0, 0,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, SYSUTCDATETIME()
            )""",
            (
                slide_row["id"],
                case_row["id"],
                user_username,
                MODEL_NAME,
                MODEL_VERSION,
                roi_info["x"],
                roi_info["y"],
                roi_info["width"],
                roi_info["height"],
                roi_info.get("level", 0),
                summary.get("candidate_count", len(detections)),
                summary.get("candidate_density_per_mm2"),
                summary.get("mean_score"),
                json.dumps(summary_payload),
                json.dumps(predictions_payload),
                json.dumps(gallery),
                roi_filename,
                overlay_filename,
                report_filename,
                DISCLAIMER_TEXT,
            ),
        )
        analysis_id = cursor.lastrowid
        db.commit()
        return analysis_id


def init_kongnet_schema(connect_func):
    """Seed MITOTIC_DETECTION in ai_features table as implemented."""
    with closing(connect_func()) as db:
        db.execute(
            """IF NOT EXISTS (SELECT 1 FROM ai_features WHERE feature_code = 'MITOTIC_DETECTION')
            INSERT INTO ai_features (
                feature_code, feature_name, description, is_implemented, is_active,
                catalog_category, catalog_summary, catalog_purpose, catalog_value,
                catalog_title, catalog_display_order
            ) VALUES (
                'MITOTIC_DETECTION', 'KongNet-Det',
                'Detects candidate mitotic figures using KongNet-Det, provides mitotic candidate count, spatial density per mm², and confidence scores.',
                1, 1,
                'Cell analysis',
                'Identifies candidate mitotic figures for confirmation during microscopic assessment.',
                'Detects cell appearances associated with mitosis and presents candidates for pathologist verification.',
                'Supports efficient mitotic assessment while keeping final classification with the pathologist.',
                'KongNet-Det — Mitotic Figure Detection', 2
            )
            ELSE
            UPDATE ai_features SET
                is_implemented = 1,
                catalog_title = COALESCE(catalog_title, 'KongNet-Det — Mitotic Figure Detection')
            WHERE feature_code = 'MITOTIC_DETECTION'"""
        )
        db.commit()

