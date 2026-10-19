"""Enterprise platform extensions for the SQL Server pathology backend.

The module provides vendor-neutral infrastructure. Scanner, LIS, and AI adapters
must be validated with real partner artifacts before clinical deployment.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import logging
import os
import shutil
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from image_validation import ImageValidationError, validate_image_file
from audit_context import get_audit_context
from scanner_adapters import ADAPTERS, CONVERTERS

logger = logging.getLogger('pathology.ingestion')


DDL = [
"""IF COL_LENGTH('cases','priority') IS NULL ALTER TABLE cases ADD priority VARCHAR(30) NOT NULL CONSTRAINT DF_cases_priority DEFAULT 'Routine'""",
"""IF COL_LENGTH('cases','due_at') IS NULL ALTER TABLE cases ADD due_at DATETIME2 NULL""",
"""IF COL_LENGTH('cases','exception_reason') IS NULL ALTER TABLE cases ADD exception_reason VARCHAR(2000) NULL""",
"""IF OBJECT_ID('patients','U') IS NULL CREATE TABLE patients (
 id INT IDENTITY PRIMARY KEY, external_id VARCHAR(255) UNIQUE, display_label VARCHAR(255) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('accessions','U') IS NULL CREATE TABLE accessions (
 id INT IDENTITY PRIMARY KEY, case_id INT NULL REFERENCES cases(id), patient_id INT NULL REFERENCES patients(id), accession_number VARCHAR(255) UNIQUE NOT NULL,
 order_id VARCHAR(255), priority VARCHAR(30) NOT NULL DEFAULT 'Routine', due_at DATETIME2 NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('specimens','U') IS NULL CREATE TABLE specimens (
 id INT IDENTITY PRIMARY KEY, accession_id INT NOT NULL REFERENCES accessions(id), specimen_code VARCHAR(255) NOT NULL,
 specimen_type VARCHAR(255), description VARCHAR(MAX), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('blocks','U') IS NULL CREATE TABLE blocks (
 id INT IDENTITY PRIMARY KEY, specimen_id INT NOT NULL REFERENCES specimens(id), block_code VARCHAR(255) NOT NULL,
 description VARCHAR(MAX), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF COL_LENGTH('slides','block_id') IS NULL ALTER TABLE slides ADD block_id INT NULL REFERENCES blocks(id)""",
"""IF COL_LENGTH('slides','slide_identifier') IS NULL ALTER TABLE slides ADD slide_identifier VARCHAR(255) NULL""",
"""IF COL_LENGTH('slides','scanner_name') IS NULL ALTER TABLE slides ADD scanner_name VARCHAR(255) NULL""",
"""IF OBJECT_ID('case_links','U') IS NULL CREATE TABLE case_links (
 id INT IDENTITY PRIMARY KEY, case_id INT NOT NULL REFERENCES cases(id), related_case_id INT NOT NULL REFERENCES cases(id),
 link_type VARCHAR(30) NOT NULL, reason VARCHAR(2000), created_by VARCHAR(255) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), UNIQUE(case_id,related_case_id,link_type))""",
"""IF OBJECT_ID('research_folders','U') IS NULL CREATE TABLE research_folders (
 id INT IDENTITY PRIMARY KEY, name VARCHAR(255) NOT NULL, description VARCHAR(2000), created_by VARCHAR(255) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('research_folder_members','U') IS NULL CREATE TABLE research_folder_members (
 folder_id INT NOT NULL REFERENCES research_folders(id), case_id INT NOT NULL REFERENCES cases(id), added_by VARCHAR(255) NOT NULL,
 added_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), PRIMARY KEY(folder_id,case_id))""",
"""IF OBJECT_ID('research_folder_access','U') IS NULL CREATE TABLE research_folder_access (
 folder_id INT NOT NULL REFERENCES research_folders(id), role_name VARCHAR(80) NOT NULL, granted_by VARCHAR(255) NOT NULL,
 granted_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), PRIMARY KEY(folder_id,role_name))""",
"""IF OBJECT_ID('case_operations','U') IS NULL CREATE TABLE case_operations (
 id INT IDENTITY PRIMARY KEY, operation_type VARCHAR(30) NOT NULL, source_case_id INT NOT NULL REFERENCES cases(id),
 target_case_id INT NULL REFERENCES cases(id), details_json VARCHAR(MAX) NOT NULL, authorized_by VARCHAR(255) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('viewer_bookmarks','U') IS NULL CREATE TABLE viewer_bookmarks (
 id INT IDENTITY PRIMARY KEY, slide_id INT NOT NULL REFERENCES slides(id), name VARCHAR(120) NOT NULL,
 viewport_x FLOAT NOT NULL, viewport_y FLOAT NOT NULL, viewport_width FLOAT NOT NULL, viewport_height FLOAT NOT NULL,
 rotation FLOAT NOT NULL DEFAULT 0, created_by VARCHAR(255) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('viewer_preferences','U') IS NULL CREATE TABLE viewer_preferences (
 id INT IDENTITY PRIMARY KEY, slide_id INT NOT NULL REFERENCES slides(id), username VARCHAR(255) NOT NULL,
 settings_json VARCHAR(MAX) NOT NULL, updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
 CONSTRAINT UQ_viewer_preferences UNIQUE(slide_id,username), CONSTRAINT CK_viewer_preferences_json CHECK (ISJSON(settings_json)=1))""",
"""IF COL_LENGTH('reports','workflow_status') IS NULL ALTER TABLE reports ADD workflow_status VARCHAR(30) NOT NULL CONSTRAINT DF_reports_workflow DEFAULT 'Draft', approved_by VARCHAR(255) NULL, approved_at DATETIME2 NULL, review_comment VARCHAR(4000) NULL, template_id INT NULL""",
"""IF OBJECT_ID('report_templates','U') IS NULL CREATE TABLE report_templates (
 id INT IDENTITY PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL, sections_json VARCHAR(MAX) NOT NULL, active BIT NOT NULL DEFAULT 1,
 created_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('report_reviews','U') IS NULL CREATE TABLE report_reviews (
 id INT IDENTITY PRIMARY KEY, report_id INT NOT NULL REFERENCES reports(id), previous_status VARCHAR(30), new_status VARCHAR(30) NOT NULL,
 comment VARCHAR(4000), actor VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('case_consultations','U') IS NULL CREATE TABLE case_consultations (
 id INT IDENTITY PRIMARY KEY, case_id INT NOT NULL REFERENCES cases(id), requested_by VARCHAR(255) NOT NULL,
 consultant_username VARCHAR(255) NOT NULL, message VARCHAR(4000), status VARCHAR(30) NOT NULL DEFAULT 'Open',
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), completed_at DATETIME2 NULL)""",
"""IF OBJECT_ID('ingestion_jobs','U') IS NULL CREATE TABLE ingestion_jobs (
 id INT IDENTITY PRIMARY KEY, correlation_id VARCHAR(64) UNIQUE NOT NULL, source_path VARCHAR(2048), source_device VARCHAR(255),
 adapter_name VARCHAR(120) NOT NULL DEFAULT 'manual', original_filename VARCHAR(512) NOT NULL, checksum_sha256 VARCHAR(64),
 file_size BIGINT, status VARCHAR(30) NOT NULL, attempts INT NOT NULL DEFAULT 0, progress INT NOT NULL DEFAULT 0,
 error_detail VARCHAR(MAX), quarantine_path VARCHAR(2048), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
 validation_json VARCHAR(MAX), match_identifier VARCHAR(255), matched_case_id INT NULL REFERENCES cases(id), match_method VARCHAR(80), stored_filename VARCHAR(512), imported_by VARCHAR(255), started_at DATETIME2 NULL, completed_at DATETIME2 NULL)""",
"""IF COL_LENGTH('ingestion_jobs','validation_json') IS NULL ALTER TABLE ingestion_jobs ADD validation_json VARCHAR(MAX) NULL""",
"""IF COL_LENGTH('accessions','case_id') IS NULL ALTER TABLE accessions ADD case_id INT NULL REFERENCES cases(id)""",
"""IF COL_LENGTH('ingestion_jobs','match_identifier') IS NULL ALTER TABLE ingestion_jobs ADD match_identifier VARCHAR(255) NULL, matched_case_id INT NULL REFERENCES cases(id), match_method VARCHAR(80) NULL, stored_filename VARCHAR(512) NULL, imported_by VARCHAR(255) NULL""",
"""IF OBJECT_ID('image_versions','U') IS NULL CREATE TABLE image_versions (
 id INT IDENTITY PRIMARY KEY, slide_id INT NOT NULL REFERENCES slides(id), version INT NOT NULL, checksum_sha256 VARCHAR(64) NOT NULL,
 source_path VARCHAR(2048), source_device VARCHAR(255), scanner_make VARCHAR(120), scanner_model VARCHAR(120),
 magnification FLOAT NULL, pixel_size_microns FLOAT NULL, acquisition_time DATETIME2 NULL, metadata_json VARCHAR(MAX),
 stored_filename VARCHAR(512) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), UNIQUE(slide_id,version))""",
"""IF OBJECT_ID('image_metadata_revisions','U') IS NULL CREATE TABLE image_metadata_revisions (
 id INT IDENTITY PRIMARY KEY, image_version_id INT NOT NULL REFERENCES image_versions(id), field_name VARCHAR(120) NOT NULL,
 original_value VARCHAR(MAX), corrected_value VARCHAR(MAX), editor VARCHAR(255) NOT NULL, reason VARCHAR(2000) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('reconciliation_items','U') IS NULL CREATE TABLE reconciliation_items (
 id INT IDENTITY PRIMARY KEY, ingestion_job_id INT REFERENCES ingestion_jobs(id), proposed_case_id VARCHAR(255), reason VARCHAR(MAX) NOT NULL,
 status VARCHAR(30) NOT NULL DEFAULT 'Open', resolved_case_id INT NULL REFERENCES cases(id), resolved_by VARCHAR(255),
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), resolved_at DATETIME2 NULL)""",
"""IF OBJECT_ID('scanner_compatibility','U') IS NULL CREATE TABLE scanner_compatibility (
 id INT IDENTITY PRIMARY KEY, manufacturer VARCHAR(120) NOT NULL, model VARCHAR(120) NOT NULL, software_version VARCHAR(120),
 format VARCHAR(80) NOT NULL, import_method VARCHAR(120) NOT NULL, metadata_coverage VARCHAR(MAX), limitations VARCHAR(MAX),
 validation_status VARCHAR(30) NOT NULL DEFAULT 'Unverified', tested_at DATETIME2 NULL)""",
"""IF OBJECT_ID('ai_models','U') IS NULL CREATE TABLE ai_models (
 id INT IDENTITY PRIMARY KEY, name VARCHAR(255) NOT NULL, version VARCHAR(120) NOT NULL, checksum VARCHAR(128) NOT NULL,
 task VARCHAR(255) NOT NULL, intended_use VARCHAR(MAX), compatible_inputs VARCHAR(MAX), limitations VARCHAR(MAX),
 approval_status VARCHAR(30) NOT NULL DEFAULT 'Research', adapter_name VARCHAR(120) NOT NULL,
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), UNIQUE(name,version))""",
"""IF OBJECT_ID('ai_jobs','U') IS NULL CREATE TABLE ai_jobs (
 id INT IDENTITY PRIMARY KEY, correlation_id VARCHAR(64) UNIQUE NOT NULL, model_id INT NOT NULL REFERENCES ai_models(id),
 slide_id INT NOT NULL REFERENCES slides(id), image_version_id INT NULL REFERENCES image_versions(id), status VARCHAR(30) NOT NULL,
 progress INT NOT NULL DEFAULT 0, parameters_json VARCHAR(MAX), environment_json VARCHAR(MAX), error_detail VARCHAR(MAX),
 requested_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), started_at DATETIME2 NULL, completed_at DATETIME2 NULL)""",
"""IF OBJECT_ID('ai_results','U') IS NULL CREATE TABLE ai_results (
 id INT IDENTITY PRIMARY KEY, ai_job_id INT NOT NULL REFERENCES ai_jobs(id), result_type VARCHAR(80) NOT NULL,
 geometry_json VARCHAR(MAX), measurements_json VARCHAR(MAX), confidence FLOAT NULL, raw_output_json VARCHAR(MAX),
 review_status VARCHAR(30) NOT NULL DEFAULT 'Unreviewed', reviewed_by VARCHAR(255), review_note VARCHAR(MAX),
 created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), reviewed_at DATETIME2 NULL)""",
"""IF COL_LENGTH('annotations','image_version_id') IS NULL ALTER TABLE annotations ADD image_version_id INT NULL REFERENCES image_versions(id)""",
"""IF COL_LENGTH('annotations','category') IS NULL ALTER TABLE annotations ADD category VARCHAR(120) NULL, annotation_status VARCHAR(30) NULL, author VARCHAR(255) NULL, source_type VARCHAR(30) NOT NULL CONSTRAINT DF_annotations_source DEFAULT 'Human', source_ai_result_id INT NULL REFERENCES ai_results(id), measurements_json VARCHAR(MAX) NULL""",
"""IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('annotations') AND definition LIKE '%annotation_type%') BEGIN DECLARE @anncheck sysname; SELECT TOP 1 @anncheck=name FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('annotations') AND definition LIKE '%annotation_type%'; EXEC('ALTER TABLE annotations DROP CONSTRAINT ['+@anncheck+']'); END; IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_annotations_supported_type') ALTER TABLE annotations ADD CONSTRAINT CK_annotations_supported_type CHECK (annotation_type IN ('point','line','polygon','rectangle','ellipse','freehand','arrow'))""",
"""IF OBJECT_ID('annotation_revisions','U') IS NULL CREATE TABLE annotation_revisions (
 id INT IDENTITY PRIMARY KEY, annotation_id INT NOT NULL REFERENCES annotations(id), revision_number INT NOT NULL,
 geometry_json VARCHAR(MAX) NOT NULL, label VARCHAR(120) NOT NULL, note VARCHAR(2000), category VARCHAR(120), annotation_status VARCHAR(30),
 changed_by VARCHAR(255) NOT NULL, change_reason VARCHAR(2000), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
 UNIQUE(annotation_id,revision_number))""",
