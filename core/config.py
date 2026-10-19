"""
Application Configuration and Environment Settings.
Easily read, understand, and configure system defaults in one place.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    """Helper to read boolean environment variables cleanly."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- App Info ---
APP_NAME = os.getenv("APP_NAME", "Pathology Demo API")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

# --- Database Settings (MS SQL Server) ---
DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
DB_SERVER = os.getenv("DB_SERVER", "localhost")
DB_NAME = os.getenv("DB_NAME", "Hello")
DB_TRUSTED_CONNECTION = env_bool("DB_TRUSTED_CONNECTION", True)
DB_TRUST_SERVER_CERTIFICATE = env_bool("DB_TRUST_SERVER_CERTIFICATE", False)
DB_CONNECTION_TIMEOUT = int(os.getenv("DB_CONNECTION_TIMEOUT", "30"))
DB_USERNAME = os.getenv("DB_USERNAME", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# --- Security & JWT Authentication ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-insecure-secret-key-change-in-production")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_PROTECTION_ENABLED = env_bool("CSRF_PROTECTION_ENABLED", True)
REQUIRE_PASSWORD_COMPLEXITY = env_bool("REQUIRE_PASSWORD_COMPLEXITY", True)

# --- Rate Limiting ---
LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "60"))

# --- Storage & Uploads ---
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", BASE_DIR / "uploads"))
if not UPLOADS_DIR.is_absolute():
    UPLOADS_DIR = BASE_DIR / UPLOADS_DIR
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

RASTER_MAX_BYTES = int(os.getenv("RASTER_MAX_UPLOAD_MB", "10")) * 1024 * 1024
WSI_MAX_BYTES = int(os.getenv("WSI_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
UPLOAD_CHUNK_BYTES = int(os.getenv("UPLOAD_CHUNK_MB", "8")) * 1024 * 1024
TILE_SIZE = int(os.getenv("TILE_SIZE", "254"))
TILE_OVERLAP = int(os.getenv("TILE_OVERLAP", "1"))
RASTER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
