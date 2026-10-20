"""Patient profile, longitudinal history, and versioned JSON exchange APIs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import json
import mimetypes
import re
import shutil
from uuid import uuid4

import pyodbc
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator


from core import (
    ADMIN_ROLES,
    CLINICAL_WRITE_ROLES,
    EXPORT_ROLES,
    READ_ROLES,
    WRITE_ROLES,
)

PATIENT_SCHEMA_VERSION = "1.0"
DOCUMENT_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "text/plain": ".txt"}



CNIC_PATTERN = re.compile(r"^\d{13}$")


def _normalize_cnic(value):
    """Strip dashes/spaces so 12345-1234567-1 and 1234512345671 are the same CNIC."""
    if isinstance(value, str):
        return re.sub(r"[\s-]", "", value)
    return value


def _valid_cnic(value):
    if value is None:
        return value
    if not CNIC_PATTERN.match(value):
        raise ValueError("CNIC must be exactly 13 digits")
    return value


DDL = [
    '''DECLARE @external_constraint SYSNAME, @external_sql NVARCHAR(4000); SELECT TOP (1) @external_constraint=kc.name FROM sys.key_constraints kc JOIN sys.index_columns ic ON ic.object_id=kc.parent_object_id AND ic.index_id=kc.unique_index_id JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id WHERE kc.parent_object_id=OBJECT_ID('patients') AND kc.type='UQ' GROUP BY kc.name HAVING COUNT(*)=1 AND MAX(c.name)='external_id'; IF @external_constraint IS NOT NULL BEGIN SET @external_sql=N'ALTER TABLE patients DROP CONSTRAINT ' + QUOTENAME(@external_constraint); EXEC sp_executesql @external_sql; END''',
    '''IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_external' AND object_id=OBJECT_ID('patients')) CREATE UNIQUE INDEX UQ_patients_external ON patients(external_id) WHERE external_id IS NOT NULL''',
    "IF COL_LENGTH('patients','cnic') IS NULL ALTER TABLE patients ADD cnic CHAR(13) NULL",
    "IF COL_LENGTH('patients','patient_identifier') IS NULL ALTER TABLE patients ADD patient_identifier VARCHAR(64) NULL",
    "IF COL_LENGTH('patients','medical_record_number') IS NULL ALTER TABLE patients ADD medical_record_number VARCHAR(120) NULL",
    "IF COL_LENGTH('patients','first_name') IS NULL ALTER TABLE patients ADD first_name NVARCHAR(120) NULL",
    "IF COL_LENGTH('patients','middle_name') IS NULL ALTER TABLE patients ADD middle_name NVARCHAR(120) NULL",
    "IF COL_LENGTH('patients','last_name') IS NULL ALTER TABLE patients ADD last_name NVARCHAR(120) NULL",
    "IF COL_LENGTH('patients','date_of_birth') IS NULL ALTER TABLE patients ADD date_of_birth DATE NULL",
    "IF COL_LENGTH('patients','sex') IS NULL ALTER TABLE patients ADD sex VARCHAR(30) NULL",
    "IF COL_LENGTH('patients','gender_identity') IS NULL ALTER TABLE patients ADD gender_identity NVARCHAR(80) NULL",
    "IF COL_LENGTH('patients','blood_group') IS NULL ALTER TABLE patients ADD blood_group VARCHAR(10) NULL",
    "IF COL_LENGTH('patients','phone') IS NULL ALTER TABLE patients ADD phone VARCHAR(40) NULL",
    "IF COL_LENGTH('patients','address') IS NULL ALTER TABLE patients ADD address NVARCHAR(1000) NULL",
    "IF COL_LENGTH('patients','status') IS NULL ALTER TABLE patients ADD status VARCHAR(30) NOT NULL CONSTRAINT DF_patients_status DEFAULT 'Active'",
    "IF COL_LENGTH('patients','demographics_json') IS NULL ALTER TABLE patients ADD demographics_json NVARCHAR(MAX) NULL",
    "IF COL_LENGTH('patients','created_by') IS NULL ALTER TABLE patients ADD created_by VARCHAR(255) NULL",
    "IF COL_LENGTH('patients','updated_by') IS NULL ALTER TABLE patients ADD updated_by VARCHAR(255) NULL",
    "IF COL_LENGTH('patients','updated_at') IS NULL ALTER TABLE patients ADD updated_at DATETIME2 NOT NULL CONSTRAINT DF_patients_updated DEFAULT SYSUTCDATETIME()",
    "IF COL_LENGTH('patients','archived_at') IS NULL ALTER TABLE patients ADD archived_at DATETIME2 NULL",
    "IF COL_LENGTH('patients','organization_id') IS NULL ALTER TABLE patients ADD organization_id VARCHAR(100) NULL",
    "IF COL_LENGTH('patients','facility_name') IS NULL ALTER TABLE patients ADD facility_name NVARCHAR(255) NULL",
    "IF COL_LENGTH('patients','department') IS NULL ALTER TABLE patients ADD department NVARCHAR(255) NULL",
    "IF COL_LENGTH('patients','care_team') IS NULL ALTER TABLE patients ADD care_team NVARCHAR(MAX) NULL",
    "IF COL_LENGTH('patients','consent_status') IS NULL ALTER TABLE patients ADD consent_status VARCHAR(50) NOT NULL CONSTRAINT DF_patients_consent DEFAULT 'Granted'",
    "IF COL_LENGTH('patients','legal_hold') IS NULL ALTER TABLE patients ADD legal_hold BIT NOT NULL CONSTRAINT DF_patients_legal_hold DEFAULT 0",
    "IF COL_LENGTH('patients','retention_until') IS NULL ALTER TABLE patients ADD retention_until DATETIME2 NULL",
    "UPDATE patients SET patient_identifier=CONCAT('PAT-',RIGHT(CONCAT('000000',CAST(id AS VARCHAR(20))),6)) WHERE patient_identifier IS NULL",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_identifier' AND object_id=OBJECT_ID('patients')) CREATE UNIQUE INDEX UQ_patients_identifier ON patients(patient_identifier) WHERE patient_identifier IS NOT NULL",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_mrn' AND object_id=OBJECT_ID('patients')) CREATE UNIQUE INDEX UQ_patients_mrn ON patients(medical_record_number) WHERE medical_record_number IS NOT NULL",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_cnic' AND object_id=OBJECT_ID('patients')) CREATE UNIQUE INDEX UQ_patients_cnic ON patients(cnic) WHERE cnic IS NOT NULL",
    "IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_patients_demographics_json') ALTER TABLE patients ADD CONSTRAINT CK_patients_demographics_json CHECK (demographics_json IS NULL OR ISJSON(demographics_json)=1)",
    """IF OBJECT_ID('patient_identity_mappings','U') IS NULL CREATE TABLE patient_identity_mappings (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), authority VARCHAR(120) NOT NULL,
        identifier_value VARCHAR(255) NOT NULL, identifier_type VARCHAR(80) NOT NULL DEFAULT 'External', is_primary BIT NOT NULL DEFAULT 0,
        created_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_patient_identity_mapping UNIQUE(authority,identifier_value))""",
    """IF OBJECT_ID('patient_medical_history','U') IS NULL CREATE TABLE patient_medical_history (
        patient_id INT NOT NULL PRIMARY KEY REFERENCES patients(id), schema_version VARCHAR(20) NOT NULL DEFAULT '1.0',
        history_json NVARCHAR(MAX) NOT NULL DEFAULT '{}', revision_number INT NOT NULL DEFAULT 1,
        created_by VARCHAR(255) NOT NULL, updated_by VARCHAR(255) NOT NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_patient_medical_history_json CHECK (ISJSON(history_json)=1))""",
    """IF OBJECT_ID('patient_medical_history_revisions','U') IS NULL CREATE TABLE patient_medical_history_revisions (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), revision_number INT NOT NULL,
        schema_version VARCHAR(20) NOT NULL, history_json NVARCHAR(MAX) NOT NULL, changed_by VARCHAR(255) NOT NULL,
        change_reason NVARCHAR(1000) NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_patient_history_revision UNIQUE(patient_id,revision_number),
        CONSTRAINT CK_patient_history_revision_json CHECK (ISJSON(history_json)=1))""",
    """IF OBJECT_ID('patient_encounters','U') IS NULL CREATE TABLE patient_encounters (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), encounter_identifier VARCHAR(120) NOT NULL,
        encounter_type VARCHAR(80) NOT NULL, status VARCHAR(30) NOT NULL DEFAULT 'Open', occurred_at DATETIME2 NOT NULL,
        ended_at DATETIME2 NULL, location NVARCHAR(255) NULL, clinician VARCHAR(255) NULL, metadata_json NVARCHAR(MAX) NULL,
        created_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_patient_encounter UNIQUE(patient_id,encounter_identifier), CONSTRAINT CK_patient_encounter_json CHECK (metadata_json IS NULL OR ISJSON(metadata_json)=1))""",
    """IF OBJECT_ID('patient_tests','U') IS NULL CREATE TABLE patient_tests (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), encounter_id INT NULL REFERENCES patient_encounters(id),
        case_id INT NULL REFERENCES cases(id), report_id INT NULL REFERENCES reports(id), test_identifier VARCHAR(120) NOT NULL,
        test_name NVARCHAR(255) NOT NULL, test_category VARCHAR(100) NOT NULL, order_date DATETIME2 NULL, collection_date DATETIME2 NULL,
        result_date DATETIME2 NULL, status VARCHAR(30) NOT NULL DEFAULT 'Ordered', summary_result NVARCHAR(2000) NULL,
        source_system VARCHAR(120) NOT NULL DEFAULT 'Digital Pathology', provenance_json NVARCHAR(MAX) NULL,
        created_by VARCHAR(255) NOT NULL, updated_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_patient_test_identifier UNIQUE(patient_id,test_identifier), CONSTRAINT CK_patient_test_provenance_json CHECK (provenance_json IS NULL OR ISJSON(provenance_json)=1))""",
    """IF OBJECT_ID('patient_test_results','U') IS NULL CREATE TABLE patient_test_results (
        id INT IDENTITY PRIMARY KEY, test_id INT NOT NULL REFERENCES patient_tests(id), analyte_name NVARCHAR(255) NOT NULL,
        result_value NVARCHAR(1000) NULL, result_type VARCHAR(40) NOT NULL DEFAULT 'Text', unit NVARCHAR(80) NULL,
        reference_range NVARCHAR(255) NULL, abnormal_flag VARCHAR(30) NULL, result_json NVARCHAR(MAX) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), CONSTRAINT CK_patient_test_result_json CHECK (result_json IS NULL OR ISJSON(result_json)=1))""",
    """IF OBJECT_ID('patient_clinical_notes','U') IS NULL CREATE TABLE patient_clinical_notes (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), case_id INT NULL REFERENCES cases(id),
        encounter_id INT NULL REFERENCES patient_encounters(id), note_type VARCHAR(80) NOT NULL DEFAULT 'Clinical', note_text NVARCHAR(MAX) NOT NULL,
        revision_number INT NOT NULL DEFAULT 1, is_archived BIT NOT NULL DEFAULT 0, created_by VARCHAR(255) NOT NULL,
        updated_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
    """IF OBJECT_ID('patient_clinical_note_revisions','U') IS NULL CREATE TABLE patient_clinical_note_revisions (
        id INT IDENTITY PRIMARY KEY, note_id INT NOT NULL REFERENCES patient_clinical_notes(id), revision_number INT NOT NULL,
        note_text NVARCHAR(MAX) NOT NULL, changed_by VARCHAR(255) NOT NULL, change_reason NVARCHAR(1000) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), CONSTRAINT UQ_patient_note_revision UNIQUE(note_id,revision_number))""",
    """IF OBJECT_ID('patient_documents','U') IS NULL CREATE TABLE patient_documents (
        id INT IDENTITY PRIMARY KEY, patient_id INT NOT NULL REFERENCES patients(id), case_id INT NULL REFERENCES cases(id),
        document_type VARCHAR(80) NOT NULL, original_filename NVARCHAR(512) NOT NULL, stored_filename VARCHAR(512) NOT NULL UNIQUE,
        content_type VARCHAR(120) NOT NULL, file_size BIGINT NOT NULL, checksum_sha256 VARCHAR(64) NOT NULL,
        description NVARCHAR(1000) NULL, uploaded_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), archived_at DATETIME2 NULL,
        scan_status VARCHAR(50) NOT NULL DEFAULT 'Clean', quarantine_reason NVARCHAR(1000) NULL, approved_by VARCHAR(255) NULL, approved_at DATETIME2 NULL)""",
    "IF COL_LENGTH('patient_documents','scan_status') IS NULL ALTER TABLE patient_documents ADD scan_status VARCHAR(50) NOT NULL CONSTRAINT DF_patient_doc_scan DEFAULT 'Clean'",
    "IF COL_LENGTH('patient_documents','quarantine_reason') IS NULL ALTER TABLE patient_documents ADD quarantine_reason NVARCHAR(1000) NULL",
    "IF COL_LENGTH('patient_documents','approved_by') IS NULL ALTER TABLE patient_documents ADD approved_by VARCHAR(255) NULL",
    "IF COL_LENGTH('patient_documents','approved_at') IS NULL ALTER TABLE patient_documents ADD approved_at DATETIME2 NULL",
    """IF OBJECT_ID('patient_imports','U') IS NULL CREATE TABLE patient_imports (
        id INT IDENTITY PRIMARY KEY, patient_id INT NULL REFERENCES patients(id), schema_version VARCHAR(20) NOT NULL,
        source_identifier VARCHAR(255) NULL, payload_hash VARCHAR(64) NOT NULL, status VARCHAR(30) NOT NULL,
        summary_json NVARCHAR(MAX) NOT NULL, imported_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_patient_import_summary_json CHECK (ISJSON(summary_json)=1))""",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_identity_patient' AND object_id=OBJECT_ID('patient_identity_mappings')) CREATE INDEX idx_patient_identity_patient ON patient_identity_mappings(patient_id,id)",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_encounters_timeline' AND object_id=OBJECT_ID('patient_encounters')) CREATE INDEX idx_patient_encounters_timeline ON patient_encounters(patient_id,occurred_at DESC)",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_tests_history' AND object_id=OBJECT_ID('patient_tests')) CREATE INDEX idx_patient_tests_history ON patient_tests(patient_id,order_date DESC,id DESC)",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_notes_history' AND object_id=OBJECT_ID('patient_clinical_notes')) CREATE INDEX idx_patient_notes_history ON patient_clinical_notes(patient_id,created_at DESC,id DESC)",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_documents' AND object_id=OBJECT_ID('patient_documents')) CREATE INDEX idx_patient_documents ON patient_documents(patient_id,created_at DESC)",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_accessions_patient' AND object_id=OBJECT_ID('accessions')) CREATE INDEX idx_accessions_patient ON accessions(patient_id,case_id)",
]


