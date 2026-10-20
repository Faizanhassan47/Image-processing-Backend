"""Interactively create the first administrator without storing a raw password."""

import getpass
from contextlib import closing

from core.database import connect
from core.security import get_password_hash, validate_password


def main():
    username = input("System administrator username [etd-dev]: ").strip() or "etd-dev"
    display_name = input("Display name [Administrator]: ").strip() or "Administrator"
    password = getpass.getpass("Password: ")
    validate_password(password)
    with closing(connect()) as db:
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise SystemExit(f"User '{username}' already exists.")
        db.execute(
            "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, 'System Administrator', ?)",
            (username, display_name, get_password_hash(password)),
        )
        db.commit()
    print(f"System Administrator user '{username}' created.")


if __name__ == "__main__":
    main()
