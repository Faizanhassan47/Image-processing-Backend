"""Isolated DICOM WSI ingestion verification against LocalDB Vista_DICOM_Dev.

Does not connect to the live Hello database. Run:

    python scripts/verify_dicom_wsi_ingestion.py

Exit code 0 means the ingestion-to-tile path succeeded.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Configure the isolated development database before importing app modules.
# load_dotenv() does not override these values.
os.environ["DB_SERVER"] = r"(localdb)\MSSQLLocalDB"
os.environ["DB_NAME"] = "Vista_DICOM_Dev"
os.environ["DB_DRIVER"] = "ODBC Driver 17 for SQL Server"
os.environ["DB_TRUSTED_CONNECTION"] = "true"
os.environ["DB_TRUST_SERVER_CERTIFICATE"] = "true"
os.environ["IDRIVE_E2_ENABLED"] = "false"

WORK = Path(tempfile.mkdtemp(prefix="vista-dicom-verify-"))
WATCHED = WORK / "watched"
UPLOADS = WORK / "uploads"
WATCHED.mkdir(parents=True)
UPLOADS.mkdir(parents=True)
os.environ["WATCHED_FOLDER"] = str(WATCHED)
os.environ["UPLOADS_DIR"] = str(UPLOADS)
os.environ["QUARANTINE_DIR"] = str(WORK / "quarantine")
os.environ["INGESTION_STABLE_SECONDS"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import sql  # noqa: E402
import sql.init_tables as init_tables  # noqa: E402

_original_init_db = init_tables.init_db


def _tolerant_init_db(connect_fn):
    """LocalDB schema bootstrap can fail on optional report_findings cascades."""
    try:
        _original_init_db(connect_fn)
    except Exception as exc:
        message = str(exc)
        if "report_findings" not in message and "multiple cascade paths" not in message:
            raise
        print(f"init_db warning (ignored for verification): {exc}")


init_tables.init_db = _tolerant_init_db
sql.init_db = _tolerant_init_db

import routes  # noqa: E402
import routes.api as routes_api  # noqa: E402

_original_init_all = routes_api.init_all_schemas


def _tolerant_init_all(connect_fn):
    try:
        _original_init_all(connect_fn)
    except Exception as exc:
        # Isolated LocalDB only needs slides/cases/ingestion tables for this check.
        print(f"init_all_schemas warning (ignored for verification): {exc}")


routes_api.init_all_schemas = _tolerant_init_all
routes.init_all_schemas = _tolerant_init_all

from core.database import connect  # noqa: E402
from core.security import create_access_token, get_password_hash  # noqa: E402
from main import app  # noqa: E402
from routes.cases_api import slide_dzi_url  # noqa: E402
from services.dicom_wsi_sample import write_synthetic_wsi  # noqa: E402


def auth_headers() -> dict[str, str]:
    token = create_access_token({"sub": "dicom_verifier", "role": "System Administrator"})
    return {"Authorization": f"Bearer {token}"}


def seed() -> int:
    password_hash = get_password_hash("ValidPass123!")
    with connect() as db:
        existing = db.execute("SELECT id FROM users WHERE username=?", ("dicom_verifier",)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO users (username, display_name, password_hash, role, is_active) VALUES (?,?,?,?,1)",
                ("dicom_verifier", "DICOM Verifier", password_hash, "System Administrator"),
            )
        case = db.execute("SELECT id FROM cases WHERE case_id=?", ("ACC-DEMO03",)).fetchone()
        if case:
            case_pk = case["id"]
        else:
            cursor = db.execute(
                "INSERT INTO cases (case_id, description, filename, status) VALUES (?,?, '', 'New')",
                ("ACC-DEMO03", "Isolated DICOM verification case"),
            )
            case_pk = cursor.lastrowid
        accession = db.execute(
            "SELECT id FROM accessions WHERE accession_number=?",
            ("ACC-DEMO03",),
        ).fetchone()
        if not accession:
            patient = db.execute(
                "SELECT id FROM patients WHERE medical_record_number=?",
                ("MRN-DICOM-DEV",),
            ).fetchone()
            if patient:
                patient_id = patient["id"]
            else:
                patient_cursor = db.execute(
                    "INSERT INTO patients (medical_record_number, display_label, created_by) VALUES (?,?,?)",
                    ("MRN-DICOM-DEV", "DICOM Dev Patient", "dicom_verifier"),
                )
                patient_id = patient_cursor.lastrowid
            db.execute(
                "INSERT INTO accessions (patient_id, accession_number, order_id, priority, case_id) VALUES (?,?,?,?,?)",
                (patient_id, "ACC-DEMO03", "ORD-DICOM-DEV", "Routine", case_pk),
            )
        db.commit()
    return case_pk


def main() -> int:
    report = {
        "database": "Vista_DICOM_Dev",
        "server": r"(localdb)\MSSQLLocalDB",
        "live_hello_touched": False,
        "steps": [],
    }
    case_pk = seed()
    report["steps"].append({"seed_case_id": case_pk, "case_code": "ACC-DEMO03"})

    series_dir = WATCHED / "series"
    series_dir.mkdir(parents=True, exist_ok=True)
    dicom_path = write_synthetic_wsi(series_dir)
    report["steps"].append({"synthetic_dicom": str(dicom_path)})

    client = TestClient(app)
    headers = auth_headers()

    queue = client.post(
        "/api/platform/ingestion/jobs",
        headers=headers,
        json={
            "source_path": str(dicom_path),
            "match_identifier": "ACC-DEMO03",
            "adapter_name": "auto",
            "source_device": "local-folder",
        },
    )
    report["steps"].append({"queue_status": queue.status_code, "queue_body": queue.json()})
    if queue.status_code not in {200, 202}:
        Path(WORK / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1
    job_id = queue.json()["id"]

    # process_ingestion enforces max(1, INGESTION_STABLE_SECONDS) before accepting a file.
    time.sleep(1.2)

    processed = client.post(f"/api/platform/ingestion/jobs/{job_id}/process", headers=headers)
    report["steps"].append({"process_status": processed.status_code, "process_body": processed.json()})
    if processed.status_code != 200:
        Path(WORK / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    body = processed.json()
    slide_id = body.get("slide_id")
    if not slide_id:
        report["error"] = "process response missing slide_id"
        print(json.dumps(report, indent=2))
        return 1

    with connect() as db:
        slides = db.execute("SELECT id, file_type, filename FROM slides WHERE case_id=?", (case_pk,)).fetchall()
        slide = db.execute("SELECT * FROM slides WHERE id=?", (slide_id,)).fetchone()
    report["steps"].append({
        "slide_count_for_case": len(slides),
        "slide": {
            "id": slide["id"],
            "file_type": slide["file_type"],
            "filename": slide["filename"],
        },
    })
    if len(slides) != 1 or slide["file_type"] != "dicom-wsi":
        report["error"] = "expected exactly one dicom-wsi slide"
        print(json.dumps(report, indent=2))
        return 1

    case_payload = client.get(f"/api/cases/{case_pk}", headers=headers)
    report["steps"].append({"case_status": case_payload.status_code})
    if case_payload.status_code != 200:
        print(json.dumps(report, indent=2))
        return 1
    case_json = case_payload.json()
    serialized = next(item for item in case_json.get("slides", []) if item["id"] == slide_id)
    expected_dzi = slide_dzi_url(slide_id, "dicom-wsi")
    report["steps"].append({"serialized_dzi_url": serialized.get("dzi_url"), "expected": expected_dzi})
    if serialized.get("dzi_url") != expected_dzi:
        report["error"] = "case API dzi_url mismatch"
        print(json.dumps(report, indent=2))
        return 1

    dzi = client.get(serialized["dzi_url"], headers=headers)
    report["steps"].append({"dzi_status": dzi.status_code, "dzi_content_type": dzi.headers.get("content-type")})
    if dzi.status_code != 200 or b"Image" not in dzi.content:
        report["error"] = "DZI descriptor failed"
        print(json.dumps(report, indent=2))
        return 1

    tile = client.get(f"/api/slides/{slide_id}/slide_files/0/0_0.jpeg", headers=headers)
    # Deep Zoom level 0 is the lowest-resolution level; also try the highest via metadata.
    meta = client.get(f"/api/slides/{slide_id}/slide", headers=headers)
    report["steps"].append({"meta_status": meta.status_code, "meta": meta.json() if meta.status_code == 200 else meta.text})
    if meta.status_code != 200:
        report["error"] = "slide metadata failed"
        print(json.dumps(report, indent=2))
        return 1
    level_count = int(meta.json()["level_count"])
    top = level_count - 1
    tile = client.get(f"/api/slides/{slide_id}/slide_files/{top}/0_0.jpeg", headers=headers)
    thumb = client.get(f"/api/slides/{slide_id}/thumbnail", headers=headers)
    report["steps"].append({
        "tile_status": tile.status_code,
        "tile_bytes": len(tile.content),
        "thumbnail_status": thumb.status_code,
        "thumbnail_bytes": len(thumb.content),
    })
    if tile.status_code != 200 or not tile.content.startswith(b"\xff\xd8"):
        report["error"] = "tile request failed"
        print(json.dumps(report, indent=2))
        return 1
    if thumb.status_code != 200 or not thumb.content.startswith(b"\xff\xd8"):
        report["error"] = "thumbnail request failed"
        print(json.dumps(report, indent=2))
        return 1

    # SVS/WSI serializer regression: existing wsi rows still receive dzi_url.
    report["steps"].append({
        "svs_serializer_regression": {
            "wsi": slide_dzi_url(99, "wsi"),
            "raster": slide_dzi_url(99, "raster"),
        }
    })
    assert slide_dzi_url(99, "wsi") == "/api/slides/99/slide.dzi"

    report["result"] = "passed"
    report["work_dir"] = str(WORK)
    print(json.dumps(report, indent=2))
    Path(WORK / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
