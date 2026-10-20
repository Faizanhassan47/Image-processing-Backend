"""
Cases and Slides API.
Handles:
- Pathology Cases (CRUD, status, assign reviewer, review notes, review history)
- Whole Slide Images (WSI tiles, DeepZoom DZI, thumbnails, raster images, and QC)
"""

from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import pyodbc

from core.config import (
    RASTER_MAX_BYTES,
    TILE_OVERLAP,
    TILE_SIZE,
    UPLOAD_CHUNK_BYTES,
    WSI_MAX_BYTES,
)
from core.security import (
    ADMIN_ROLES,
    CLINICAL_READ_ROLES,
    INTAKE_ROLES,
    ROLE_PATHOLOGIST,
)
from services.image_validation import ImageValidationError, validate_image_file
from services.slide_cache import (
    DeepZoomGenerator,
    get_cached_tile,
    get_cached_wsi_slide_helper,
    openslide,
    set_cached_tile,
    slide_cache,
)


class ReviewerChange(BaseModel):
    reviewer: str = Field(default="", max_length=120)


class StatusChange(BaseModel):
    status: str


class SlideQcUpdate(BaseModel):
    qc_status: str | None = None
    qc_missing_tissue: bool = False
    qc_blur: bool = False
    qc_scan_artifact: bool = False
    qc_incorrect_label: bool = False
    qc_unusual_staining: bool = False
    qc_note: str = Field(default="", max_length=4000)


class ReviewNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


class SlideUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    stain: str = Field(default="", max_length=120)
    specimen_description: str = Field(default="", max_length=1000)


