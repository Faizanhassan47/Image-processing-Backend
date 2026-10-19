from collections import OrderedDict, defaultdict, deque
import asyncio
from contextlib import asynccontextmanager, closing, contextmanager
from io import BytesIO
from io import StringIO
import csv
import html
import math
import json
import os
import secrets
import time
import logging
from pathlib import Path
import pyodbc
import os
from threading import RLock
from uuid import uuid4

from dotenv import load_dotenv
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta, timezone
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi import Depends, HTTPException, status, Request
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from image_validation import ImageValidationError, validate_image_file
from audit_context import get_audit_context, reset_audit_context, set_audit_context

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

UPLOADS = Path(os.getenv("UPLOADS_DIR", BASE / "uploads"))
if not UPLOADS.is_absolute():
    UPLOADS = BASE / UPLOADS
UPLOADS.mkdir(parents=True, exist_ok=True)

RASTER_MAX_BYTES = int(os.getenv("RASTER_MAX_UPLOAD_MB", "10")) * 1024 * 1024
WSI_MAX_BYTES = int(os.getenv("WSI_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
UPLOAD_CHUNK_BYTES = int(os.getenv("UPLOAD_CHUNK_MB", "8")) * 1024 * 1024
TILE_SIZE = int(os.getenv("TILE_SIZE", "254"))
TILE_OVERLAP = int(os.getenv("TILE_OVERLAP", "1"))
RASTER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


def _load_openslide():
    try:
        dll_path = os.getenv("OPENSLIDE_PATH")
        if os.name == "nt" and dll_path and hasattr(os, "add_dll_directory"):
            with os.add_dll_directory(dll_path):
                import openslide
                from openslide.deepzoom import DeepZoomGenerator
        else:
            import openslide
            from openslide.deepzoom import DeepZoomGenerator
        return openslide, DeepZoomGenerator, None
    except (ImportError, OSError) as exc:
        return None, None, str(exc)


openslide, DeepZoomGenerator, OPENSLIDE_IMPORT_ERROR = _load_openslide()



class DictCursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self._lastrowid = lastrowid
    def execute(self, *args):
        self.cursor.execute(*args)
        return self
    def fetchone(self):
        row = self.cursor.fetchone()
        if not row: return None
        if hasattr(self.cursor, 'description') and self.cursor.description:
            return dict(zip([column[0].lower() for column in self.cursor.description], row))
        return dict(row) # fallback
    
    def __iter__(self):
        return iter(self.fetchall())
    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows: return []
        if hasattr(self.cursor, 'description') and self.cursor.description:
            cols = [column[0].lower() for column in self.cursor.description]
            return [dict(zip(cols, row)) for row in rows]
        return [dict(row) for row in rows]
    @property
    def lastrowid(self):
        return self._lastrowid

class DBWrapper:
    def __init__(self):
        driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
        server = os.getenv("DB_SERVER", "localhost")
        dbname = os.getenv("DB_NAME", "Hello")
        trusted = env_bool("DB_TRUSTED_CONNECTION", True)
        trust_certificate = env_bool("DB_TRUST_SERVER_CERTIFICATE", False)
        timeout = int(os.getenv("DB_CONNECTION_TIMEOUT", "30"))
        parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={dbname}"]
        if trusted:
            parts.append("Trusted_Connection=yes")
        else:
            username = os.getenv("DB_USERNAME", "")
            password = os.getenv("DB_PASSWORD", "")
            if not username or not password:
                raise RuntimeError("DB_USERNAME and DB_PASSWORD are required when trusted authentication is disabled")
            parts.extend([f"UID={username}", f"PWD={password}"])
        parts.append(f"TrustServerCertificate={'yes' if trust_certificate else 'no'}")
        self.conn = pyodbc.connect(";".join(parts) + ";", autocommit=False, timeout=timeout)
    def execute(self, *args):
        cursor = self.conn.cursor()
        cursor.execute(*args)
        lastrowid = None
        sql = args[0].strip().upper()
        if sql.startswith("INSERT"):
            try:
                cursor.execute("SELECT @@IDENTITY")
                row = cursor.fetchone()
                if row and row[0] is not None:
                    lastrowid = int(row[0])
            except:
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
        if exc_type is None:
            self.commit()
        self.close()

def connect():
    return DBWrapper()


def init_db():
    """Create the slide model and idempotently lift legacy case files into it."""
    with closing(connect()) as db:
        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='users' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='users' and xtype='U')
CREATE TABLE users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                display_name VARCHAR(MAX) NOT NULL,
                role VARCHAR(MAX) NOT NULL,
                password_hash VARCHAR(MAX) NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at VARCHAR(MAX) DEFAULT CURRENT_TIMESTAMP,
                last_login DATETIME
            )
        """)
        user_columns = {row['name'] for row in db.execute("SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='users'")}
        if 'password' in user_columns:
            db.execute("UPDATE users SET password = NULL")
            try:
                db.execute("ALTER TABLE users DROP COLUMN password")
            except pyodbc.Error:
                pass
        db.execute("UPDATE users SET role = 'System Administrator' WHERE role = 'Admin'")
        db.execute("UPDATE users SET role = 'Histotechnologist' WHERE role = 'Technician'")
        db.execute("UPDATE users SET role = 'Auditor' WHERE role = 'Viewer'")
        db.execute("UPDATE users SET username = 'ETDadmin' WHERE LOWER(username) = 'eidadmin' AND NOT EXISTS (SELECT 1 FROM users WHERE LOWER(username) = 'etdadmin')")
        db.execute("UPDATE users SET role = 'Super Administrator' WHERE LOWER(username) IN ('etddev', 'etdadmin')")
        db.execute("IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_users_super_admin_reserved') ALTER TABLE users ADD CONSTRAINT CK_users_super_admin_reserved CHECK (role <> 'Super Administrator' OR LOWER(username) IN ('etddev','etdadmin'))")
            
        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='cases' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='cases' and xtype='U')
CREATE TABLE cases (
            id INT IDENTITY(1,1) PRIMARY KEY,
            case_id VARCHAR(255) NOT NULL UNIQUE,
            description VARCHAR(MAX) NOT NULL,
            filename VARCHAR(255) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        case_columns = {row['name'] for row in db.execute("SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='cases'")}
        for name, definition in {
            "file_type": "TEXT NOT NULL DEFAULT 'raster'",
            "format": "TEXT",
            "width": "INT",
            "height": "INT",
            "file_size": "INT",
            "status": "TEXT NOT NULL DEFAULT 'New'",
            "reviewer": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in case_columns:
                db.execute(f"ALTER TABLE cases ADD {name} {definition}")

        db.execute('''IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slides' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slides' and xtype='U')
CREATE TABLE slides (
            id INT IDENTITY(1,1) PRIMARY KEY,
            case_id INT NOT NULL REFERENCES cases(id) ,
            label VARCHAR(MAX) NOT NULL,
            stain VARCHAR(MAX) NOT NULL DEFAULT '',
            specimen_description VARCHAR(MAX) NOT NULL DEFAULT '',
            filename VARCHAR(255) NOT NULL UNIQUE,
            file_type VARCHAR(MAX) NOT NULL CHECK (file_type IN ('raster', 'wsi')),
            format VARCHAR(MAX) NOT NULL,
            file_size INT NOT NULL,
            width INT NOT NULL,
            height INT NOT NULL,
            level_count INT NOT NULL DEFAULT 1,
            thumbnail VARBINARY(MAX),
            upload_state VARCHAR(MAX) NOT NULL DEFAULT 'uploading' CHECK (upload_state IN ('uploading', 'processing', 'ready', 'error')),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            qc_status VARCHAR(MAX),
            qc_missing_tissue INTEGER DEFAULT 0,
            qc_blur INTEGER DEFAULT 0,
            qc_scan_artifact INTEGER DEFAULT 0,
            qc_incorrect_label INTEGER DEFAULT 0,
            qc_unusual_staining INTEGER DEFAULT 0,
            qc_note VARCHAR(MAX) DEFAULT '',
            qc_actor VARCHAR(MAX) DEFAULT '',
            qc_timestamp DATETIME
        )''')
        slide_columns = {row['name'] for row in db.execute("SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='slides'")}
        for name, definition in {
            "qc_status": "TEXT",
            "qc_missing_tissue": "INTEGER DEFAULT 0",
            "qc_blur": "INTEGER DEFAULT 0",
            "qc_scan_artifact": "INTEGER DEFAULT 0",
            "qc_incorrect_label": "INTEGER DEFAULT 0",
            "qc_unusual_staining": "INTEGER DEFAULT 0",
            "qc_note": "TEXT DEFAULT ''",
            "qc_actor": "TEXT DEFAULT ''",
            "qc_timestamp": "TEXT",
        }.items():
            if name not in slide_columns:
                db.execute(f"ALTER TABLE slides ADD {name} {definition}")
                
        db.execute('''IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slide_qc_history' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slide_qc_history' and xtype='U')
CREATE TABLE slide_qc_history (
            id INT IDENTITY(1,1) PRIMARY KEY,
            slide_id INT NOT NULL REFERENCES slides(id) ,
            qc_status VARCHAR(MAX),
            qc_missing_tissue BIT,
            qc_blur BIT,
            qc_scan_artifact BIT,
            qc_incorrect_label BIT,
            qc_unusual_staining BIT,
            qc_note VARCHAR(MAX),
            actor VARCHAR(MAX) NOT NULL,
            created_at VARCHAR(MAX) DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_slide_qc_history_slide_id' AND object_id = OBJECT_ID('slide_qc_history')) CREATE INDEX idx_slide_qc_history_slide_id ON slide_qc_history(slide_id, id)")
        db.execute("IF COL_LENGTH('slide_qc_history','qc_unusual_staining') IS NULL ALTER TABLE slide_qc_history ADD qc_unusual_staining BIT NULL")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_slides_case_id' AND object_id = OBJECT_ID('slides')) CREATE INDEX idx_slides_case_id ON slides(case_id, id)")

        cursor = db.execute("SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='annotations'")
        columns = [row['name'] for row in cursor.fetchall()]
        if columns and 'points' not in columns:
            db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='annotations_new' and xtype='U')
CREATE TABLE annotations_new (
                id INT IDENTITY(1,1) PRIMARY KEY,
                slide_id INT NOT NULL REFERENCES slides(id),
                annotation_type VARCHAR(MAX) NOT NULL DEFAULT 'rectangle'
                    CHECK (annotation_type IN ('rectangle', 'polygon', 'arrow')),
                label VARCHAR(MAX) NOT NULL,
                note VARCHAR(MAX) NOT NULL DEFAULT '',
                color VARCHAR(MAX) NOT NULL DEFAULT '#e85d75',
                x REAL,
                y REAL,
                width REAL,
                height REAL,
                points VARCHAR(MAX),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at VARCHAR(MAX) NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""INSERT INTO annotations_new 
                (id, slide_id, annotation_type, label, note, color, x, y, width, height, created_at, updated_at)
                SELECT id, slide_id, annotation_type, label, note, color, x, y, width, height, created_at, updated_at 
                FROM annotations""")
            db.execute("DROP TABLE annotations")
            db.execute("ALTER TABLE annotations_new RENAME TO annotations")
            db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id = OBJECT_ID('annotations')) CREATE INDEX idx_annotations_slide_id ON annotations(slide_id, id)")
        else:
            db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='annotations' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='annotations' and xtype='U')
