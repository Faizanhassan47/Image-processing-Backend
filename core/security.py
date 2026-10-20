"""
Security, Password Hashing, JWT Tokens, and Role Verification.
Centralized location for all authentication rules and access controls.
"""

from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request, status
import jwt
from passlib.context import CryptContext
from audit_context import get_audit_context
from .config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    PASSWORD_MIN_LENGTH,
    REQUIRE_PASSWORD_COMPLEXITY,
)
from .database import connect

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# =====================================================================
# SINGLE SOURCE OF TRUTH FOR SYSTEM ROLES & ACCESS CONTROL GROUPS
# =====================================================================

ROLE_ADMIN = "System Administrator"
ROLE_LAB_MANAGER = "Laboratory Manager"
ROLE_PATHOLOGIST = "Pathologist"
ROLE_AUDITOR = "Auditor"

# All valid individual roles in the system
USER_ROLES = {
    ROLE_ADMIN,
    ROLE_LAB_MANAGER,
    ROLE_PATHOLOGIST,
    ROLE_AUDITOR,
}
ASSIGNABLE_USER_ROLES = USER_ROLES

# Predefined role permission groups (used across routes & services)
ADMIN_ROLES = [ROLE_ADMIN]
INTAKE_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER]
CLINICAL_WRITE_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER]
CLINICAL_READ_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER, ROLE_PATHOLOGIST, ROLE_AUDITOR]
AI_RUN_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER, ROLE_PATHOLOGIST]
AI_READ_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER, ROLE_PATHOLOGIST, ROLE_AUDITOR]
INTEGRATION_ROLES = [ROLE_ADMIN]
OPERATIONAL_ROLES = [ROLE_ADMIN, ROLE_LAB_MANAGER, ROLE_AUDITOR]
AUDIT_ROLES = [ROLE_ADMIN, ROLE_AUDITOR]

# Aliases for backwards compatibility with legacy routes
READ_ROLES = CLINICAL_READ_ROLES
WRITE_ROLES = INTAKE_ROLES
EXPORT_ROLES = CLINICAL_READ_ROLES

# Protected System Administrator accounts
PROTECTED_SUPER_ADMINS = {"etddev", "etdadmin"}



def is_protected_super_admin(username: str) -> bool:
    """Checks if a given username corresponds to a reserved protected administrator."""
    return (username or "").casefold() in PROTECTED_SUPER_ADMINS


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against a stored bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Generates a secure salted bcrypt hash for a password."""
    return pwd_context.hash(password)


def validate_password(password: str):
    """Enforces enterprise password length and complexity requirements."""
    pwd = password or ""
    if len(pwd) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {PASSWORD_MIN_LENGTH} characters",
        )
    if REQUIRE_PASSWORD_COMPLEXITY:
        has_letter = any(ch.isalpha() for ch in pwd)
        has_digit = any(ch.isdigit() for ch in pwd)
        if not (has_letter and has_digit):
            raise HTTPException(
                status_code=400,
                detail="Password must contain both letters and numbers",
            )


def create_access_token(data: dict) -> str:
    """Issues a signed JWT access token with an expiration timestamp."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency to extract and authenticate the requesting user.
    Reads token from HTTP-only cookie or Authorization: Bearer header.
    """
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not user["is_active"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
        return dict(user)


def require_roles(allowed_roles: list[str]):
    """
    FastAPI dependency factory to enforce Role-Based Access Control (RBAC).
    """
    def role_checker(current_user: dict = Depends(get_current_user)):
        expanded_roles = set(allowed_roles)
        # Aliases for backwards compatibility
        if "Admin" in expanded_roles:
            expanded_roles.add("System Administrator")
        if "Technician" in expanded_roles:
            expanded_roles.add("Laboratory Manager")
        if "Viewer" in expanded_roles:
            expanded_roles.update({"Laboratory Manager", "Pathologist"})

        if current_user["role"] not in expanded_roles and current_user["role"] not in {"System Administrator", "Super Administrator"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
        return current_user

    return role_checker


def audit(db, actor: str, action: str, target_type: str, target_id="", details=""):
    """Records an immutable security audit event into the audit_log table."""
    metadata = get_audit_context()
    db.execute(
        """INSERT INTO audit_log (
            actor_username, action, target_type, target_id, details,
            ip_address, device_name, device_location, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            actor,
            action,
            target_type,
            str(target_id),
            details,
            metadata.get("ip_address"),
            metadata.get("device_name"),
            metadata.get("device_location"),
            metadata.get("user_agent"),
        ),
    )
