"""
Services API Aggregator and Core Service Router.
Exports and manages enterprise endpoints and services.
"""

from pathlib import Path
from fastapi import APIRouter
from routes.patient_api import create_patient_router, init_patient_schema
from routes.platform_api import create_platform_router, init_platform_schema

__all__ = [
    "create_services_router",
    "init_services_schemas",
    "create_patient_router",
    "create_platform_router",
]


def init_services_schemas(connect_fn):
    """Initialize schemas across all service modules."""
    init_platform_schema(connect_fn)
    init_patient_schema(connect_fn)


def create_services_router(connect_fn, require_roles_fn, get_current_user_fn, audit_fn, uploads_path: Path, openslide_module=None):
    """
    Creates an aggregated router including platform services and patient services.
    """
    router = APIRouter()
    
    # Mount platform services router
    platform_router = create_platform_router(
        connect=connect_fn,
        require_roles=require_roles_fn,
        get_current_user=get_current_user_fn,
        uploads_dir=uploads_path,
        openslide_module=openslide_module
    )
    router.include_router(platform_router)
    
    # Mount patient services router
    patient_router = create_patient_router(
        connect=connect_fn,
        require_roles=require_roles_fn,
        audit=audit_fn,
        uploads_dir=uploads_path
    )
    router.include_router(patient_router)
    
    return router