def create_cases_router(
    connect,
    require_roles,
    get_current_user,
    uploads_dir: Path,
    get_case_fn,
    get_slide_fn,
    require_assigned_case_fn,
    stored_path_fn,
    audit,
):
    router = APIRouter()
    pathologist_and_ops = INTAKE_ROLES + [ROLE_PATHOLOGIST]

    def _nonblank(value: str, field_name: str):
        val = value.strip()
        if not val:
            raise HTTPException(400, f"{field_name} cannot be blank")
        return val

    def _validated_case_id(value: str):
        val = value.strip()
        if not val or len(val) > 80:
            raise HTTPException(400, "Case ID must be 1 to 80 characters")
        return val

    def _append_history(db, case_pk, change_type, previous, new, actor):
        db.execute(
            """INSERT INTO case_review_history
               (case_id, change_type, previous_value, new_value, actor)
               VALUES (?, ?, ?, ?, ?)""",
            (case_pk, change_type, previous, new, actor),
        )

    def serialize_slide(row):
        item = dict(row)
        base = f"/api/slides/{item['id']}"
        item["image_url"] = f"{base}/image" if item["file_type"] == "raster" else None
        item["thumbnail_url"] = f"{base}/thumbnail"
        item["dzi_url"] = f"{base}/slide.dzi" if item["file_type"] == "wsi" else None
        for bool_field in ("qc_missing_tissue", "qc_blur", "qc_scan_artifact", "qc_incorrect_label", "qc_unusual_staining"):
            if bool_field in item and item[bool_field] is not None:
                item[bool_field] = bool(item[bool_field])
        return item

    def serialize_case(row, include_slides=True):
        item = dict(row)
        if include_slides:
            with closing(connect()) as db:
                slides = db.execute("SELECT * FROM slides WHERE case_id = ? ORDER BY id", (row["id"],)).fetchall()
                notes = db.execute("SELECT * FROM case_review_notes WHERE case_id = ? ORDER BY id DESC", (row["id"],)).fetchall()
                history = db.execute("SELECT * FROM case_review_history WHERE case_id = ? ORDER BY id DESC", (row["id"],)).fetchall()
            item["slides"] = [serialize_slide(slide) for slide in slides]
            item["slide_count"] = len(slides)
            item["review_notes"] = [dict(note) for note in notes]
            item["review_history"] = [dict(entry) for entry in history]
        return item

    def require_wsi_slide(slide_id: int):
        row = get_slide_fn(slide_id)
        if row["file_type"] != "wsi":
            raise HTTPException(400, "Slide is not a whole-slide image")
        path = stored_path_fn(row["filename"])
        if not path.is_file():
            raise HTTPException(404, "Whole-slide image file is missing")
        if openslide is None:
            raise HTTPException(503, "OpenSlide is unavailable. Install openslide-python and the native OpenSlide library.")
        return row, path

    def get_cached_wsi(slide_id: int):
        return get_cached_wsi_slide_helper(slide_id, require_wsi_slide)

    def first_slide(case_pk: int):
        get_case_fn(case_pk)
        with closing(connect()) as db:
            row = db.execute("SELECT TOP 1 * FROM slides WHERE case_id = ? ORDER BY id", (case_pk,)).fetchone()
        if row is None:
            raise HTTPException(404, "Case has no slides")
        return row

    def raster_response(row):
        if row["file_type"] != "raster":
            raise HTTPException(400, "Whole-slide images are available through the tile API")
        path = stored_path_fn(row["filename"])
        if not path.is_file():
            raise HTTPException(404, "Image file is missing")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    async def _save_stream(upload: UploadFile, path: Path, limit: int):
        written = 0
        try:
            with path.open("xb") as output:
                while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, f"Whole-slide image exceeds the {limit // 1048576} MB limit")
                    output.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        if not written:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "Uploaded file is empty")
        return written

    async def validate_and_store(upload: UploadFile):
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".svs"}:
            await upload.close()
            raise HTTPException(400, "Upload a JPG, PNG, or Aperio SVS file")
        if suffix == ".svs" and openslide is None:
            await upload.close()
            raise HTTPException(503, "WSI upload requires OpenSlide. Install openslide-python or configure OPENSLIDE_PATH.")
        # Storage Guard: Prevent ingestion if storage free space is critically low (< 256 MB)
        try:
            usage = shutil.disk_usage(uploads_dir)
            if usage.free < 256 * 1024 * 1024:
                await upload.close()
                raise HTTPException(507, "Insufficient storage capacity to accept whole-slide images")
        except HTTPException:
            raise
        except Exception:
            pass

        filename = f"{uuid4().hex}{suffix}"
        path = stored_path_fn(filename)
        try:
            await _save_stream(upload, path, WSI_MAX_BYTES if suffix == ".svs" else RASTER_MAX_BYTES)
            validation = validate_image_file(path, openslide)
        except ImageValidationError as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return filename, validation

    async def add_slide_record(case_pk, image, label, stain="", specimen_description=""):
        get_case_fn(case_pk)
        label = label.strip()
        if not label or len(label) > 120:
            raise HTTPException(400, "Slide label must be 1 to 120 characters")
        stored = await validate_and_store(image)
        filename, validation = stored
        qc_meta = validation.get("image_qc") or {}
        qc_status_init = "Flagged" if qc_meta.get("qc_status") == "Flagged" else "Passed"
        qc_blur_flag = 1 if not qc_meta.get("is_sharp", True) else 0
        qc_tissue_flag = 1 if not qc_meta.get("has_tissue", True) else 0
        qc_note_init = "; ".join(qc_meta.get("flags") or []) if qc_meta.get("flags") else "Automated optical QC passed"
        try:
            with closing(connect()) as db:
                duplicate = db.execute("""
                    SELECT TOP 1 iv.stored_filename, c.case_id AS case_code
                    FROM image_versions iv
                    JOIN slides s ON s.id = iv.slide_id
                    JOIN cases c ON c.id = s.case_id
                    WHERE iv.checksum_sha256 = ?
                """, (validation["checksum_sha256"],)).fetchone()
                if duplicate:
                    case_ref = duplicate["case_code"] if duplicate.get("case_code") else "another active case"
                    raise HTTPException(409, f"Duplicate image content already exists in Case '{case_ref}'")
                cursor = db.execute("""INSERT INTO slides
                    (case_id, label, stain, specimen_description, filename, file_type,
                     format, file_size, width, height, level_count, upload_state,
                     qc_status, qc_blur, qc_missing_tissue, qc_note, qc_actor, qc_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, 'System Automated QC', SYSUTCDATETIME())""",
                    (case_pk, label, stain.strip(), specimen_description.strip(), filename,
                     validation["file_type"], validation["format"], validation["file_size"],
                     validation["width"], validation["height"], validation["level_count"],
                     qc_status_init, qc_blur_flag, qc_tissue_flag, qc_note_init))
                calibration = validation["physical_calibration"]
                pixel_size = calibration["pixel_size_microns_x"] if calibration["available"] else None
                db.execute("""INSERT INTO image_versions
                    (slide_id,version,checksum_sha256,pixel_size_microns,metadata_json,stored_filename)
                    VALUES (?,1,?,?,?,?)""",
                    (cursor.lastrowid, validation["checksum_sha256"], pixel_size, json.dumps(validation), filename))
                db.commit()
                row = db.execute("SELECT * FROM slides WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except Exception:
            stored_path_fn(filename).unlink(missing_ok=True)
            raise
        return row

    # --- Cases Endpoints ---

    @router.get("/api/cases", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_cases(
        status_filter: str | None = Query(None, alias="status"),
        reviewer: str | None = Query(None),
        search: str | None = Query(None),
        current_user: dict = Depends(get_current_user),
    ):
        clauses = []
        params = []
        if current_user["role"] == "Pathologist":
            clauses.append("(LOWER(CAST(reviewer AS NVARCHAR(MAX))) = LOWER(?) OR LOWER(CAST(reviewer AS NVARCHAR(MAX))) = LOWER(?))")
            params.extend([current_user["username"], current_user["display_name"]])
        if status_filter:
            if status_filter not in {"New", "In Review", "Reviewed"}:
                raise HTTPException(400, "Status must be New, In Review, or Reviewed")
            clauses.append("status = ?")
            params.append(status_filter)

        if reviewer is not None:
            if reviewer.strip() == "":
                clauses.append("reviewer = ''")
            else:
                clauses.append("LOWER(CAST(reviewer AS NVARCHAR(MAX))) = LOWER(?)")
                params.append(reviewer.strip())

        if search and search.strip():
            search_term = f"%{search.strip()}%"
            clauses.append("(case_id LIKE ? OR description LIKE ?)")
            params.extend([search_term, search_term])

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(connect()) as db:
            rows = db.execute(f"SELECT * FROM cases{where} ORDER BY id DESC", params).fetchall()
        return [serialize_case(row) for row in rows]

    @router.get("/api/cases/{case_pk}", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def case_detail(case_pk: int, current_user: dict = Depends(get_current_user)):
        case = get_case_fn(case_pk)
        require_assigned_case_fn(case, current_user)
        return serialize_case(case)

    @router.post("/api/cases", status_code=201, dependencies=[Depends(require_roles(INTAKE_ROLES))])
    async def create_case(
        case_id: str = Form(...),
        description: str = Form(""),
        image: UploadFile | None = File(None),
        slide_label: str = Form("Slide 1"),
        stain: str = Form(""),
        specimen_description: str = Form(""),
    ):
        case_code = _validated_case_id(case_id)
        try:
            with closing(connect()) as db:
                cursor = db.execute(
                    """INSERT INTO cases
                       (case_id, description, filename, file_type, format, width, height, file_size)
                       VALUES (?, ?, '', 'raster', NULL, NULL, NULL, NULL)""",
                    (case_code, description.strip()),
                )
                db.commit()
                case_pk = cursor.lastrowid
        except pyodbc.IntegrityError as exc:
            raise HTTPException(409, "Case ID already exists") from exc

        try:
            if image is not None and image.filename:
                slide = await add_slide_record(case_pk, image, slide_label, stain, specimen_description)
                with closing(connect()) as db:
                    db.execute(
                        """UPDATE cases SET filename=?, file_type=?, format=?, width=?, height=?, file_size=?
                           WHERE id=?""",
                        (slide["filename"], slide["file_type"], slide["format"], slide["width"],
                         slide["height"], slide["file_size"], case_pk),
                    )
                    db.commit()
        except Exception:
            with closing(connect()) as db:
                db.execute("DELETE FROM cases WHERE id = ?", (case_pk,))
                db.commit()
            raise
        return serialize_case(get_case_fn(case_pk))

    @router.patch("/api/cases/{case_pk}/reviewer", dependencies=[Depends(require_roles(INTAKE_ROLES))])
    def assign_reviewer(case_pk: int, change: ReviewerChange, current_user: dict = Depends(get_current_user)):
        case = get_case_fn(case_pk)
        actor = current_user["display_name"]
        reviewer = change.reviewer.strip()
        previous = case["reviewer"]
        if reviewer == previous:
            raise HTTPException(400, "Reviewer is already set to that value")
        with closing(connect()) as db:
            db.execute("UPDATE cases SET reviewer = ? WHERE id = ?", (reviewer, case_pk))
            _append_history(db, case_pk, "reviewer", previous, reviewer, actor)
            db.commit()
        return serialize_case(get_case_fn(case_pk))

    @router.patch("/api/cases/{case_pk}/status", dependencies=[Depends(require_roles(pathologist_and_ops))])
    def change_case_status(case_pk: int, change: StatusChange, current_user: dict = Depends(get_current_user)):
        case = get_case_fn(case_pk)
        require_assigned_case_fn(case, current_user)
        actor = current_user["display_name"]
        new_status = change.status.strip()
        valid_statuses = {"New", "In Review", "Reviewed"}
        if new_status not in valid_statuses:
            raise HTTPException(400, "Status must be New, In Review, or Reviewed")
        current = case["status"]
        if new_status == current:
            return serialize_case(case)
        if new_status == "In Review":
            with closing(connect()) as db:
                needs_rescan = db.execute("SELECT COUNT(*) AS count FROM slides WHERE case_id = ? AND qc_status = 'Needs rescan'", (case_pk,)).fetchone()['count']
                if needs_rescan > 0:
                    raise HTTPException(409, "Cannot mark case ready for review while a slide needs a rescan")
        with closing(connect()) as db:
            db.execute("UPDATE cases SET status = ? WHERE id = ?", (new_status, case_pk))
            _append_history(db, case_pk, "status", current, new_status, actor)
            db.commit()
        return serialize_case(get_case_fn(case_pk))

    @router.get("/api/cases/{case_pk}/review-notes", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_review_notes(case_pk: int, current_user: dict = Depends(get_current_user)):
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        with closing(connect()) as db:
            rows = db.execute("SELECT * FROM case_review_notes WHERE case_id = ? ORDER BY id DESC", (case_pk,)).fetchall()
        return [dict(row) for row in rows]

    @router.post("/api/cases/{case_pk}/review-notes", status_code=201, dependencies=[Depends(require_roles(ADMIN_ROLES + [ROLE_PATHOLOGIST]))])
    def add_review_note(case_pk: int, review_note: ReviewNoteCreate, current_user: dict = Depends(get_current_user)):
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        author = current_user["display_name"]
        note = _nonblank(review_note.note, "Review note")
        with closing(connect()) as db:
            cursor = db.execute(
                "INSERT INTO case_review_notes (case_id, author, note) VALUES (?, ?, ?)",
                (case_pk, author, note),
            )
            db.commit()
            row = db.execute("SELECT * FROM case_review_notes WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    @router.get("/api/cases/{case_pk}/review-history", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_review_history(case_pk: int, current_user: dict = Depends(get_current_user)):
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        with closing(connect()) as db:
            rows = db.execute("SELECT * FROM case_review_history WHERE case_id = ? ORDER BY id DESC", (case_pk,)).fetchall()
        return [dict(row) for row in rows]

    # --- Slides Endpoints ---

    @router.post("/api/cases/{case_pk}/slides", status_code=201, dependencies=[Depends(require_roles(INTAKE_ROLES))])
    async def add_slide(
        case_pk: int,
        image: UploadFile = File(...),
        label: str = Form(...),
        stain: str = Form(""),
        specimen_description: str = Form(""),
    ):
        return serialize_slide(await add_slide_record(case_pk, image, label, stain, specimen_description))

    @router.patch("/api/slides/{slide_id}", dependencies=[Depends(require_roles(INTAKE_ROLES))])
    def update_slide(slide_id: int, update: SlideUpdate):
        get_slide_fn(slide_id)
        label = update.label.strip()
        if not label:
            raise HTTPException(400, "Slide label cannot be blank")
        with closing(connect()) as db:
            db.execute(
                """UPDATE slides SET label=?, stain=?, specimen_description=? WHERE id=?""",
                (label, update.stain.strip(), update.specimen_description.strip(), slide_id),
            )
            db.commit()
            row = db.execute("SELECT * FROM slides WHERE id = ?", (slide_id,)).fetchone()
        return serialize_slide(row)

    @router.post("/api/slides/{slide_pk}/qc", status_code=200, dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def add_slide_qc(slide_pk: int, qc: SlideQcUpdate, current_user: dict = Depends(get_current_user)):
        get_slide_fn(slide_pk)
        actor = current_user["display_name"]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with closing(connect()) as db:
            db.execute(
                """UPDATE slides SET 
                   qc_status = ?, qc_missing_tissue = ?, qc_blur = ?, 
                   qc_scan_artifact = ?, qc_incorrect_label = ?, qc_unusual_staining = ?, qc_note = ?, 
                   qc_actor = ?, qc_timestamp = ? 
                   WHERE id = ?""",
                (qc.qc_status, qc.qc_missing_tissue, qc.qc_blur,
                 qc.qc_scan_artifact, qc.qc_incorrect_label, qc.qc_unusual_staining, qc.qc_note,
                 actor, now, slide_pk),
            )
            db.execute(
                """INSERT INTO slide_qc_history (
                   slide_id, qc_status, qc_missing_tissue, qc_blur, 
                   qc_scan_artifact, qc_incorrect_label, qc_unusual_staining, qc_note, actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slide_pk, qc.qc_status, qc.qc_missing_tissue, qc.qc_blur,
                 qc.qc_scan_artifact, qc.qc_incorrect_label, qc.qc_unusual_staining, qc.qc_note, actor),
            )
            db.commit()
        return serialize_slide(get_slide_fn(slide_pk))

    @router.get("/api/slides/{slide_id}/validation", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_validation(slide_id: int):
        get_slide_fn(slide_id)
        with closing(connect()) as db:
            version = db.execute("SELECT TOP 1 * FROM image_versions WHERE slide_id=? ORDER BY version DESC", (slide_id,)).fetchone()
        if version is None:
            raise HTTPException(404, "No validation record exists for this slide")
        result = json.loads(version["metadata_json"] or "{}")
        result["image_version"] = version["version"]
        return result

    @router.post("/api/slides/{slide_id}/versions", status_code=201, dependencies=[Depends(require_roles(INTAKE_ROLES))])
    async def reimport_slide_version(slide_id: int, image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
        get_slide_fn(slide_id)
        filename, validation = await validate_and_store(image)
        try:
            with closing(connect()) as db:
                previous = db.execute("SELECT TOP 1 version, checksum_sha256 FROM image_versions WHERE slide_id=? ORDER BY version DESC", (slide_id,)).fetchone()
                next_version = (previous["version"] if previous else 0) + 1
                validation["duplicate_of_previous_version"] = bool(previous and previous["checksum_sha256"] == validation["checksum_sha256"])
                calibration = validation["physical_calibration"]
                cursor = db.execute(
                    """INSERT INTO image_versions
                       (slide_id, version, checksum_sha256, source_device, pixel_size_microns, metadata_json, stored_filename)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (slide_id, next_version, validation["checksum_sha256"], "manual-reimport",
                     calibration["pixel_size_microns_x"] if calibration["available"] else None, json.dumps(validation), filename),
                )
                audit(db, current_user["username"], "image.version_import", "slide", slide_id, f"version={next_version}; image_version_id={cursor.lastrowid}")
                db.commit()
            return {"id": cursor.lastrowid, "slide_id": slide_id, "version": next_version, "stored_filename": filename, "validation": validation}
        except Exception:
            stored_path_fn(filename).unlink(missing_ok=True)
            raise

    # --- WSI DeepZoom & Image Rendering Endpoints ---

    @router.get("/api/slides/{slide_id}/image", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_image(slide_id: int, current_user: dict = Depends(get_current_user)):
        slide = get_slide_fn(slide_id)
        require_assigned_case_fn(get_case_fn(slide["case_id"]), current_user)
        return raster_response(slide)

    @router.get("/api/slides/{slide_id}/thumbnail", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_thumbnail(slide_id: int, current_user: dict = Depends(get_current_user)):
        row = get_slide_fn(slide_id)
        require_assigned_case_fn(get_case_fn(row["case_id"]), current_user)
        if row["file_type"] == "raster":
            return raster_response(row)
        _, path = require_wsi_slide(slide_id)
        with slide_cache.acquire(path) as slide:
            thumbnail = slide.get_thumbnail((512, 512)).convert("RGB")
            output = BytesIO()
            thumbnail.save(output, "JPEG", quality=82)
        return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/api/slides/{slide_id}/slide", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_metadata(slide_id: int, current_user: dict = Depends(get_current_user)):
        row, path = require_wsi_slide(slide_id)
        require_assigned_case_fn(get_case_fn(row["case_id"]), current_user)
        with slide_cache.acquire(path) as slide:
            deep_zoom = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP)
            return {
                "id": row["id"],
                "format": row["format"],
                "width": row["width"],
                "height": row["height"],
                "level_count": deep_zoom.level_count,
                "tile_size": TILE_SIZE,
                "overlap": TILE_OVERLAP,
                "dzi_url": f"/api/slides/{slide_id}/slide.dzi",
            }

    @router.get("/api/slides/{slide_id}/slide.dzi", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_descriptor(slide_id: int, current_user: dict = Depends(get_current_user)):
        row, path = require_wsi_slide(slide_id)
        require_assigned_case_fn(get_case_fn(row["case_id"]), current_user)
        with slide_cache.acquire(path) as slide:
            descriptor = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP).get_dzi("jpeg")
        return Response(descriptor, media_type="application/xml", headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/api/slides/{slide_id}/slide_files/{level}/{column}_{row}.jpeg", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def slide_tile(slide_id: int, level: int, column: int, row: int, current_user: dict = Depends(get_current_user)):
        slide = get_slide_fn(slide_id)
        require_assigned_case_fn(get_case_fn(slide["case_id"]), current_user)
        if min(level, column, row) < 0:
            raise HTTPException(404, "Tile not found")

        cached_tile = get_cached_tile(slide_id, level, column, row)
        if cached_tile is not None:
            return Response(cached_tile, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

        _, path = get_cached_wsi(slide_id)
        try:
            with slide_cache.acquire_generator(path) as deep_zoom:
                if level >= deep_zoom.level_count:
                    raise HTTPException(404, "Tile not found")
                tiles_x, tiles_y = deep_zoom.level_tiles[level]
                if column >= tiles_x or row >= tiles_y:
                    raise HTTPException(404, "Tile not found")
                tile = deep_zoom.get_tile(level, (column, row)).convert("RGB")

            output = BytesIO()
            tile.save(output, "JPEG", quality=85)
            jpeg_data = output.getvalue()
            set_cached_tile(slide_id, level, column, row, jpeg_data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"Could not render slide tile: {exc}") from exc
        return Response(jpeg_data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # --- Legacy Case-Level Resolution Aliases ---

    @router.get("/api/cases/{case_pk}/image", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def legacy_image(case_pk: int, current_user: dict = Depends(get_current_user)):
        slide = first_slide(case_pk)
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        return raster_response(slide)

    @router.get("/api/cases/{case_pk}/thumbnail", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def legacy_thumbnail(case_pk: int, current_user: dict = Depends(get_current_user)):
        return slide_thumbnail(first_slide(case_pk)["id"], current_user=current_user)

    @router.get("/api/cases/{case_pk}/slide", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def legacy_metadata(case_pk: int, current_user: dict = Depends(get_current_user)):
        return slide_metadata(first_slide(case_pk)["id"], current_user=current_user)

    @router.get("/api/cases/{case_pk}/slide.dzi", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def legacy_descriptor(case_pk: int, current_user: dict = Depends(get_current_user)):
        return slide_descriptor(first_slide(case_pk)["id"], current_user=current_user)

    @router.get("/api/cases/{case_pk}/slide_files/{level}/{column}_{row}.jpeg", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def legacy_tile(case_pk: int, level: int, column: int, row: int, current_user: dict = Depends(get_current_user)):
        return slide_tile(first_slide(case_pk)["id"], level, column, row, current_user=current_user)

    return router
