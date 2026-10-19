"""
User Token and Access Authentication Middleware.
Inspects incoming requests, validates JWT access tokens (from Cookie or Bearer header),
checks user active status against the database, and injects authenticated user context
into request.state.
"""

from contextlib import closing
import logging
from typing import Set

from fastapi import Request, Response
import jwt
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import ALGORITHM, SECRET_KEY
from core.database import connect

logger = logging.getLogger("pathology.auth_middleware")

# Endpoints that are explicitly public and do not require user authentication
PUBLIC_PREFIXES = (
    "/api/auth/login",
    "/api/status",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def extract_token_from_request(request: Request) -> str | None:
    """Extract JWT token from cookies or Authorization Bearer header."""
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    return token


def decode_and_verify_token(token: str) -> dict | None:
    """Verifies JWT signature and extracts user payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


class AuthAccessMiddleware(BaseHTTPMiddleware):
    """
    Middleware that intercepts requests to authenticate users,
    validates active credentials, and populates `request.state.user`.
    """

    async def dispatch(self, request: Request, call_next):
        # Default unauthenticated state
        request.state.user = None
        request.state.is_authenticated = False

        token = extract_token_from_request(request)

        if token:
            payload = decode_and_verify_token(token)
            if payload and "sub" in payload:
                username = payload["sub"]
                try:
                    with closing(connect()) as db:
                        user = db.execute(
                            "SELECT id, username, display_name, role, is_active FROM users WHERE username = ?",
                            (username,),
                        ).fetchone()
                        if user and user.get("is_active"):
                            request.state.user = dict(user)
                            request.state.is_authenticated = True
                except Exception as exc:
                    logger.warning("AuthAccessMiddleware database check failed: %s", exc)

        return await call_next(request)
