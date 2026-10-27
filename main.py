"""
Digital Pathology Platform - Master Application Entry Point.
Coordinates core configuration, security middleware, database bootstrapping,
and mounts all domain route controllers.
"""

import asyncio
from contextlib import asynccontextmanager, closing
import logging
from pathlib import Path
import secrets
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from core.audit_context import reset_audit_context, set_audit_context
from core.config import (
    APP_NAME,
    CSRF_PROTECTION_ENABLED,
    UPLOADS_DIR,
    WSI_MAX_BYTES,
    SECRET_KEY,
    ALGORITHM,
    env_bool,
)
from core.database import connect
from core.security import (
    audit,
    get_current_user,
    get_password_hash,
    require_roles,
    validate_password,
)
from routes import create_api_router, init_all_schemas
from services.slide_cache import (
    OPENSLIDE_IMPORT_ERROR,
    openslide,
    slide_cache,
)
from sql import init_db as init_database_tables


# --- Database Initialization on Startup ---
def init_db():
    """Create the core database schema and idempotently lift legacy case files into it."""
    init_database_tables(connect)


init_db()


# --- Application Lifespan Events ---
@asynccontextmanager
async def lifespan(_app):
    from routes.platform_api import watched_folder_loop
    from services.ai_engine import hovernet_manager
    from services.kongnet_engine import kongnet_manager

    stop_event = asyncio.Event()
    watcher = asyncio.create_task(watched_folder_loop(connect, stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await watcher
        slide_cache.clear()
        hovernet_manager.stop_worker()
        kongnet_manager.stop_worker()


app = FastAPI(title=APP_NAME, lifespan=lifespan)

# --- Application Middlewares ---
from middleware import (
    AuthAccessMiddleware,
    CSRFMiddleware,
    ObservabilityMiddleware,
    SecurityHeadersMiddleware,
)

# 1. Observability, correlation IDs, and client IP detection
app.add_middleware(ObservabilityMiddleware)

# 2. User authentication & token inspection
app.add_middleware(AuthAccessMiddleware)

# 3. Cross-Site Request Forgery protection
app.add_middleware(CSRFMiddleware)

# 4. Conservative browser security headers. CSP/HSTS are deployment concerns
# and must be configured only after the production HTTPS/topology is known.
app.add_middleware(SecurityHeadersMiddleware)

# 5. CORS configuration
import os
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_credentials=env_bool("CORS_ALLOW_CREDENTIALS", False),
    allow_methods=[method.strip() for method in os.getenv("CORS_ALLOW_METHODS", "GET,POST,PATCH,DELETE,OPTIONS").split(",") if method.strip()],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)



# --- Common Slide & Case Resolvers ---
def stored_path(filename: str) -> Path:
    path = (UPLOADS_DIR / filename).resolve()
    if not filename or path.parent != UPLOADS_DIR.resolve():
        raise HTTPException(400, "Invalid stored filename")
    return path


def get_case(case_pk: int):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM cases WHERE id = ?", (case_pk,)).fetchone()
    if row is None:
        raise HTTPException(404, "Case not found")
    return row


def get_slide(slide_id: int):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM slides WHERE id = ?", (slide_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Slide not found")
    return row


def require_assigned_case(case, current_user: dict):
    if current_user["role"] != "Pathologist":
        return
    assigned = (case["reviewer"] or "").strip().casefold()
    identities = {
        (current_user.get("username") or "").strip().casefold(),
        (current_user.get("display_name") or "").strip().casefold(),
    }
    if not assigned or assigned not in identities:
        raise HTTPException(status_code=403, detail="Pathologists may access only cases assigned to them")


# --- System Status ---
@app.get("/api/status")
def api_status():
    return {
        "ok": True,
        "openslide_available": openslide is not None,
        "openslide_error": OPENSLIDE_IMPORT_ERROR,
        "wsi_max_upload_mb": WSI_MAX_BYTES // (1024 * 1024),
    }


# --- Mount Modular Domain Routers ---
init_all_schemas(connect)
app.include_router(
    create_api_router(
        connect_fn=connect,
        require_roles_fn=require_roles,
        get_current_user_fn=get_current_user,
        audit_fn=audit,
        uploads_path=UPLOADS_DIR,
        openslide_module=openslide,
        get_slide_fn=get_slide,
        get_case_fn=get_case,
        require_assigned_case_fn=require_assigned_case,
        stored_path_fn=stored_path,
    )
)