CREATE TABLE annotations (
                id INT IDENTITY(1,1) PRIMARY KEY,
                slide_id INT NOT NULL REFERENCES slides(id) ,
                annotation_type VARCHAR(MAX) NOT NULL DEFAULT 'rectangle'
                    CHECK (annotation_type IN ('rectangle', 'polygon', 'arrow')),
                label VARCHAR(MAX) NOT NULL,
                note VARCHAR(MAX) NOT NULL DEFAULT '',
                color VARCHAR(MAX) NOT NULL DEFAULT '#e85d75',
                x REAL,
                y REAL,
                width REAL,
                height REAL,
                points VARCHAR(MAX),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at VARCHAR(MAX) NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id = OBJECT_ID('annotations')) CREATE INDEX idx_annotations_slide_id ON annotations(slide_id, id)")

        db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_notes' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_notes' and xtype='U')
CREATE TABLE case_review_notes (
            id INT IDENTITY(1,1) PRIMARY KEY,
            case_id INT NOT NULL REFERENCES cases(id) ,
            author VARCHAR(MAX) NOT NULL,
            note VARCHAR(MAX) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_case_review_notes_case_id' AND object_id = OBJECT_ID('case_review_notes')) CREATE INDEX idx_case_review_notes_case_id ON case_review_notes(case_id, id)")

        db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_history' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_history' and xtype='U')
CREATE TABLE case_review_history (
            id INT IDENTITY(1,1) PRIMARY KEY,
            case_id INT NOT NULL REFERENCES cases(id) ,
            change_type VARCHAR(MAX) NOT NULL CHECK (change_type IN ('status', 'reviewer')),
            previous_value VARCHAR(MAX) NOT NULL,
            new_value VARCHAR(MAX) NOT NULL,
            actor VARCHAR(MAX) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_case_review_history_case_id' AND object_id = OBJECT_ID('case_review_history')) CREATE INDEX idx_case_review_history_case_id ON case_review_history(case_id, id)")

        db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='reports' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='reports' and xtype='U')
