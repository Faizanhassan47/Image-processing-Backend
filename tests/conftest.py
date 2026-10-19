from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

import pyodbc
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
from platform_api import init_platform_schema
from patient_api import init_patient_schema


def _server_connection(database):
    driver = os.getenv('DB_DRIVER', 'ODBC Driver 17 for SQL Server')
    server = os.getenv('DB_SERVER', 'localhost')
    parts = [f'DRIVER={{{driver}}}', f'SERVER={server}', f'DATABASE={database}']
    if main.env_bool('DB_TRUSTED_CONNECTION', True):
        parts.append('Trusted_Connection=yes')
    else:
        parts.extend([f"UID={os.environ['DB_USERNAME']}", f"PWD={os.environ['DB_PASSWORD']}"])
    parts.append(f"TrustServerCertificate={'yes' if main.env_bool('DB_TRUST_SERVER_CERTIFICATE') else 'no'}")
    return pyodbc.connect(';'.join(parts) + ';', autocommit=True, timeout=15)


@pytest.fixture(scope='session')
def sql_test_database():
    original = os.environ.get('DB_NAME', 'Hello')
    database = f'PathologyTest_{uuid4().hex[:10]}'
    with _server_connection('master') as connection:
        connection.execute(f'CREATE DATABASE [{database}]')
    os.environ['DB_NAME'] = database
    try:
        main.init_db()
        init_platform_schema(main.connect)
        init_patient_schema(main.connect)
        yield database
    finally:
        main.slide_cache.clear()
        os.environ['DB_NAME'] = original
        with _server_connection('master') as connection:
            connection.execute(f"ALTER DATABASE [{database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            connection.execute(f'DROP DATABASE [{database}]')


@pytest.fixture()
def client(sql_test_database, tmp_path, monkeypatch):
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    monkeypatch.setattr(main, 'UPLOADS', uploads)
    main.slide_cache.clear()
    with main.connect() as db:
        for table in ('ai_analyses','patient_imports','patient_documents','patient_clinical_note_revisions','patient_clinical_notes','patient_test_results','patient_tests','patient_encounters','patient_medical_history_revisions','patient_medical_history','patient_identity_mappings','research_folder_access','research_folder_members','research_folders','case_links','case_operations','viewer_bookmarks','viewer_preferences','case_consultations','report_reviews','report_findings','reports','report_templates','annotation_revisions','annotations','ai_results','ai_jobs','image_metadata_revisions','image_versions','reconciliation_items','ingestion_jobs','audit_log','case_review_history','case_review_notes','slide_qc_history','slides','blocks','specimens','accessions','patients','cases','users'):
            if db.execute("SELECT OBJECT_ID(?, 'U') AS id", (table,)).fetchone()['id']:
                db.execute(f'DELETE FROM [{table}]')
        password_hash = main.get_password_hash('test-password-123')
        db.execute("INSERT INTO users (username,display_name,role,password_hash,is_active) VALUES (?,?,?,?,1)", ('admin','Test Administrator','System Administrator',password_hash))
        db.commit()
    test_client = TestClient(main.app)
    token = main.create_access_token({'sub':'admin','role':'System Administrator'})
    test_client.cookies.set('access_token', token)
    test_client.cookies.set('csrf_token', 'test-csrf-token')
    test_client.headers.update({'X-CSRF-Token':'test-csrf-token'})
    yield test_client
    main.slide_cache.clear()


@pytest.fixture()
def png_file():
    output = BytesIO()
    Image.new('RGB', (64, 48), 'purple').save(output, format='PNG')
    return output.getvalue()