"""IF OBJECT_ID('hl7_channels','U') IS NULL CREATE TABLE hl7_channels (
 id INT IDENTITY PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL, direction VARCHAR(20) NOT NULL, version VARCHAR(20) NOT NULL DEFAULT '2.5.1',
 transport VARCHAR(30) NOT NULL DEFAULT 'MLLP', host VARCHAR(255), port INT, enabled BIT NOT NULL DEFAULT 0,
 mapping_json VARCHAR(MAX), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('integration_events','U') IS NULL CREATE TABLE integration_events (
 id INT IDENTITY PRIMARY KEY, channel_id INT NULL REFERENCES hl7_channels(id), correlation_id VARCHAR(64) UNIQUE NOT NULL,
 message_control_id VARCHAR(255), message_type VARCHAR(40), direction VARCHAR(20) NOT NULL, raw_payload VARCHAR(MAX) NOT NULL,
 normalized_json VARCHAR(MAX), status VARCHAR(30) NOT NULL, acknowledgement VARCHAR(MAX), attempts INT NOT NULL DEFAULT 0,
 error_detail VARCHAR(MAX), created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), processed_at DATETIME2 NULL)""",
"""IF OBJECT_ID('system_settings','U') IS NULL CREATE TABLE system_settings (
 setting_key VARCHAR(255) PRIMARY KEY, setting_value VARCHAR(MAX), updated_by VARCHAR(255), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
"""IF OBJECT_ID('backup_runs','U') IS NULL CREATE TABLE backup_runs (
 id INT IDENTITY PRIMARY KEY, correlation_id VARCHAR(64) UNIQUE NOT NULL, status VARCHAR(30) NOT NULL, destination VARCHAR(2048),
 manifest_json VARCHAR(MAX), error_detail VARCHAR(MAX), started_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), completed_at DATETIME2 NULL)""",
]


class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255); version: str = Field(min_length=1, max_length=120)
    checksum: str = Field(min_length=8, max_length=128); task: str = Field(min_length=1, max_length=255)
    intended_use: str = ""; compatible_inputs: str = ""; limitations: str = ""
    approval_status: str = "Research"; adapter_name: str = "external"

class AiJobCreate(BaseModel):
    model_id: int; slide_id: int; image_version_id: int | None = None; parameters: dict = {}

class ChannelCreate(BaseModel):
    name: str; direction: str; version: str = "2.5.1"; transport: str = "MLLP"
    host: str | None = None; port: int | None = None; enabled: bool = False; mapping: dict = {}

class Hl7MessageCreate(BaseModel):
    channel_id: int | None = None; direction: str = "inbound"; raw_payload: str

class PatientCreate(BaseModel):
    external_id: str | None = Field(default=None, max_length=255)
    display_label: str = Field(min_length=1, max_length=255)

class AccessionCreate(BaseModel):
    case_id: int | None = None
    patient_id: int | None = None
    accession_number: str = Field(min_length=1, max_length=255)
    order_id: str | None = Field(default=None, max_length=255)
    priority: str = "Routine"
    due_at: datetime | None = None

class SpecimenCreate(BaseModel):
    accession_id: int
    specimen_code: str = Field(min_length=1, max_length=255)
    specimen_type: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=4000)

class BlockCreate(BaseModel):
    specimen_id: int
    block_code: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4000)

class SlideHierarchyUpdate(BaseModel):
    block_id: int
    slide_identifier: str = Field(min_length=1, max_length=255)
    scanner_name: str | None = Field(default=None, max_length=255)

class CaseConfiguration(BaseModel):
    status: str | None = None
    reviewer: str | None = Field(default=None, max_length=120)
    priority: str | None = None
    due_at: datetime | None = None
    exception_reason: str | None = Field(default=None, max_length=2000)

class CaseLinkCreate(BaseModel):
    related_case_id: int
    link_type: str = Field(default='Prior', pattern=r'^(Prior|Related)$')
    reason: str = Field(default='', max_length=2000)

class ResearchFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default='', max_length=2000)
    allowed_roles: list[str] = Field(default_factory=list, max_length=20)

class FolderMemberCreate(BaseModel):
    case_id: int

class CaseMerge(BaseModel):
    source_case_id: int
    target_case_id: int
    reason: str = Field(min_length=1, max_length=2000)

class CaseSplit(BaseModel):
    source_case_id: int
    new_case_id: str = Field(min_length=1, max_length=80)
    slide_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)

class ViewerBookmarkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    viewport_x: float
    viewport_y: float
    viewport_width: float = Field(gt=0)
    viewport_height: float = Field(gt=0)
    rotation: float = 0

class ViewerPreferencesUpdate(BaseModel):
    display_profile: str = Field(pattern=r'^(standard|grayscale|inverted|high-contrast|low-saturation|high-saturation|red-channel|green-channel|blue-channel|hue-positive|hue-negative)$')
    brightness: int = Field(ge=50, le=150, multiple_of=5)
    contrast: int = Field(ge=50, le=150, multiple_of=5)

class AiCorrectionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default='', max_length=2000)
    category: str = Field(default='AI correction', max_length=120)

class ReportWorkflowUpdate(BaseModel):
    status: str = Field(pattern=r'^(Draft|Pending Review|Approved|Rejected|Amended)$')
    comment: str = Field(default='', max_length=4000)

class ReportTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sections: list[str] = Field(min_length=1, max_length=30)

class ConsultationCreate(BaseModel):
    consultant_username: str = Field(min_length=1, max_length=255)
    message: str = Field(default='', max_length=4000)

class IngestionCreate(BaseModel):
    source_path: str
    match_identifier: str | None = Field(default=None, max_length=255)
    source_device: str = "local-folder"
    adapter_name: str = "auto"

class CompatibilityCreate(BaseModel):
    manufacturer: str; model: str; software_version: str = ""; format: str
    import_method: str = "manual"; metadata_coverage: str = ""; limitations: str = ""
    validation_status: str = "Unverified"

class JobStatusUpdate(BaseModel):
    status: str; progress: int = Field(default=0, ge=0, le=100); error_detail: str = ""

class AiResultCreate(BaseModel):
    result_type: str; geometry: dict | list | None = None; measurements: dict = {}
    confidence: float | None = None; raw_output: dict = {}

class AiReview(BaseModel):
    review_status: str; review_note: str = ""

class SettingUpdate(BaseModel):
    value: str = Field(max_length=4000)

class BackupCreate(BaseModel):
    name: str = Field(default="pathology", pattern=r"^[A-Za-z0-9_-]{1,80}$")

class ReconciliationResolve(BaseModel):
    resolved_case_id: int

class MetadataCorrection(BaseModel):
    field_name: str = Field(pattern=r"^(slide_label|barcode|scanner_vendor|scanner_model|magnification|acquisition_time)$")
    corrected_value: str = Field(max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class SlideCalibrationUpdate(BaseModel):
    pixel_size_microns_x: float = Field(gt=0, le=1000)
    pixel_size_microns_y: float = Field(gt=0, le=1000)
    reason: str = Field(min_length=3, max_length=2000)

class IngestionBatchAction(BaseModel):
    job_ids: list[int] = Field(min_length=1, max_length=500)
    action: str = Field(pattern=r"^(pause|resume|cancel|retry)$")

TERMINAL_INGESTION = {'Completed', 'Failed', 'Quarantined', 'Duplicate', 'Cancelled'}
_watch_state: dict[str, tuple[int, int, float]] = {}


def _rows(connect, sql, params=()):
    with connect() as db: return [dict(row) for row in db.execute(sql, params).fetchall()]

def _parse_hl7(payload: str):
    segments = [line.split('|') for line in payload.replace('\n', '\r').split('\r') if line]
    if not segments or segments[0][0] != 'MSH': raise HTTPException(400, 'HL7 message must start with MSH')
    msh = segments[0]
    return {'message_type': msh[8] if len(msh) > 8 else '', 'message_control_id': msh[9] if len(msh) > 9 else '',
            'version': msh[11] if len(msh) > 11 else '', 'segments': {segment[0]: segment[1:] for segment in segments}}


def discover_watched_folder(connect) -> int:
    """Poll the configured folder and enqueue files only after two stable observations."""
    watched=os.getenv('WATCHED_FOLDER','').strip()
    if not watched: return 0
    root=Path(watched).resolve()
    if not root.is_dir(): return 0
    stable_seconds=max(1,int(os.getenv('INGESTION_STABLE_SECONDS','10'))); now=time.monotonic(); created=0
    for source in root.rglob('*'):
        if not source.is_file(): continue
        key=str(source.resolve()); stat=source.stat(); signature=(stat.st_size,stat.st_mtime_ns)
        previous=_watch_state.get(key)
        if not previous or previous[:2] != signature:
            _watch_state[key]=(signature[0],signature[1],now); continue
        if now-previous[2] < stable_seconds: continue
        with connect() as db:
            if db.execute('SELECT TOP 1 id FROM ingestion_jobs WHERE source_path=? AND file_size=?',(key,stat.st_size)).fetchone(): continue
            digest=hashlib.sha256()
            with source.open('rb') as handle:
                for chunk in iter(lambda:handle.read(8*1024*1024),b''): digest.update(chunk)
            try: adapter=ADAPTERS.for_path(source); adapter_name=adapter.name; error=None
            except ValueError as exc: adapter_name='unsupported'; error=str(exc)
            identifier=source.stem
            matches=[row['id'] for row in db.execute("SELECT id FROM cases WHERE LOWER(case_id)=LOWER(?) UNION SELECT case_id AS id FROM accessions WHERE case_id IS NOT NULL AND (LOWER(accession_number)=LOWER(?) OR LOWER(order_id)=LOWER(?))",(identifier,identifier,identifier)).fetchall()]
            matched=matches[0] if len(matches)==1 else None; status='Queued' if matched and not error else 'Quarantined'; cid=uuid4().hex
            cur=db.execute('INSERT INTO ingestion_jobs (correlation_id,source_path,source_device,adapter_name,original_filename,checksum_sha256,file_size,status,error_detail,match_identifier,matched_case_id,match_method,imported_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(cid,key,'watched-folder',adapter_name,source.name,digest.hexdigest(),stat.st_size,status,error,identifier,matched,'filename-identifier' if matched else None,'system-watcher'))
            if not matched and not error: db.execute('INSERT INTO reconciliation_items (ingestion_job_id,proposed_case_id,reason) VALUES (?,?,?)',(cur.lastrowid,identifier,'Watched-file name did not uniquely match a case/accession/order'))
            db.commit(); created+=1
    return created


async def watched_folder_loop(connect, stop_event):
    import asyncio
    interval=max(2,int(os.getenv('WATCHED_FOLDER_POLL_SECONDS','5')))
    while not stop_event.is_set():
        try:
            created=await asyncio.to_thread(discover_watched_folder,connect)
            if created: logger.info('watched_folder.discovered count=%s',created)
        except Exception:
            logger.exception('watched_folder.poll_failed')
        try: await asyncio.wait_for(stop_event.wait(),timeout=interval)
        except TimeoutError: pass

def create_platform_router(connect, require_roles, get_current_user, uploads_dir: Path, openslide_module=None):
    router = APIRouter(prefix='/api/platform', tags=['platform'])
    administrators = ['System Administrator']
    intake_roles = ['System Administrator', 'Laboratory Manager']
    ai_roles = ['System Administrator', 'Pathologist']
    integration_roles = ['System Administrator']
    operational_roles = ['System Administrator', 'Laboratory Manager']
    clinical_read_roles = ['System Administrator', 'Laboratory Manager', 'Pathologist']
    audit_roles = ['System Administrator']
    quarantine_dir = Path(os.getenv('QUARANTINE_DIR', uploads_dir.parent / 'quarantine'))
    if not quarantine_dir.is_absolute(): quarantine_dir = uploads_dir.parent / quarantine_dir
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    @router.get('/patients')
    def patients(_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect, 'SELECT TOP 500 * FROM patients ORDER BY id DESC')

    @router.post('/patients', status_code=201)
    def create_patient(item: PatientCreate, _=Depends(require_roles(intake_roles))):
        with connect() as db:
            cur=db.execute('INSERT INTO patients (external_id,display_label) VALUES (?,?)',(item.external_id,item.display_label)); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.get('/accessions')
    def accessions(_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect, 'SELECT TOP 500 * FROM accessions ORDER BY id DESC')

    @router.post('/accessions', status_code=201)
    def create_accession(item: AccessionCreate, _=Depends(require_roles(intake_roles))):
        if item.priority not in {'Routine','Urgent','Stat'}: raise HTTPException(400,'Invalid priority')
        with connect() as db:
            if item.case_id and not db.execute('SELECT id FROM cases WHERE id=?',(item.case_id,)).fetchone(): raise HTTPException(400,'Case does not exist')
            cur=db.execute('INSERT INTO accessions (case_id,patient_id,accession_number,order_id,priority,due_at) VALUES (?,?,?,?,?,?)',(item.case_id,item.patient_id,item.accession_number,item.order_id,item.priority,item.due_at)); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.get('/specimens')
    def specimens(_=Depends(require_roles(clinical_read_roles))): return _rows(connect,'SELECT TOP 500 * FROM specimens ORDER BY id DESC')

    @router.post('/specimens', status_code=201)
    def create_specimen(item: SpecimenCreate, _=Depends(require_roles(intake_roles))):
        with connect() as db:
            cur=db.execute('INSERT INTO specimens (accession_id,specimen_code,specimen_type,description) VALUES (?,?,?,?)',(item.accession_id,item.specimen_code,item.specimen_type,item.description)); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.get('/blocks')
    def blocks(_=Depends(require_roles(clinical_read_roles))): return _rows(connect,'SELECT TOP 500 * FROM blocks ORDER BY id DESC')

    @router.post('/blocks', status_code=201)
    def create_block(item: BlockCreate, _=Depends(require_roles(intake_roles))):
        with connect() as db:
            if not db.execute('SELECT id FROM specimens WHERE id=?',(item.specimen_id,)).fetchone(): raise HTTPException(400,'Specimen does not exist')
            cur=db.execute('INSERT INTO blocks (specimen_id,block_code,description) VALUES (?,?,?)',(item.specimen_id,item.block_code,item.description)); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.patch('/slides/{slide_id}/hierarchy')
    def assign_slide_hierarchy(slide_id:int,item:SlideHierarchyUpdate,user=Depends(require_roles(intake_roles))):
        with connect() as db:
            if not db.execute('SELECT id FROM blocks WHERE id=?',(item.block_id,)).fetchone(): raise HTTPException(404,'Block not found')
            if not db.execute('SELECT id FROM slides WHERE id=?',(slide_id,)).fetchone(): raise HTTPException(404,'Slide not found')
            duplicate=db.execute('SELECT id FROM slides WHERE slide_identifier=? AND id<>?',(item.slide_identifier,slide_id)).fetchone()
            if duplicate: raise HTTPException(409,'Slide identifier is already assigned')
            db.execute('UPDATE slides SET block_id=?,slide_identifier=?,scanner_name=? WHERE id=?',(item.block_id,item.slide_identifier,item.scanner_name,slide_id)); db.commit()
        return {'id':slide_id,**item.model_dump(),'updated_by':user['username']}

    @router.get('/slides/{slide_id}/bookmarks')
    def viewer_bookmarks(slide_id:int,_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect,'SELECT * FROM viewer_bookmarks WHERE slide_id=? ORDER BY id DESC',(slide_id,))

    @router.post('/slides/{slide_id}/bookmarks',status_code=201)
    def create_viewer_bookmark(slide_id:int,item:ViewerBookmarkCreate,user=Depends(require_roles(['System Administrator','Pathologist']))):
        with connect() as db:
            if not db.execute('SELECT id FROM slides WHERE id=?',(slide_id,)).fetchone(): raise HTTPException(404,'Slide not found')
            cur=db.execute('INSERT INTO viewer_bookmarks (slide_id,name,viewport_x,viewport_y,viewport_width,viewport_height,rotation,created_by) VALUES (?,?,?,?,?,?,?,?)',
              (slide_id,item.name,item.viewport_x,item.viewport_y,item.viewport_width,item.viewport_height,item.rotation,user['username'])); db.commit()
        return {'id':cur.lastrowid,'slide_id':slide_id,**item.model_dump(),'created_by':user['username']}

    @router.delete('/slides/{slide_id}/bookmarks/{bookmark_id}',status_code=204)
    def delete_viewer_bookmark(slide_id:int,bookmark_id:int,user=Depends(require_roles(['System Administrator','Pathologist']))):
        with connect() as db:
            row=db.execute('SELECT created_by FROM viewer_bookmarks WHERE id=? AND slide_id=?',(bookmark_id,slide_id)).fetchone()
            if not row: raise HTTPException(404,'Bookmark not found')
            if user['role'] not in {'System Administrator','Super Administrator'} and row['created_by'] != user['username']: raise HTTPException(403,'Only the bookmark owner can delete it')
            db.execute('DELETE FROM viewer_bookmarks WHERE id=?',(bookmark_id,)); db.commit()

    @router.get('/slides/{slide_id}/viewer-preferences')
    def viewer_preferences(slide_id:int,user=Depends(require_roles(clinical_read_roles))):
        with connect() as db:
            if not db.execute('SELECT id FROM slides WHERE id=?',(slide_id,)).fetchone(): raise HTTPException(404,'Slide not found')
            row=db.execute('SELECT settings_json,updated_at FROM viewer_preferences WHERE slide_id=? AND username=?',(slide_id,user['username'])).fetchone()
        if not row: return {'display_profile':'standard','brightness':100,'contrast':100,'saved':False}
        return {**json.loads(row['settings_json']),'saved':True,'updated_at':row['updated_at']}

    @router.put('/slides/{slide_id}/viewer-preferences')
    def save_viewer_preferences(slide_id:int,item:ViewerPreferencesUpdate,user=Depends(require_roles(clinical_read_roles))):
        settings=item.model_dump(); payload=json.dumps(settings,separators=(',',':'))
        with connect() as db:
            if not db.execute('SELECT id FROM slides WHERE id=?',(slide_id,)).fetchone(): raise HTTPException(404,'Slide not found')
            db.execute("""MERGE viewer_preferences AS target USING (SELECT ? AS slide_id, ? AS username) AS source
              ON target.slide_id=source.slide_id AND target.username=source.username
              WHEN MATCHED THEN UPDATE SET settings_json=?,updated_at=SYSUTCDATETIME()
              WHEN NOT MATCHED THEN INSERT (slide_id,username,settings_json) VALUES (?,?,?);""",
              (slide_id,user['username'],payload,slide_id,user['username'],payload)); db.commit()
        return {**settings,'saved':True}

    @router.get('/cases/search')
    def search_cases(identifier:str|None=None,status:str|None=None,date_from:datetime|None=None,date_to:datetime|None=None,
                     specimen_type:str|None=None,stain:str|None=None,scanner:str|None=None,assigned_user:str|None=None,
                     ai_status:str|None=None,_=Depends(require_roles(clinical_read_roles))):
        clauses=[]; params=[]
        if identifier:
            token=f'%{identifier.strip()}%'; clauses.append("(c.case_id LIKE ? OR EXISTS (SELECT 1 FROM accessions a WHERE a.case_id=c.id AND (a.accession_number LIKE ? OR a.order_id LIKE ?)) OR EXISTS (SELECT 1 FROM slides s WHERE s.case_id=c.id AND s.slide_identifier LIKE ?))"); params.extend([token]*4)
        if status: clauses.append('c.status=?'); params.append(status)
        if date_from: clauses.append('c.created_at>=?'); params.append(date_from)
        if date_to: clauses.append('c.created_at<=?'); params.append(date_to)
        if specimen_type: clauses.append('EXISTS (SELECT 1 FROM accessions a JOIN specimens sp ON sp.accession_id=a.id WHERE a.case_id=c.id AND sp.specimen_type=?)'); params.append(specimen_type)
        if stain: clauses.append('EXISTS (SELECT 1 FROM slides s WHERE s.case_id=c.id AND s.stain=?)'); params.append(stain)
        if scanner: clauses.append('EXISTS (SELECT 1 FROM slides s WHERE s.case_id=c.id AND s.scanner_name=?)'); params.append(scanner)
        if assigned_user: clauses.append('LOWER(CAST(c.reviewer AS VARCHAR(MAX)))=LOWER(?)'); params.append(assigned_user)
        if ai_status: clauses.append('EXISTS (SELECT 1 FROM slides s JOIN ai_jobs aj ON aj.slide_id=s.id WHERE s.case_id=c.id AND aj.status=?)'); params.append(ai_status)
        where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
        sql="""SELECT TOP 500 c.id,c.case_id,c.description,c.status,c.reviewer,c.priority,c.due_at,c.exception_reason,c.created_at
          FROM cases c"""+where+' ORDER BY c.created_at DESC'
        return _rows(connect,sql,tuple(params))

    @router.patch('/cases/{case_id}/configuration')
    def configure_case(case_id:int,item:CaseConfiguration,user=Depends(require_roles(operational_roles + ['Pathologist']))):
        statuses={'New','In Review','Reviewed','On Hold'}; priorities={'Routine','Urgent','Stat'}
        if item.status is not None and item.status not in statuses: raise HTTPException(400,'Invalid case status')
        if item.priority is not None and item.priority not in priorities: raise HTTPException(400,'Invalid case priority')
        supplied=item.model_fields_set
        assignments=[]; params=[]
        for field in ('status','reviewer','priority','due_at','exception_reason'):
            if field in supplied: assignments.append(f'{field}=?'); params.append(getattr(item,field))
        if not assignments: raise HTTPException(400,'No case fields were supplied')
        with connect() as db:
            if not db.execute('SELECT id FROM cases WHERE id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
            db.execute(f"UPDATE cases SET {','.join(assignments)} WHERE id=?",(*params,case_id))
            db.execute('INSERT INTO case_operations (operation_type,source_case_id,details_json,authorized_by) VALUES (?,?,?,?)',('configuration',case_id,json.dumps(item.model_dump(mode='json',exclude_unset=True)),user['username'])); db.commit()
        return {'id':case_id,**item.model_dump(exclude_unset=True)}

    @router.get('/cases/{case_id}/timeline')
    def case_timeline(case_id:int,_=Depends(require_roles(clinical_read_roles))):
        with connect() as db:
            if not db.execute('SELECT id FROM cases WHERE id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
            rows=db.execute("""SELECT event_type,event_at,actor,summary FROM (
              SELECT 'case-created' event_type,created_at event_at,'' actor,'Case created' summary FROM cases WHERE id=?
              UNION ALL SELECT 'slide-ingested',created_at,'',CONCAT('Slide ',label) FROM slides WHERE case_id=?
              UNION ALL SELECT 'qc',h.created_at,h.actor,CONCAT('QC: ',COALESCE(h.qc_status,'')) FROM slide_qc_history h JOIN slides s ON s.id=h.slide_id WHERE s.case_id=?
              UNION ALL SELECT 'annotation',a.created_at,'',CONCAT('Annotation: ',a.label) FROM annotations a JOIN slides s ON s.id=a.slide_id WHERE s.case_id=?
              UNION ALL SELECT 'review',created_at,actor,CONCAT(change_type,': ',COALESCE(new_value,'')) FROM case_review_history WHERE case_id=?
              UNION ALL SELECT 'report',created_at,reviewer_name,CONCAT('Report v',version,' ',status) FROM reports WHERE case_id=?
              UNION ALL SELECT 'operation',created_at,authorized_by,operation_type FROM case_operations WHERE source_case_id=? OR target_case_id=?
            ) events ORDER BY event_at DESC""",(case_id,case_id,case_id,case_id,case_id,case_id,case_id,case_id)).fetchall()
        return [dict(row) for row in rows]

    @router.post('/cases/{case_id}/links', status_code=201)
    def link_case(case_id:int,item:CaseLinkCreate,user=Depends(require_roles(['System Administrator','Pathologist']))):
        if case_id==item.related_case_id: raise HTTPException(400,'A case cannot link to itself')
        with connect() as db:
            found=db.execute('SELECT COUNT(*) AS n FROM cases WHERE id IN (?,?)',(case_id,item.related_case_id)).fetchone()['n']
            if found != 2: raise HTTPException(404,'Case not found')
            cur=db.execute('INSERT INTO case_links (case_id,related_case_id,link_type,reason,created_by) VALUES (?,?,?,?,?)',(case_id,item.related_case_id,item.link_type,item.reason,user['username'])); db.commit()
        return {'id':cur.lastrowid,'case_id':case_id,**item.model_dump()}

    @router.get('/cases/{case_id}/links')
    def case_links(case_id:int,_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect,'SELECT * FROM case_links WHERE case_id=? OR related_case_id=? ORDER BY id DESC',(case_id,case_id))

    @router.get('/slides/{slide_id}/annotations/export')
    def export_annotations(slide_id:int,format:str='geojson',_=Depends(require_roles(clinical_read_roles))):
        rows=_rows(connect,'SELECT * FROM annotations WHERE slide_id=? ORDER BY id',(slide_id,))
        if format == 'csv':
            import csv
            from io import StringIO
            output=StringIO(); writer=csv.writer(output)
            writer.writerow(['annotation_id','image_version_id','type','label','category','status','author','source','geometry','measurements'])
            for row in rows:
                geometry={'x':row.get('x'),'y':row.get('y'),'width':row.get('width'),'height':row.get('height'),'points':json.loads(row.get('points') or 'null')}
                writer.writerow([row['id'],row.get('image_version_id'),row['annotation_type'],row['label'],row.get('category'),row.get('annotation_status'),row.get('author'),row.get('source_type'),json.dumps(geometry),row.get('measurements_json')])
            return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename=slide_{slide_id}_annotations.csv'})
        if format != 'geojson': raise HTTPException(400,'Format must be geojson or csv')
        features=[]
        for row in rows:
            points=json.loads(row.get('points') or 'null')
            if row['annotation_type']=='point' and points: geometry={'type':'Point','coordinates':points[0]}
            elif row['annotation_type'] in {'line','arrow','freehand'} and points: geometry={'type':'LineString','coordinates':points}
            elif row['annotation_type']=='polygon' and points: geometry={'type':'Polygon','coordinates':[points]}
            elif row['annotation_type'] in {'rectangle','ellipse'}: geometry={'type':'Polygon','coordinates':[[[row['x'],row['y']],[row['x']+row['width'],row['y']],[row['x']+row['width'],row['y']+row['height']],[row['x'],row['y']+row['height']],[row['x'],row['y']]]]}
            else: geometry=None
            features.append({'type':'Feature','id':row['id'],'geometry':geometry,'properties':{key:row.get(key) for key in ('image_version_id','label','note','category','annotation_status','author','source_type','measurements_json')}})
        return {'type':'FeatureCollection','coordinate_system':'level-0 image pixels; origin top-left; x right; y down','slide_id':slide_id,'features':features}

    @router.post('/ai/results/{result_id}/corrections',status_code=201)
    def correct_ai_region(result_id:int,item:AiCorrectionCreate,user=Depends(require_roles(['System Administrator','Pathologist']))):
        with connect() as db:
            result=db.execute('SELECT r.*,j.slide_id,j.image_version_id FROM ai_results r JOIN ai_jobs j ON j.id=r.ai_job_id WHERE r.id=?',(result_id,)).fetchone()
            if not result: raise HTTPException(404,'AI result not found')
            geometry=json.loads(result['geometry_json'] or '{}'); points=geometry.get('points'); annotation_type=geometry.get('annotation_type') or geometry.get('type','polygon').lower()
            if annotation_type not in {'point','line','polygon','rectangle','ellipse','freehand','arrow'}: raise HTTPException(400,'AI geometry cannot be converted to an annotation')
            cur=db.execute('INSERT INTO annotations (slide_id,annotation_type,label,note,color,x,y,width,height,points,image_version_id,category,annotation_status,author,source_type,source_ai_result_id,measurements_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (result['slide_id'],annotation_type,item.label,item.note,'#d39a3c',geometry.get('x'),geometry.get('y'),geometry.get('width'),geometry.get('height'),json.dumps(points) if points else None,result['image_version_id'],item.category,'Corrected',user['username'],'AI correction',result_id,result['measurements_json']))
            db.execute("UPDATE ai_results SET review_status='Corrected',reviewed_by=?,review_note=?,reviewed_at=SYSUTCDATETIME() WHERE id=?",(user['username'],item.note,result_id)); db.commit()
        return {'id':cur.lastrowid,'slide_id':result['slide_id'],'source_ai_result_id':result_id,'source_type':'AI correction'}

    @router.get('/report-templates')
    def report_templates(_=Depends(require_roles(clinical_read_roles))): return _rows(connect,'SELECT * FROM report_templates WHERE active=1 ORDER BY name')

    @router.post('/report-templates',status_code=201)
    def create_report_template(item:ReportTemplateCreate,user=Depends(require_roles(administrators))):
        with connect() as db:
            cur=db.execute('INSERT INTO report_templates (name,sections_json,created_by) VALUES (?,?,?)',(item.name,json.dumps(item.sections),user['username'])); db.commit()
        return {'id':cur.lastrowid,**item.model_dump()}

    @router.patch('/reports/{report_id}/workflow')
    def report_workflow(report_id:int,item:ReportWorkflowUpdate,user=Depends(require_roles(['System Administrator','Pathologist']))):
        allowed={'Draft':{'Pending Review'},'Pending Review':{'Approved','Rejected'},'Rejected':{'Amended'},'Approved':{'Amended'},'Amended':{'Pending Review'}}
        with connect() as db:
            report=db.execute('SELECT workflow_status FROM reports WHERE id=?',(report_id,)).fetchone()
            if not report: raise HTTPException(404,'Report not found')
            current=report['workflow_status']
            if item.status not in allowed.get(current,set()): raise HTTPException(409,f'Cannot transition report from {current} to {item.status}')
            approver=user['username'] if item.status=='Approved' else None
            db.execute('UPDATE reports SET workflow_status=?,review_comment=?,approved_by=CASE WHEN ? IS NULL THEN approved_by ELSE ? END,approved_at=CASE WHEN ? IS NULL THEN approved_at ELSE SYSUTCDATETIME() END WHERE id=?',(item.status,item.comment,approver,approver,approver,report_id))
            db.execute('INSERT INTO report_reviews (report_id,previous_status,new_status,comment,actor) VALUES (?,?,?,?,?)',(report_id,current,item.status,item.comment,user['username'])); db.commit()
        return {'id':report_id,'previous_status':current,'workflow_status':item.status,'actor':user['username'],'comment':item.comment}

    @router.get('/reports/{report_id}/reviews')
    def report_review_history(report_id:int,_=Depends(require_roles(clinical_read_roles))): return _rows(connect,'SELECT * FROM report_reviews WHERE report_id=? ORDER BY id',(report_id,))

    @router.post('/cases/{case_id}/consultations',status_code=201)
    def create_consultation(case_id:int,item:ConsultationCreate,user=Depends(require_roles(['System Administrator','Pathologist']))):
        with connect() as db:
            consultant=db.execute('SELECT username,role,is_active FROM users WHERE username=?',(item.consultant_username,)).fetchone()
            if not consultant or not consultant['is_active'] or consultant['role'] not in {'Pathologist','System Administrator'}: raise HTTPException(400,'Consultant must be an active authorized clinical account')
            if not db.execute('SELECT id FROM cases WHERE id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
            cur=db.execute('INSERT INTO case_consultations (case_id,requested_by,consultant_username,message) VALUES (?,?,?,?)',(case_id,user['username'],item.consultant_username,item.message)); db.commit()
        return {'id':cur.lastrowid,'case_id':case_id,**item.model_dump(),'status':'Open'}

    @router.post('/research-folders',status_code=201)
    def create_research_folder(item:ResearchFolderCreate,user=Depends(require_roles(['System Administrator']))):
        permitted=set(clinical_read_roles); invalid=set(item.allowed_roles)-permitted
        if invalid: raise HTTPException(400,f"Invalid allowed roles: {', '.join(sorted(invalid))}")
        with connect() as db:
            cur=db.execute('INSERT INTO research_folders (name,description,created_by) VALUES (?,?,?)',(item.name,item.description,user['username'])); folder_id=cur.lastrowid
            for role in set(item.allowed_roles): db.execute('INSERT INTO research_folder_access (folder_id,role_name,granted_by) VALUES (?,?,?)',(folder_id,role,user['username']))
            db.commit()
        return {'id':folder_id,**item.model_dump()}

    @router.get('/research-folders')
    def research_folders(user=Depends(require_roles(clinical_read_roles))):
        if user['role'] in {'System Administrator','Super Administrator'}: return _rows(connect,'SELECT * FROM research_folders ORDER BY id DESC')
        return _rows(connect,'SELECT f.* FROM research_folders f JOIN research_folder_access a ON a.folder_id=f.id WHERE a.role_name=? ORDER BY f.id DESC',(user['role'],))

    @router.post('/research-folders/{folder_id}/members',status_code=201)
    def add_folder_member(folder_id:int,item:FolderMemberCreate,user=Depends(require_roles(['System Administrator']))):
        with connect() as db:
            if not db.execute('SELECT id FROM research_folders WHERE id=?',(folder_id,)).fetchone(): raise HTTPException(404,'Research folder not found')
            if not db.execute('SELECT id FROM cases WHERE id=?',(item.case_id,)).fetchone(): raise HTTPException(404,'Case not found')
            db.execute('INSERT INTO research_folder_members (folder_id,case_id,added_by) VALUES (?,?,?)',(folder_id,item.case_id,user['username'])); db.commit()
        return {'folder_id':folder_id,'case_id':item.case_id}

    @router.post('/cases/merge')
    def merge_cases(item:CaseMerge,user=Depends(require_roles(administrators))):
        if item.source_case_id==item.target_case_id: raise HTTPException(400,'Source and target cases must differ')
        with connect() as db:
            if db.execute("SELECT TOP 1 id FROM reports WHERE case_id=? AND status='Finalized'",(item.source_case_id,)).fetchone(): raise HTTPException(409,'A case with a finalized report cannot be merged')
            if db.execute('SELECT COUNT(*) AS n FROM cases WHERE id IN (?,?)',(item.source_case_id,item.target_case_id)).fetchone()['n'] != 2: raise HTTPException(404,'Case not found')
            db.execute('UPDATE slides SET case_id=? WHERE case_id=?',(item.target_case_id,item.source_case_id)); db.execute('UPDATE accessions SET case_id=? WHERE case_id=?',(item.target_case_id,item.source_case_id))
            db.execute("UPDATE cases SET status='Merged',exception_reason=? WHERE id=?",(item.reason,item.source_case_id))
            db.execute('INSERT INTO case_operations (operation_type,source_case_id,target_case_id,details_json,authorized_by) VALUES (?,?,?,?,?)',('merge',item.source_case_id,item.target_case_id,json.dumps({'reason':item.reason}),user['username'])); db.commit()
        return {'source_case_id':item.source_case_id,'target_case_id':item.target_case_id,'status':'Merged'}

    @router.post('/cases/split',status_code=201)
    def split_case(item:CaseSplit,user=Depends(require_roles(administrators))):
        with connect() as db:
            placeholders=','.join('?' for _ in item.slide_ids)
            slides=db.execute(f'SELECT id FROM slides WHERE case_id=? AND id IN ({placeholders})',(item.source_case_id,*item.slide_ids)).fetchall()
            if len(slides)!=len(set(item.slide_ids)): raise HTTPException(400,'Every slide must belong to the source case')
            cur=db.execute("INSERT INTO cases (case_id,description,filename,status,priority) VALUES (?,?,'','New','Routine')",(item.new_case_id,f'Split from case {item.source_case_id}'))
            target_id=cur.lastrowid; db.execute(f'UPDATE slides SET case_id=? WHERE id IN ({placeholders})',(target_id,*item.slide_ids))
            db.execute('INSERT INTO case_operations (operation_type,source_case_id,target_case_id,details_json,authorized_by) VALUES (?,?,?,?,?)',('split',item.source_case_id,target_id,json.dumps({'slide_ids':item.slide_ids,'reason':item.reason}),user['username'])); db.commit()
        return {'source_case_id':item.source_case_id,'target_case_id':target_id,'new_case_id':item.new_case_id,'slide_ids':item.slide_ids}

    @router.get('/compatibility')
    def compatibility(_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect, 'SELECT * FROM scanner_compatibility ORDER BY manufacturer, model')

    @router.post('/compatibility', status_code=201)
    def add_compatibility(item:CompatibilityCreate,_=Depends(require_roles(administrators))):
        if item.validation_status not in {'Unverified','Testing','Verified','Unsupported'}: raise HTTPException(400,'Invalid validation status')
        with connect() as db:
            cur=db.execute('INSERT INTO scanner_compatibility (manufacturer,model,software_version,format,import_method,metadata_coverage,limitations,validation_status,tested_at) VALUES (?,?,?,?,?,?,?,?,CASE WHEN ?=\'Verified\' THEN SYSUTCDATETIME() ELSE NULL END)',
              (item.manufacturer,item.model,item.software_version,item.format,item.import_method,item.metadata_coverage,item.limitations,item.validation_status,item.validation_status)); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.get('/ingestion/jobs')
    def ingestion_jobs(_=Depends(require_roles(intake_roles + ['Laboratory Manager']))):
        jobs = _rows(connect, 'SELECT TOP 200 * FROM ingestion_jobs ORDER BY id DESC')
        for job in jobs:
            raw = job.pop('validation_json', None)
            job['validation'] = json.loads(raw) if raw else None
        return jobs

    @router.get('/ingestion/adapters')
    def ingestion_adapters(_=Depends(require_roles(intake_roles + ['Laboratory Manager']))):
        return {'adapters': ADAPTERS.describe(), 'converters': sorted(CONVERTERS)}

    @router.post('/ingestion/jobs', status_code=202)
    def queue_ingestion(item: IngestionCreate, _=Depends(require_roles(intake_roles))):
        watched = os.getenv('WATCHED_FOLDER','').strip()
        if not watched: raise HTTPException(409,'WATCHED_FOLDER is not configured')
        root=Path(watched).resolve(); source=Path(item.source_path).resolve()
        if source != root and root not in source.parents: raise HTTPException(400,'Source path must be inside WATCHED_FOLDER')
        if not source.is_file(): raise HTTPException(404,'Source file not found')
        try: adapter=ADAPTERS.for_path(source) if item.adapter_name == 'auto' else ADAPTERS.get(item.adapter_name)
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc
        digest=hashlib.sha256()
        with source.open('rb') as handle:
            for chunk in iter(lambda:handle.read(8*1024*1024),b''): digest.update(chunk)
        checksum=digest.hexdigest()
        with connect() as db:
            duplicate=db.execute("SELECT TOP 1 id FROM ingestion_jobs WHERE checksum_sha256=? AND status='Completed'",(checksum,)).fetchone()
            matches=[]; identifier=(item.match_identifier or '').strip()
            if identifier:
                matches=[row['id'] for row in db.execute("SELECT id FROM cases WHERE LOWER(case_id)=LOWER(?) UNION SELECT case_id AS id FROM accessions WHERE case_id IS NOT NULL AND (LOWER(accession_number)=LOWER(?) OR LOWER(order_id)=LOWER(?))",(identifier,identifier,identifier)).fetchall()]
            matched_case_id=matches[0] if len(matches)==1 else None
            job_status='Duplicate' if duplicate else ('Queued' if matched_case_id else 'Quarantined'); cid=uuid4().hex
            cur=db.execute('INSERT INTO ingestion_jobs (correlation_id,source_path,source_device,adapter_name,original_filename,checksum_sha256,file_size,status,match_identifier,matched_case_id,match_method,imported_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
              (cid,str(source),item.source_device,adapter.name,source.name,checksum,source.stat().st_size,job_status,identifier or None,matched_case_id,'exact-identifier' if matched_case_id else None,_['username']));
            if not duplicate and not matched_case_id:
                reason='No identifier was supplied' if not identifier else ('Identifier did not match a case' if not matches else 'Identifier matched multiple cases')
                db.execute('INSERT INTO reconciliation_items (ingestion_job_id,proposed_case_id,reason) VALUES (?,?,?)',(cur.lastrowid,identifier or None,reason))
            db.commit()
            return {'id':cur.lastrowid,'correlation_id':cid,'checksum_sha256':checksum,'status':job_status,'matched_case_id':matched_case_id,'duplicate_of':duplicate['id'] if duplicate else None}

    @router.patch('/ingestion/jobs/{job_id}')
    def update_ingestion(job_id:int,item:JobStatusUpdate,_=Depends(require_roles(intake_roles))):
        allowed={'Queued','Paused','Processing','Completed','Failed','Quarantined','Duplicate','Cancelled'}
        if item.status not in allowed: raise HTTPException(400,'Invalid status')
        with connect() as db:
            row=db.execute('SELECT id FROM ingestion_jobs WHERE id=?',(job_id,)).fetchone()
            if not row: raise HTTPException(404,'Ingestion job not found')
            db.execute("UPDATE ingestion_jobs SET status=?,progress=?,error_detail=?,attempts=attempts+1,started_at=COALESCE(started_at,SYSUTCDATETIME()),completed_at=CASE WHEN ? IN ('Completed','Failed','Quarantined','Cancelled') THEN SYSUTCDATETIME() ELSE completed_at END WHERE id=?",(item.status,item.progress,item.error_detail,item.status,job_id)); db.commit()
        return {'id':job_id,**item.model_dump()}

    @router.post('/ingestion/batch')
    def batch_ingestion(item:IngestionBatchAction,_=Depends(require_roles(intake_roles))):
        transitions={'pause':("Paused",{'Queued'}),'resume':("Queued",{'Paused'}),'cancel':("Cancelled",{'Queued','Paused'}),'retry':("Queued",{'Failed','Quarantined','Cancelled'})}
        target,allowed=transitions[item.action]; updated=[]; skipped=[]
        with connect() as db:
            for job_id in dict.fromkeys(item.job_ids):
                row=db.execute('SELECT status FROM ingestion_jobs WHERE id=?',(job_id,)).fetchone()
                if not row or row['status'] not in allowed: skipped.append(job_id); continue
                db.execute("UPDATE ingestion_jobs SET status=?,progress=CASE WHEN ?='Queued' THEN 0 ELSE progress END,error_detail=CASE WHEN ?='Queued' THEN NULL ELSE error_detail END,completed_at=CASE WHEN ?='Cancelled' THEN SYSUTCDATETIME() ELSE NULL END WHERE id=?",(target,target,target,target,job_id)); updated.append(job_id)
            db.commit()
        return {'action':item.action,'status':target,'updated':updated,'skipped':skipped}

    @router.post('/ingestion/jobs/{job_id}/process')
    def process_ingestion(job_id:int, user=Depends(require_roles(intake_roles))):
        """Process one queued file. Vendor-specific scanners should enqueue files through adapters."""
        with connect() as db:
            job=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(job_id,)).fetchone()
            if not job: raise HTTPException(404,'Ingestion job not found')
            if job['status'] in TERMINAL_INGESTION: raise HTTPException(409,f"Job is already {job['status']}")
            db.execute("UPDATE ingestion_jobs SET status='Processing',progress=10,attempts=attempts+1,started_at=COALESCE(started_at,SYSUTCDATETIME()) WHERE id=?",(job_id,)); db.commit()
        source=Path(job['source_path'])
        stable_seconds=max(1,int(os.getenv('INGESTION_STABLE_SECONDS','10')))
        if source.is_file() and datetime.now(timezone.utc).timestamp()-source.stat().st_mtime < stable_seconds:
            raise HTTPException(409,f'File is still within the {stable_seconds}-second copy-stability window')
        try:
            if not source.is_file(): raise FileNotFoundError('Source file no longer exists')
            before=(source.stat().st_size,source.stat().st_mtime_ns)
            validation=ADAPTERS.get(job['adapter_name']).validate(source, openslide_module)
            after=(source.stat().st_size,source.stat().st_mtime_ns)
            if before != after: raise ImageValidationError('File changed during validation and may still be copying')
            with connect() as db:
                duplicate=db.execute("SELECT TOP 1 id FROM ingestion_jobs WHERE id<>? AND checksum_sha256=? AND status='Completed' ORDER BY id",(job_id,validation['checksum_sha256'])).fetchone()
                if duplicate:
                    validation['duplicate']=True; validation['duplicate_of']=duplicate['id']
                    db.execute("UPDATE ingestion_jobs SET status='Duplicate',progress=100,validation_json=?,completed_at=SYSUTCDATETIME(),error_detail=NULL WHERE id=?",(json.dumps(validation),job_id)); db.commit()
                    return {'id':job_id,'status':'Duplicate','validation':validation}
            destination=uploads_dir / f"{uuid4().hex}{source.suffix.lower()}"
            shutil.copy2(source,destination)
            with connect() as db:
                if not job['matched_case_id']: raise ValueError('Image must be matched to a case before processing')
                cur=db.execute("INSERT INTO slides (case_id,label,stain,specimen_description,filename,file_type,format,file_size,width,height,level_count,upload_state) VALUES (?,?, '', '', ?,?,?,?,?,?,?,'ready')",(job['matched_case_id'],source.stem[:120] or 'Imported slide',destination.name,validation['file_type'],validation['format'],validation['file_size'],validation['width'],validation['height'],validation['level_count']))
                calibration=validation['physical_calibration']; pixel_size=calibration['pixel_size_microns_x'] if calibration['available'] else None
                db.execute("INSERT INTO image_versions (slide_id,version,checksum_sha256,source_path,source_device,pixel_size_microns,metadata_json,stored_filename) VALUES (?,1,?,?,?,?,?,?)",(cur.lastrowid,validation['checksum_sha256'],job['source_path'],job['source_device'],pixel_size,json.dumps(validation),destination.name))
                db.execute("UPDATE ingestion_jobs SET status='Completed',progress=100,checksum_sha256=?,file_size=?,validation_json=?,stored_filename=?,completed_at=SYSUTCDATETIME(),error_detail=NULL WHERE id=?",(validation['checksum_sha256'],validation['file_size'],json.dumps(validation),destination.name,job_id)); db.commit()
            return {'id':job_id,'status':'Completed','slide_id':cur.lastrowid,'stored_filename':destination.name,'checksum_sha256':validation['checksum_sha256'],'validation':validation}
        except Exception as exc:
            quarantine_path=None
            try:
                if source.is_file():
                    target=quarantine_dir / f"{job_id}-{source.name}"
                    shutil.copy2(source,target); quarantine_path=str(target)
            except OSError:
                pass
            with connect() as db:
                failed_validation=json.dumps({'valid':False,'supported':not isinstance(exc,ImageValidationError) or 'Unsupported' not in str(exc),'integrity':'invalid','error':str(exc)[:4000]})
                db.execute("UPDATE ingestion_jobs SET status='Quarantined',error_detail=?,quarantine_path=?,validation_json=?,completed_at=SYSUTCDATETIME() WHERE id=?",(str(exc)[:4000],quarantine_path,failed_validation,job_id)); db.commit()
            raise HTTPException(422,f'Ingestion failed and was quarantined: {exc}')

    @router.post('/ingestion/jobs/{job_id}/retry', status_code=202)
    def retry_ingestion(job_id:int,_=Depends(require_roles(intake_roles))):
        with connect() as db:
            row=db.execute('SELECT status FROM ingestion_jobs WHERE id=?',(job_id,)).fetchone()
            if not row: raise HTTPException(404,'Ingestion job not found')
            if row['status'] not in {'Failed','Quarantined','Cancelled'}: raise HTTPException(409,'Only failed, quarantined, or cancelled jobs can be retried')
            db.execute("UPDATE ingestion_jobs SET status='Queued',progress=0,error_detail=NULL,completed_at=NULL WHERE id=?",(job_id,)); db.commit()
        return {'id':job_id,'status':'Queued'}

    @router.get('/reconciliation')
    def reconciliation(_=Depends(require_roles(intake_roles + ['Pathologist', 'Laboratory Manager']))):
        return _rows(connect, 'SELECT TOP 200 * FROM reconciliation_items ORDER BY id DESC')

    @router.patch('/reconciliation/{item_id}/resolve')
    def resolve_reconciliation(item_id:int,item:ReconciliationResolve,user=Depends(require_roles(intake_roles))):
        with connect() as db:
            case=db.execute('SELECT id FROM cases WHERE id=?',(item.resolved_case_id,)).fetchone()
            if not case: raise HTTPException(400,'Resolved case does not exist')
            reconciliation=db.execute("SELECT ingestion_job_id FROM reconciliation_items WHERE id=? AND status='Open'",(item_id,)).fetchone()
            if not reconciliation: raise HTTPException(404,'Open reconciliation item not found')
            cur=db.execute("UPDATE reconciliation_items SET status='Resolved',resolved_case_id=?,resolved_by=?,resolved_at=SYSUTCDATETIME() WHERE id=? AND status='Open'",(item.resolved_case_id,user['username'],item_id))
            db.execute("UPDATE ingestion_jobs SET matched_case_id=?,match_method='manual-reconciliation',status='Queued',error_detail=NULL,completed_at=NULL WHERE id=?",(item.resolved_case_id,reconciliation['ingestion_job_id']))
            db.commit()
        return {'id':item_id,'status':'Resolved','resolved_case_id':item.resolved_case_id}

    @router.patch('/image-versions/{version_id}/metadata')
    def correct_metadata(version_id:int,item:MetadataCorrection,user=Depends(require_roles(intake_roles))):
        with connect() as db:
            version=db.execute('SELECT metadata_json FROM image_versions WHERE id=?',(version_id,)).fetchone()
            if not version: raise HTTPException(404,'Image version not found')
            metadata=json.loads(version['metadata_json'] or '{}'); normalized=metadata.setdefault('normalized_metadata',{})
            original=normalized.get(item.field_name); normalized[item.field_name]=item.corrected_value
            db.execute('INSERT INTO image_metadata_revisions (image_version_id,field_name,original_value,corrected_value,editor,reason) VALUES (?,?,?,?,?,?)',(version_id,item.field_name,None if original is None else str(original),item.corrected_value,user['username'],item.reason))
            db.execute('UPDATE image_versions SET metadata_json=? WHERE id=?',(json.dumps(metadata),version_id)); db.commit()
        return {'image_version_id':version_id,'field_name':item.field_name,'original_value':original,'corrected_value':item.corrected_value,'editor':user['username'],'reason':item.reason}

    @router.patch('/slides/{slide_id}/calibration')
    def calibrate_slide(slide_id:int,item:SlideCalibrationUpdate,user=Depends(require_roles(intake_roles))):
        with connect() as db:
            version=db.execute('SELECT TOP 1 id,version,metadata_json,pixel_size_microns FROM image_versions WHERE slide_id=? ORDER BY version DESC',(slide_id,)).fetchone()
            if not version: raise HTTPException(404,'No image version exists for this slide')
            metadata=json.loads(version['metadata_json'] or '{}')
            original=metadata.get('physical_calibration') or {}
            calibration={
                'available':True,
                'pixel_size_microns_x':item.pixel_size_microns_x,
                'pixel_size_microns_y':item.pixel_size_microns_y,
                'source':'authorized-manual-calibration',
                'reason':item.reason,
                'editor':user['username'],
            }
            metadata['physical_calibration']=calibration
            for axis,value in (('pixel_size_microns_x',item.pixel_size_microns_x),('pixel_size_microns_y',item.pixel_size_microns_y)):
                db.execute('INSERT INTO image_metadata_revisions (image_version_id,field_name,original_value,corrected_value,editor,reason) VALUES (?,?,?,?,?,?)',(version['id'],f'physical_calibration.{axis}',None if original.get(axis) is None else str(original.get(axis)),str(value),user['username'],item.reason))
            db.execute('UPDATE image_versions SET pixel_size_microns=?,metadata_json=? WHERE id=?',(item.pixel_size_microns_x,json.dumps(metadata),version['id']))
            db.commit()
        metadata['image_version']=version['version']
        return metadata

    @router.delete('/slides/{slide_id}',status_code=204)
    def delete_slide(slide_id:int,user=Depends(require_roles(intake_roles))):
        filenames=[]
        with connect() as db:
            slide=db.execute('SELECT id,case_id,label,filename FROM slides WHERE id=?',(slide_id,)).fetchone()
            if not slide: raise HTTPException(404,'Slide not found')
            versions=db.execute('SELECT id,stored_filename FROM image_versions WHERE slide_id=?',(slide_id,)).fetchall()
            version_ids=[row['id'] for row in versions]
            filenames=list({row['stored_filename'] for row in versions if row.get('stored_filename')} | ({slide['filename']} if slide.get('filename') else set()))
            db.execute('UPDATE report_findings SET annotation_id=NULL WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id=?)',(slide_id,))
            db.execute('UPDATE report_findings SET slide_id=NULL WHERE slide_id=?',(slide_id,))
            db.execute('DELETE FROM annotation_revisions WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id=?)',(slide_id,))
            db.execute('DELETE FROM annotations WHERE slide_id=?',(slide_id,))
            db.execute('UPDATE annotations SET source_ai_result_id=NULL WHERE source_ai_result_id IN (SELECT r.id FROM ai_results r JOIN ai_jobs j ON j.id=r.ai_job_id WHERE j.slide_id=?)',(slide_id,))
            db.execute('DELETE FROM ai_results WHERE ai_job_id IN (SELECT id FROM ai_jobs WHERE slide_id=?)',(slide_id,))
            db.execute('DELETE FROM ai_jobs WHERE slide_id=?',(slide_id,))
            if version_ids:
                placeholders=','.join('?' for _ in version_ids)
                db.execute(f'UPDATE annotations SET image_version_id=NULL WHERE image_version_id IN ({placeholders})',tuple(version_ids))
                db.execute(f'DELETE FROM image_metadata_revisions WHERE image_version_id IN ({placeholders})',tuple(version_ids))
            db.execute('DELETE FROM viewer_bookmarks WHERE slide_id=?',(slide_id,))
            db.execute('DELETE FROM viewer_preferences WHERE slide_id=?',(slide_id,))
            db.execute('DELETE FROM slide_qc_history WHERE slide_id=?',(slide_id,))
            db.execute('DELETE FROM image_versions WHERE slide_id=?',(slide_id,))
            db.execute('DELETE FROM slides WHERE id=?',(slide_id,))
            metadata=get_audit_context()
            db.execute('INSERT INTO audit_log (actor_username,action,target_type,target_id,details,ip_address,device_name,device_location,user_agent) VALUES (?,?,?,?,?,?,?,?,?)',(user['username'],'slide.delete','slide',str(slide_id),json.dumps({'case_id':slide['case_id'],'label':slide['label'],'filenames':filenames}),metadata.get('ip_address'),metadata.get('device_name'),metadata.get('device_location'),metadata.get('user_agent')))
            db.commit()
        deleted_dir=(uploads_dir / '_deleted_slides').resolve(); deleted_dir.mkdir(parents=True,exist_ok=True)
        upload_root=uploads_dir.resolve()
        for filename in filenames:
            source=(uploads_dir / filename).resolve()
            if upload_root not in source.parents or not source.is_file(): continue
            try:
                destination=deleted_dir / f'{slide_id}_{source.name}'
                if destination.exists(): destination=deleted_dir / f'{slide_id}_{uuid4().hex}_{source.name}'
                shutil.move(str(source),str(destination))
            except OSError:
                logger.exception('Could not quarantine deleted slide file',extra={'slide_id':slide_id,'filename':filename})
        return Response(status_code=204)

    @router.delete('/cases/{case_id}',status_code=204)
    def delete_case(case_id:int,user=Depends(require_roles(administrators))):
        filenames=[]
        with connect() as db:
            case=db.execute('SELECT id,case_id,filename,status FROM cases WHERE id=?',(case_id,)).fetchone()
            if not case: raise HTTPException(404,'Case not found')
            finalized_reports=db.execute("SELECT COUNT(*) AS n FROM reports WHERE case_id=? AND status='Finalized'",(case_id,)).fetchone()['n']
            slides=db.execute('SELECT id,filename FROM slides WHERE case_id=?',(case_id,)).fetchall()
            slide_ids=[row['id'] for row in slides]
            filenames.extend(row['filename'] for row in slides if row.get('filename'))
            if case.get('filename'): filenames.append(case['filename'])
            if slide_ids:
                slide_marks=','.join('?' for _ in slide_ids)
                versions=db.execute(f'SELECT id,stored_filename FROM image_versions WHERE slide_id IN ({slide_marks})',tuple(slide_ids)).fetchall()
                version_ids=[row['id'] for row in versions]
                filenames.extend(row['stored_filename'] for row in versions if row.get('stored_filename'))
                db.execute(f'UPDATE report_findings SET annotation_id=NULL WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id IN ({slide_marks}))',tuple(slide_ids))
                db.execute(f'DELETE FROM annotation_revisions WHERE annotation_id IN (SELECT id FROM annotations WHERE slide_id IN ({slide_marks}))',tuple(slide_ids))
                db.execute(f'DELETE FROM annotations WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                db.execute(f'UPDATE annotations SET source_ai_result_id=NULL WHERE source_ai_result_id IN (SELECT r.id FROM ai_results r JOIN ai_jobs j ON j.id=r.ai_job_id WHERE j.slide_id IN ({slide_marks}))',tuple(slide_ids))
                db.execute(f'DELETE FROM ai_results WHERE ai_job_id IN (SELECT id FROM ai_jobs WHERE slide_id IN ({slide_marks}))',tuple(slide_ids))
                db.execute(f'DELETE FROM ai_jobs WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                if version_ids:
                    version_marks=','.join('?' for _ in version_ids)
                    db.execute(f'UPDATE annotations SET image_version_id=NULL WHERE image_version_id IN ({version_marks})',tuple(version_ids))
                    db.execute(f'DELETE FROM image_metadata_revisions WHERE image_version_id IN ({version_marks})',tuple(version_ids))
                db.execute(f'DELETE FROM viewer_bookmarks WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                db.execute(f'DELETE FROM viewer_preferences WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                db.execute(f'DELETE FROM slide_qc_history WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                db.execute(f'DELETE FROM image_versions WHERE slide_id IN ({slide_marks})',tuple(slide_ids))
                db.execute(f'DELETE FROM slides WHERE id IN ({slide_marks})',tuple(slide_ids))
            db.execute('DELETE FROM report_findings WHERE report_id IN (SELECT id FROM reports WHERE case_id=?)',(case_id,))
            db.execute('DELETE FROM report_reviews WHERE report_id IN (SELECT id FROM reports WHERE case_id=?)',(case_id,))
            db.execute('DELETE FROM reports WHERE case_id=?',(case_id,))
            db.execute('DELETE FROM case_review_notes WHERE case_id=?',(case_id,))
            db.execute('DELETE FROM case_review_history WHERE case_id=?',(case_id,))
            db.execute('DELETE FROM case_consultations WHERE case_id=?',(case_id,))
            db.execute('DELETE FROM research_folder_members WHERE case_id=?',(case_id,))
            db.execute('DELETE FROM case_links WHERE case_id=? OR related_case_id=?',(case_id,case_id))
            db.execute('DELETE FROM case_operations WHERE source_case_id=? OR target_case_id=?',(case_id,case_id))
            db.execute('UPDATE accessions SET case_id=NULL WHERE case_id=?',(case_id,))
            db.execute('UPDATE ingestion_jobs SET matched_case_id=NULL WHERE matched_case_id=?',(case_id,))
            db.execute('UPDATE reconciliation_items SET resolved_case_id=NULL WHERE resolved_case_id=?',(case_id,))
            db.execute('DELETE FROM cases WHERE id=?',(case_id,))
            metadata=get_audit_context()
            db.execute('INSERT INTO audit_log (actor_username,action,target_type,target_id,details,ip_address,device_name,device_location,user_agent) VALUES (?,?,?,?,?,?,?,?,?)',(user['username'],'case.delete','case',str(case_id),json.dumps({'case_id':case['case_id'],'status':case['status'],'slide_count':len(slide_ids),'finalized_report_count':finalized_reports,'filenames':sorted(set(filenames))}),metadata.get('ip_address'),metadata.get('device_name'),metadata.get('device_location'),metadata.get('user_agent')))
            db.commit()
        deleted_dir=(uploads_dir / '_deleted_cases').resolve(); deleted_dir.mkdir(parents=True,exist_ok=True)
        upload_root=uploads_dir.resolve()
        for filename in set(filenames):
            source=(uploads_dir / filename).resolve()
            if upload_root not in source.parents or not source.is_file(): continue
            try:
                destination=deleted_dir / f'{case_id}_{source.name}'
                if destination.exists(): destination=deleted_dir / f'{case_id}_{uuid4().hex}_{source.name}'
                shutil.move(str(source),str(destination))
            except OSError:
                logger.exception('Could not quarantine deleted case file',extra={'case_id':case_id,'filename':filename})
        return Response(status_code=204)

    @router.get('/image-versions/{version_id}/metadata-history')
    def metadata_history(version_id:int,_=Depends(require_roles(clinical_read_roles))):
        return _rows(connect,'SELECT * FROM image_metadata_revisions WHERE image_version_id=? ORDER BY id DESC',(version_id,))

    @router.get('/ai/models')
    def models(_=Depends(require_roles(ai_roles))): return _rows(connect, 'SELECT * FROM ai_models ORDER BY name, version DESC')

    @router.post('/ai/models', status_code=201)
    def create_model(item: ModelCreate, user=Depends(require_roles(administrators))):
        if item.approval_status not in {'Research','Validated','Retired'}: raise HTTPException(400, 'Invalid approval status')
        with connect() as db:
            cur=db.execute('INSERT INTO ai_models (name,version,checksum,task,intended_use,compatible_inputs,limitations,approval_status,adapter_name) VALUES (?,?,?,?,?,?,?,?,?)',
              (item.name,item.version,item.checksum,item.task,item.intended_use,item.compatible_inputs,item.limitations,item.approval_status,item.adapter_name)); db.commit()
            return {'id':cur.lastrowid, **item.model_dump()}

    @router.get('/ai/jobs')
    def ai_jobs(_=Depends(require_roles(ai_roles))): return _rows(connect, 'SELECT TOP 200 * FROM ai_jobs ORDER BY id DESC')

    @router.post('/ai/jobs', status_code=202)
    def create_ai_job(item: AiJobCreate, user=Depends(require_roles(ai_roles))):
        with connect() as db:
            model=db.execute('SELECT id,approval_status FROM ai_models WHERE id=?',(item.model_id,)).fetchone()
            slide=db.execute('SELECT id FROM slides WHERE id=?',(item.slide_id,)).fetchone()
            if not model or not slide: raise HTTPException(400,'Invalid model or slide')
            if user['role'] == 'Pathologist' and model['approval_status'] != 'Validated': raise HTTPException(409,'Pathologists may run only validated models')
            if model['approval_status'] == 'Retired': raise HTTPException(409,'Retired models cannot accept new jobs')
            image_version_id=item.image_version_id
            if image_version_id is None:
                version=db.execute('SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC',(item.slide_id,)).fetchone()
                image_version_id=version['id'] if version else None
            cid=uuid4().hex; cur=db.execute("INSERT INTO ai_jobs (correlation_id,model_id,slide_id,image_version_id,status,parameters_json,requested_by) VALUES (?,?,?,?,?,?,?)",
              (cid,item.model_id,item.slide_id,image_version_id,'Queued',json.dumps(item.parameters),user['username'])); db.commit()
            return {'id':cur.lastrowid,'correlation_id':cid,'status':'Queued'}

    @router.patch('/ai/jobs/{job_id}')
    def update_ai_job(job_id:int,item:JobStatusUpdate,_=Depends(require_roles(administrators))):
        allowed={'Queued','Running','Completed','Failed','Cancelled'}
        if item.status not in allowed: raise HTTPException(400,'Invalid AI job status')
        with connect() as db:
            db.execute("UPDATE ai_jobs SET status=?,progress=?,error_detail=?,started_at=COALESCE(started_at,SYSUTCDATETIME()),completed_at=CASE WHEN ? IN ('Completed','Failed','Cancelled') THEN SYSUTCDATETIME() ELSE completed_at END WHERE id=?",(item.status,item.progress,item.error_detail,item.status,job_id)); db.commit()
        return {'id':job_id,**item.model_dump()}

    @router.get('/ai/jobs/{job_id}/results')
    def ai_results(job_id:int,_=Depends(require_roles(ai_roles))): return _rows(connect,'SELECT * FROM ai_results WHERE ai_job_id=? ORDER BY id',(job_id,))

    @router.get('/ai/slides/{slide_id}/results')
    def slide_ai_results(slide_id:int,_=Depends(require_roles(ai_roles))):
        rows=_rows(connect,"SELECT r.*,j.status AS job_status,j.parameters_json,m.name AS model_name,m.version AS model_version,m.limitations FROM ai_results r JOIN ai_jobs j ON j.id=r.ai_job_id JOIN ai_models m ON m.id=j.model_id WHERE j.slide_id=? ORDER BY r.id",(slide_id,))
        for row in rows:
            for key in ('geometry_json','measurements_json','raw_output_json','parameters_json'):
                row[key[:-5] if key.endswith('_json') else key]=json.loads(row.pop(key) or 'null')
        return rows

    @router.post('/ai/jobs/{job_id}/results',status_code=201)
    def create_ai_result(job_id:int,item:AiResultCreate,_=Depends(require_roles(administrators))):
        with connect() as db:
            cur=db.execute('INSERT INTO ai_results (ai_job_id,result_type,geometry_json,measurements_json,confidence,raw_output_json) VALUES (?,?,?,?,?,?)',(job_id,item.result_type,json.dumps(item.geometry),json.dumps(item.measurements),item.confidence,json.dumps(item.raw_output))); db.commit()
            return {'id':cur.lastrowid,'ai_job_id':job_id,**item.model_dump()}

    @router.patch('/ai/results/{result_id}/review')
    def review_ai_result(result_id:int,item:AiReview,user=Depends(require_roles(['System Administrator','Pathologist']))):
        if item.review_status not in {'Accepted','Rejected','Corrected'}: raise HTTPException(400,'Invalid review status')
        with connect() as db:
            db.execute('UPDATE ai_results SET review_status=?,reviewed_by=?,review_note=?,reviewed_at=SYSUTCDATETIME() WHERE id=?',(item.review_status,user['username'],item.review_note,result_id)); db.commit()
        return {'id':result_id,**item.model_dump(),'reviewed_by':user['username']}

    @router.get('/hl7/channels')
    def channels(_=Depends(require_roles(integration_roles))): return _rows(connect, 'SELECT * FROM hl7_channels ORDER BY name')

    @router.post('/hl7/channels', status_code=201)
    def create_channel(item: ChannelCreate, _=Depends(require_roles(integration_roles))):
        if item.direction not in {'inbound','outbound'}: raise HTTPException(400,'Invalid direction')
        with connect() as db:
            cur=db.execute('INSERT INTO hl7_channels (name,direction,version,transport,host,port,enabled,mapping_json) VALUES (?,?,?,?,?,?,?,?)',
              (item.name,item.direction,item.version,item.transport,item.host,item.port,int(item.enabled),json.dumps(item.mapping))); db.commit()
            return {'id':cur.lastrowid,**item.model_dump()}

    @router.post('/hl7/messages', status_code=202)
    def receive_hl7(item: Hl7MessageCreate, _=Depends(require_roles(integration_roles))):
        parsed=_parse_hl7(item.raw_payload); cid=uuid4().hex
        with connect() as db:
            cur=db.execute("INSERT INTO integration_events (channel_id,correlation_id,message_control_id,message_type,direction,raw_payload,normalized_json,status) VALUES (?,?,?,?,?,?,?,'Received')",
              (item.channel_id,cid,parsed['message_control_id'],parsed['message_type'],item.direction,item.raw_payload,json.dumps(parsed))); db.commit()
            return {'id':cur.lastrowid,'correlation_id':cid,'status':'Received','parsed':parsed}

    @router.get('/hl7/events')
    def events(_=Depends(require_roles(integration_roles))): return _rows(connect, 'SELECT TOP 200 * FROM integration_events ORDER BY id DESC')

    @router.post('/hl7/events/{event_id}/replay',status_code=202)
    def replay_event(event_id:int,_=Depends(require_roles(integration_roles))):
        with connect() as db:
            row=db.execute('SELECT id FROM integration_events WHERE id=?',(event_id,)).fetchone()
            if not row: raise HTTPException(404,'Integration event not found')
            db.execute("UPDATE integration_events SET status='Received',attempts=attempts+1,error_detail=NULL,processed_at=NULL WHERE id=?",(event_id,)); db.commit()
        return {'id':event_id,'status':'Received'}

    @router.post('/hl7/events/{event_id}/ack')
    def acknowledge_event(event_id:int,accepted:bool=True,_=Depends(require_roles(integration_roles))):
        code='AA' if accepted else 'AE'; ack=f'MSH|^~\\&|PATHOLOGY|LOCAL|LIS|LOCAL|{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") }||ACK|{uuid4().hex[:20]}|P|2.5.1\rMSA|{code}|EVENT-{event_id}'
        with connect() as db:
            db.execute("UPDATE integration_events SET acknowledgement=?,status=?,processed_at=SYSUTCDATETIME() WHERE id=?",(ack,'Acknowledged' if accepted else 'Rejected',event_id)); db.commit()
        return {'id':event_id,'acknowledgement':ack,'status':'Acknowledged' if accepted else 'Rejected'}

    @router.get('/health')
    def health(request: Request, _=Depends(require_roles(operational_roles))):
        usage=shutil.disk_usage(uploads_dir); db_ok=True
        try:
            with connect() as db: db.execute('SELECT 1').fetchone()
        except Exception: db_ok=False
        return {'ok':db_ok,'database':{'ok':db_ok},'storage':{'path':str(uploads_dir),'total':usage.total,'used':usage.used,'free':usage.free,'used_percent':round(usage.used/usage.total*100,2)},
                'version':os.getenv('APP_VERSION','development'),'request_id':getattr(request.state,'request_id',None),'time':datetime.now(timezone.utc).isoformat()}

    @router.get('/settings')
    def settings(_=Depends(require_roles(administrators))):
        return _rows(connect,'SELECT setting_key,setting_value,updated_by,updated_at FROM system_settings ORDER BY setting_key')

    @router.put('/settings/{key}')
    def update_setting(key:str,item:SettingUpdate,user=Depends(require_roles(administrators))):
        allowed={'retention_days','storage_warning_percent','storage_critical_percent','ingestion_max_attempts'}
        if key not in allowed: raise HTTPException(400,'Unsupported setting')
        try: value=int(item.value)
        except ValueError: raise HTTPException(400,'Setting value must be an integer')
        if value < 1: raise HTTPException(400,'Setting value must be positive')
        with connect() as db:
            db.execute("MERGE system_settings AS target USING (SELECT ? AS setting_key) AS source ON target.setting_key=source.setting_key WHEN MATCHED THEN UPDATE SET setting_value=?,updated_by=?,updated_at=SYSUTCDATETIME() WHEN NOT MATCHED THEN INSERT (setting_key,setting_value,updated_by) VALUES (?,?,?);",(key,str(value),user['username'],key,str(value),user['username'])); db.commit()
        return {'setting_key':key,'setting_value':str(value),'updated_by':user['username']}

    @router.get('/backups')
    def backups(_=Depends(require_roles(administrators))):
        return _rows(connect,'SELECT TOP 100 * FROM backup_runs ORDER BY id DESC')

    @router.post('/backups',status_code=202)
    def create_backup(item:BackupCreate,_=Depends(require_roles(administrators))):
        """Run a SQL Server native backup to the configured server-visible directory."""
        backup_root=Path(os.getenv('BACKUP_DIR','backups')).resolve(); backup_root.mkdir(parents=True,exist_ok=True)
        destination=(backup_root/f"{item.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.bak").resolve()
        if backup_root not in destination.parents: raise HTTPException(400,'Invalid backup destination')
        cid=uuid4().hex
        with connect() as db:
            cur=db.execute("INSERT INTO backup_runs (correlation_id,status,destination) VALUES (?,'Running',?)",(cid,str(destination))); db.commit(); run_id=cur.lastrowid
        try:
            database=os.getenv('DB_NAME','').replace(']','')
            if not database: raise RuntimeError('DB_NAME is not configured')
            with connect() as db:
                db.execute(f"BACKUP DATABASE [{database}] TO DISK=? WITH COPY_ONLY, CHECKSUM, INIT",(str(destination),)); db.commit()
                manifest={'database':database,'destination':str(destination),'copy_only':True,'checksum':True}
                db.execute("UPDATE backup_runs SET status='Completed',manifest_json=?,completed_at=SYSUTCDATETIME() WHERE id=?",(json.dumps(manifest),run_id)); db.commit()
            return {'id':run_id,'correlation_id':cid,'status':'Completed','destination':str(destination)}
        except Exception as exc:
            with connect() as db:
                db.execute("UPDATE backup_runs SET status='Failed',error_detail=?,completed_at=SYSUTCDATETIME() WHERE id=?",(str(exc)[:4000],run_id)); db.commit()
            raise HTTPException(500,'Backup failed. Verify SQL Server service-account access to BACKUP_DIR.')

    @router.get('/retention/preview')
    def retention_preview(days:int=3650,_=Depends(require_roles(administrators))):
        if days < 1: raise HTTPException(400,'days must be positive')
        with connect() as db:
            counts={
              'audit_events':db.execute('SELECT COUNT(*) AS n FROM audit_log WHERE created_at < DATEADD(day, ?, SYSUTCDATETIME())',(-days,)).fetchone()['n'],
              'integration_events':db.execute('SELECT COUNT(*) AS n FROM integration_events WHERE created_at < DATEADD(day, ?, SYSUTCDATETIME())',(-days,)).fetchone()['n'],
            }
        return {'retention_days':days,'preview_only':True,'eligible':counts}

    @router.get('/audit')
    def audit_events(_=Depends(require_roles(audit_roles))): return _rows(connect, 'SELECT TOP 500 * FROM audit_log ORDER BY id DESC')
    return router


def init_platform_schema(connect):
    with connect() as db:
        for statement in DDL: db.execute(statement)
        db.commit()
