"""
Cross-Site Request Forgery (CSRF) Protection Middleware.
Ensures mutating HTTP verbs (POST, PATCH, PUT, DELETE) present a valid CSRF token
when an access token cookie is present.
"""

import secrets

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import CSRF_PROTECTION_ENABLED


class CSRFMiddleware(BaseHTTPMiddleware):
    """Guards against Cross-Site Request Forgery for authenticated browser sessions."""

    async def dispatch(self, request: Request, call_next):
        if (
            CSRF_PROTECTION_ENABLED
            and request.method in {"POST", "PATCH", "PUT", "DELETE"}
            and request.cookies.get("access_token")
        ):
            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("X-CSRF-Token")
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                return Response(
                    content='{"detail":"Invalid CSRF token"}',
                    status_code=403,
                    media_type="application/json",
                )
        return await call_next(request)
