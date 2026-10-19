"""Provision the two protected local Super Administrator accounts."""

import getpass
import os
from contextlib import closing

from main import connect, get_password_hash


PROTECTED_ADMIN_USERS = (
    ("ETDDev", "ETD Developer"),
    ("ETDadmin", "EID Administrator"),
)


def create_super_admins():
    password = os.getenv("PROTECTED_ADMIN_PASSWORD") or getpass.getpass("Password for both protected administrators: ")
    if not password:
        raise SystemExit("Password is required.")
    password_hash = get_password_hash(password)

    with closing(connect()) as db:
        try:
            for username, display_name in PROTECTED_ADMIN_USERS:
                existing = db.execute(
                    "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
                    (username,),
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE users SET username=?, display_name=?, role='System Administrator', password_hash=?, is_active=1 WHERE id=?",
                        (username, display_name, password_hash, existing["id"]),
                    )
                else:
                    db.execute(
                        "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, 'System Administrator', ?)",
                        (username, display_name, password_hash),
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise SystemExit("Failed to provision protected System Administrator accounts.")

    print("The two protected Super Administrator accounts were provisioned successfully.")


if __name__ == "__main__":
    create_super_admins()
