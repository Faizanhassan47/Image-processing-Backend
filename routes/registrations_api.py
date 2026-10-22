"""
Multi-Slide Registration and Advanced Viewer API.
Provides endpoints for:
- Slide viewing trails & coverage tracking (persistence, clear, retrieval)
- Slide registrations CRUD (draft, calculated, reviewed, rejected)
- Manual landmark calculate & review
- Automatic alignment (OpenCV feature matching + RANSAC)
- Registration audit history & status lifecycle
"""

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.security import (
    ADMIN_ROLES,
    CLINICAL_READ_ROLES,
    ROLE_PATHOLOGIST,
)
from services.registration_service import (
    RegistrationError,
    estimate_manual_registration,
    run_automatic_registration,
)

logger = logging.getLogger(__name__)

# Request & Response Models
class ViewingTrailUpdate(BaseModel):
    coverage_boxes: List[Dict[str, float]] = Field(default_factory=list)
    total_viewed_area_px: Optional[float] = None
    last_center_x: Optional[float] = None
    last_center_y: Optional[float] = None
    last_zoom: Optional[float] = None


class LandmarkItem(BaseModel):
    id: Optional[int] = None
    ref_x: float
    ref_y: float
    mov_x: float
    mov_y: float
    display_index: Optional[int] = 1
    label: Optional[str] = ""


class RegistrationCreate(BaseModel):
    case_id: int
    reference_slide_id: int
    moving_slide_id: int
    registration_method: str = Field(default="manual", pattern=r"^(manual|automatic)$")
    transform_type: str = Field(default="affine", pattern=r"^(translation|rigid|similarity|affine)$")
    landmarks: List[LandmarkItem] = Field(default_factory=list)


class RegistrationUpdate(BaseModel):
    transform_type: Optional[str] = Field(None, pattern=r"^(translation|rigid|similarity|affine)$")
    landmarks: Optional[List[LandmarkItem]] = None
    status: Optional[str] = Field(None, pattern=r"^(Draft|Calculated|Reviewed|Rejected)$")
    review_notes: Optional[str] = Field(None, max_length=1000)


class LandmarksUpdate(BaseModel):
    landmarks: List[LandmarkItem]


class CalculateAlignmentRequest(BaseModel):
    transform_type: str = Field(default="affine", pattern=r"^(translation|rigid|similarity|affine)$")
    landmarks: Optional[List[LandmarkItem]] = None


class AutoAlignRequest(BaseModel):
    transform_type: str = Field(default="affine", pattern=r"^(translation|rigid|similarity|affine)$")
    max_dimension: Optional[int] = Field(default=1600, ge=800, le=3200)


class ReviewRegistrationRequest(BaseModel):
    status: str = Field(..., pattern=r"^(Reviewed|Rejected)$")
    review_notes: Optional[str] = Field(default="", max_length=1000)