def init_patient_schema(connect):
    with connect() as db:
        for statement in DDL:
            db.execute(statement)
        db.commit()


class PatientCreate(BaseModel):
    patient_identifier: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{3,64}$")
    cnic: str
    medical_record_number: str | None = Field(default=None, max_length=120)
    external_id: str | None = Field(default=None, max_length=255)
    first_name: str = Field(min_length=1, max_length=120)
    middle_name: str | None = Field(default=None, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    date_of_birth: date | None = None
    sex: str | None = Field(default=None, pattern=r"^(Female|Male|Intersex|Unknown|Not stated)$")
    gender_identity: str | None = Field(default=None, max_length=80)
    blood_group: str | None = Field(default=None, pattern=r"^(A|B|AB|O)[+-]$")
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=1000)
    status: str = Field(default="Active", pattern=r"^(Active|Inactive|Deceased|Archived)$")
    additional_details: dict = Field(default_factory=dict)
    organization_id: str | None = Field(default=None, max_length=100)
    facility_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    care_team: list[str] | str | None = None
    consent_status: str = Field(default="Granted", pattern=r"^(Granted|Revoked|Restricted|Pending)$")
    legal_hold: bool = False
    retention_until: datetime | None = None

    @field_validator("cnic", mode="before")
    @classmethod
    def normalize_cnic(cls, value):
        return _normalize_cnic(value)

    @field_validator("cnic")
    @classmethod
    def valid_cnic(cls, value):
        return _valid_cnic(value)

    @field_validator("date_of_birth")
    @classmethod
    def valid_dob(cls, value):
        if value and value > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return value


