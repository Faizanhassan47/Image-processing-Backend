"""
Annotations and Region of Interest (ROI) API.
Endpoints for managing pathologist annotations, measurements, and revisions.
"""

from contextlib import closing
import json
import math

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from core.security import ADMIN_ROLES, CLINICAL_READ_ROLES, ROLE_PATHOLOGIST


class AnnotationCreate(BaseModel):
    annotation_type: str = Field(default="rectangle", pattern="^(point|line|polygon|rectangle|ellipse|freehand|arrow)$")
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    color: str = Field(default="#e85d75", pattern=r"^#[0-9a-fA-F]{6}$")
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    points: list[list[float]] | None = None
    category: str = Field(default="Pathologist", max_length=120)
    annotation_status: str = Field(default="Active", pattern="^(Active|Resolved|Excluded|Corrected)$")


class AnnotationUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    color: str = Field(default="#e85d75", pattern=r"^#[0-9a-fA-F]{6}$")
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    points: list[list[float]] | None = None
    category: str = Field(default="Pathologist", max_length=120)
    annotation_status: str = Field(default="Active", pattern="^(Active|Resolved|Excluded|Corrected)$")
    change_reason: str = Field(default="Annotation updated", min_length=1, max_length=2000)


def create_annotations_router(connect, require_roles, get_current_user, get_slide_fn, audit):
    router = APIRouter(tags=["Annotations"])
    pathologist_and_admin = ADMIN_ROLES + [ROLE_PATHOLOGIST]

    def _annotation_for_slide(slide_id: int, annotation_id: int):
        get_slide_fn(slide_id)
        with closing(connect()) as db:
            row = db.execute(
                "SELECT * FROM annotations WHERE id = ? AND slide_id = ?",
                (annotation_id, slide_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Annotation not found for this slide")
        return row

    def _validated_geometry(slide, annotation: AnnotationCreate):
        label = annotation.label.strip()
        if not label:
            raise HTTPException(400, "Annotation label cannot be blank")

        if annotation.annotation_type in ("rectangle", "ellipse"):
            if annotation.x is None or annotation.y is None or annotation.width is None or annotation.height is None:
                raise HTTPException(400, "Rectangle must have x, y, width, and height")
            values = (annotation.x, annotation.y, annotation.width, annotation.height)
            if not all(math.isfinite(value) for value in values):
                raise HTTPException(400, "Annotation coordinates must be finite numbers")
            if annotation.x < 0 or annotation.y < 0 or annotation.width <= 0 or annotation.height <= 0:
                raise HTTPException(400, "Annotation coordinates must be non-negative with positive width and height")
            if annotation.x + annotation.width > slide["width"] or annotation.y + annotation.height > slide["height"]:
                raise HTTPException(400, "Annotation rectangle is outside the slide bounds")
        elif annotation.annotation_type in ("point", "line", "polygon", "freehand", "arrow"):
            if not annotation.points:
                raise HTTPException(400, f"{annotation.annotation_type.title()} must have points")
            if annotation.annotation_type == "arrow" and len(annotation.points) != 2:
                raise HTTPException(400, "Arrow must contain exactly 2 points")
            if annotation.annotation_type == "polygon" and len(annotation.points) < 3:
                raise HTTPException(400, "Polygon must contain at least 3 points")
            for pt in annotation.points:
                if not isinstance(pt, list) or len(pt) != 2 or not all(math.isfinite(v) for v in pt):
                    raise HTTPException(400, "Invalid points geometry")
                if pt[0] < 0 or pt[1] < 0 or pt[0] > slide["width"] or pt[1] > slide["height"]:
                    raise HTTPException(400, f"Annotation point {pt} is outside the slide bounds")
            minimum = {"point": 1, "line": 2, "freehand": 2}.get(annotation.annotation_type)
            if minimum and len(annotation.points) < minimum:
                raise HTTPException(400, f"{annotation.annotation_type.title()} must contain at least {minimum} point(s)")

        return label

    @router.get("/api/slides/{slide_id}/annotations", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_annotations(slide_id: int):
        get_slide_fn(slide_id)
        with closing(connect()) as db:
            rows = db.execute(
                "SELECT * FROM annotations WHERE slide_id = ? ORDER BY id",
                (slide_id,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            if d.get("points"):
                d["points"] = json.loads(d["points"])
            out.append(d)
        return out

    @router.post("/api/slides/{slide_id}/annotations", status_code=201, dependencies=[Depends(require_roles(pathologist_and_admin))])
    def create_annotation(slide_id: int, annotation: AnnotationCreate, current_user: dict = Depends(get_current_user)):
        slide = get_slide_fn(slide_id)
        label = _validated_geometry(slide, annotation)
        points_json = json.dumps(annotation.points) if annotation.points else None
        with closing(connect()) as db:
            image_version = db.execute("SELECT TOP 1 id, pixel_size_microns FROM image_versions WHERE slide_id=? ORDER BY version DESC", (slide_id,)).fetchone()
            measurements = {}
            if image_version and image_version["pixel_size_microns"]:
                scale = image_version["pixel_size_microns"]
                if annotation.width and annotation.height:
                    measurements = {
                        "width_microns": annotation.width * scale,
                        "height_microns": annotation.height * scale,
                        "area_square_microns": annotation.width * annotation.height * scale * scale,
                        "calibrated": True,
                    }
            cursor = db.execute(
                """INSERT INTO annotations
                (slide_id, annotation_type, label, note, color, x, y, width, height, points, image_version_id, category, annotation_status, author, source_type, measurements_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slide_id, annotation.annotation_type, label, annotation.note.strip(), annotation.color.lower(),
                 annotation.x, annotation.y, annotation.width, annotation.height, points_json,
                 image_version["id"] if image_version else None, annotation.category, annotation.annotation_status,
                 current_user["username"], "Human", json.dumps(measurements))
            )
            db.commit()
            row = db.execute("SELECT * FROM annotations WHERE id = ?", (cursor.lastrowid,)).fetchone()
        out = dict(row)
        if out.get("points"):
            out["points"] = json.loads(out["points"])
        return out

    @router.patch("/api/slides/{slide_id}/annotations/{annotation_id}", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def update_annotation(slide_id: int, annotation_id: int, update: AnnotationUpdate, current_user: dict = Depends(get_current_user)):
        existing = _annotation_for_slide(slide_id, annotation_id)
        slide = get_slide_fn(slide_id)
        label = update.label.strip()
        if not label:
            raise HTTPException(400, "Annotation label cannot be blank")
        with closing(connect()) as db:
            revision = (db.execute("SELECT COUNT(*) AS count FROM annotation_revisions WHERE annotation_id=?", (annotation_id,)).fetchone()["count"] + 1)
            geometry = {
                "annotation_type": existing["annotation_type"],
                "x": existing["x"],
                "y": existing["y"],
                "width": existing["width"],
                "height": existing["height"],
                "points": json.loads(existing["points"]) if existing["points"] else None,
            }
            db.execute(
                """INSERT INTO annotation_revisions
                (annotation_id, revision_number, geometry_json, label, note, category, annotation_status, changed_by, change_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (annotation_id, revision, json.dumps(geometry), existing["label"], existing["note"],
                 existing.get("category"), existing.get("annotation_status"), current_user["username"], update.change_reason)
            )
            candidate = AnnotationCreate(
                annotation_type=existing["annotation_type"],
                label=label,
                note=update.note,
                color=update.color,
                x=update.x if update.x is not None else existing["x"],
                y=update.y if update.y is not None else existing["y"],
                width=update.width if update.width is not None else existing["width"],
                height=update.height if update.height is not None else existing["height"],
                points=update.points if update.points is not None else (json.loads(existing["points"]) if existing["points"] else None),
                category=update.category,
                annotation_status=update.annotation_status,
            )
            _validated_geometry(slide, candidate)
            db.execute(
                """UPDATE annotations
                SET label=?, note=?, color=?, x=?, y=?, width=?, height=?, points=?, category=?, annotation_status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id = ? AND slide_id = ?""",
                (label, update.note.strip(), update.color.lower(), candidate.x, candidate.y, candidate.width,
                 candidate.height, json.dumps(candidate.points) if candidate.points else None,
                 update.category, update.annotation_status, annotation_id, slide_id)
            )
            db.commit()
            row = db.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id, slide_id)).fetchone()
        return dict(row)

    @router.delete("/api/slides/{slide_id}/annotations/{annotation_id}", status_code=204, dependencies=[Depends(require_roles(pathologist_and_admin))])
    def delete_annotation(slide_id: int, annotation_id: int, current_user: dict = Depends(get_current_user)):
        annotation = _annotation_for_slide(slide_id, annotation_id)
        with closing(connect()) as db:
            db.execute("DELETE FROM annotation_revisions WHERE annotation_id = ?", (annotation_id,))
            result = db.execute(
                "DELETE FROM annotations WHERE id = ? AND slide_id = ?",
                (annotation_id, slide_id),
            )
            if result.cursor.rowcount != 1:
                raise HTTPException(409, "Annotation changed before it could be deleted")
            audit(db, current_user["username"], "annotation.delete", "annotation", annotation_id, json.dumps({
                "slide_id": slide_id,
                "label": annotation["label"],
                "annotation_type": annotation["annotation_type"],
            }))
            db.commit()
        return Response(status_code=204)

    return router
