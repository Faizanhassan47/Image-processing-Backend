
"""Master API router for Hello Lica Digital Pathology."""

import logging
from pathlib import Path

from fastapi import APIRouter

from routes.patient_api import (
    create_patient_router,
    init_patient_schema,
)
from routes.platform_api import (
    create_platform_router,
    init_platform_schema,
)
from routes.ai_api import create_ai_router
from routes.ai_subscription_api import (
    create_ai_subscription_router,
)
from routes.ai_licensing_api import (
    create_ai_licensing_router,
)
from routes.auth_api import create_auth_router
from routes.cases_api import create_cases_router
from routes.annotations_api import create_annotations_router
from routes.reports_api import create_reports_router
from routes.grandqc_api import create_grandqc_router
from routes.dip_api import create_dip_router

from services.ai_engine import init_ai_schema
from services.kongnet_engine import init_kongnet_schema
from services.grandqc_repository import init_grandqc_schema

logger = logging.getLogger("pathology.routes")

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
    "create_grandqc_router",
]


def init_all_schemas(connect_fn):
    """Initialize existing tables and GrandQC QC records."""

    init_platform_schema(connect_fn)
    init_patient_schema(connect_fn)

    try:
        init_ai_schema(connect_fn)
    except Exception:
        logger.exception(
            "Existing AI schema initialization failed"
        )

    try:
        init_kongnet_schema(connect_fn)
    except Exception:
        logger.exception(
            "KongNet schema initialization failed"
        )

    # GrandQC requires these tables to accept analyses.
    # Allow startup to report errors rather than hiding them.
    init_grandqc_schema(connect_fn)


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
    """Build the application's master API router."""

    master_router = APIRouter()

    # AI subscriptions and licensing
    master_router.include_router(
        create_ai_subscription_router(
            connect_fn,
            require_roles_fn,
            get_current_user_fn,
            audit_fn,
        )
    )

    master_router.include_router(
        create_ai_licensing_router(
            connect_fn,
            get_current_user_fn,
        )
    )

    # Authentication
    master_router.include_router(
        create_auth_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            get_current_user=get_current_user_fn,
            audit=audit_fn,
        )
    )

    # Platform operations
    master_router.include_router(
        create_platform_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            get_current_user=get_current_user_fn,
            uploads_dir=uploads_path,
            openslide_module=openslide_module,
        )
    )

    # Patients
    master_router.include_router(
        create_patient_router(
            connect=connect_fn,
            require_roles=require_roles_fn,
            audit=audit_fn,
            uploads_dir=uploads_path,
        )
    )

    # Routes requiring slide/case access helpers
    if (
        get_slide_fn
        and get_case_fn
        and require_assigned_case_fn
        and stored_path_fn
    ):
        # Cases and slides
        master_router.include_router(
            create_cases_router(
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
        )

        # Annotations
        master_router.include_router(
            create_annotations_router(
                connect=connect_fn,
                require_roles=require_roles_fn,
                get_current_user=get_current_user_fn,
                get_slide_fn=get_slide_fn,
                audit=audit_fn,
            )
        )

        # Pathology reports
        master_router.include_router(
            create_reports_router(
                connect=connect_fn,
                require_roles=require_roles_fn,
                get_current_user=get_current_user_fn,
                get_case_fn=get_case_fn,
                require_assigned_case_fn=require_assigned_case_fn,
                audit=audit_fn,
            )
        )

        # Existing HoVer-Net and KongNet
        master_router.include_router(
            create_ai_router(
                connect_func=connect_fn,
                require_roles_func=require_roles_fn,
                get_current_user_func=get_current_user_fn,
                get_slide_func=get_slide_fn,
                get_case_func=get_case_fn,
                stored_path_func=stored_path_fn,
                require_assigned_case_func=require_assigned_case_fn,
            )
        )

        master_router.include_router(
            create_dip_router(
                connect_func=connect_fn,
                require_roles_func=require_roles_fn,
                get_slide_func=get_slide_fn,
                get_case_func=get_case_fn,
                stored_path_func=stored_path_fn,
                require_assigned_case_func=require_assigned_case_fn,
            )
        )

        # GrandQC whole-slide quality control
        master_router.include_router(
            create_grandqc_router(
                connect_func=connect_fn,
                require_roles_func=require_roles_fn,
                get_slide_func=get_slide_fn,
                get_case_func=get_case_fn,
                stored_path_func=stored_path_fn,
                require_assigned_case_func=require_assigned_case_fn,
                audit_func=audit_fn,
            )
        )

        # Multi-slide registration and advanced viewing
        from routes.registrations_api import (
            create_registrations_router,
        )

        master_router.include_router(
            create_registrations_router(
                connect=connect_fn,
                require_roles=require_roles_fn,
                get_current_user=get_current_user_fn,
                audit=audit_fn,
                uploads_dir=uploads_path,
                openslide_module=openslide_module,
                get_slide_fn=get_slide_fn,
                get_case_fn=get_case_fn,
                require_assigned_case_fn=require_assigned_case_fn,
                stored_path_fn=stored_path_fn,
            )
        )

        from routes.dip_api import create_dip_router
        master_router.include_router(create_dip_router(
            get_current_user_fn=get_current_user_fn,
            get_slide_fn=get_slide_fn,
            stored_path_fn=stored_path_fn,
        ))

    return master_router