CREATE TABLE reports (
            id INT IDENTITY(1,1) PRIMARY KEY,
            case_id INT NOT NULL REFERENCES cases(id) ,
            version INT NOT NULL,
            status VARCHAR(MAX) NOT NULL CHECK (status IN ('Draft', 'Finalized')),
            specimen_clinical VARCHAR(MAX) NOT NULL DEFAULT '',
            gross_description VARCHAR(MAX) NOT NULL DEFAULT '',
            microscopic_findings VARCHAR(MAX) NOT NULL DEFAULT '',
            diagnosis VARCHAR(MAX) NOT NULL DEFAULT '',
            comments VARCHAR(MAX) NOT NULL DEFAULT '',
            reviewer_name VARCHAR(MAX) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finalized_at VARCHAR(MAX)
        )""")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_reports_case_id' AND object_id = OBJECT_ID('reports')) CREATE INDEX idx_reports_case_id ON reports(case_id, version DESC)")

        db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='audit_log' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='audit_log' and xtype='U')
CREATE TABLE audit_log (
            id INT IDENTITY(1,1) PRIMARY KEY,
            actor_username VARCHAR(MAX) NOT NULL,
            action VARCHAR(MAX) NOT NULL,
            target_type VARCHAR(MAX) NOT NULL,
            target_id VARCHAR(MAX) NOT NULL DEFAULT '',
            details VARCHAR(MAX) NOT NULL DEFAULT '',
            ip_address VARCHAR(45) NULL,
            device_name VARCHAR(255) NULL,
            device_location VARCHAR(255) NULL,
            user_agent VARCHAR(1000) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("IF COL_LENGTH('audit_log','ip_address') IS NULL ALTER TABLE audit_log ADD ip_address VARCHAR(45) NULL")
        db.execute("IF COL_LENGTH('audit_log','device_name') IS NULL ALTER TABLE audit_log ADD device_name VARCHAR(255) NULL")
        db.execute("IF COL_LENGTH('audit_log','device_location') IS NULL ALTER TABLE audit_log ADD device_location VARCHAR(255) NULL")
        db.execute("IF COL_LENGTH('audit_log','user_agent') IS NULL ALTER TABLE audit_log ADD user_agent VARCHAR(1000) NULL")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_audit_log_created_at' AND object_id = OBJECT_ID('audit_log')) CREATE INDEX idx_audit_log_created_at ON audit_log(created_at DESC)")

        db.execute("""IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='report_findings' and xtype='U')
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='report_findings' and xtype='U')
CREATE TABLE report_findings (
            id INT IDENTITY(1,1) PRIMARY KEY,
            report_id INT NOT NULL REFERENCES reports(id) ,
            slide_id INT REFERENCES slides(id) ON DELETE SET NULL,
            annotation_id INT REFERENCES annotations(id) ON DELETE SET NULL,
            text VARCHAR(MAX) NOT NULL DEFAULT '',
            sort_order INT NOT NULL DEFAULT 0
        )""")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_report_findings_report_id' AND object_id = OBJECT_ID('report_findings')) CREATE INDEX idx_report_findings_report_id ON report_findings(report_id, sort_order)")

        # Lift legacy case rows: cases that have a non-empty filename but no
        # corresponding slide row yet get a Slide-1 entry inserted automatically.
        legacy_cases = db.execute(
            """SELECT id, filename, file_type, format, width, height, file_size
               FROM cases WHERE filename != '' AND id NOT IN (SELECT DISTINCT case_id FROM slides)"""
        ).fetchall()
        for lc in legacy_cases:
            db.execute(
                """INSERT INTO slides
                   (case_id, label, stain, specimen_description, filename,
                    file_type, format, file_size, width, height, level_count,
                    upload_state)
                   VALUES (?, 'Slide 1', '', '', ?, ?, ?, ?, ?, ?, 1, 'ready')""",
                (lc["id"], lc["filename"],
                 lc["file_type"] or "raster",
                 lc["format"] or "PNG",
                 lc["file_size"] or 0,
                 lc["width"] or 0,
                 lc["height"] or 0),
            )

        db.commit()



init_db()


class SlideCache:
    """Bounded LRU; caches OpenSlide handles and DeepZoomGenerator instances."""

    def __init__(self, capacity=4):
        self.capacity = capacity
        self._entries = OrderedDict()
        self._cache_lock = RLock()

    def _get_entry(self, path: Path):
        key = str(path)
        with self._cache_lock:
            entry = self._entries.get(key)
            if entry is None:
                slide = openslide.OpenSlide(key)
                deep_zoom = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP)
                entry = {
                    "slide": slide,
                    "deep_zoom": deep_zoom,
                    "lock": RLock(),
                }
                self._entries[key] = entry
                while len(self._entries) > self.capacity:
                    _, old_entry = self._entries.popitem(last=False)
                    old_entry["slide"].close()
            else:
                self._entries.move_to_end(key)
            return entry

    @contextmanager
    def acquire(self, path: Path):
        entry = self._get_entry(path)
        with entry["lock"]:
            yield entry["slide"]

    @contextmanager
    def acquire_generator(self, path: Path):
        entry = self._get_entry(path)
        with entry["lock"]:
            yield entry["deep_zoom"]

    def clear(self):
        with self._cache_lock:
            for entry in self._entries.values():
                entry["slide"].close()
            self._entries.clear()
        clear_tile_cache()
        clear_slide_meta_cache()


slide_cache = SlideCache(int(os.getenv("OPENSLIDE_CACHE_SIZE", "4")))

TILE_CACHE_CAPACITY = int(os.getenv("TILE_CACHE_CAPACITY", "1000"))
_tile_cache = OrderedDict()
_tile_cache_lock = RLock()

def get_cached_tile(slide_id: int, level: int, col: int, row: int):
    key = (slide_id, level, col, row)
    with _tile_cache_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
            return _tile_cache[key]
    return None

def set_cached_tile(slide_id: int, level: int, col: int, row: int, data: bytes):
    key = (slide_id, level, col, row)
    with _tile_cache_lock:
        _tile_cache[key] = data
        if len(_tile_cache) > TILE_CACHE_CAPACITY:
            _tile_cache.popitem(last=False)

def clear_tile_cache():
    with _tile_cache_lock:
        _tile_cache.clear()

_slide_meta_cache = {}
_slide_meta_lock = RLock()

def get_cached_wsi_slide(slide_id: int):
    with _slide_meta_lock:
        if slide_id in _slide_meta_cache:
            return _slide_meta_cache[slide_id]
    row, path = require_wsi_slide(slide_id)
    with _slide_meta_lock:
        _slide_meta_cache[slide_id] = (row, path)
    return row, path

def clear_slide_meta_cache():
    with _slide_meta_lock:
        _slide_meta_cache.clear()


@asynccontextmanager
async def lifespan(_app):
    from platform_api import watched_folder_loop
    stop_event=asyncio.Event(); watcher=asyncio.create_task(watched_folder_loop(connect,stop_event))
    try: yield
    finally:
        stop_event.set(); await watcher; slide_cache.clear()


app = FastAPI(title=os.getenv("APP_NAME", "Pathology Demo API"), lifespan=lifespan)

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
request_logger = logging.getLogger("pathology.api")

@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    started = time.perf_counter()
    request.state.request_id = request_id
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    device_ip = request.headers.get("X-Device-IP", "").strip()
    client_ip = (device_ip or forwarded or real_ip) if env_bool("TRUST_PROXY_HEADERS", False) else ""
    if not client_ip:
        client_ip = (request.client.host if request.client else "") or device_ip or forwarded or real_ip or "127.0.0.1"

    device_name = request.headers.get("X-Device-Name", "").strip()[:255]
    device_location = request.headers.get("X-Device-Location", "").strip()[:255]
    user_agent = request.headers.get("User-Agent", "").strip()[:1000]

    if not device_location:
        if client_ip in ("127.0.0.1", "::1", "localhost", "testclient"):
            device_location = "Local Workstation"
        else:
            device_location = "Remote Network"

    if not device_name:
        device_name = "Workstation"

    audit_token = set_audit_context({
        "ip_address": client_ip[:45],
        "device_name": device_name,
        "device_location": device_location,
        "user_agent": user_agent,
    })
    request_logger.info("request.start request_id=%s method=%s path=%s", request_id, request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        request_logger.exception("request.failed request_id=%s method=%s path=%s", request_id, request.method, request.url.path)
        raise
    finally:
        reset_audit_context(audit_token)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    request_logger.info("request.complete request_id=%s method=%s path=%s status=%s duration_ms=%s", request_id, request.method, request.url.path, response.status_code, duration_ms)
    return response


SECRET_KEY = os.environ["JWT_SECRET_KEY"]
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "4"))
COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_PROTECTION_ENABLED = env_bool("CSRF_PROTECTION_ENABLED", True)
LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "60"))
_login_attempts = defaultdict(deque)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def validate_password(password: str):
    if len(password or "") < PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {PASSWORD_MIN_LENGTH} characters")

def audit(db, actor: str, action: str, target_type: str, target_id="", details=""):
    metadata = get_audit_context()
    db.execute(
        "INSERT INTO audit_log (actor_username, action, target_type, target_id, details, ip_address, device_name, device_location, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (actor, action, target_type, str(target_id), details, metadata.get("ip_address"), metadata.get("device_name"), metadata.get("device_location"), metadata.get("user_agent")),
    )

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(request: Request):
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
        role: str = payload.get("role")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    
    with closing(connect()) as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not user["is_active"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
        return dict(user)

def require_roles(allowed_roles: list[str]):
    def role_checker(current_user: dict = Depends(get_current_user)):
        expanded_roles = set(allowed_roles)
        if "Admin" in expanded_roles:
            expanded_roles.add("System Administrator")
        if "Technician" in expanded_roles:
            expanded_roles.add("Histotechnologist")
        if "Viewer" in expanded_roles:
            expanded_roles.update({"Auditor", "Laboratory Manager", "AI Analyst"})
        if current_user["role"] not in expanded_roles and current_user["role"] not in {"System Administrator", "Super Administrator"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
        return current_user
    return role_checker

class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str
    role: str

USER_ROLES = {
    "System Administrator", "Laboratory Manager", "Pathologist",
    "Histotechnologist", "AI Analyst", "Integration Engineer", "Auditor",
}
ASSIGNABLE_USER_ROLES = USER_ROLES
PROTECTED_SUPER_ADMINS = {"etddev", "etdadmin"}

def is_protected_super_admin(username: str) -> bool:
    return (username or "").casefold() in PROTECTED_SUPER_ADMINS

@app.get("/api/users")
def get_users(current_user: dict = Depends(require_roles(["System Administrator"]))):
    with closing(connect()) as db:
        users = db.execute("SELECT id, username, display_name, role, is_active, created_at, last_login FROM users").fetchall()
        return [dict(u) for u in users]

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_roles(["System Administrator"]))):
    with closing(connect()) as db:
        target = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if is_protected_super_admin(target["username"]):
            raise HTTPException(status_code=403, detail="Protected System Administrator accounts cannot be deleted")
            
        count_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        if count_row and count_row["cnt"] <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining user")
            
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        audit(db, current_user["username"], "user.delete", "user", user_id)
        db.commit()
        return {"message": "User deleted successfully"}

@app.post("/api/users")
def create_user(user: UserCreate, current_user: dict = Depends(require_roles(["System Administrator"]))):
    if is_protected_super_admin(user.username):
        raise HTTPException(status_code=403, detail="Reserved Super Administrator usernames cannot be created through user management")
    if user.role not in ASSIGNABLE_USER_ROLES:
        raise HTTPException(status_code=400, detail="Invalid or protected role")
    validate_password(user.password)
    with closing(connect()) as db:
        existing = db.execute("SELECT id FROM users WHERE username = ?", (user.username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Username already exists")
        
        hashed = get_password_hash(user.password)
        try:
            cursor = db.execute(
                "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                (user.username, user.display_name, user.role, hashed)
            )
            audit(db, current_user["username"], "user.create", "user", cursor.lastrowid, user.role)
            db.commit()
            return {"id": cursor.lastrowid, "username": user.username, "display_name": user.display_name, "role": user.role}
        except Exception as e:
            raise HTTPException(status_code=500, detail="Could not create user")


@app.patch("/api/users/{user_id}/role")
def update_user_role(user_id: int, change: dict, current_user: dict = Depends(require_roles(["System Administrator"]))):
    if change.get("role") not in ASSIGNABLE_USER_ROLES:
        raise HTTPException(status_code=400, detail="Invalid or protected role")
    with closing(connect()) as db:
        target = db.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if is_protected_super_admin(target["username"]):
            raise HTTPException(status_code=403, detail="Protected System Administrator roles cannot be changed")
            
        if target["role"] == "System Administrator" and change["role"] != "System Administrator":
            count_row = db.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'System Administrator'").fetchone()
            if count_row and count_row["cnt"] <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last System Administrator")
                
        db.execute("UPDATE users SET role = ? WHERE id = ?", (change["role"], user_id))
        audit(db, current_user["username"], "user.role_change", "user", user_id, change["role"])
        db.commit()
    return {"message": "Role updated"}

@app.patch("/api/users/{user_id}/password")
def reset_user_password(user_id: int, change: dict, current_user: dict = Depends(require_roles(["System Administrator"]))):
    new_password = change.get("password")
    validate_password(new_password)
    with closing(connect()) as db:
        target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
            
        hashed = get_password_hash(new_password)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))
        audit(db, current_user["username"], "user.password_reset", "user", user_id)
        db.commit()
    return {"message": "Password reset successfully"}

@app.post("/api/auth/login")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    client_key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _login_attempts[client_key]
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
        response.set_cookie(key="access_token", value=access_token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        response.set_cookie(key="csrf_token", value=csrf_token, httponly=False, secure=COOKIE_SECURE, samesite="lax", max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        return response

@app.post("/api/auth/logout")
def logout(current_user: dict = Depends(get_current_user)):
    response = Response(content='{"message": "logged out"}', media_type="application/json")
    response.delete_cookie("access_token")
    response.delete_cookie("csrf_token")
    with closing(connect()) as db:
        audit(db, current_user["username"], "auth.logout", "user", current_user["id"])
        db.commit()
    return response

@app.get("/api/auth/me")
def read_users_me(response: Response, current_user: dict = Depends(get_current_user)):
    response.set_cookie(
        key="csrf_token",
        value=secrets.token_urlsafe(32),
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return {"username": current_user["username"], "role": current_user["role"], "display_name": current_user["display_name"]}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_credentials=env_bool("CORS_ALLOW_CREDENTIALS", False),
    allow_methods=[method.strip() for method in os.getenv("CORS_ALLOW_METHODS", "GET,POST,PATCH,DELETE,OPTIONS").split(",") if method.strip()],
    allow_headers=["*"],
)

@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    if CSRF_PROTECTION_ENABLED and request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.cookies.get("access_token"):
        cookie_token = request.cookies.get("csrf_token")
        header_token = request.headers.get("X-CSRF-Token")
        if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
            return Response(content='{"detail":"Invalid CSRF token"}', status_code=403, media_type="application/json")
    return await call_next(request)


def _stored_path(filename: str) -> Path:
    path = (UPLOADS / filename).resolve()
    if not filename or path.parent != UPLOADS.resolve():
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


def first_slide(case_pk: int):
    get_case(case_pk)
    with closing(connect()) as db:
        row = db.execute("SELECT TOP 1 * FROM slides WHERE case_id = ? ORDER BY id", (case_pk,)).fetchone()
    if row is None:
        raise HTTPException(404, "Case has no slides")
    return row


def serialize_slide(row):
    item = dict(row)
    base = f"/api/slides/{item['id']}"
    item["image_url"] = f"{base}/image" if item["file_type"] == "raster" else None
    item["thumbnail_url"] = f"{base}/thumbnail"
    item["dzi_url"] = f"{base}/slide.dzi" if item["file_type"] == "wsi" else None
    # Normalize SQLite integer booleans to Python bools
    for bool_field in ("qc_missing_tissue", "qc_blur", "qc_scan_artifact", "qc_incorrect_label", "qc_unusual_staining"):
        if bool_field in item and item[bool_field] is not None:
            item[bool_field] = bool(item[bool_field])
    return item


@app.get("/api/slides/{slide_id}/validation", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_validation(slide_id: int):
    get_slide(slide_id)
    with closing(connect()) as db:
        version = db.execute("SELECT TOP 1 * FROM image_versions WHERE slide_id=? ORDER BY version DESC", (slide_id,)).fetchone()
    if version is None:
        raise HTTPException(404, "No validation record exists for this slide")
    result = json.loads(version["metadata_json"] or "{}")
    result["image_version"] = version["version"]
    return result



def serialize_case(row, include_slides=True):
    item = dict(row)
    if include_slides:
        with closing(connect()) as db:
            slides = db.execute("SELECT * FROM slides WHERE case_id = ? ORDER BY id", (row["id"],)).fetchall()
        item["slides"] = [serialize_slide(slide) for slide in slides]
        item["slide_count"] = len(slides)
        with closing(connect()) as db:
            notes = db.execute(
                "SELECT * FROM case_review_notes WHERE case_id = ? ORDER BY id DESC", (row["id"],)
            ).fetchall()
            history = db.execute(
                "SELECT * FROM case_review_history WHERE case_id = ? ORDER BY id DESC", (row["id"],)
            ).fetchall()
        item["review_notes"] = [dict(note) for note in notes]
        item["review_history"] = [dict(entry) for entry in history]
    return item


def require_wsi_slide(slide_id: int):
    row = get_slide(slide_id)
    if row["file_type"] != "wsi":
        raise HTTPException(400, "Slide is not a whole-slide image")
    path = _stored_path(row["filename"])
    if not path.is_file():
        raise HTTPException(404, "Whole-slide image file is missing")
    if openslide is None:
        detail = "OpenSlide is unavailable. Install openslide-python and the native OpenSlide library."
        if OPENSLIDE_IMPORT_ERROR:
            detail += f" ({OPENSLIDE_IMPORT_ERROR})"
        raise HTTPException(503, detail)
    return row, path


@app.get("/api/status")
def api_status():
    return {
        "ok": True,
        "openslide_available": openslide is not None,
        "openslide_error": OPENSLIDE_IMPORT_ERROR,
        "wsi_max_upload_mb": WSI_MAX_BYTES // (1024 * 1024),
    }


@app.get("/api/cases", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def list_cases(
    status_filter: str | None = Query(None, alias="status"),
    reviewer: str | None = Query(None),
    search: str | None = Query(None),
):
    clauses = []
    params = []
    if status_filter:
        if status_filter not in {"New", "In Review", "Reviewed"}:
            raise HTTPException(400, "Status must be New, In Review, or Reviewed")
        clauses.append("status = ?")
        params.append(status_filter)
    
    if reviewer is not None:
        if reviewer.strip() == "":
            clauses.append("reviewer = ''")
        else:
            clauses.append("LOWER(reviewer) = LOWER(?)")
            params.append(reviewer.strip())
            
    if search and search.strip():
        search_term = f"%{search.strip()}%"
        clauses.append("(case_id LIKE ? OR description LIKE ?)")
        params.extend([search_term, search_term])
        
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect()) as db:
        rows = db.execute(f"SELECT * FROM cases{where} ORDER BY id DESC", params).fetchall()
    return [serialize_case(row) for row in rows]


@app.get("/api/cases/{case_pk}", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def case_detail(case_pk: int):
    return serialize_case(get_case(case_pk))


class ReviewerChange(BaseModel):
    reviewer: str = Field(default="", max_length=120)

class StatusChange(BaseModel):
    status: str

class SlideQcUpdate(BaseModel):
    qc_status: str | None = None
    qc_missing_tissue: bool = False
    qc_blur: bool = False
    qc_scan_artifact: bool = False
    qc_incorrect_label: bool = False
    qc_unusual_staining: bool = False
    qc_note: str = Field(default="", max_length=4000)

class ReviewNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


def _nonblank(value: str, field_name: str):
    value = value.strip()
    if not value:
        raise HTTPException(400, f"{field_name} cannot be blank")
    return value


def _append_history(db, case_pk, change_type, previous, new, actor):
    db.execute("""INSERT INTO case_review_history
        (case_id, change_type, previous_value, new_value, actor)
        VALUES (?, ?, ?, ?, ?)""", (case_pk, change_type, previous, new, actor))


@app.patch("/api/cases/{case_pk}/reviewer", dependencies=[Depends(require_roles(["Admin", "Pathologist", "Laboratory Manager"]))])
def assign_reviewer(case_pk: int, change: ReviewerChange, current_user: dict = Depends(get_current_user)):
    case = get_case(case_pk)
    actor = current_user["display_name"]
    reviewer = change.reviewer.strip()
    previous = case["reviewer"]
    if reviewer == previous:
        raise HTTPException(400, "Reviewer is already set to that value")
    with closing(connect()) as db:
        db.execute("UPDATE cases SET reviewer = ? WHERE id = ?", (reviewer, case_pk))
        _append_history(db, case_pk, "reviewer", previous, reviewer, actor)
        db.commit()
    return serialize_case(get_case(case_pk))



@app.post("/api/slides/{slide_pk}/qc", status_code=200, dependencies=[Depends(require_roles(["Admin", "Technician"]))])
def add_slide_qc(slide_pk: int, qc: SlideQcUpdate, current_user: dict = Depends(get_current_user)):
    get_slide(slide_pk)
    actor = current_user["display_name"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    with closing(connect()) as db:
        db.execute('''UPDATE slides SET 
            qc_status = ?, qc_missing_tissue = ?, qc_blur = ?, 
            qc_scan_artifact = ?, qc_incorrect_label = ?, qc_unusual_staining = ?, qc_note = ?, 
            qc_actor = ?, qc_timestamp = ? 
            WHERE id = ?''', 
            (qc.qc_status, qc.qc_missing_tissue, qc.qc_blur, 
             qc.qc_scan_artifact, qc.qc_incorrect_label, qc.qc_unusual_staining, qc.qc_note, 
             actor, now, slide_pk))
        
        db.execute('''INSERT INTO slide_qc_history (
            slide_id, qc_status, qc_missing_tissue, qc_blur, 
            qc_scan_artifact, qc_incorrect_label, qc_unusual_staining, qc_note, actor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (slide_pk, qc.qc_status, qc.qc_missing_tissue, qc.qc_blur,
             qc.qc_scan_artifact, qc.qc_incorrect_label, qc.qc_unusual_staining, qc.qc_note, actor))
        db.commit()
    
    return serialize_slide(get_slide(slide_pk))

@app.patch("/api/cases/{case_pk}/status", dependencies=[Depends(require_roles(["Admin", "Pathologist", "Technician", "Laboratory Manager"]))])
def change_case_status(case_pk: int, change: StatusChange, current_user: dict = Depends(get_current_user)):
    case = get_case(case_pk)
    actor = current_user["display_name"]
    new_status = change.status.strip()
    valid_statuses = {"New", "In Review", "Reviewed"}
    if new_status not in valid_statuses:
        raise HTTPException(400, "Status must be New, In Review, or Reviewed")
    current = case["status"]
    if new_status == current:
        return serialize_case(case)
    if new_status == "In Review":
        with closing(connect()) as db:
            needs_rescan = db.execute("SELECT COUNT(*) AS count FROM slides WHERE case_id = ? AND qc_status = 'Needs rescan'", (case_pk,)).fetchone()['count']
            if needs_rescan > 0:
                raise HTTPException(409, "Cannot mark case ready for review while a slide needs a rescan")
    with closing(connect()) as db:
        db.execute("UPDATE cases SET status = ? WHERE id = ?", (new_status, case_pk))
        _append_history(db, case_pk, "status", current, new_status, actor)
        db.commit()
    return serialize_case(get_case(case_pk))


@app.get("/api/cases/{case_pk}/review-notes", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def list_review_notes(case_pk: int):
    get_case(case_pk)
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT * FROM case_review_notes WHERE case_id = ? ORDER BY id DESC", (case_pk,)
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/cases/{case_pk}/review-notes", status_code=201, dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def add_review_note(case_pk: int, review_note: ReviewNoteCreate, current_user: dict = Depends(get_current_user)):
    get_case(case_pk)
    author = current_user["display_name"]
    note = _nonblank(review_note.note, "Review note")
    with closing(connect()) as db:
        cursor = db.execute(
            "INSERT INTO case_review_notes (case_id, author, note) VALUES (?, ?, ?)",
            (case_pk, author, note),
        )
        db.commit()
        row = db.execute("SELECT * FROM case_review_notes WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/cases/{case_pk}/review-history", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def list_review_history(case_pk: int):
    get_case(case_pk)
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT * FROM case_review_history WHERE case_id = ? ORDER BY id DESC", (case_pk,)
        ).fetchall()
    return [dict(row) for row in rows]


def _validated_case_id(value: str):
    value = value.strip()
    if not value or len(value) > 80:
        raise HTTPException(400, "Case ID must be 1 to 80 characters")
    return value


async def _save_stream(upload: UploadFile, path: Path, limit: int):
    written = 0
    try:
        with path.open("xb") as output:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"Whole-slide image exceeds the {limit // 1048576} MB demo limit")
                output.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if not written:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "Uploaded file is empty")
    return written


async def validate_and_store(upload: UploadFile):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".svs"}:
        await upload.close()
        raise HTTPException(400, "Upload a JPG, PNG, or Aperio SVS file")
    if suffix == ".svs" and openslide is None:
        await upload.close()
        raise HTTPException(503, "WSI upload requires OpenSlide. Install openslide-python and openslide-bin, or configure OPENSLIDE_PATH.")
    filename = f"{uuid4().hex}{suffix}"
    path = _stored_path(filename)
    try:
        await _save_stream(upload, path, WSI_MAX_BYTES if suffix == ".svs" else RASTER_MAX_BYTES)
        validation = validate_image_file(path, openslide)
    except ImageValidationError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return filename, validation


async def add_slide_record(case_pk, image, label, stain="", specimen_description=""):
    get_case(case_pk)
    label = label.strip()
    if not label or len(label) > 120:
        raise HTTPException(400, "Slide label must be 1 to 120 characters")
    stored = await validate_and_store(image)
    filename, validation = stored
    try:
        with closing(connect()) as db:
            duplicate = db.execute("SELECT TOP 1 stored_filename FROM image_versions WHERE checksum_sha256=?", (validation["checksum_sha256"],)).fetchone()
            if duplicate:
                raise HTTPException(409, "Duplicate image content already exists")
            cursor = db.execute("""INSERT INTO slides
                (case_id, label, stain, specimen_description, filename, file_type,
                 format, file_size, width, height, level_count, upload_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')""",
                (case_pk, label, stain.strip(), specimen_description.strip(), filename,
                 validation["file_type"], validation["format"], validation["file_size"],
                 validation["width"], validation["height"], validation["level_count"]))
            calibration = validation["physical_calibration"]
            pixel_size = calibration["pixel_size_microns_x"] if calibration["available"] else None
            db.execute("""INSERT INTO image_versions
                (slide_id,version,checksum_sha256,pixel_size_microns,metadata_json,stored_filename)
                VALUES (?,1,?,?,?,?)""",
                (cursor.lastrowid, validation["checksum_sha256"], pixel_size, json.dumps(validation), filename))
            db.commit()
            row = db.execute("SELECT * FROM slides WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except Exception:
        _stored_path(filename).unlink(missing_ok=True)
        raise
    return row


@app.post("/api/cases", status_code=201, dependencies=[Depends(require_roles(["Admin", "Technician"]))])
async def create_case(
    case_id: str = Form(...),
    description: str = Form(""),
    image: UploadFile | None = File(None),
    slide_label: str = Form("Slide 1"),
    stain: str = Form(""),
    specimen_description: str = Form(""),
):
    case_code = _validated_case_id(case_id)
    try:
        with closing(connect()) as db:
            cursor = db.execute(
                """INSERT INTO cases
                   (case_id, description, filename, file_type, format, width, height, file_size)
                   VALUES (?, ?, '', 'raster', NULL, NULL, NULL, NULL)""",
                (case_code, description.strip()),
            )
            db.commit()
            case_pk = cursor.lastrowid
    except pyodbc.IntegrityError as exc:
        raise HTTPException(409, "Case ID already exists") from exc

    try:
        if image is not None and image.filename:
            slide = await add_slide_record(case_pk, image, slide_label, stain, specimen_description)
            # Populate legacy columns so older clients still see the first slide.
            with closing(connect()) as db:
                db.execute("""UPDATE cases SET filename=?, file_type=?, format=?, width=?, height=?, file_size=?
                              WHERE id=?""",
                           (slide["filename"], slide["file_type"], slide["format"], slide["width"],
                            slide["height"], slide["file_size"], case_pk))
                db.commit()
    except Exception:
        with closing(connect()) as db:
            db.execute("DELETE FROM cases WHERE id = ?", (case_pk,))
            db.commit()
        raise
    return serialize_case(get_case(case_pk))


@app.post("/api/cases/{case_pk}/slides", status_code=201, dependencies=[Depends(require_roles(["Admin", "Technician"]))])
async def add_slide(
    case_pk: int,
    image: UploadFile = File(...),
    label: str = Form(...),
    stain: str = Form(""),
    specimen_description: str = Form(""),
):
    return serialize_slide(await add_slide_record(case_pk, image, label, stain, specimen_description))


@app.post("/api/slides/{slide_id}/versions", status_code=201, dependencies=[Depends(require_roles(["Admin", "Technician"]))])
async def reimport_slide_version(slide_id: int, image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    get_slide(slide_id)
    filename, validation = await validate_and_store(image)
    try:
        with closing(connect()) as db:
            previous = db.execute("SELECT TOP 1 version,checksum_sha256 FROM image_versions WHERE slide_id=? ORDER BY version DESC", (slide_id,)).fetchone()
            next_version = (previous["version"] if previous else 0) + 1
            validation["duplicate_of_previous_version"] = bool(previous and previous["checksum_sha256"] == validation["checksum_sha256"])
            calibration = validation["physical_calibration"]
            cursor = db.execute("""INSERT INTO image_versions
                (slide_id,version,checksum_sha256,source_device,pixel_size_microns,metadata_json,stored_filename)
                VALUES (?,?,?,?,?,?,?)""", (slide_id,next_version,validation["checksum_sha256"],"manual-reimport",
                calibration["pixel_size_microns_x"] if calibration["available"] else None,json.dumps(validation),filename))
            audit(db,current_user["username"],"image.version_import","slide",slide_id,f"version={next_version}; image_version_id={cursor.lastrowid}")
            db.commit()
        return {"id":cursor.lastrowid,"slide_id":slide_id,"version":next_version,"stored_filename":filename,"validation":validation}
    except Exception:
        _stored_path(filename).unlink(missing_ok=True)
        raise


class SlideUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    stain: str = Field(default="", max_length=120)
    specimen_description: str = Field(default="", max_length=1000)


class AnnotationCreate(BaseModel):
    annotation_type: str = Field(default="rectangle", pattern="^(point|line|polygon|rectangle|ellipse|freehand|arrow)$")
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    color: str = Field(default="#e85d75", pattern=r"^#[0-9a-fA-F]{6}$")
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    points: list[list[float]] | None = None
    category: str = Field(default="Pathologist", max_length=120)
    annotation_status: str = Field(default="Active", pattern="^(Active|Resolved|Excluded|Corrected)$")


class AnnotationUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    color: str = Field(default="#e85d75", pattern=r"^#[0-9a-fA-F]{6}$")


@app.patch("/api/slides/{slide_id}", dependencies=[Depends(require_roles(["Admin", "Technician"]))])
def update_slide(slide_id: int, update: SlideUpdate):
    get_slide(slide_id)
    label = update.label.strip()
    if not label:
        raise HTTPException(400, "Slide label cannot be blank")
    with closing(connect()) as db:
        db.execute("""UPDATE slides SET label=?, stain=?, specimen_description=? WHERE id=?""",
                   (label, update.stain.strip(), update.specimen_description.strip(), slide_id))
        db.commit()
        row = db.execute("SELECT * FROM slides WHERE id = ?", (slide_id,)).fetchone()
    return serialize_slide(row)


def _annotation_for_slide(slide_id: int, annotation_id: int):
    get_slide(slide_id)
    with closing(connect()) as db:
        row = db.execute(
            "SELECT * FROM annotations WHERE id = ? AND slide_id = ?",
            (annotation_id, slide_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Annotation not found for this slide")
    return row


def _validated_geometry(slide, annotation: AnnotationCreate):
    label = annotation.label.strip()
    if not label:
        raise HTTPException(400, "Annotation label cannot be blank")

    if annotation.annotation_type == "rectangle":
        if annotation.x is None or annotation.y is None or annotation.width is None or annotation.height is None:
            raise HTTPException(400, "Rectangle must have x, y, width, and height")
        values = (annotation.x, annotation.y, annotation.width, annotation.height)
        if not all(math.isfinite(value) for value in values):
            raise HTTPException(400, "Annotation coordinates must be finite numbers")
        if annotation.x < 0 or annotation.y < 0 or annotation.width <= 0 or annotation.height <= 0:
            raise HTTPException(400, "Annotation coordinates must be non-negative with positive width and height")
        if annotation.x + annotation.width > slide["width"] or annotation.y + annotation.height > slide["height"]:
            raise HTTPException(400, "Annotation rectangle is outside the slide bounds")
    elif annotation.annotation_type in ("polygon", "arrow"):
        if not annotation.points:
            raise HTTPException(400, f"{annotation.annotation_type.title()} must have points")
        if annotation.annotation_type == "arrow" and len(annotation.points) != 2:
            raise HTTPException(400, "Arrow must contain exactly 2 points")
        if annotation.annotation_type == "polygon" and len(annotation.points) < 3:
            raise HTTPException(400, "Polygon must contain at least 3 points")
        for pt in annotation.points:
            if not isinstance(pt, list) or len(pt) != 2 or not all(math.isfinite(v) for v in pt):
                raise HTTPException(400, "Invalid points geometry")
            if pt[0] < 0 or pt[1] < 0 or pt[0] > slide["width"] or pt[1] > slide["height"]:
                raise HTTPException(400, f"Annotation point {pt} is outside the slide bounds")
                
    return label


@app.get("/api/slides/{slide_id}/annotations", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def list_annotations(slide_id: int):
    get_slide(slide_id)
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT * FROM annotations WHERE slide_id = ? ORDER BY id",
            (slide_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get('points'):
            d['points'] = json.loads(d['points'])
        out.append(d)
    return out


@app.post("/api/slides/{slide_id}/annotations", status_code=201, dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def create_annotation(slide_id: int, annotation: AnnotationCreate):
    slide = get_slide(slide_id)
    label = _validated_geometry(slide, annotation)
    points_json = json.dumps(annotation.points) if annotation.points else None
    with closing(connect()) as db:
        cursor = db.execute("""INSERT INTO annotations
            (slide_id, annotation_type, label, note, color, x, y, width, height, points)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slide_id, annotation.annotation_type, label, annotation.note.strip(), annotation.color.lower(),
             annotation.x, annotation.y, annotation.width, annotation.height, points_json))
        db.commit()
        row = db.execute("SELECT * FROM annotations WHERE id = ?", (cursor.lastrowid,)).fetchone()
    out = dict(row)
    if out.get('points'):
        out['points'] = json.loads(out['points'])
    return out


@app.patch("/api/slides/{slide_id}/annotations/{annotation_id}", dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def update_annotation(slide_id: int, annotation_id: int, update: AnnotationUpdate):
    _annotation_for_slide(slide_id, annotation_id)
    label = update.label.strip()
    if not label:
        raise HTTPException(400, "Annotation label cannot be blank")
    with closing(connect()) as db:
        db.execute("""UPDATE annotations
            SET label = ?, note = ?, color = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND slide_id = ?""",
            (label, update.note.strip(), update.color.lower(), annotation_id, slide_id))
        db.commit()
        row = db.execute(
            "SELECT * FROM annotations WHERE id = ? AND slide_id = ?",
            (annotation_id, slide_id),
        ).fetchone()
    return dict(row)


@app.delete("/api/slides/{slide_id}/annotations/{annotation_id}", status_code=204, dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def delete_annotation(slide_id: int, annotation_id: int, current_user: dict = Depends(get_current_user)):
    annotation = _annotation_for_slide(slide_id, annotation_id)
    with closing(connect()) as db:
        db.execute("DELETE FROM annotation_revisions WHERE annotation_id = ?", (annotation_id,))
        result = db.execute(
            "DELETE FROM annotations WHERE id = ? AND slide_id = ?",
            (annotation_id, slide_id),
        )
        if result.cursor.rowcount != 1:
            raise HTTPException(409, "Annotation changed before it could be deleted")
        audit(db, current_user["username"], "annotation.delete", "annotation", annotation_id, json.dumps({
            "slide_id": slide_id,
            "label": annotation["label"],
            "annotation_type": annotation["annotation_type"],
        }))
        db.commit()
    return Response(status_code=204)


def raster_response(row):
    if row["file_type"] != "raster":
        raise HTTPException(400, "Whole-slide images are available through the tile API")
    path = _stored_path(row["filename"])
    if not path.is_file():
        raise HTTPException(404, "Image file is missing")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/slides/{slide_id}/image", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_image(slide_id: int):
    return raster_response(get_slide(slide_id))


@app.get("/api/slides/{slide_id}/thumbnail", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_thumbnail(slide_id: int):
    row = get_slide(slide_id)
    if row["file_type"] == "raster":
        return raster_response(row)
    _, path = require_wsi_slide(slide_id)
    with slide_cache.acquire(path) as slide:
        thumbnail = slide.get_thumbnail((512, 512)).convert("RGB")
        output = BytesIO()
        thumbnail.save(output, "JPEG", quality=82)
    return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/slides/{slide_id}/slide", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_metadata(slide_id: int):
    row, path = require_wsi_slide(slide_id)
    with slide_cache.acquire(path) as slide:
        deep_zoom = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP)
        return {"id": row["id"], "format": row["format"], "width": row["width"],
                "height": row["height"], "level_count": deep_zoom.level_count,
                "tile_size": TILE_SIZE, "overlap": TILE_OVERLAP,
                "dzi_url": f"/api/slides/{slide_id}/slide.dzi"}


@app.get("/api/slides/{slide_id}/slide.dzi", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_descriptor(slide_id: int):
    _, path = require_wsi_slide(slide_id)
    with slide_cache.acquire(path) as slide:
        descriptor = DeepZoomGenerator(slide, tile_size=TILE_SIZE, overlap=TILE_OVERLAP).get_dzi("jpeg")
    return Response(descriptor, media_type="application/xml", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/slides/{slide_id}/slide_files/{level}/{column}_{row}.jpeg", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def slide_tile(slide_id: int, level: int, column: int, row: int):
    if min(level, column, row) < 0:
        raise HTTPException(404, "Tile not found")

    cached_tile = get_cached_tile(slide_id, level, column, row)
    if cached_tile is not None:
        return Response(cached_tile, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    _, path = get_cached_wsi_slide(slide_id)
    try:
        with slide_cache.acquire_generator(path) as deep_zoom:
            if level >= deep_zoom.level_count:
                raise HTTPException(404, "Tile not found")
            tiles_x, tiles_y = deep_zoom.level_tiles[level]
            if column >= tiles_x or row >= tiles_y:
                raise HTTPException(404, "Tile not found")
            tile = deep_zoom.get_tile(level, (column, row)).convert("RGB")

        output = BytesIO()
        tile.save(output, "JPEG", quality=85)
        jpeg_data = output.getvalue()
        set_cached_tile(slide_id, level, column, row, jpeg_data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Could not render slide tile: {exc}") from exc
    return Response(jpeg_data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# Legacy case-level viewer URLs resolve to the first slide after migration.
@app.get("/api/cases/{case_pk}/image", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def legacy_image(case_pk: int):
    return raster_response(first_slide(case_pk))


@app.get("/api/cases/{case_pk}/thumbnail", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def legacy_thumbnail(case_pk: int):
    return slide_thumbnail(first_slide(case_pk)["id"])


@app.get("/api/cases/{case_pk}/slide", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def legacy_metadata(case_pk: int):
    return slide_metadata(first_slide(case_pk)["id"])


@app.get("/api/cases/{case_pk}/slide.dzi", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def legacy_descriptor(case_pk: int):
    return slide_descriptor(first_slide(case_pk)["id"])


@app.get("/api/cases/{case_pk}/slide_files/{level}/{column}_{row}.jpeg", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def legacy_tile(case_pk: int, level: int, column: int, row: int):
    return slide_tile(first_slide(case_pk)["id"], level, column, row)


# --- Reports Feature ---

from typing import List, Optional
from datetime import datetime, timezone

class ReportFindingCreate(BaseModel):
    slide_id: Optional[int] = None
    annotation_id: Optional[int] = None
    text: str = Field(default="", max_length=4000)
    sort_order: int = 0

class ReportUpdate(BaseModel):
    specimen_clinical: str = Field(default="", max_length=4000)
    gross_description: str = Field(default="", max_length=4000)
    microscopic_findings: str = Field(default="", max_length=4000)
    diagnosis: str = Field(default="", max_length=4000)
    comments: str = Field(default="", max_length=4000)
    reviewer_name: str = Field(default="", max_length=120)
    findings: List[ReportFindingCreate] = []

def serialize_report(row):
    report = dict(row)
    with closing(connect()) as db:
        findings = db.execute(
            "SELECT * FROM report_findings WHERE report_id = ? ORDER BY sort_order",
            (report["id"],)
        ).fetchall()
    report["findings"] = [dict(f) for f in findings]
    return report

@app.get("/api/cases/{case_pk}/reports", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def list_reports(case_pk: int):
    get_case(case_pk)
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT * FROM reports WHERE case_id = ? ORDER BY version DESC",
            (case_pk,)
        ).fetchall()
    return [serialize_report(row) for row in rows]

@app.post("/api/cases/{case_pk}/reports", status_code=201, dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def create_draft_report(case_pk: int, current_user: dict = Depends(get_current_user)):
    get_case(case_pk)
    with closing(connect()) as db:
        # Check if a draft already exists
        existing_draft = db.execute(
            "SELECT * FROM reports WHERE case_id = ? AND status = 'Draft'", (case_pk,)
        ).fetchone()
        if existing_draft:
            return serialize_report(existing_draft)
        
        # Determine next version
        latest = db.execute(
            "SELECT TOP 1 version FROM reports WHERE case_id = ? ORDER BY version DESC",
            (case_pk,)
        ).fetchone()
        next_version = (latest["version"] + 1) if latest else 1

        # If there's a finalized previous version, copy its fields
        prev = db.execute(
            "SELECT TOP 1 * FROM reports WHERE case_id = ? AND status = 'Finalized' ORDER BY version DESC",
            (case_pk,)
        ).fetchone()
        
        if prev:
            cursor = db.execute(
                '''INSERT INTO reports 
                   (case_id, version, status, specimen_clinical, gross_description, 
                    microscopic_findings, diagnosis, comments, reviewer_name)
                   VALUES (?, ?, 'Draft', ?, ?, ?, ?, ?, ?)''',
                (case_pk, next_version, prev["specimen_clinical"], prev["gross_description"],
                 prev["microscopic_findings"], prev["diagnosis"], prev["comments"], current_user["display_name"])
            )
            new_id = cursor.lastrowid
            # copy findings
            findings = db.execute("SELECT * FROM report_findings WHERE report_id = ?", (prev["id"],)).fetchall()
            for f in findings:
                db.execute(
                    "INSERT INTO report_findings (report_id, slide_id, annotation_id, text, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (new_id, f["slide_id"], f["annotation_id"], f["text"], f["sort_order"])
                )
        else:
            cursor = db.execute(
                "INSERT INTO reports (case_id, version, status, reviewer_name) VALUES (?, ?, 'Draft', ?)",
                (case_pk, next_version, current_user['display_name'])
            )
            new_id = cursor.lastrowid
            
        db.commit()
        row = db.execute("SELECT * FROM reports WHERE id = ?", (new_id,)).fetchone()
    return serialize_report(row)

@app.patch("/api/reports/{report_id}", dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def update_report(report_id: int, update: ReportUpdate, current_user: dict = Depends(get_current_user)):
    with closing(connect()) as db:
        report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        if report["status"] != "Draft":
            raise HTTPException(400, "Cannot edit a finalized report")
            
        db.execute(
            '''UPDATE reports SET 
               specimen_clinical=?, gross_description=?, microscopic_findings=?, 
               diagnosis=?, comments=?, reviewer_name=?
               WHERE id = ?''',
            (update.specimen_clinical.strip(), update.gross_description.strip(),
             update.microscopic_findings.strip(), update.diagnosis.strip(),
             update.comments.strip(), current_user["display_name"], report_id)
        )
        
        db.execute("DELETE FROM report_findings WHERE report_id = ?", (report_id,))
        for f in update.findings:
            if f.slide_id:
                slide = db.execute("SELECT id, case_id FROM slides WHERE id = ?", (f.slide_id,)).fetchone()
                if not slide or slide["case_id"] != report["case_id"]:
                    raise HTTPException(400, f"Slide {f.slide_id} is invalid or not in this case")
            if f.annotation_id:
                ann = db.execute("SELECT id, slide_id FROM annotations WHERE id = ?", (f.annotation_id,)).fetchone()
                if not ann or ann["slide_id"] != f.slide_id:
                    raise HTTPException(400, f"Annotation {f.annotation_id} is invalid or not in slide {f.slide_id}")
                    
            db.execute(
                "INSERT INTO report_findings (report_id, slide_id, annotation_id, text, sort_order) VALUES (?, ?, ?, ?, ?)",
                (report_id, f.slide_id, f.annotation_id, f.text.strip(), f.sort_order)
            )
        db.commit()
        row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return serialize_report(row)

@app.post("/api/reports/{report_id}/finalize", dependencies=[Depends(require_roles(["Admin", "Pathologist"]))])
def finalize_report(report_id: int):
    with closing(connect()) as db:
        report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        if report["status"] == "Finalized":
            return serialize_report(report)
            
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.execute("UPDATE reports SET status = 'Finalized', finalized_at = ? WHERE id = ?", (now, report_id))
        db.commit()
        row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return serialize_report(row)

from fpdf import FPDF

@app.get("/api/reports/{report_id}/pdf", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def export_report_pdf(report_id: int):
    with closing(connect()) as db:
        report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        case = db.execute("SELECT case_id FROM cases WHERE id = ?", (report["case_id"],)).fetchone()
        findings = db.execute("SELECT * FROM report_findings WHERE report_id = ? ORDER BY sort_order", (report_id,)).fetchall()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 10, f"Pathology Report - Case {case['case_id']}", ln=True, align="C")
    
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, f"Status: {report['status']} (Version {report['version']})", ln=True, align="C")
    if report['finalized_at']:
        pdf.cell(0, 6, f"Finalized: {report['finalized_at']}", ln=True, align="C")
    pdf.cell(0, 6, f"Reviewer: {report['reviewer_name']}", ln=True, align="C")
    pdf.ln(10)
    
    sections = [
        ("Specimen & Clinical", report['specimen_clinical']),
        ("Gross Description", report['gross_description']),
        ("Microscopic Findings", report['microscopic_findings']),
        ("Diagnosis", report['diagnosis']),
        ("Comments", report['comments']),
    ]
    
    for title, content in sections:
        if content:
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 8, title, ln=True)
            pdf.set_font("helvetica", "", 11)
            pdf.multi_cell(0, 6, content)
            pdf.ln(4)
            
    if findings:
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, "Specific Findings", ln=True)
        pdf.set_font("helvetica", "", 11)
        for idx, f in enumerate(findings, 1):
            text = f['text']
            if f['slide_id']:
                text += f" [Linked to Slide {f['slide_id']}]"
            pdf.multi_cell(0, 6, f"{idx}. {text}")
            pdf.ln(2)

    output = bytes(pdf.output())
    return Response(content=output, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=report_{case['case_id']}_v{report['version']}.pdf"
    })

@app.get("/api/reports/{report_id}/csv", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def export_report_csv(report_id: int):
    with closing(connect()) as db:
        report=db.execute("SELECT * FROM reports WHERE id=?",(report_id,)).fetchone()
        if not report: raise HTTPException(404,"Report not found")
        case=db.execute("SELECT case_id FROM cases WHERE id=?",(report["case_id"],)).fetchone()
        findings=db.execute("SELECT * FROM report_findings WHERE report_id=? ORDER BY sort_order",(report_id,)).fetchall()
    output=StringIO(); writer=csv.writer(output); writer.writerow(["case_id","report_version","status","reviewer","section","value","slide_id","annotation_id"])
    for key,label in (("specimen_clinical","Specimen and clinical"),("gross_description","Gross description"),("microscopic_findings","Microscopic findings"),("diagnosis","Diagnosis"),("comments","Comments")):
        writer.writerow([case["case_id"],report["version"],report["status"],report["reviewer_name"],label,report[key],"",""])
    for finding in findings: writer.writerow([case["case_id"],report["version"],report["status"],report["reviewer_name"],"Finding",finding["text"],finding["slide_id"] or "",finding["annotation_id"] or ""])
    return Response(output.getvalue(),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename=report_{case['case_id']}_v{report['version']}.csv"})

@app.get("/api/reports/{report_id}/html", dependencies=[Depends(require_roles(["Admin", "Technician", "Pathologist", "Viewer"]))])
def export_report_html(report_id: int):
    with closing(connect()) as db:
        report=db.execute("SELECT * FROM reports WHERE id=?",(report_id,)).fetchone()
        if not report: raise HTTPException(404,"Report not found")
        case=db.execute("SELECT case_id FROM cases WHERE id=?",(report["case_id"],)).fetchone()
    sections="".join(f"<section><h2>{html.escape(label)}</h2><p>{html.escape(str(report[key] or ''))}</p></section>" for key,label in (("specimen_clinical","Specimen and clinical"),("gross_description","Gross description"),("microscopic_findings","Microscopic findings"),("diagnosis","Diagnosis"),("comments","Comments")))
    document=f"<!doctype html><html><head><meta charset='utf-8'><title>Report {html.escape(case['case_id'])}</title></head><body><main><h1>Pathology report</h1><p>Case {html.escape(case['case_id'])} · Version {report['version']} · {html.escape(report['status'])}</p>{sections}<footer>Reviewer: {html.escape(report['reviewer_name'])}</footer></main></body></html>"
    return Response(document,media_type="text/html",headers={"Content-Disposition":f"attachment; filename=report_{case['case_id']}_v{report['version']}.html"})


from platform_api import create_platform_router, init_platform_schema
init_platform_schema(connect)
app.include_router(create_platform_router(connect, require_roles, get_current_user, UPLOADS, openslide))