def create_registrations_router(
    connect,
    require_roles,
    get_current_user,
    audit,
    uploads_dir: Path,
    openslide_module=None,
    get_slide_fn=None,
    get_case_fn=None,
    require_assigned_case_fn=None,
    stored_path_fn=None,
):
    router = APIRouter(tags=["Multi-Slide Registration & Viewer Services"])
    pathologist_and_admin = ADMIN_ROLES + [ROLE_PATHOLOGIST]

    def _get_slide_with_access(slide_id: int, current_user: dict):
        slide = get_slide_fn(slide_id)
        case = get_case_fn(slide["case_id"])
        require_assigned_case_fn(case, current_user)
        return slide, case

    def _get_slide_pixel_size(slide_id: int) -> Optional[float]:
        with closing(connect()) as db:
            row = db.execute(
                "SELECT TOP 1 pixel_size_microns FROM image_versions WHERE slide_id = ? ORDER BY version DESC",
                (slide_id,),
            ).fetchone()
            if row and row["pixel_size_microns"]:
                return float(row["pixel_size_microns"])
        return None

    def _get_registration_or_404(reg_id: int, current_user: dict):
        with closing(connect()) as db:
            reg = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not reg:
            raise HTTPException(404, "Registration record not found")
        reg_dict = dict(reg)
        # Check authorization for case
        case = get_case_fn(reg_dict["case_id"])
        require_assigned_case_fn(case, current_user)
        return reg_dict

    def _serialize_registration(row: dict) -> dict:
        item = dict(row)
        for json_col in ("transform_matrix_json", "landmarks_json", "quality_metrics_json"):
            if item.get(json_col):
                try:
                    item[json_col.replace("_json", "")] = json.loads(item[json_col])
                except Exception:
                    item[json_col.replace("_json", "")] = None
            else:
                item[json_col.replace("_json", "")] = None
            item.pop(json_col, None)

        # Check if source images have newer versions since creation
        item["is_stale"] = False
        with closing(connect()) as db:
            latest_ref_v = db.execute("SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC", (item["reference_slide_id"],)).fetchone()
            latest_mov_v = db.execute("SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC", (item["moving_slide_id"],)).fetchone()
            if (latest_ref_v and item.get("reference_image_version_id") and latest_ref_v["id"] != item["reference_image_version_id"]) or \
               (latest_mov_v and item.get("moving_image_version_id") and latest_mov_v["id"] != item["moving_image_version_id"]):
                item["is_stale"] = True
        return item

    def _append_reg_history(db, reg_id: int, action: str, actor: str, details: dict):
        db.execute(
            """INSERT INTO slide_registration_history (registration_id, action, actor, details_json)
               VALUES (?, ?, ?, ?)""",
            (reg_id, action, actor, json.dumps(details)),
        )

    # =========================================================================
    # MODULE 1: VIEWING TRAILS / COVERAGE TRACKING
    # =========================================================================

    @router.get("/api/slides/{slide_id}/trail", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def get_slide_viewing_trail(slide_id: int, current_user: dict = Depends(get_current_user)):
        _get_slide_with_access(slide_id, current_user)
        username = current_user["username"]
        with closing(connect()) as db:
            row = db.execute(
                "SELECT coverage_json, updated_at FROM slide_viewing_trails WHERE slide_id = ? AND username = ?",
                (slide_id, username),
            ).fetchone()
        if not row:
            return {"slide_id": slide_id, "coverage_boxes": [], "updated_at": None}
        try:
            coverage = json.loads(row["coverage_json"])
        except Exception:
            coverage = {}
        return {
            "slide_id": slide_id,
            "coverage_boxes": coverage.get("coverage_boxes", []),
            "total_viewed_area_px": coverage.get("total_viewed_area_px"),
            "last_center_x": coverage.get("last_center_x"),
            "last_center_y": coverage.get("last_center_y"),
            "last_zoom": coverage.get("last_zoom"),
            "updated_at": row["updated_at"],
        }

    @router.put("/api/slides/{slide_id}/trail", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def save_slide_viewing_trail(slide_id: int, trail: ViewingTrailUpdate, current_user: dict = Depends(get_current_user)):
        _get_slide_with_access(slide_id, current_user)
        username = current_user["username"]
        payload = json.dumps(trail.model_dump(), separators=(",", ":"))
        with closing(connect()) as db:
            db.execute("""
                MERGE dbo.slide_viewing_trails AS target
                USING (SELECT ? AS slide_id, ? AS username) AS source
                ON target.slide_id = source.slide_id AND target.username = source.username
                WHEN MATCHED THEN
                    UPDATE SET coverage_json = ?, updated_at = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN
                    INSERT (slide_id, username, coverage_json)
                    VALUES (?, ?, ?);
            """, (slide_id, username, payload, slide_id, username, payload))
            db.commit()
        return {"ok": True, "slide_id": slide_id}

    @router.delete("/api/slides/{slide_id}/trail", status_code=204, dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def clear_slide_viewing_trail(slide_id: int, current_user: dict = Depends(get_current_user)):
        _get_slide_with_access(slide_id, current_user)
        username = current_user["username"]
        with closing(connect()) as db:
            db.execute("DELETE FROM dbo.slide_viewing_trails WHERE slide_id = ? AND username = ?", (slide_id, username))
            db.commit()

    # =========================================================================
    # MODULE 2: MULTI-SLIDE REGISTRATION
    # =========================================================================

    @router.get("/api/registrations", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_registrations(
        case_id: Optional[int] = Query(None),
        slide_id: Optional[int] = Query(None),
        current_user: dict = Depends(get_current_user),
    ):
        if case_id:
            case = get_case_fn(case_id)
            require_assigned_case_fn(case, current_user)
        elif slide_id:
            slide = get_slide_fn(slide_id)
            case = get_case_fn(slide["case_id"])
            require_assigned_case_fn(case, current_user)
            case_id = case["id"]
        else:
            raise HTTPException(400, "Either case_id or slide_id query parameter is required")

        with closing(connect()) as db:
            if slide_id:
                rows = db.execute(
                    """SELECT * FROM slide_registrations
                       WHERE case_id = ? AND (reference_slide_id = ? OR moving_slide_id = ?)
                       ORDER BY id DESC""",
                    (case_id, slide_id, slide_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM slide_registrations WHERE case_id = ? ORDER BY id DESC",
                    (case_id,),
                ).fetchall()

        return [_serialize_registration(dict(r)) for r in rows]

    @router.post("/api/registrations", status_code=201, dependencies=[Depends(require_roles(pathologist_and_admin))])
    def create_registration(item: RegistrationCreate, current_user: dict = Depends(get_current_user)):
        # Validate case access
        case = get_case_fn(item.case_id)
        require_assigned_case_fn(case, current_user)

        # Validate both slides exist and belong to this case
        ref_slide = get_slide_fn(item.reference_slide_id)
        mov_slide = get_slide_fn(item.moving_slide_id)

        if ref_slide["case_id"] != item.case_id or mov_slide["case_id"] != item.case_id:
            raise HTTPException(400, "Both reference and moving slides must belong to the specified case")

        if item.reference_slide_id == item.moving_slide_id:
            raise HTTPException(400, "Reference and moving slides must be distinct slides")

        with closing(connect()) as db:
            ref_v = db.execute("SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC", (item.reference_slide_id,)).fetchone()
            mov_v = db.execute("SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC", (item.moving_slide_id,)).fetchone()

            ref_v_id = ref_v["id"] if ref_v else None
            mov_v_id = mov_v["id"] if mov_v else None

            landmarks_list = [lm.model_dump() for lm in item.landmarks]
            landmarks_json = json.dumps(landmarks_list)

            cursor = db.execute("""
                INSERT INTO slide_registrations
                (case_id, reference_slide_id, moving_slide_id, reference_image_version_id, moving_image_version_id,
                 registration_method, transform_type, landmarks_json, status, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Draft', ?)
            """, (
                item.case_id, item.reference_slide_id, item.moving_slide_id, ref_v_id, mov_v_id,
                item.registration_method, item.transform_type, landmarks_json, current_user["username"]
            ))
            reg_id = cursor.lastrowid
            _append_reg_history(db, reg_id, "created", current_user["display_name"], {"method": item.registration_method, "transform": item.transform_type})
            db.commit()

            audit(db, current_user["username"], "registration.created", "slide_registration", reg_id, f"case={item.case_id}")

            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.get("/api/registrations/{reg_id}", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def get_registration(reg_id: int, current_user: dict = Depends(get_current_user)):
        reg = _get_registration_or_404(reg_id, current_user)
        return _serialize_registration(reg)

    @router.patch("/api/registrations/{reg_id}", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def update_registration(reg_id: int, update: RegistrationUpdate, current_user: dict = Depends(get_current_user)):
        reg = _get_registration_or_404(reg_id, current_user)

        updates = []
        params = []

        if update.transform_type is not None:
            updates.append("transform_type = ?")
            params.append(update.transform_type)

        if update.landmarks is not None:
            landmarks_list = [lm.model_dump() for lm in update.landmarks]
            updates.append("landmarks_json = ?")
            params.append(json.dumps(landmarks_list))

        if update.status is not None:
            updates.append("status = ?")
            params.append(update.status)

        if update.review_notes is not None:
            updates.append("review_notes = ?")
            params.append(update.review_notes)

        if not updates:
            return _serialize_registration(reg)

        updates.append("updated_at = SYSUTCDATETIME()")
        params.append(reg_id)

        with closing(connect()) as db:
            db.execute(f"UPDATE slide_registrations SET {', '.join(updates)} WHERE id = ?", tuple(params))
            _append_reg_history(db, reg_id, "updated", current_user["display_name"], update.model_dump(exclude_unset=True))
            db.commit()
            audit(db, current_user["username"], "registration.updated", "slide_registration", reg_id, f"fields={list(update.model_dump(exclude_unset=True).keys())}")
            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.delete("/api/registrations/{reg_id}", status_code=204, dependencies=[Depends(require_roles(pathologist_and_admin))])
    def delete_registration(reg_id: int, current_user: dict = Depends(get_current_user)):
        _get_registration_or_404(reg_id, current_user)
        with closing(connect()) as db:
            db.execute("DELETE FROM slide_registrations WHERE id = ?", (reg_id,))
            audit(db, current_user["username"], "registration.deleted", "slide_registration", reg_id, "deleted")
            db.commit()

    @router.post("/api/registrations/{reg_id}/landmarks", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def update_landmarks(reg_id: int, req: LandmarksUpdate, current_user: dict = Depends(get_current_user)):
        _get_registration_or_404(reg_id, current_user)
        landmarks_list = [lm.model_dump() for lm in req.landmarks]
        landmarks_json = json.dumps(landmarks_list)

        with closing(connect()) as db:
            db.execute(
                "UPDATE slide_registrations SET landmarks_json = ?, updated_at = SYSUTCDATETIME() WHERE id = ?",
                (landmarks_json, reg_id),
            )
            _append_reg_history(db, reg_id, "landmarks_updated", current_user["display_name"], {"count": len(landmarks_list)})
            db.commit()
            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.post("/api/registrations/{reg_id}/calculate", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def calculate_alignment(reg_id: int, req: CalculateAlignmentRequest, current_user: dict = Depends(get_current_user)):
        reg = _get_registration_or_404(reg_id, current_user)

        landmarks_to_use = [lm.model_dump() for lm in req.landmarks] if req.landmarks is not None else []
        if not landmarks_to_use and reg.get("landmarks_json"):
            try:
                landmarks_to_use = json.loads(reg["landmarks_json"])
            except Exception:
                landmarks_to_use = []

        pixel_size = _get_slide_pixel_size(reg["reference_slide_id"])

        try:
            matrix, metrics = estimate_manual_registration(req.transform_type, landmarks_to_use, pixel_size)
        except RegistrationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            logger.exception("Alignment calculation failed")
            raise HTTPException(500, f"Alignment calculation error: {exc}") from exc

        matrix_json = json.dumps(matrix.tolist())
        metrics_json = json.dumps(metrics)
        landmarks_json = json.dumps(landmarks_to_use)

        with closing(connect()) as db:
            db.execute("""
                UPDATE slide_registrations
                SET transform_type = ?, transform_matrix_json = ?, quality_metrics_json = ?,
                    landmarks_json = ?, status = 'Calculated', updated_at = SYSUTCDATETIME()
                WHERE id = ?
            """, (req.transform_type, matrix_json, metrics_json, landmarks_json, reg_id))
            _append_reg_history(db, reg_id, "calculated", current_user["display_name"], {
                "transform_type": req.transform_type,
                "mean_residual_px": metrics.get("mean_residual_px"),
                "landmark_count": metrics.get("landmark_count"),
            })
            db.commit()
            audit(db, current_user["username"], "registration.calculated", "slide_registration", reg_id, f"type={req.transform_type}")
            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.post("/api/registrations/{reg_id}/auto-align", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def auto_align_registration(reg_id: int, req: AutoAlignRequest, current_user: dict = Depends(get_current_user)):
        reg = _get_registration_or_404(reg_id, current_user)

        ref_slide = get_slide_fn(reg["reference_slide_id"])
        mov_slide = get_slide_fn(reg["moving_slide_id"])

        ref_path = stored_path_fn(ref_slide["filename"])
        mov_path = stored_path_fn(mov_slide["filename"])

        if not ref_path.exists() or not mov_path.exists():
            raise HTTPException(404, "One or both slide image files could not be located in storage")

        pixel_size = _get_slide_pixel_size(reg["reference_slide_id"])

        try:
            result = run_automatic_registration(
                ref_slide_row=dict(ref_slide),
                ref_file_path=ref_path,
                mov_slide_row=dict(mov_slide),
                mov_file_path=mov_path,
                openslide_module=openslide_module,
                transform_type=req.transform_type,
                max_dimension=req.max_dimension or 1600,
                pixel_size_microns=pixel_size,
            )
        except RegistrationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            logger.exception("Automatic registration error")
            raise HTTPException(500, f"Automatic registration failed: {exc}") from exc

        matrix_json = json.dumps(result["transform_matrix"])
        metrics_json = json.dumps(result["quality_metrics"])
        landmarks_json = json.dumps(result["landmarks"])

        with closing(connect()) as db:
            db.execute("""
                UPDATE slide_registrations
                SET registration_method = 'automatic', transform_type = ?,
                    transform_matrix_json = ?, quality_metrics_json = ?,
                    landmarks_json = ?, status = 'Calculated', updated_at = SYSUTCDATETIME()
                WHERE id = ?
            """, (req.transform_type, matrix_json, metrics_json, landmarks_json, reg_id))
            _append_reg_history(db, reg_id, "auto_aligned", current_user["display_name"], result["quality_metrics"])
            db.commit()
            audit(db, current_user["username"], "registration.auto_aligned", "slide_registration", reg_id, f"type={req.transform_type}")
            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.post("/api/registrations/{reg_id}/review", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def review_registration(reg_id: int, req: ReviewRegistrationRequest, current_user: dict = Depends(get_current_user)):
        reg = _get_registration_or_404(reg_id, current_user)

        new_status = req.status
        actor = current_user["display_name"]
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        with closing(connect()) as db:
            if new_status == "Reviewed":
                db.execute("""
                    UPDATE slide_registrations
                    SET status = 'Reviewed', review_notes = ?, approved_by = ?, approved_at = ?, updated_at = SYSUTCDATETIME()
                    WHERE id = ?
                """, (req.review_notes, actor, now, reg_id))
            else:
                db.execute("""
                    UPDATE slide_registrations
                    SET status = 'Rejected', review_notes = ?, updated_at = SYSUTCDATETIME()
                    WHERE id = ?
                """, (req.review_notes, reg_id))

            _append_reg_history(db, reg_id, f"marked_{new_status.lower()}", actor, {"notes": req.review_notes})
            db.commit()
            audit(db, current_user["username"], f"registration.{new_status.lower()}", "slide_registration", reg_id, req.review_notes)
            row = db.execute("SELECT * FROM slide_registrations WHERE id = ?", (reg_id,)).fetchone()

        return _serialize_registration(dict(row))

    @router.get("/api/registrations/{reg_id}/history", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def get_registration_history(reg_id: int, current_user: dict = Depends(get_current_user)):
        _get_registration_or_404(reg_id, current_user)
        with closing(connect()) as db:
            rows = db.execute(
                "SELECT * FROM slide_registration_history WHERE registration_id = ? ORDER BY id DESC",
                (reg_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("details_json"):
                try:
                    d["details"] = json.loads(d["details_json"])
                except Exception:
                    d["details"] = d["details_json"]
            out.append(d)
        return out

    return router
