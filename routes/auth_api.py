"""
Authentication, User Management, and Session Controller.
Handles:
- /api/auth/login
- /api/auth/logout
- /api/auth/me
- /api/users (CRUD and password resets)
"""

from collections import defaultdict, deque
from contextlib import closing
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from core.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    COOKIE_SECURE,
    LOGIN_RATE_LIMIT,
    LOGIN_RATE_WINDOW_SECONDS,
)
from core.security import (
    ADMIN_ROLES,
    ASSIGNABLE_USER_ROLES,
    create_access_token,
    get_password_hash,
    is_protected_super_admin,
    validate_password,
    verify_password,
)


class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str
    role: str


class UserStatusUpdate(BaseModel):
    is_active: bool


def create_auth_router(connect, require_roles, get_current_user, audit):
    router = APIRouter()
    login_attempts = defaultdict(deque)

    def is_super_admin(user):
        return user.get("role") == "Super Administrator" or is_protected_super_admin(user.get("username"))

    def allowed_managed_roles(user):
        if is_super_admin(user):
            return set(ASSIGNABLE_USER_ROLES)
        return {"Laboratory Manager", "Pathologist"}

    # --- Authentication Endpoints ---

    @router.post("/api/auth/login")
    def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
        client_key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        attempts = login_attempts[client_key]
        while attempts and now - attempts[0] > LOGIN_RATE_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again shortly.")

        with closing(connect()) as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (form_data.username,)).fetchone()
            if not user or not verify_password(form_data.password, user["password_hash"]):
                attempts.append(now)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
            if not user["is_active"]:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")

            attempts.clear()
            db.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
            audit(db, user["username"], "auth.login", "user", user["id"])
            db.commit()

            access_token = create_access_token(data={"sub": user["username"], "role": user["role"]})
            csrf_token = secrets.token_urlsafe(32)

            response = Response(content='{"message": "success"}', media_type="application/json")
            response.set_cookie(
                key="access_token",
                value=access_token,
                httponly=True,
                secure=COOKIE_SECURE,
                samesite="lax",
                max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            )
            response.set_cookie(
                key="csrf_token",
                value=csrf_token,
                httponly=False,
                secure=COOKIE_SECURE,
                samesite="lax",
                max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            )
            return response

    @router.post("/api/auth/logout")
    def logout(current_user: dict = Depends(get_current_user)):
        response = Response(content='{"message": "logged out"}', media_type="application/json")
        response.delete_cookie("access_token")
        response.delete_cookie("csrf_token")
        with closing(connect()) as db:
            audit(db, current_user["username"], "auth.logout", "user", current_user["id"])
            db.commit()
        return response

    @router.get("/api/auth/me")
    def read_users_me(response: Response, current_user: dict = Depends(get_current_user)):
        response.set_cookie(
            key="csrf_token",
            value=secrets.token_urlsafe(32),
            httponly=False,
            secure=COOKIE_SECURE,
            samesite="lax",
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        return {
            "username": current_user["username"],
            "role": current_user["role"],
            "display_name": current_user["display_name"],
        }

    # --- User Management Endpoints (Admin Only) ---

    @router.get("/api/users")
    def get_users(current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        with closing(connect()) as db:
            query = "SELECT id, username, display_name, role, is_active, is_premium, created_at, last_login FROM users"
            if not is_super_admin(current_user):
                query += " WHERE role <> 'Super Administrator' AND LOWER(username) NOT IN ('etddev', 'etdadmin')"
            users = db.execute(query).fetchall()
            return [dict(u) for u in users]

    @router.post("/api/users")
    def create_user(user: UserCreate, current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        if is_protected_super_admin(user.username):
            raise HTTPException(
                status_code=403,
                detail="Reserved protected-administrator usernames cannot be created through user management",
            )
        if user.role not in allowed_managed_roles(current_user):
            raise HTTPException(status_code=403, detail="You cannot create an account with this role")
        validate_password(user.password)
        with closing(connect()) as db:
            if user.role == "Super Administrator":
                count = db.execute("SELECT COUNT(*) AS count FROM users WITH (UPDLOCK,HOLDLOCK) WHERE role='Super Administrator'").fetchone()["count"]
                if count >= 2:
                    raise HTTPException(status_code=409, detail="A maximum of two Super Administrators is allowed")
            existing = db.execute("SELECT id FROM users WHERE username = ?", (user.username,)).fetchone()
            if existing:
                raise HTTPException(status_code=400, detail="Username already exists")

            hashed = get_password_hash(user.password)
            try:
                cursor = db.execute(
                    "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                    (user.username, user.display_name, user.role, hashed),
                )
                audit(db, current_user["username"], "user.create", "user", cursor.lastrowid, user.role)
                db.commit()
                return {
                    "id": cursor.lastrowid,
                    "username": user.username,
                    "display_name": user.display_name,
                    "role": user.role,
                }
            except Exception:
                raise HTTPException(status_code=500, detail="Could not create user")

    @router.delete("/api/users/{user_id}")
    def delete_user(user_id: int, current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        with closing(connect()) as db:
            target = db.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target["id"] == current_user["id"]:
                raise HTTPException(status_code=400, detail="You cannot delete your own account")
            if target["role"] in {"Super Administrator", "System Administrator"} and not is_super_admin(current_user):
                raise HTTPException(status_code=403, detail="Only a Super Administrator may delete administrator accounts")

            count_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            if count_row and count_row["cnt"] <= 1:
                raise HTTPException(status_code=400, detail="Cannot delete the last remaining user")

            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            audit(db, current_user["username"], "user.delete", "user", user_id)
            db.commit()
            return {"message": "User deleted successfully"}

    @router.patch("/api/users/{user_id}/role")
    def update_user_role(user_id: int, change: dict, current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        requested_role = change.get("role")
        if requested_role not in allowed_managed_roles(current_user):
            raise HTTPException(status_code=403, detail="You cannot assign this role")
        with closing(connect()) as db:
            target = db.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target["id"] == current_user["id"]:
                raise HTTPException(status_code=400, detail="You cannot change your own role")
            if target["role"] in {"Super Administrator", "System Administrator"} and not is_super_admin(current_user):
                raise HTTPException(status_code=403, detail="Only a Super Administrator may change administrator roles")
            if requested_role == "Super Administrator" and target["role"] != "Super Administrator":
                count = db.execute("SELECT COUNT(*) AS count FROM users WITH (UPDLOCK,HOLDLOCK) WHERE role='Super Administrator'").fetchone()["count"]
                if count >= 2:
                    raise HTTPException(status_code=409, detail="A maximum of two Super Administrators is allowed")

            if target["role"] == "System Administrator" and change["role"] != "System Administrator":
                count_row = db.execute(
                    "SELECT COUNT(*) as cnt FROM users WHERE role = 'System Administrator'"
                ).fetchone()
                if count_row and count_row["cnt"] <= 1:
                    raise HTTPException(status_code=400, detail="Cannot demote the last System Administrator")

            db.execute("UPDATE users SET role = ? WHERE id = ?", (requested_role, user_id))
            audit(db, current_user["username"], "user.role_change", "user", user_id, requested_role)
            db.commit()
        return {"message": "Role updated", "role": requested_role}

    @router.patch("/api/users/{user_id}/status")
    def update_user_status(user_id: int, change: UserStatusUpdate, current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        with closing(connect()) as db:
            target = db.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target["id"] == current_user["id"] and not change.is_active:
                raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
            if (is_protected_super_admin(target["username"]) or target["role"] in {"Super Administrator", "System Administrator"}) and not is_super_admin(current_user):
                raise HTTPException(status_code=403, detail="Only a Super Administrator may change administrator account status")
            if not is_super_admin(current_user) and target["role"] not in allowed_managed_roles(current_user):
                raise HTTPException(status_code=403, detail="You cannot change this account status")
            db.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if change.is_active else 0, user_id))
            audit(db, current_user["username"], "user.status_change", "user", user_id, f"is_active={change.is_active}")
            db.commit()
        return {"message": "User status updated", "is_active": change.is_active}

    @router.patch("/api/users/{user_id}/password")
    def reset_user_password(user_id: int, change: dict, current_user: dict = Depends(require_roles(ADMIN_ROLES))):
        new_password = change.get("password")
        validate_password(new_password)
        with closing(connect()) as db:
            target = db.execute("SELECT id,role FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target["role"] in {"Super Administrator", "System Administrator"} and not is_super_admin(current_user):
                raise HTTPException(status_code=403, detail="Only a Super Administrator may reset administrator passwords")

            hashed = get_password_hash(new_password)
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))
            audit(db, current_user["username"], "user.password_reset", "user", user_id)
            db.commit()
        return {"message": "Password reset successfully"}

    return router
