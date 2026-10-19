"""
Request Observability and Audit Tracking Middleware.
Generates X-Request-ID, captures client IP, device information, and execution duration,
and maintains request-scoped audit context variables.
"""

import logging
import time
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from core.audit_context import reset_audit_context, set_audit_context
from core.config import env_bool

logger = logging.getLogger("pathology.api")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Logs incoming requests, performance metrics, and maintains client metadata."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        started = time.perf_counter()
        request.state.request_id = request_id

        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        real_ip = request.headers.get("X-Real-IP", "").strip()
        device_ip = request.headers.get("X-Device-IP", "").strip()

        client_ip = (device_ip or forwarded or real_ip) if env_bool("TRUST_PROXY_HEADERS", False) else ""
        if not client_ip:
            client_ip = (request.client.host if request.client else "") or device_ip or forwarded or real_ip or "127.0.0.1"

        device_name = request.headers.get("X-Device-Name", "").strip()[:255] or "Workstation"
        device_location = request.headers.get("X-Device-Location", "").strip()[:255]
        user_agent = request.headers.get("User-Agent", "").strip()[:1000]

        if not device_location:
            if client_ip in ("127.0.0.1", "::1", "localhost", "testclient"):
                device_location = "Local Workstation"
            else:
                device_location = "Remote Network"

        audit_token = set_audit_context({
            "ip_address": client_ip[:45],
            "device_name": device_name,
            "device_location": device_location,
            "user_agent": user_agent,
        })

        logger.info("request.start request_id=%s method=%s path=%s", request_id, request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request.failed request_id=%s method=%s path=%s", request_id, request.method, request.url.path)
            raise
        finally:
            reset_audit_context(audit_token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request.complete request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
