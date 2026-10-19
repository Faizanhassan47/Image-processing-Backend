"""
Database Connection and Query Execution Wrappers.
Handles SQL Server pyodbc connections, dictionary-based row cursors, and transactions.
"""

import pyodbc
from .config import (
    DB_DRIVER,
    DB_SERVER,
    DB_NAME,
    DB_TRUSTED_CONNECTION,
    DB_TRUST_SERVER_CERTIFICATE,
    DB_CONNECTION_TIMEOUT,
    DB_USERNAME,
    DB_PASSWORD,
)


class DictCursor:
    """Cursor wrapper that returns rows as dicts for clean, intuitive key access."""

    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self._lastrowid = lastrowid

    def execute(self, *args):
        self.cursor.execute(*args)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if not row:
            return None
        if hasattr(self.cursor, "description") and self.cursor.description:
            return dict(zip([column[0].lower() for column in self.cursor.description], row))
        return dict(row)

    def __iter__(self):
        return iter(self.fetchall())

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        if hasattr(self.cursor, "description") and self.cursor.description:
            cols = [column[0].lower() for column in self.cursor.description]
            return [dict(zip(cols, row)) for row in rows]
        return [dict(row) for row in rows]

    @property
    def lastrowid(self):
        return self._lastrowid


class DBWrapper:
    """Manages an active SQL Server database connection session."""

    def __init__(self):
        parts = [f"DRIVER={{{DB_DRIVER}}}", f"SERVER={DB_SERVER}", f"DATABASE={DB_NAME}"]
        if DB_TRUSTED_CONNECTION:
            parts.append("Trusted_Connection=yes")
        else:
            if not DB_USERNAME or not DB_PASSWORD:
                raise RuntimeError("DB_USERNAME and DB_PASSWORD are required when trusted authentication is disabled")
            parts.extend([f"UID={DB_USERNAME}", f"PWD={DB_PASSWORD}"])
        parts.append(f"TrustServerCertificate={'yes' if DB_TRUST_SERVER_CERTIFICATE else 'no'}")
        
        self.conn = pyodbc.connect(";".join(parts) + ";", autocommit=False, timeout=DB_CONNECTION_TIMEOUT)

    def execute(self, *args):
        cursor = self.conn.cursor()
        cursor.execute(*args)
        lastrowid = None
        sql = args[0].strip().upper() if args else ""
        if sql.startswith("INSERT"):
            try:
                cursor.execute("SELECT @@IDENTITY")
                row = cursor.fetchone()
                if row and row[0] is not None:
                    lastrowid = int(row[0])
            except Exception:
                pass
        return DictCursor(cursor, lastrowid)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        self.close()


def connect():
    """Factory helper to obtain a DBWrapper connection."""
    return DBWrapper()