class PatientUpdate(BaseModel):
    cnic: str | None = None
    medical_record_number: str | None = Field(default=None, max_length=120)
    external_id: str | None = Field(default=None, max_length=255)
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    middle_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, min_length=1, max_length=120)
    date_of_birth: date | None = None
    sex: str | None = Field(default=None, pattern=r"^(Female|Male|Intersex|Unknown|Not stated)$")
    gender_identity: str | None = Field(default=None, max_length=80)
    blood_group: str | None = Field(default=None, pattern=r"^(A|B|AB|O)[+-]$")
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(default=None, pattern=r"^(Active|Inactive|Deceased|Archived)$")
    additional_details: dict | None = None
    organization_id: str | None = Field(default=None, max_length=100)
    facility_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    care_team: list[str] | str | None = None
    consent_status: str | None = Field(default=None, pattern=r"^(Granted|Revoked|Restricted|Pending)$")
    legal_hold: bool | None = None
    retention_until: datetime | None = None

    @field_validator("cnic", mode="before")
    @classmethod
    def normalize_cnic(cls, value):
        return _normalize_cnic(value) or None

    @field_validator("cnic")
    @classmethod
    def valid_cnic(cls, value):
        return _valid_cnic(value)


class MedicalHistoryUpdate(BaseModel):
    presentingComplaints: list[str] = Field(default_factory=list, max_length=100)
    pastMedicalHistory: list[str] = Field(default_factory=list, max_length=100)
    chronicConditions: list[str] = Field(default_factory=list, max_length=100)
    surgicalHistory: list[str] = Field(default_factory=list, max_length=100)
    familyHistory: list[str] = Field(default_factory=list, max_length=100)
    allergies: list[str] = Field(default_factory=list, max_length=100)
    medications: list[str] = Field(default_factory=list, max_length=200)
    riskFactors: list[str] = Field(default_factory=list, max_length=100)
    clinicalObservations: list[str] = Field(default_factory=list, max_length=100)
    additionalInformation: dict = Field(default_factory=dict)
    change_reason: str = Field(default="Profile update", min_length=3, max_length=1000)


class TestResultCreate(BaseModel):
    analyte_name: str = Field(min_length=1, max_length=255)
    result_value: str | None = Field(default=None, max_length=1000)
    result_type: str = Field(default="Text", pattern=r"^(Text|Numeric|Boolean|Coded|JSON)$")
    unit: str | None = Field(default=None, max_length=80)
    reference_range: str | None = Field(default=None, max_length=255)
    abnormal_flag: str | None = Field(default=None, pattern=r"^(Normal|Low|High|Critical|Abnormal|Unknown)$")
    structured_result: dict | None = None


class TestCreate(BaseModel):
    test_identifier: str = Field(min_length=1, max_length=120)
    test_name: str = Field(min_length=1, max_length=255)
    test_category: str = Field(min_length=1, max_length=100)
    encounter_id: int | None = None
    case_id: int | None = None
    report_id: int | None = None
    order_date: datetime | None = None
    collection_date: datetime | None = None
    result_date: datetime | None = None
    status: str = Field(default="Ordered", pattern=r"^(Ordered|Collected|In Progress|Preliminary|Final|Cancelled)$")
    summary_result: str | None = Field(default=None, max_length=2000)
    source_system: str = Field(default="Digital Pathology", max_length=120)
    provenance: dict = Field(default_factory=dict)
    results: list[TestResultCreate] = Field(default_factory=list, max_length=200)


class ClinicalNoteCreate(BaseModel):
    note_type: str = Field(default="Clinical", max_length=80)
    note_text: str = Field(min_length=1, max_length=20000)
    case_id: int | None = None
    encounter_id: int | None = None


class ClinicalNoteUpdate(BaseModel):
    note_text: str = Field(min_length=1, max_length=20000)
    change_reason: str = Field(min_length=3, max_length=1000)


class CaseLink(BaseModel):
    case_id: int
    accession_number: str = Field(min_length=1, max_length=255)
    order_id: str | None = Field(default=None, max_length=255)
    priority: str = Field(default="Routine", pattern=r"^(Routine|Urgent|Stat)$")


class PatientImportRequest(BaseModel):
    document: dict
    confirm: bool = False


def _json(value, default=None):
    if value in (None, ""):
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _patient_or_404(db, patient_id: int):
    row = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Patient not found")
    return row


def _serialize_patient(row):
    item = dict(row)
    item["additional_details"] = _json(item.pop("demographics_json", None))
    dob = item.get("date_of_birth")
    today = date.today()
    item["age"] = (today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))) if dob else None
    return item


def _page(page: int, page_size: int):
    return (page - 1) * page_size


