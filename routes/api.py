from pathlib import Path
from fastapi import APIRouter

from routes.patient_api import create_patient_router, init_patient_schema
from routes.platform_api import create_platform_router, init_platform_schema
from routes.ai_api import create_ai_router
from routes.auth_api import create_auth_router
from routes.cases_api import create_cases_router
from routes.annotations_api import create_annotations_router
from routes.reports_api import create_reports_router
from services.ai_engine import init_ai_schema

__all__ = [
    "create_api_router",
    "init_all_schemas",
    "create_patient_router",
    "create_platform_router",
    "create_ai_router",
    "create_auth_router",
    "create_cases_router",
    "create_annotations_router",
    "create_reports_router",
]


def init_all_schemas(connect_fn):
    """Idempotently initialize all module database schemas."""
    init_platform_schema(connect_fn)
    init_patient_schema(connect_fn)
    try:
        init_ai_schema(connect_fn)
    except Exception:
        pass


def create_api_router(
    connect_fn,
    require_roles_fn,
    get_current_user_fn,
    audit_fn,
    uploads_path: Path,
    openslide_module=None,
    get_slide_fn=None,
    get_case_fn=None,
    require_assigned_case_fn=None,
    stored_path_fn=None,
):
    """
    Constructs and returns the master APIRouter containing all domain sub-routers.
    """
    master_router = APIRouter()

    # 1. Authentication & User Management
    auth_router = create_auth_router(
        connect=connect_fn,
        require_roles=require_roles_fn,
        get_current_user=get_current_user_fn,
        audit=audit_fn,
    )
    master_router.include_router(auth_router)

    # 2. Platform Operations & Scanner Ingestion
    platform_router = create_platform_router(
        connect=connect_fn,
        require_roles=require_roles_fn,
        get_current_user=get_current_user_fn,
        uploads_dir=uploads_path,
        openslide_module=openslide_module,
    )
    master_router.include_router(platform_router)

    # 3. Patient Management & Clinical Documents
    patient_router = create_patient_router(
        connect=connect_fn,
        require_roles=require_roles_fn,
        audit=audit_fn,
        uploads_dir=uploads_path,
    )
    master_router.include_router(patient_router)

    # 4. Pathology Cases & Slides
    if get_slide_fn and get_case_fn and require_assigned_case_fn and stored_path_fn:
        cases_router = create_cases_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            get_current_user=get_current_user_fn,
            uploads_dir=uploads_path,
            get_case_fn=get_case_fn,
            get_slide_fn=get_slide_fn,
            require_assigned_case_fn=require_assigned_case_fn,
            stored_path_fn=stored_path_fn,
            audit=audit_fn,
        )
        master_router.include_router(cases_router)

        # 5. Annotations & Measurements
        annotations_router = create_annotations_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            get_current_user=get_current_user_fn,
            get_slide_fn=get_slide_fn,
            audit=audit_fn,
        )
        master_router.include_router(annotations_router)

        # 6. Pathology Reports
        reports_router = create_reports_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            get_current_user=get_current_user_fn,
            get_case_fn=get_case_fn,
            require_assigned_case_fn=require_assigned_case_fn,
        )
        master_router.include_router(reports_router)

        # 7. AI Inference & Nuclear Segmentation (HoVer-Net)
        ai_router = create_ai_router(
            connect_func=connect_fn,
            require_roles_func=require_roles_fn,
            get_current_user_func=get_current_user_fn,
            get_slide_func=get_slide_fn,
            get_case_func=get_case_fn,
            stored_path_func=stored_path_fn,
        )
        master_router.include_router(ai_router)

    return master_router

