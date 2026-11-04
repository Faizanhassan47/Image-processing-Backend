
"""GrandQC authenticated whole-slide quality-control API."""

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from core import AI_READ_ROLES, AI_RUN_ROLES
from services.ai_subscription_service import AISubscriptionService
from services.grandqc_engine import (
    GRANDQC_OUTPUT_ROOT,
    GrandQCCancelled,
    grandqc_manager,
)
from services.grandqc_repository import (
    FEATURE_CODE,
    get_grandqc_analysis,
    list_grandqc_analyses,
    save_grandqc_analysis,
)

logger = logging.getLogger("pathology.grandqc_api")

_JOBS = {}
_JOBS_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def public_job(job):
    """Never return threading.Event objects through JSON."""
    with _JOBS_LOCK:
        return {
            key: value
            for key, value in job.items()
            if key != "cancel_event"
        }


def create_grandqc_router(
    connect_func,
    require_roles_func,
    get_slide_func,
    get_case_func,
    stored_path_func,
    require_assigned_case_func,
    audit_func=None,
):
    router = APIRouter(tags=["GrandQC - Slide Quality"])
    subscriptions = AISubscriptionService(connect_func)

    def authorized_slide(slide_id, user):
        slide = get_slide_func(slide_id)
        case = get_case_func(slide["case_id"])
        require_assigned_case_func(case, user)
        return slide, case

    def authorized_analysis(analysis_id, user):
        row = get_grandqc_analysis(
            connect_func,
            analysis_id,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="GrandQC analysis not found",
            )

        case = get_case_func(row["case_id"])
        require_assigned_case_func(case, user)
        return row

    def authorized_job(job_id, user):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="QC job not found",
            )

        case = get_case_func(job["case_id"])
        require_assigned_case_func(case, user)
        return job

    @router.post(
        "/api/slides/{slide_id}/qc/analyses",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_qc(
        slide_id: int,
        current_user=Depends(
            require_roles_func(AI_RUN_ROLES)
        ),
    ):
        slide, case = authorized_slide(
            slide_id,
            current_user,
        )

        subscriptions.require_entitlement(
            current_user,
            FEATURE_CODE,
        )

        slide_path = stored_path_func(slide["filename"])

        if not slide_path.is_file():
            raise HTTPException(
                status_code=404,
                detail="Slide file not found",
            )

        if slide_path.suffix.lower() not in {
            ".svs",
            ".tif",
            ".tiff",
        }:
            raise HTTPException(
                status_code=422,
                detail="GrandQC requires a compatible WSI",
            )

        try:
            grandqc_manager.validate()
        except Exception:
            logger.exception(
                "GrandQC environment validation failed"
            )
            raise HTTPException(
                status_code=503,
                detail="GrandQC environment unavailable",
            )

        job_id = uuid.uuid4().hex

        job = {
            "job_id": job_id,
            "slide_id": slide_id,
            "case_id": case["id"],
            "requested_by": current_user["username"],
            "status": "queued",
            "stage": "queued",
            "message": "Waiting to start",
            "created_at": utc_now(),
            "completed_at": None,
            "analysis_id": None,
            "error": None,
            "cancel_event": threading.Event(),
        }

        with closing(connect_func()) as db:
            db.execute("""
                INSERT INTO audit_log (
                    actor_username,
                    action,
                    target_type,
                    target_id,
                    details
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                current_user["username"],
                "qc.analysis.requested",
                "slide",
                str(slide_id),
                json.dumps({
                    "feature_code": FEATURE_CODE,
                    "job_id": job_id,
                }),
            ))
            db.commit()

        with _JOBS_LOCK:
            _JOBS[job_id] = job

        def on_progress(payload):
            with _JOBS_LOCK:
                job["stage"] = payload.get(
                    "stage",
                    job["stage"],
                )
                job["message"] = str(
                    payload.get("message", "")
                )[-400:]

        def run_background():
            with _JOBS_LOCK:
                job["status"] = "processing"
                job["stage"] = "tissue"

            try:
                result = grandqc_manager.analyze_slide(
                    slide_path,
                    cancel_event=job["cancel_event"],
                    progress_callback=on_progress,
                    job_id=job_id,
                )

                if job["cancel_event"].is_set():
                    raise GrandQCCancelled(
                        "Cancelled before results were saved"
                    )

                analysis_id = save_grandqc_analysis(
                    connect_func,
                    slide_id=slide_id,
                    case_id=case["id"],
                    requested_by=current_user["username"],
                    job_id=job_id,
                    result=result,
                )

                with _JOBS_LOCK:
                    job.update({
                        "status": "completed",
                        "stage": "complete",
                        "analysis_id": analysis_id,
                        "message": "GrandQC completed",
                        "completed_at": utc_now(),
                    })

            except GrandQCCancelled:
                with _JOBS_LOCK:
                    job.update({
                        "status": "cancelled",
                        "stage": "cancelled",
                        "message": "Analysis cancelled",
                        "completed_at": utc_now(),
                    })

            except Exception as exc:
                logger.exception(
                    "GrandQC job %s failed",
                    job_id,
                )

                with _JOBS_LOCK:
                    job.update({
                        "status": "failed",
                        "stage": "failed",
                        "error": str(exc)[-1000:],
                        "message": "GrandQC analysis failed",
                        "completed_at": utc_now(),
                    })

        worker_thread = threading.Thread(
            target=run_background,
            name=f"GrandQC-{job_id[:8]}",
            daemon=True,
        )
        worker_thread.start()

        return public_job(job)

    @router.get("/api/qc/jobs/{job_id}")
    def get_job(
        job_id: str,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        job = authorized_job(job_id, current_user)
        return public_job(job)

    @router.post("/api/qc/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: str,
        current_user=Depends(
            require_roles_func(AI_RUN_ROLES)
        ),
    ):
        job = authorized_job(job_id, current_user)

        if (
            current_user.get("role") == "Pathologist"
            and job["requested_by"].casefold()
            != current_user["username"].casefold()
        ):
            raise HTTPException(
                status_code=403,
                detail="Cannot cancel another user's job",
            )

        with _JOBS_LOCK:
            if job["status"] not in {
                "queued",
                "processing",
            }:
                raise HTTPException(
                    status_code=409,
                    detail="Job is not cancellable",
                )

            job["cancel_event"].set()
            job["message"] = "Cancellation requested"

        return {
            "job_id": job_id,
            "cancellation_requested": True,
        }

    @router.get(
        "/api/slides/{slide_id}/qc/analyses"
    )
    def list_qc(
        slide_id: int,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        authorized_slide(slide_id, current_user)

        return list_grandqc_analyses(
            connect_func,
            slide_id,
        )

    @router.get("/api/qc/analyses/{analysis_id}")
    def get_qc(
        analysis_id: int,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        row = authorized_analysis(
            analysis_id,
            current_user,
        )

        return {
            "id": row["id"],
            "slide_id": row["slide_id"],
            "case_id": row["case_id"],
            "status": row["status"].lower(),
            "model_name": row["model_name"],
            "model_version": row["model_version"],
            "feature_code": FEATURE_CODE,
            "polygon_count": row["polygon_count"],
            "elapsed_seconds": row["elapsed_seconds"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "geojson_url":
                f"/api/qc/analyses/{analysis_id}/geojson",
            "overlay_url":
                f"/api/qc/analyses/{analysis_id}/overlay",
            "artifact_map_url":
                f"/api/qc/analyses/{analysis_id}/artifact-map",
            "research_only": True,
        }

    def resolve_asset(row, kind):
        result = json.loads(row["result_json"])

        job_id = row["job_id"]

        if not job_id.isalnum() or len(job_id) > 64:
            raise HTTPException(
                status_code=500,
                detail="Invalid saved job ID",
            )

        root = (
            GRANDQC_OUTPUT_ROOT
            / job_id
            / "results"
        ).resolve()

        configured = result.get(
            "output_files", {}
        ).get(kind)

        if not configured:
            raise HTTPException(
                status_code=404,
                detail="Requested asset unavailable",
            )

        path = Path(configured).resolve()

        if root not in path.parents or not path.is_file():
            raise HTTPException(
                status_code=404,
                detail="QC file missing or invalid",
            )

        return path

    @router.get(
        "/api/qc/analyses/{analysis_id}/geojson"
    )
    def get_geojson(
        analysis_id: int,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        row = authorized_analysis(
            analysis_id,
            current_user,
        )

        return FileResponse(
            resolve_asset(row, "geojson"),
            media_type="application/geo+json",
        )

    @router.get(
        "/api/qc/analyses/{analysis_id}/overlay"
    )
    def get_overlay(
        analysis_id: int,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        row = authorized_analysis(
            analysis_id,
            current_user,
        )

        return FileResponse(
            resolve_asset(row, "overlay"),
            media_type="image/jpeg",
        )

    @router.get(
        "/api/qc/analyses/{analysis_id}/artifact-map"
    )
    def get_artifact_map(
        analysis_id: int,
        current_user=Depends(
            require_roles_func(AI_READ_ROLES)
        ),
    ):
        row = authorized_analysis(
            analysis_id,
            current_user,
        )

        return FileResponse(
            resolve_asset(row, "artifact_map"),
            media_type="image/png",
        )

    return router