def create_patient_router(connect, require_roles, audit, uploads_dir: Path):
    router = APIRouter(prefix="/api/patients", tags=["patients"])
    documents_dir = (uploads_dir / "patient-documents").resolve()
    documents_dir.mkdir(parents=True, exist_ok=True)

    @router.get("")
    def list_patients(search: str = "", status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), _=Depends(require_roles(READ_ROLES))):
        clauses = ["p.archived_at IS NULL"]
        params = []
        if search.strip():
            term = f"%{search.strip()}%"
            digits = re.sub(r"\D", "", search)
            clauses.append("(p.patient_identifier LIKE ? OR p.medical_record_number LIKE ? OR p.external_id LIKE ? OR p.display_label LIKE ? OR CONCAT(p.first_name,' ',p.last_name) LIKE ?" + (" OR p.cnic LIKE ?)" if digits else ")"))
            params.extend([term] * 5)
            if digits: params.append(f"%{digits}%")
        if status:
            clauses.append("p.status=?"); params.append(status)
        where = " AND ".join(clauses)
        with connect() as db:
            total = db.execute(f"SELECT COUNT(*) AS count FROM patients p WHERE {where}", tuple(params)).fetchone()["count"]
            rows = db.execute(f"""SELECT p.*,
                (SELECT COUNT(DISTINCT a.case_id) FROM accessions a WHERE a.patient_id=p.id AND a.case_id IS NOT NULL) AS total_cases
                FROM patients p WHERE {where} ORDER BY p.created_at DESC, p.id DESC
                OFFSET ? ROWS FETCH NEXT ? ROWS ONLY""", tuple(params + [_page(page, page_size), page_size])).fetchall()
        return {"items": [_serialize_patient(row) for row in rows], "page": page, "page_size": page_size, "total": total}

    @router.get("/duplicates")
    def duplicate_candidates(cnic: str | None = None, medical_record_number: str | None = None, first_name: str | None = None, last_name: str | None = None, date_of_birth: date | None = None, _=Depends(require_roles(WRITE_ROLES))):
        clauses, params = [], []
        if cnic:
            clauses.append("cnic=?"); params.append(_normalize_cnic(cnic))
        if medical_record_number:
            clauses.append("medical_record_number=?"); params.append(medical_record_number)
        if first_name and last_name and date_of_birth:
            clauses.append("(LOWER(first_name)=LOWER(?) AND LOWER(last_name)=LOWER(?) AND date_of_birth=?)")
            params.extend([first_name, last_name, date_of_birth])
        if not clauses:
            return []
        with connect() as db:
            rows = db.execute(f"SELECT TOP 20 * FROM patients WHERE {' OR '.join(clauses)} ORDER BY id", tuple(params)).fetchall()
        return [_serialize_patient(row) for row in rows]

    @router.post("", status_code=201)
    def create_patient(item: PatientCreate, user=Depends(require_roles(WRITE_ROLES))):
        display = " ".join(part for part in [item.first_name, item.middle_name, item.last_name] if part)
        with connect() as db:
            duplicate = db.execute("SELECT id FROM patients WHERE cnic=?", (item.cnic,)).fetchone()
            if duplicate:
                raise HTTPException(409, detail={"message": "A patient with this CNIC already exists", "patient_id": duplicate["id"]})
            if item.medical_record_number:
                duplicate = db.execute("SELECT id FROM patients WHERE medical_record_number=?", (item.medical_record_number,)).fetchone()
            if item.external_id and not duplicate:
                duplicate = db.execute("SELECT id FROM patients WHERE external_id=?", (item.external_id,)).fetchone()
            if duplicate:
                raise HTTPException(409, detail={"message": "A patient with this identifier already exists", "patient_id": duplicate["id"]})
            try:
                care_team_str = json.dumps(item.care_team) if isinstance(item.care_team, list) else item.care_team
                cur = db.execute("""INSERT INTO patients (external_id,display_label,patient_identifier,cnic,medical_record_number,first_name,middle_name,last_name,date_of_birth,sex,gender_identity,blood_group,phone,address,status,demographics_json,created_by,updated_by,organization_id,facility_name,department,care_team,consent_status,legal_hold,retention_until)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item.external_id, display, item.patient_identifier, item.cnic, item.medical_record_number, item.first_name, item.middle_name, item.last_name, item.date_of_birth, item.sex, item.gender_identity, item.blood_group, item.phone, item.address, item.status, json.dumps(item.additional_details), user["username"], user["username"], item.organization_id, item.facility_name, item.department, care_team_str, item.consent_status, 1 if item.legal_hold else 0, item.retention_until))
                patient_id = cur.lastrowid
                identifier = item.patient_identifier or f"PAT-{patient_id:06d}"
                db.execute("UPDATE patients SET patient_identifier=? WHERE id=?", (identifier, patient_id))
                audit(db, user["username"], "patient.create", "patient", patient_id, json.dumps({"patient_identifier": identifier}))
                db.commit()
            except pyodbc.IntegrityError:
                db.rollback()
                conflict_checks = [
                    ('cnic', item.cnic, 'A patient with this CNIC already exists'),
                    ('medical_record_number', item.medical_record_number, 'A patient with this medical record number already exists'),
                    ('external_id', item.external_id, 'A patient with this external identifier already exists'),
                    ('patient_identifier', item.patient_identifier, 'A patient with this patient identifier already exists'),
                ]
                for column, value, message in conflict_checks:
                    if value:
                        duplicate = db.execute(f'SELECT id FROM patients WHERE {column}=?', (value,)).fetchone()
                        if duplicate:
                            raise HTTPException(409, detail={'message': message, 'patient_id': duplicate['id']})
                raise HTTPException(409, "Patient identifier already exists")
            row = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        return _serialize_patient(row)

    @router.get("/{patient_id}")
    def get_patient(patient_id: int, _=Depends(require_roles(READ_ROLES))):
        with connect() as db: return _serialize_patient(_patient_or_404(db, patient_id))

    @router.patch("/{patient_id}")
    def update_patient(patient_id: int, item: PatientUpdate, user=Depends(require_roles(WRITE_ROLES))):
        values = item.model_dump(exclude_unset=True)
        additional = values.pop("additional_details", None)
        if not values and additional is None: raise HTTPException(400, "No changes supplied")
        allowed = {"cnic", "medical_record_number", "external_id", "first_name", "middle_name", "last_name", "date_of_birth", "sex", "gender_identity", "blood_group", "phone", "address", "status", "organization_id", "facility_name", "department", "care_team", "consent_status", "legal_hold", "retention_until"}
        if "care_team" in values and isinstance(values["care_team"], list):
            values["care_team"] = json.dumps(values["care_team"])
        if "legal_hold" in values and values["legal_hold"] is not None:
            values["legal_hold"] = 1 if values["legal_hold"] else 0
        if "cnic" in values and values["cnic"] is None: values.pop("cnic")
        with connect() as db:
            current = _patient_or_404(db, patient_id)
            if values.get("cnic"):
                taken = db.execute("SELECT id FROM patients WHERE cnic=? AND id<>?", (values["cnic"], patient_id)).fetchone()
                if taken: raise HTTPException(409, detail={"message": "A patient with this CNIC already exists", "patient_id": taken["id"]})
            sets, params = [], []
            for key, value in values.items():
                if key in allowed: sets.append(f"{key}=?"); params.append(value)
            if additional is not None: sets.append("demographics_json=?"); params.append(json.dumps(additional))
            first = values.get("first_name", current.get("first_name")); middle = values.get("middle_name", current.get("middle_name")); last = values.get("last_name", current.get("last_name"))
            if any(k in values for k in ("first_name", "middle_name", "last_name")):
                sets.append("display_label=?"); params.append(" ".join(p for p in (first, middle, last) if p))
            sets.extend(["updated_by=?", "updated_at=SYSUTCDATETIME()"]); params.extend([user["username"], patient_id])
            try:
                db.execute(f"UPDATE patients SET {','.join(sets)} WHERE id=?", tuple(params))
                audit(db, user["username"], "patient.update", "patient", patient_id, json.dumps({"fields": sorted(values.keys())}))
                db.commit()
            except pyodbc.IntegrityError:
                db.rollback(); raise HTTPException(409, "Patient identifier already exists")
            return _serialize_patient(db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone())

    @router.delete("/{patient_id}")
    def delete_patient(patient_id: int, cascade_cases: bool = Query(True), user=Depends(require_roles(ADMIN_ROLES))):
        filenames = []
        with connect() as db:
            patient = _patient_or_404(db, patient_id)
            if bool(patient.get("legal_hold")):
                raise HTTPException(400, "Cannot delete patient record under active legal hold")
            
            # Find all linked cases
            linked_cases = db.execute("SELECT DISTINCT case_id FROM accessions WHERE patient_id=? AND case_id IS NOT NULL", (patient_id,)).fetchall()
            case_ids = [row["case_id"] for row in linked_cases if row.get("case_id")]

            if cascade_cases and case_ids:
                for case_id in case_ids:
                    case = db.execute("SELECT id, case_id, filename, status FROM cases WHERE id=?", (case_id,)).fetchone()
                    if not case:
                        continue
                    if case.get("filename"):
                        filenames.append(case["filename"])
                    slides = db.execute("SELECT id, filename FROM slides WHERE case_id=?", (case_id,)).fetchall()
                    slide_ids = [row["id"] for row in slides]
                    filenames.extend(row["filename"] for row in slides if row.get("filename"))
                    if slide_ids:
                        slide_marks = ",".join("?" for _ in slide_ids)
                        versions = db.execute(f"SELECT id, stored_filename FROM image_versions WHERE slide_id IN ({slide_marks})", tuple(slide_ids)).fetchall()
                        filenames.extend(row["stored_filename"] for row in versions if row.get("stored_filename"))
                        version_ids = [row["id"] for row in versions]
                        db.execute(f"UPDATE report_findings SET annotation_id=NULL WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id IN ({slide_marks}))", tuple(slide_ids))
                        db.execute(f"DELETE FROM annotation_revisions WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id IN ({slide_marks}))", tuple(slide_ids))
                        db.execute(f"DELETE FROM annotations WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        db.execute(f"UPDATE annotations SET source_ai_result_id=NULL WHERE source_ai_result_id IN (SELECT r.id FROM ai_results r JOIN ai_jobs j ON j.id=r.ai_job_id WHERE j.slide_id IN ({slide_marks}))", tuple(slide_ids))
                        db.execute(f"DELETE FROM ai_results WHERE ai_job_id IN (SELECT id FROM ai_jobs WHERE slide_id IN ({slide_marks}))", tuple(slide_ids))
                        db.execute(f"DELETE FROM ai_jobs WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        try:
                            db.execute(f"DELETE FROM ai_analyses WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        except Exception:
                            pass
                        if version_ids:
                            version_marks = ",".join("?" for _ in version_ids)
                            db.execute(f"UPDATE annotations SET image_version_id=NULL WHERE image_version_id IN ({version_marks})", tuple(version_ids))
                            db.execute(f"DELETE FROM image_metadata_revisions WHERE image_version_id IN ({version_marks})", tuple(version_ids))
                        db.execute(f"DELETE FROM viewer_bookmarks WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        db.execute(f"DELETE FROM viewer_preferences WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        db.execute(f"DELETE FROM slide_qc_history WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        db.execute(f"DELETE FROM image_versions WHERE slide_id IN ({slide_marks})", tuple(slide_ids))
                        db.execute(f"DELETE FROM slides WHERE id IN ({slide_marks})", tuple(slide_ids))
                    try:
                        db.execute("DELETE FROM ai_analyses WHERE case_id=?", (case_id,))
                    except Exception:
                        pass
                    db.execute("DELETE FROM report_findings WHERE report_id IN (SELECT id FROM reports WHERE case_id=?)", (case_id,))
                    db.execute("DELETE FROM report_reviews WHERE report_id IN (SELECT id FROM reports WHERE case_id=?)", (case_id,))
                    db.execute("DELETE FROM reports WHERE case_id=?", (case_id,))
                    db.execute("DELETE FROM case_review_notes WHERE case_id=?", (case_id,))
                    db.execute("DELETE FROM case_review_history WHERE case_id=?", (case_id,))
                    db.execute("DELETE FROM case_consultations WHERE case_id=?", (case_id,))
                    db.execute("DELETE FROM research_folder_members WHERE case_id=?", (case_id,))
                    db.execute("DELETE FROM case_links WHERE case_id=? OR related_case_id=?", (case_id, case_id))
                    db.execute("DELETE FROM case_operations WHERE source_case_id=? OR target_case_id=?", (case_id, case_id))
                    db.execute("DELETE FROM accessions WHERE case_id=?", (case_id,))
                    db.execute("UPDATE ingestion_jobs SET matched_case_id=NULL WHERE matched_case_id=?", (case_id,))
                    db.execute("UPDATE reconciliation_items SET resolved_case_id=NULL WHERE resolved_case_id=?", (case_id,))
                    db.execute("DELETE FROM cases WHERE id=?", (case_id,))
                    db.execute("INSERT INTO audit_log (actor_username,action,target_type,target_id,details) VALUES (?,?,?,?,?)",
                               (user["username"], "case.delete", "case", str(case_id), json.dumps({"cascade_patient_id": patient_id, "case_code": case.get("case_id")})))
            else:
                # Unlink accessions so cases are preserved if cascade is false
                db.execute("UPDATE accessions SET patient_id=NULL WHERE patient_id=?", (patient_id,))

            # Clean up unlinked accessions belonging directly to this patient
            db.execute("DELETE FROM accessions WHERE patient_id=?", (patient_id,))

            # Remove associated child clinical and longitudinal data
            for child_table in (
                "patient_test_results",
                "patient_clinical_note_revisions",
                "patient_medical_history_revisions",
            ):
                if db.execute("SELECT OBJECT_ID(?, 'U') AS id", (child_table,)).fetchone()["id"]:
                    if child_table == "patient_test_results":
                        db.execute("DELETE FROM patient_test_results WHERE test_id IN (SELECT id FROM patient_tests WHERE patient_id=?)", (patient_id,))
                    elif child_table == "patient_clinical_note_revisions":
                        db.execute("DELETE FROM patient_clinical_note_revisions WHERE note_id IN (SELECT id FROM patient_clinical_notes WHERE patient_id=?)", (patient_id,))
                    elif child_table == "patient_medical_history_revisions":
                        db.execute("DELETE FROM patient_medical_history_revisions WHERE patient_id=?", (patient_id,))
            for direct_table in (
                "patient_documents",
                "patient_clinical_notes",
                "patient_tests",
                "patient_encounters",
                "patient_medical_history",
                "patient_identity_mappings",
                "patient_imports",
            ):
                if db.execute("SELECT OBJECT_ID(?, 'U') AS id", (direct_table,)).fetchone()["id"]:
                    db.execute(f"DELETE FROM {direct_table} WHERE patient_id=?", (patient_id,))
            db.execute("DELETE FROM patients WHERE id=?", (patient_id,))
            audit(db, user["username"], "patient.delete", "patient", patient_id, json.dumps({"patient_identifier": patient.get("patient_identifier"), "cascaded_cases": len(case_ids) if cascade_cases else 0}))
            db.commit()

        if filenames:
            deleted_dir = (uploads_dir / "_deleted_cases").resolve()
            deleted_dir.mkdir(parents=True, exist_ok=True)
            upload_root = uploads_dir.resolve()
            for filename in set(filenames):
                source = (uploads_dir / filename).resolve()
                if upload_root not in source.parents or not source.is_file():
                    continue
                try:
                    destination = deleted_dir / f"patient_{patient_id}_{source.name}"
                    if destination.exists():
                        destination = deleted_dir / f"patient_{patient_id}_{uuid4().hex}_{source.name}"
                    shutil.move(str(source), str(destination))
                except OSError:
                    pass

        return {"message": "Patient profile deleted successfully", "patient_id": patient_id}

    @router.get("/{patient_id}/medical-history")
    def medical_history(patient_id: int, _=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            _patient_or_404(db, patient_id)
            row = db.execute("SELECT * FROM patient_medical_history WHERE patient_id=?", (patient_id,)).fetchone()
            revisions = db.execute("SELECT id,revision_number,changed_by,change_reason,created_at FROM patient_medical_history_revisions WHERE patient_id=? ORDER BY revision_number DESC", (patient_id,)).fetchall()
        return {"schema_version": PATIENT_SCHEMA_VERSION, "history": _json(row["history_json"]) if row else {}, "revision_number": row["revision_number"] if row else 0, "updated_at": row["updated_at"] if row else None, "revisions": [dict(x) for x in revisions]}

    @router.put("/{patient_id}/medical-history")
    def save_medical_history(patient_id: int, item: MedicalHistoryUpdate, user=Depends(require_roles(CLINICAL_WRITE_ROLES))):
        payload = item.model_dump(exclude={"change_reason"})
        encoded = json.dumps(payload, ensure_ascii=False)
        with connect() as db:
            _patient_or_404(db, patient_id)
            current = db.execute("SELECT * FROM patient_medical_history WHERE patient_id=?", (patient_id,)).fetchone()
            revision = (current["revision_number"] + 1) if current else 1
            db.execute("INSERT INTO patient_medical_history_revisions (patient_id,revision_number,schema_version,history_json,changed_by,change_reason) VALUES (?,?,?,?,?,?)", (patient_id, revision, PATIENT_SCHEMA_VERSION, encoded, user["username"], item.change_reason))
            if current:
                db.execute("UPDATE patient_medical_history SET schema_version=?,history_json=?,revision_number=?,updated_by=?,updated_at=SYSUTCDATETIME() WHERE patient_id=?", (PATIENT_SCHEMA_VERSION, encoded, revision, user["username"], patient_id))
            else:
                db.execute("INSERT INTO patient_medical_history (patient_id,schema_version,history_json,revision_number,created_by,updated_by) VALUES (?,?,?,?,?,?)", (patient_id, PATIENT_SCHEMA_VERSION, encoded, revision, user["username"], user["username"]))
            audit(db, user["username"], "patient.medical_history.update", "patient", patient_id, json.dumps({"revision": revision}))
            db.commit()
        return {"schema_version": PATIENT_SCHEMA_VERSION, "history": payload, "revision_number": revision}

    @router.post("/{patient_id}/cases", status_code=201)
    def link_case(patient_id: int, item: CaseLink, user=Depends(require_roles(WRITE_ROLES))):
        with connect() as db:
            _patient_or_404(db, patient_id)
            if not db.execute("SELECT id FROM cases WHERE id=?", (item.case_id,)).fetchone(): raise HTTPException(404, "Case not found")
            existing = db.execute("SELECT id,patient_id FROM accessions WHERE case_id=?", (item.case_id,)).fetchone()
            if existing and existing["patient_id"] not in (None, patient_id): raise HTTPException(409, "Case is linked to another patient")
            if existing:
                db.execute("UPDATE accessions SET patient_id=?,accession_number=?,order_id=?,priority=? WHERE id=?", (patient_id,item.accession_number,item.order_id,item.priority,existing["id"])); accession_id=existing["id"]
            else:
                accession_id=db.execute("INSERT INTO accessions (case_id,patient_id,accession_number,order_id,priority) VALUES (?,?,?,?,?)", (item.case_id,patient_id,item.accession_number,item.order_id,item.priority)).lastrowid
            audit(db,user["username"],"patient.case.link","patient",patient_id,json.dumps({"case_id":item.case_id,"accession_id":accession_id})); db.commit()
        return {"id":accession_id,"patient_id":patient_id,**item.model_dump()}

    @router.get("/{patient_id}/cases")
    def patient_cases(patient_id: int, search: str="", status: str|None=None, date_from: date|None=None, date_to: date|None=None, page:int=Query(1,ge=1), page_size:int=Query(25,ge=1,le=100), _=Depends(require_roles(READ_ROLES))):
        clauses=["a.patient_id=?","a.case_id IS NOT NULL"]; params=[patient_id]
        if search: clauses.append("(c.case_id LIKE ? OR a.accession_number LIKE ?)"); params.extend([f"%{search}%"]*2)
        if status: clauses.append("c.status=?"); params.append(status)
        if date_from: clauses.append("c.created_at>=?"); params.append(date_from)
        if date_to: clauses.append("c.created_at<DATEADD(day,1,?)"); params.append(date_to)
        where=" AND ".join(clauses)
        with connect() as db:
            _patient_or_404(db,patient_id)
            total=db.execute(f"SELECT COUNT(*) AS count FROM accessions a JOIN cases c ON c.id=a.case_id WHERE {where}",tuple(params)).fetchone()["count"]
            rows=db.execute(f"""SELECT c.*,a.accession_number,a.order_id,a.priority AS accession_priority,a.due_at AS received_due_at,
                (SELECT COUNT(*) FROM specimens sp WHERE sp.accession_id=a.id) specimen_count,
                (SELECT COUNT(*) FROM slides s WHERE s.case_id=c.id) slide_count,
                (SELECT TOP 1 id FROM slides s WHERE s.case_id=c.id ORDER BY id) first_slide_id,
                (SELECT TOP 1 id FROM reports r WHERE r.case_id=c.id ORDER BY version DESC) latest_report_id,
                (SELECT TOP 1 status FROM reports r WHERE r.case_id=c.id ORDER BY version DESC) report_status
                FROM accessions a JOIN cases c ON c.id=a.case_id WHERE {where} ORDER BY c.created_at DESC,c.id DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY""",tuple(params+[_page(page,page_size),page_size])).fetchall()
        return {"items":[dict(x) for x in rows],"page":page,"page_size":page_size,"total":total}

    @router.get("/{patient_id}/tests")
    def tests(patient_id:int,page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),_=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id); total=db.execute("SELECT COUNT(*) count FROM patient_tests WHERE patient_id=?",(patient_id,)).fetchone()["count"]
            rows=db.execute("SELECT * FROM patient_tests WHERE patient_id=? ORDER BY COALESCE(result_date,order_date,created_at) DESC,id DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",(patient_id,_page(page,page_size),page_size)).fetchall()
            items=[]
            for row in rows:
                item=dict(row); item["provenance"]=_json(item.pop("provenance_json",None)); item["results"]=[{**dict(r),"structured_result":_json(r["result_json"]) if r["result_json"] else None} for r in db.execute("SELECT * FROM patient_test_results WHERE test_id=? ORDER BY id",(row["id"],)).fetchall()]; items.append(item)
        return {"items":items,"page":page,"page_size":page_size,"total":total}

    @router.post("/{patient_id}/tests",status_code=201)
    def create_test(patient_id:int,item:TestCreate,user=Depends(require_roles(CLINICAL_WRITE_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id)
            if item.case_id and not db.execute("SELECT 1 ok FROM accessions WHERE patient_id=? AND case_id=?",(patient_id,item.case_id)).fetchone(): raise HTTPException(400,"Case is not linked to this patient")
            if item.report_id and not db.execute("SELECT 1 ok FROM reports r JOIN accessions a ON a.case_id=r.case_id WHERE r.id=? AND a.patient_id=?",(item.report_id,patient_id)).fetchone(): raise HTTPException(400,"Report is not linked to this patient")
            try:
                test_id=db.execute("""INSERT INTO patient_tests (patient_id,encounter_id,case_id,report_id,test_identifier,test_name,test_category,order_date,collection_date,result_date,status,summary_result,source_system,provenance_json,created_by,updated_by)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(patient_id,item.encounter_id,item.case_id,item.report_id,item.test_identifier,item.test_name,item.test_category,item.order_date,item.collection_date,item.result_date,item.status,item.summary_result,item.source_system,json.dumps(item.provenance),user["username"],user["username"])).lastrowid
                for result in item.results:
                    db.execute("INSERT INTO patient_test_results (test_id,analyte_name,result_value,result_type,unit,reference_range,abnormal_flag,result_json) VALUES (?,?,?,?,?,?,?,?)",(test_id,result.analyte_name,result.result_value,result.result_type,result.unit,result.reference_range,result.abnormal_flag,json.dumps(result.structured_result) if result.structured_result is not None else None))
                audit(db,user["username"],"patient.test.create","patient",patient_id,json.dumps({"test_id":test_id,"test_identifier":item.test_identifier})); db.commit()
            except pyodbc.IntegrityError:
                db.rollback(); raise HTTPException(409,"Test identifier already exists for this patient")
        return {"id":test_id,"patient_id":patient_id,**item.model_dump()}

    @router.get("/{patient_id}/reports")
    def reports(patient_id:int,_=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id)
            rows=db.execute("""SELECT DISTINCT r.*,c.case_id AS case_number FROM reports r JOIN cases c ON c.id=r.case_id
                JOIN accessions a ON a.case_id=c.id WHERE a.patient_id=? ORDER BY r.created_at DESC,r.version DESC""",(patient_id,)).fetchall()
        return [dict(x) for x in rows]

    @router.get("/{patient_id}/slides")
    def slides(patient_id:int,_=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id)
            rows=db.execute("""SELECT DISTINCT s.*,c.case_id AS case_number,sp.specimen_code,b.block_code,
                (SELECT TOP 1 version FROM image_versions iv WHERE iv.slide_id=s.id ORDER BY version DESC) image_version
                FROM slides s JOIN cases c ON c.id=s.case_id JOIN accessions a ON a.case_id=c.id
                LEFT JOIN blocks b ON b.id=s.block_id LEFT JOIN specimens sp ON sp.id=b.specimen_id
                WHERE a.patient_id=? ORDER BY s.created_at DESC""",(patient_id,)).fetchall()
        return [dict(x) for x in rows]

    @router.get("/{patient_id}/notes")
    def notes(patient_id:int,case_id:int|None=None,_=Depends(require_roles(READ_ROLES))):
        clause="patient_id=? AND is_archived=0"; params=[patient_id]
        if case_id is not None: clause+=" AND case_id=?"; params.append(case_id)
        with connect() as db:
            _patient_or_404(db,patient_id); rows=db.execute(f"SELECT * FROM patient_clinical_notes WHERE {clause} ORDER BY created_at DESC,id DESC",tuple(params)).fetchall()
        return [dict(x) for x in rows]

    @router.post("/{patient_id}/notes",status_code=201)
    def add_note(patient_id:int,item:ClinicalNoteCreate,user=Depends(require_roles(CLINICAL_WRITE_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id)
            if item.case_id and not db.execute("SELECT 1 ok FROM accessions WHERE patient_id=? AND case_id=?",(patient_id,item.case_id)).fetchone(): raise HTTPException(400,"Case is not linked to this patient")
            note_id=db.execute("INSERT INTO patient_clinical_notes (patient_id,case_id,encounter_id,note_type,note_text,created_by,updated_by) VALUES (?,?,?,?,?,?,?)",(patient_id,item.case_id,item.encounter_id,item.note_type,item.note_text,user["username"],user["username"])).lastrowid
            db.execute("INSERT INTO patient_clinical_note_revisions (note_id,revision_number,note_text,changed_by,change_reason) VALUES (?,1,?,?,?)",(note_id,item.note_text,user["username"],"Note created"))
            audit(db,user["username"],"patient.note.create","patient",patient_id,json.dumps({"note_id":note_id,"note_type":item.note_type})); db.commit()
            return dict(db.execute("SELECT * FROM patient_clinical_notes WHERE id=?",(note_id,)).fetchone())

    @router.patch("/{patient_id}/notes/{note_id}")
    def update_note(patient_id:int,note_id:int,item:ClinicalNoteUpdate,user=Depends(require_roles(CLINICAL_WRITE_ROLES))):
        with connect() as db:
            note=db.execute("SELECT * FROM patient_clinical_notes WHERE id=? AND patient_id=? AND is_archived=0",(note_id,patient_id)).fetchone()
            if not note: raise HTTPException(404,"Clinical note not found")
            if note["created_by"] != user["username"] and user["role"] not in {"System Administrator","Laboratory Manager"}: raise HTTPException(403,"Only the author or a manager can revise this note")
            revision=note["revision_number"]+1
            db.execute("INSERT INTO patient_clinical_note_revisions (note_id,revision_number,note_text,changed_by,change_reason) VALUES (?,?,?,?,?)",(note_id,revision,item.note_text,user["username"],item.change_reason))
            db.execute("UPDATE patient_clinical_notes SET note_text=?,revision_number=?,updated_by=?,updated_at=SYSUTCDATETIME() WHERE id=?",(item.note_text,revision,user["username"],note_id))
            audit(db,user["username"],"patient.note.update","patient",patient_id,json.dumps({"note_id":note_id,"revision":revision})); db.commit()
            return dict(db.execute("SELECT * FROM patient_clinical_notes WHERE id=?",(note_id,)).fetchone())

    @router.get("/{patient_id}/documents")
    def documents(patient_id:int,_=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            _patient_or_404(db,patient_id); rows=db.execute("SELECT id,patient_id,case_id,document_type,original_filename,content_type,file_size,description,uploaded_by,created_at FROM patient_documents WHERE patient_id=? AND archived_at IS NULL ORDER BY created_at DESC",(patient_id,)).fetchall()
        return [dict(x) for x in rows]

    @router.post("/{patient_id}/documents",status_code=201)
    async def upload_document(patient_id:int,document_type:str=Form(...),description:str=Form(""),case_id:int|None=Form(None),file:UploadFile=File(...),user=Depends(require_roles(CLINICAL_WRITE_ROLES))):
        import hashlib
        content_type=(file.content_type or mimetypes.guess_type(file.filename or "")[0] or "").lower()
        if content_type not in DOCUMENT_TYPES: raise HTTPException(415,"Only PDF, JPEG, PNG, and plain-text documents are permitted")
        limit=20*1024*1024; data=await file.read(limit+1)
        if not data or len(data)>limit: raise HTTPException(413,"Document must be between 1 byte and 20 MB")
        signatures={"application/pdf":data.startswith(b"%PDF-"),"image/jpeg":data.startswith(b"\xff\xd8\xff"),"image/png":data.startswith(b"\x89PNG\r\n\x1a\n"),"text/plain":b"\x00" not in data[:4096]}
        if not signatures[content_type]: raise HTTPException(415,"Document content does not match its declared file type")
        suffix=DOCUMENT_TYPES[content_type]; stored=f"{uuid4().hex}{suffix}"; path=(documents_dir/stored).resolve()
        if path.parent != documents_dir: raise HTTPException(400,"Invalid document path")
        with connect() as db:
            _patient_or_404(db,patient_id)
            if case_id and not db.execute("SELECT 1 ok FROM accessions WHERE patient_id=? AND case_id=?",(patient_id,case_id)).fetchone(): raise HTTPException(400,"Case is not linked to this patient")
            # Malware scanning check: Detect suspicious signatures (EICAR test or executable signatures)
            is_malware = b"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*" in data or data.startswith(b"MZ")
            scan_status = "Quarantined" if is_malware else "Clean"
            quarantine_reason = "Malware signature or suspicious executable pattern detected" if is_malware else None

            try:
                path.write_bytes(data)
                doc_id=db.execute("INSERT INTO patient_documents (patient_id,case_id,document_type,original_filename,stored_filename,content_type,file_size,checksum_sha256,description,uploaded_by,scan_status,quarantine_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(patient_id,case_id,document_type,(file.filename or "document")[:512],stored,content_type,len(data),hashlib.sha256(data).hexdigest(),description[:1000],user["username"],scan_status,quarantine_reason)).lastrowid
                audit(db,user["username"],"patient.document.upload","patient",patient_id,json.dumps({"document_id":doc_id,"document_type":document_type,"file_size":len(data),"scan_status":scan_status})); db.commit()
            except Exception:
                if path.exists(): path.unlink()
                raise
        return {"id":doc_id,"patient_id":patient_id,"original_filename":file.filename,"content_type":content_type,"file_size":len(data)}

    @router.post("/{patient_id}/documents/{document_id}/quarantine-action")
    def quarantine_action(patient_id: int, document_id: int, action: str = Form(..., pattern=r"^(approve|reject)$"), reason: str = Form(""), user=Depends(require_roles(["System Administrator", "Laboratory Manager"]))):
        with connect() as db:
            _patient_or_404(db, patient_id)
            row = db.execute("SELECT * FROM patient_documents WHERE id=? AND patient_id=? AND archived_at IS NULL", (document_id, patient_id)).fetchone()
            if not row: raise HTTPException(404, "Document not found")
            new_status = "Clean" if action == "approve" else "Rejected"
            db.execute("UPDATE patient_documents SET scan_status=?, approved_by=?, approved_at=SYSUTCDATETIME(), quarantine_reason=? WHERE id=?",
                       (new_status, user["username"], reason or f"Quarantine {action}d by {user['username']}", document_id))
            audit(db, user["username"], f"patient.document.quarantine_{action}", "patient", patient_id, json.dumps({"document_id": document_id, "status": new_status, "reason": reason}))
            db.commit()
            return {"message": f"Document quarantine {action}d successfully", "scan_status": new_status, "document_id": document_id}

    @router.get("/{patient_id}/documents/{document_id}/download")
    def download_document(patient_id:int,document_id:int,user=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            row=db.execute("SELECT * FROM patient_documents WHERE id=? AND patient_id=? AND archived_at IS NULL",(document_id,patient_id)).fetchone()
            if not row: raise HTTPException(404,"Document not found")
            if row.get("scan_status") in ("Quarantined", "Rejected") and user["role"] not in ("System Administrator", "Laboratory Manager"):
                raise HTTPException(403, "Access to quarantined or rejected document is restricted until approved")
            audit(db,user["username"],"patient.document.download","patient",patient_id,json.dumps({"document_id":document_id})); db.commit()
        path=(documents_dir/row["stored_filename"]).resolve()
        if path.parent != documents_dir or not path.is_file(): raise HTTPException(404,"Stored document is unavailable")
        return FileResponse(path,media_type=row["content_type"],filename=row["original_filename"])

    def timeline_source():
        return """SELECT event_type,event_at,description,actor,record_type,record_id FROM (
          SELECT 'Patient registration' event_type,p.created_at event_at,'Patient record registered' description,p.created_by actor,'patient' record_type,p.id record_id FROM patients p WHERE p.id=?
          UNION ALL SELECT 'Encounter',e.occurred_at,CONCAT(e.encounter_type,' encounter'),e.created_by,'encounter',e.id FROM patient_encounters e WHERE e.patient_id=?
          UNION ALL SELECT 'Case created',c.created_at,CONCAT('Case ',c.case_id),NULL,'case',c.id FROM accessions a JOIN cases c ON c.id=a.case_id WHERE a.patient_id=?
          UNION ALL SELECT 'Slide uploaded',s.created_at,CONCAT('Slide ',s.label),NULL,'slide',s.id FROM accessions a JOIN slides s ON s.case_id=a.case_id WHERE a.patient_id=?
          UNION ALL SELECT CASE WHEN r.status='Finalized' THEN 'Report finalized' ELSE 'Report drafted' END,COALESCE(TRY_CONVERT(DATETIME2,r.finalized_at),r.created_at),CONCAT('Report version ',r.version),r.reviewer_name,'report',r.id FROM accessions a JOIN reports r ON r.case_id=a.case_id WHERE a.patient_id=?
          UNION ALL SELECT 'Test recorded',COALESCE(t.result_date,t.order_date,t.created_at),t.test_name,t.created_by,'test',t.id FROM patient_tests t WHERE t.patient_id=?
          UNION ALL SELECT 'Clinical note added',n.created_at,n.note_type,n.created_by,'note',n.id FROM patient_clinical_notes n WHERE n.patient_id=? AND n.is_archived=0
          UNION ALL SELECT 'AI research analysis',j.completed_at,CONCAT(m.name,' ',m.version),j.requested_by,'ai_job',j.id FROM accessions a JOIN slides s ON s.case_id=a.case_id JOIN ai_jobs j ON j.slide_id=s.id JOIN ai_models m ON m.id=j.model_id WHERE a.patient_id=? AND j.status='Completed'
        ) events WHERE event_at IS NOT NULL"""

    @router.get("/{patient_id}/timeline")
    def timeline(patient_id:int,event_type:str|None=None,date_from:datetime|None=None,date_to:datetime|None=None,order:str=Query("desc",pattern=r"^(asc|desc)$"),page:int=Query(1,ge=1),page_size:int=Query(30,ge=1,le=100),_=Depends(require_roles(READ_ROLES))):
        clauses=[]; filters=[]
        if event_type: clauses.append("event_type=?"); filters.append(event_type)
        if date_from: clauses.append("event_at>=?"); filters.append(date_from)
        if date_to: clauses.append("event_at<=?"); filters.append(date_to)
        where=f" WHERE {' AND '.join(clauses)}" if clauses else ""
        source=timeline_source(); patient_params=[patient_id]*8
        with connect() as db:
            _patient_or_404(db,patient_id)
            total=db.execute(f"SELECT COUNT(*) AS count FROM ({source}) patient_events{where}",tuple(patient_params+filters)).fetchone()["count"]
            rows=db.execute(f"SELECT * FROM ({source}) patient_events{where} ORDER BY event_at {order.upper()},record_id {order.upper()} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",tuple(patient_params+filters+[_page(page,page_size),page_size])).fetchall()
        return {"items":[dict(x) for x in rows],"page":page,"page_size":page_size,"total":total}

    def complete_profile_data(db,patient_id:int):
        patient=_serialize_patient(_patient_or_404(db,patient_id))
        history=db.execute("SELECT history_json,revision_number,updated_at FROM patient_medical_history WHERE patient_id=?",(patient_id,)).fetchone()
        counts=db.execute("""SELECT
          (SELECT COUNT(DISTINCT case_id) FROM accessions WHERE patient_id=? AND case_id IS NOT NULL) total_cases,
          (SELECT COUNT(*) FROM patient_tests WHERE patient_id=?) total_tests,
          (SELECT COUNT(*) FROM reports r JOIN accessions a ON a.case_id=r.case_id WHERE a.patient_id=? AND r.status='Finalized') total_finalized_reports,
          (SELECT COUNT(*) FROM slides s JOIN accessions a ON a.case_id=s.case_id WHERE a.patient_id=?) total_slides""",(patient_id,)*4).fetchone()
        recent_cases=db.execute("SELECT TOP 5 c.id,c.case_id,c.status,c.created_at,a.accession_number FROM accessions a JOIN cases c ON c.id=a.case_id WHERE a.patient_id=? ORDER BY c.created_at DESC",(patient_id,)).fetchall()
        recent_tests=db.execute("SELECT TOP 5 id,test_identifier,test_name,status,COALESCE(result_date,order_date,created_at) event_at FROM patient_tests WHERE patient_id=? ORDER BY event_at DESC",(patient_id,)).fetchall()
        recent_reports=db.execute("SELECT TOP 5 r.id,r.case_id,r.version,r.status,r.reviewer_name,r.created_at,r.finalized_at FROM reports r JOIN accessions a ON a.case_id=r.case_id WHERE a.patient_id=? ORDER BY r.created_at DESC",(patient_id,)).fetchall()
        recent_timeline=[dict(x) for x in db.execute(f"SELECT TOP 8 * FROM ({timeline_source()}) patient_events ORDER BY event_at DESC,record_id DESC",(patient_id,)*8).fetchall()]
        return {"schema_version":PATIENT_SCHEMA_VERSION,"patient":patient,"summary":dict(counts),"medical_history":{"history":_json(history["history_json"]) if history else {},"revision_number":history["revision_number"] if history else 0,"updated_at":history["updated_at"] if history else None},"recent_cases":[dict(x) for x in recent_cases],"recent_tests":[dict(x) for x in recent_tests],"recent_reports":[dict(x) for x in recent_reports],"recent_timeline":recent_timeline}

    @router.get("/{patient_id}/complete-profile")
    def complete_profile(patient_id:int,user=Depends(require_roles(READ_ROLES))):
        with connect() as db:
            data=complete_profile_data(db,patient_id); audit(db,user["username"],"patient.profile.view","patient",patient_id); db.commit(); return data

    @router.get("/{patient_id}/export")
    def export_patient(patient_id:int,user=Depends(require_roles(EXPORT_ROLES))):
        with connect() as db:
            data=complete_profile_data(db,patient_id)
            data.update({"exported_at":datetime.now(timezone.utc).isoformat(),"provenance":{"system":"Digital Pathology Image Management System","exported_by":user["username"]}})
            data["case_history"]=[dict(x) for x in db.execute("SELECT c.id,c.case_id,c.status,c.created_at,a.accession_number,a.order_id FROM accessions a JOIN cases c ON c.id=a.case_id WHERE a.patient_id=? ORDER BY c.created_at",(patient_id,)).fetchall()]
            data["test_history"]=[dict(x) for x in db.execute("SELECT * FROM patient_tests WHERE patient_id=? ORDER BY id",(patient_id,)).fetchall()]
            data["reports"]=[dict(x) for x in db.execute("SELECT r.* FROM reports r JOIN accessions a ON a.case_id=r.case_id WHERE a.patient_id=? ORDER BY r.id",(patient_id,)).fetchall()]
            data["slides"]=[dict(x) for x in db.execute("SELECT s.id,s.case_id,s.label,s.stain,s.filename,s.file_type,s.format,s.created_at,s.qc_status FROM slides s JOIN accessions a ON a.case_id=s.case_id WHERE a.patient_id=? ORDER BY s.id",(patient_id,)).fetchall()]
            data["clinical_notes"]=[dict(x) for x in db.execute("SELECT * FROM patient_clinical_notes WHERE patient_id=? AND is_archived=0 ORDER BY id",(patient_id,)).fetchall()]
            data["attachments"]=[dict(x) for x in db.execute("SELECT id,case_id,document_type,original_filename,content_type,file_size,description,uploaded_by,created_at FROM patient_documents WHERE patient_id=? AND archived_at IS NULL ORDER BY id",(patient_id,)).fetchall()]
            audit(db,user["username"],"patient.export","patient",patient_id); db.commit()
        return data

    @router.post("/import")
    def import_patient(item:PatientImportRequest,user=Depends(require_roles(["System Administrator","Laboratory Manager"]))):
        import hashlib
        doc=item.document; version=doc.get("schema_version")
        if version != PATIENT_SCHEMA_VERSION: raise HTTPException(422,f"Unsupported patient JSON schema version: {version}")
        patient=doc.get("patient")
        if not isinstance(patient,dict): raise HTTPException(422,"Patient object is required")
        identifier=patient.get("patient_identifier"); mrn=patient.get("medical_record_number")
        if not identifier and not mrn: raise HTTPException(422,"Patient identifier or medical record number is required")
        with connect() as db:
            matches=db.execute("SELECT id,patient_identifier,medical_record_number,display_label FROM patients WHERE patient_identifier=? OR (? IS NOT NULL AND medical_record_number=?)",(identifier,mrn,mrn)).fetchall()
            preview={"schema_version":version,"matches":[dict(x) for x in matches],"action":"update" if len(matches)==1 else "create" if not matches else "reconcile","importable_sections":[x for x in ("patient","medical_history") if x in doc],"ignored_sections":[x for x in doc if x not in {"schema_version","patient","medical_history","exported_at","provenance"}]}
            if not item.confirm: return {"confirmed":False,"preview":preview}
            if len(matches)>1: raise HTTPException(409,"Import requires manual patient reconciliation")
            payload_hash=hashlib.sha256(json.dumps(doc,sort_keys=True,default=str).encode()).hexdigest()
            try:
                if matches:
                    patient_id=matches[0]["id"]
                    allowed={k:patient.get(k) for k in ("medical_record_number","external_id","first_name","middle_name","last_name","date_of_birth","sex","gender_identity","blood_group","phone","address","status") if k in patient}
                    if allowed:
                        sets=",".join(f"{k}=?" for k in allowed); db.execute(f"UPDATE patients SET {sets},updated_by=?,updated_at=SYSUTCDATETIME() WHERE id=?",tuple(allowed.values())+(user["username"],patient_id))
                else:
                    first=patient.get("first_name") or "Imported"; last=patient.get("last_name") or "Patient"; display=" ".join([first,last])
                    patient_id=db.execute("INSERT INTO patients (patient_identifier,medical_record_number,external_id,display_label,first_name,last_name,date_of_birth,sex,status,demographics_json,created_by,updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(identifier,mrn,patient.get("external_id"),display,first,last,patient.get("date_of_birth"),patient.get("sex"),patient.get("status","Active"),json.dumps(patient.get("additional_details",{})),user["username"],user["username"])).lastrowid
                history=doc.get("medical_history",{}).get("history") if isinstance(doc.get("medical_history"),dict) else None
                if isinstance(history,dict):
                    encoded=json.dumps(history); current=db.execute("SELECT revision_number FROM patient_medical_history WHERE patient_id=?",(patient_id,)).fetchone(); revision=(current["revision_number"]+1) if current else 1
                    db.execute("INSERT INTO patient_medical_history_revisions (patient_id,revision_number,schema_version,history_json,changed_by,change_reason) VALUES (?,?,?,?,?,?)",(patient_id,revision,version,encoded,user["username"],"Validated JSON import"))
                    if current: db.execute("UPDATE patient_medical_history SET history_json=?,revision_number=?,updated_by=?,updated_at=SYSUTCDATETIME() WHERE patient_id=?",(encoded,revision,user["username"],patient_id))
                    else: db.execute("INSERT INTO patient_medical_history (patient_id,schema_version,history_json,revision_number,created_by,updated_by) VALUES (?,?,?,?,?,?)",(patient_id,version,encoded,revision,user["username"],user["username"]))
                db.execute("INSERT INTO patient_imports (patient_id,schema_version,source_identifier,payload_hash,status,summary_json,imported_by) VALUES (?,?,?,?,?,?,?)",(patient_id,version,identifier,payload_hash,"Completed",json.dumps(preview),user["username"]))
                audit(db,user["username"],"patient.import","patient",patient_id,json.dumps({"schema_version":version,"payload_hash":payload_hash})); db.commit()
            except Exception:
                db.rollback(); raise
        return {"confirmed":True,"patient_id":patient_id,"preview":preview}

    return router
