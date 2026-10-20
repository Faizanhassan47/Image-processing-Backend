-- ==============================================================================
-- Digital Pathology Platform - Production Database Schema (SQL Server)
-- ==============================================================================
-- Clean, normalized, enterprise-grade DDL defining core entities and indexes.
-- Order of execution respects foreign key dependencies.
-- ==============================================================================

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ------------------------------------------------------------------------------
-- 1. SECURITY & IDENTITY
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.users', 'U') IS NULL
CREATE TABLE dbo.users (
    id                      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_users PRIMARY KEY,
    username                VARCHAR(255) NOT NULL CONSTRAINT UQ_users_username UNIQUE,
    display_name            NVARCHAR(255) NOT NULL,
    role                    VARCHAR(50) NOT NULL CONSTRAINT CK_users_role CHECK (role IN ('System Administrator', 'Laboratory Manager', 'Pathologist')),
    password_hash           VARCHAR(255) NOT NULL,
    is_active               BIT NOT NULL CONSTRAINT DF_users_is_active DEFAULT (1),
    must_change_password    BIT NOT NULL CONSTRAINT DF_users_must_change_password DEFAULT (0),
    failed_login_attempts   INT NOT NULL CONSTRAINT DF_users_failed_attempts DEFAULT (0),
    locked_until            DATETIME2 NULL,
    created_at              DATETIME2 NOT NULL CONSTRAINT DF_users_created DEFAULT (SYSUTCDATETIME()),
    last_login              DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.audit_log', 'U') IS NULL
CREATE TABLE dbo.audit_log (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_audit_log PRIMARY KEY,
    actor_username  VARCHAR(255) NOT NULL,
    action          VARCHAR(120) NOT NULL,
    target_type     VARCHAR(120) NOT NULL,
    target_id       VARCHAR(255) NOT NULL CONSTRAINT DF_audit_target DEFAULT (''),
    details         NVARCHAR(MAX) NOT NULL CONSTRAINT DF_audit_details DEFAULT (''),
    ip_address      VARCHAR(45) NULL,
    device_name     VARCHAR(255) NULL,
    device_location VARCHAR(255) NULL,
    user_agent      VARCHAR(1000) NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_audit_created DEFAULT (SYSUTCDATETIME())
);
GO

-- ------------------------------------------------------------------------------
-- 2. PATIENTS & CLINICAL HIERARCHY
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.patients', 'U') IS NULL
CREATE TABLE dbo.patients (
    id                      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patients PRIMARY KEY,
    patient_identifier      VARCHAR(64) NULL CONSTRAINT UQ_patients_identifier UNIQUE,
    external_id             VARCHAR(255) NULL,
    medical_record_number   VARCHAR(120) NULL,
    cnic                    CHAR(13) NULL,
    display_label           NVARCHAR(255) NOT NULL,
    first_name              NVARCHAR(120) NULL,
    middle_name             NVARCHAR(120) NULL,
    last_name               NVARCHAR(120) NULL,
    date_of_birth           DATE NULL,
    sex                     VARCHAR(30) NULL,
    gender_identity         NVARCHAR(80) NULL,
    blood_group             VARCHAR(10) NULL,
    phone                   VARCHAR(40) NULL,
    address                 NVARCHAR(1000) NULL,
    status                  VARCHAR(30) NOT NULL CONSTRAINT DF_patients_status DEFAULT ('Active'),
    demographics_json       NVARCHAR(MAX) NULL CONSTRAINT CK_patients_demographics_json CHECK (demographics_json IS NULL OR ISJSON(demographics_json)=1),
    created_by              VARCHAR(255) NULL,
    updated_by              VARCHAR(255) NULL,
    created_at              DATETIME2 NOT NULL CONSTRAINT DF_patients_created DEFAULT (SYSUTCDATETIME()),
    updated_at              DATETIME2 NOT NULL CONSTRAINT DF_patients_updated DEFAULT (SYSUTCDATETIME()),
    archived_at             DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.cases', 'U') IS NULL
CREATE TABLE dbo.cases (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_cases PRIMARY KEY,
    case_id             VARCHAR(255) NOT NULL CONSTRAINT UQ_cases_case_id UNIQUE,
    description         NVARCHAR(MAX) NOT NULL,
    filename            VARCHAR(255) NOT NULL CONSTRAINT DF_cases_filename DEFAULT (''),
    file_type           VARCHAR(50) NOT NULL CONSTRAINT DF_cases_file_type DEFAULT ('raster'),
    format              VARCHAR(50) NULL,
    width               INT NULL,
    height              INT NULL,
    file_size           BIGINT NULL,
    status              VARCHAR(50) NOT NULL CONSTRAINT DF_cases_status DEFAULT ('New'),
    priority            VARCHAR(30) NOT NULL CONSTRAINT DF_cases_priority DEFAULT ('Routine'),
    due_at              DATETIME2 NULL,
    exception_reason    NVARCHAR(2000) NULL,
    reviewer            VARCHAR(255) NOT NULL CONSTRAINT DF_cases_reviewer DEFAULT (''),
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_cases_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.accessions', 'U') IS NULL
CREATE TABLE dbo.accessions (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_accessions PRIMARY KEY,
    case_id             INT NULL CONSTRAINT FK_accessions_case REFERENCES dbo.cases(id) ON DELETE SET NULL,
    patient_id          INT NULL CONSTRAINT FK_accessions_patient REFERENCES dbo.patients(id) ON DELETE SET NULL,
    accession_number    VARCHAR(255) NOT NULL CONSTRAINT UQ_accessions_number UNIQUE,
    order_id            VARCHAR(255) NULL,
    priority            VARCHAR(30) NOT NULL CONSTRAINT DF_accessions_priority DEFAULT ('Routine'),
    due_at              DATETIME2 NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_accessions_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.specimens', 'U') IS NULL
CREATE TABLE dbo.specimens (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_specimens PRIMARY KEY,
    accession_id    INT NOT NULL CONSTRAINT FK_specimens_accession REFERENCES dbo.accessions(id) ON DELETE CASCADE,
    specimen_code   VARCHAR(255) NOT NULL,
    specimen_type   VARCHAR(255) NULL,
    description     NVARCHAR(MAX) NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_specimens_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.blocks', 'U') IS NULL
CREATE TABLE dbo.blocks (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_blocks PRIMARY KEY,
    specimen_id     INT NOT NULL CONSTRAINT FK_blocks_specimen REFERENCES dbo.specimens(id) ON DELETE CASCADE,
    block_code      VARCHAR(255) NOT NULL,
    description     NVARCHAR(MAX) NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_blocks_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.case_links', 'U') IS NULL
CREATE TABLE dbo.case_links (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_links PRIMARY KEY,
    case_id         INT NOT NULL CONSTRAINT FK_case_links_case REFERENCES dbo.cases(id),
    related_case_id INT NOT NULL CONSTRAINT FK_case_links_related REFERENCES dbo.cases(id),
    link_type       VARCHAR(30) NOT NULL,
    reason          NVARCHAR(2000) NULL,
    created_by      VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_case_links_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_case_links UNIQUE (case_id, related_case_id, link_type)
);
GO

IF OBJECT_ID('dbo.case_review_notes', 'U') IS NULL
CREATE TABLE dbo.case_review_notes (
    id          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_review_notes PRIMARY KEY,
    case_id     INT NOT NULL CONSTRAINT FK_case_review_notes_case REFERENCES dbo.cases(id) ON DELETE CASCADE,
    author      VARCHAR(255) NOT NULL,
    note        NVARCHAR(MAX) NOT NULL,
    created_at  DATETIME2 NOT NULL CONSTRAINT DF_case_review_notes_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.case_review_history', 'U') IS NULL
CREATE TABLE dbo.case_review_history (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_review_history PRIMARY KEY,
    case_id         INT NOT NULL CONSTRAINT FK_case_review_history_case REFERENCES dbo.cases(id) ON DELETE CASCADE,
    change_type     VARCHAR(30) NOT NULL CONSTRAINT CK_case_review_history_type CHECK (change_type IN ('status', 'reviewer')),
    previous_value  NVARCHAR(255) NOT NULL,
    new_value       NVARCHAR(255) NOT NULL,
    actor           VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_case_review_history_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.case_operations', 'U') IS NULL
CREATE TABLE dbo.case_operations (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_operations PRIMARY KEY,
    operation_type  VARCHAR(30) NOT NULL,
    source_case_id  INT NOT NULL CONSTRAINT FK_case_operations_source REFERENCES dbo.cases(id),
    target_case_id  INT NULL CONSTRAINT FK_case_operations_target REFERENCES dbo.cases(id),
    details_json    NVARCHAR(MAX) NOT NULL,
    authorized_by   VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_case_operations_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.case_consultations', 'U') IS NULL
CREATE TABLE dbo.case_consultations (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_consultations PRIMARY KEY,
    case_id             INT NOT NULL CONSTRAINT FK_consultations_case REFERENCES dbo.cases(id) ON DELETE CASCADE,
    requested_by        VARCHAR(255) NOT NULL,
    consultant_username VARCHAR(255) NOT NULL,
    message             NVARCHAR(4000) NULL,
    status              VARCHAR(30) NOT NULL CONSTRAINT DF_consultations_status DEFAULT ('Open'),
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_consultations_created DEFAULT (SYSUTCDATETIME()),
    completed_at        DATETIME2 NULL
);
GO

-- ------------------------------------------------------------------------------
-- 3. DIGITAL SLIDES & WHOLE SLIDE IMAGES (WSI)
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.slides', 'U') IS NULL
CREATE TABLE dbo.slides (
    id                      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slides PRIMARY KEY,
    case_id                 INT NOT NULL CONSTRAINT FK_slides_cases REFERENCES dbo.cases(id) ON DELETE CASCADE,
    block_id                INT NULL CONSTRAINT FK_slides_block REFERENCES dbo.blocks(id) ON DELETE SET NULL,
    slide_identifier        VARCHAR(255) NULL,
    label                   NVARCHAR(255) NOT NULL,
    stain                   VARCHAR(100) NOT NULL CONSTRAINT DF_slides_stain DEFAULT (''),
    specimen_description   NVARCHAR(MAX) NOT NULL CONSTRAINT DF_slides_specimen DEFAULT (''),
    scanner_name            VARCHAR(255) NULL,
    filename                VARCHAR(255) NOT NULL CONSTRAINT UQ_slides_filename UNIQUE,
    file_type               VARCHAR(20) NOT NULL CONSTRAINT CK_slides_file_type CHECK (file_type IN ('raster', 'wsi')),
    format                  VARCHAR(50) NOT NULL,
    file_size               BIGINT NOT NULL,
    width                   INT NOT NULL,
    height                  INT NOT NULL,
    level_count             INT NOT NULL CONSTRAINT DF_slides_levels DEFAULT (1),
    thumbnail               VARBINARY(MAX) NULL,
    upload_state            VARCHAR(30) NOT NULL CONSTRAINT DF_slides_upload_state DEFAULT ('uploading')
                            CONSTRAINT CK_slides_upload_state CHECK (upload_state IN ('uploading', 'processing', 'ready', 'error')),
    qc_status               VARCHAR(50) NULL,
    qc_missing_tissue       INT NULL CONSTRAINT DF_slides_qc_missing DEFAULT (0),
    qc_blur                 INT NULL CONSTRAINT DF_slides_qc_blur DEFAULT (0),
    qc_scan_artifact        INT NULL CONSTRAINT DF_slides_qc_artifact DEFAULT (0),
    qc_incorrect_label      INT NULL CONSTRAINT DF_slides_qc_label DEFAULT (0),
    qc_unusual_staining     INT NULL CONSTRAINT DF_slides_qc_staining DEFAULT (0),
    qc_note                 NVARCHAR(MAX) NULL CONSTRAINT DF_slides_qc_note DEFAULT (''),
    qc_actor                VARCHAR(255) NULL CONSTRAINT DF_slides_qc_actor DEFAULT (''),
    qc_timestamp            DATETIME2 NULL,
    created_at              DATETIME2 NOT NULL CONSTRAINT DF_slides_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.slide_qc_history', 'U') IS NULL
CREATE TABLE dbo.slide_qc_history (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slide_qc_history PRIMARY KEY,
    slide_id            INT NOT NULL CONSTRAINT FK_slide_qc_history_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    qc_status           VARCHAR(50) NULL,
    qc_missing_tissue   BIT NULL,
    qc_blur             BIT NULL,
    qc_scan_artifact    BIT NULL,
    qc_incorrect_label  BIT NULL,
    qc_unusual_staining BIT NULL,
    qc_note             NVARCHAR(MAX) NULL,
    actor               VARCHAR(255) NOT NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_slide_qc_history_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.image_versions', 'U') IS NULL
CREATE TABLE dbo.image_versions (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_image_versions PRIMARY KEY,
    slide_id            INT NOT NULL CONSTRAINT FK_image_versions_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    version             INT NOT NULL,
    checksum_sha256     VARCHAR(64) NOT NULL,
    source_path         VARCHAR(2048) NULL,
    source_device       VARCHAR(255) NULL,
    scanner_make        VARCHAR(120) NULL,
    scanner_model       VARCHAR(120) NULL,
    magnification       FLOAT NULL,
    pixel_size_microns  FLOAT NULL,
    acquisition_time    DATETIME2 NULL,
    metadata_json       NVARCHAR(MAX) NULL,
    stored_filename     VARCHAR(512) NOT NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_image_versions_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_image_versions_slide_version UNIQUE (slide_id, version)
);
GO

IF OBJECT_ID('dbo.image_metadata_revisions', 'U') IS NULL
CREATE TABLE dbo.image_metadata_revisions (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_image_metadata_revisions PRIMARY KEY,
    image_version_id    INT NOT NULL CONSTRAINT FK_metadata_revision_version REFERENCES dbo.image_versions(id) ON DELETE CASCADE,
    field_name          VARCHAR(120) NOT NULL,
    original_value      NVARCHAR(MAX) NULL,
    corrected_value     NVARCHAR(MAX) NULL,
    editor              VARCHAR(255) NOT NULL,
    reason              NVARCHAR(2000) NOT NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_metadata_revision_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.viewer_bookmarks', 'U') IS NULL
CREATE TABLE dbo.viewer_bookmarks (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_viewer_bookmarks PRIMARY KEY,
    slide_id        INT NOT NULL CONSTRAINT FK_viewer_bookmarks_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    name            NVARCHAR(120) NOT NULL,
    viewport_x      FLOAT NOT NULL,
    viewport_y      FLOAT NOT NULL,
    viewport_width  FLOAT NOT NULL,
    viewport_height FLOAT NOT NULL,
    rotation        FLOAT NOT NULL CONSTRAINT DF_viewer_bookmarks_rotation DEFAULT (0),
    created_by      VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_viewer_bookmarks_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.viewer_preferences', 'U') IS NULL
CREATE TABLE dbo.viewer_preferences (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_viewer_preferences PRIMARY KEY,
    slide_id        INT NOT NULL CONSTRAINT FK_viewer_preferences_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    username        VARCHAR(255) NOT NULL,
    settings_json   NVARCHAR(MAX) NOT NULL CONSTRAINT CK_viewer_preferences_json CHECK (ISJSON(settings_json)=1),
    updated_at      DATETIME2 NOT NULL CONSTRAINT DF_viewer_preferences_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_viewer_preferences UNIQUE (slide_id, username)
);
GO

-- ------------------------------------------------------------------------------
-- 4. ANNOTATIONS & REVISIONS
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.annotations', 'U') IS NULL
CREATE TABLE dbo.annotations (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_annotations PRIMARY KEY,
    slide_id            INT NOT NULL CONSTRAINT FK_annotations_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    image_version_id    INT NULL CONSTRAINT FK_annotations_image_version REFERENCES dbo.image_versions(id),
    annotation_type     VARCHAR(30) NOT NULL CONSTRAINT DF_annotations_type DEFAULT ('rectangle')
                        CONSTRAINT CK_annotations_type CHECK (annotation_type IN ('point', 'line', 'polygon', 'rectangle', 'ellipse', 'freehand', 'arrow')),
    label               NVARCHAR(255) NOT NULL,
    note                NVARCHAR(MAX) NOT NULL CONSTRAINT DF_annotations_note DEFAULT (''),
    category            VARCHAR(120) NULL,
    annotation_status   VARCHAR(30) NULL,
    author              VARCHAR(255) NULL,
    source_type         VARCHAR(30) NOT NULL CONSTRAINT DF_annotations_source DEFAULT ('Human'),
    color               VARCHAR(30) NOT NULL CONSTRAINT DF_annotations_color DEFAULT ('#e85d75'),
    x                   REAL NULL,
    y                   REAL NULL,
    width               REAL NULL,
    height              REAL NULL,
    points              VARCHAR(MAX) NULL,
    measurements_json   NVARCHAR(MAX) NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_annotations_created DEFAULT (SYSUTCDATETIME()),
    updated_at          DATETIME2 NOT NULL CONSTRAINT DF_annotations_updated DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.annotation_revisions', 'U') IS NULL
CREATE TABLE dbo.annotation_revisions (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_annotation_revisions PRIMARY KEY,
    annotation_id       INT NOT NULL CONSTRAINT FK_annotation_revisions_ann REFERENCES dbo.annotations(id) ON DELETE CASCADE,
    revision_number     INT NOT NULL,
    geometry_json       NVARCHAR(MAX) NOT NULL,
    label               NVARCHAR(120) NOT NULL,
    note                NVARCHAR(2000) NULL,
    category            VARCHAR(120) NULL,
    annotation_status   VARCHAR(30) NULL,
    changed_by          VARCHAR(255) NOT NULL,
    change_reason       NVARCHAR(2000) NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_annotation_revisions_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_annotation_revision UNIQUE (annotation_id, revision_number)
);
GO

-- ------------------------------------------------------------------------------
-- 5. PATHOLOGY REPORTS & FINDINGS
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.report_templates', 'U') IS NULL
CREATE TABLE dbo.report_templates (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_report_templates PRIMARY KEY,
    name            NVARCHAR(255) NOT NULL CONSTRAINT UQ_report_templates_name UNIQUE,
    sections_json   NVARCHAR(MAX) NOT NULL,
    active          BIT NOT NULL CONSTRAINT DF_report_templates_active DEFAULT (1),
    created_by      VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_report_templates_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.reports', 'U') IS NULL
CREATE TABLE dbo.reports (
    id                      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_reports PRIMARY KEY,
    case_id                 INT NOT NULL CONSTRAINT FK_reports_case REFERENCES dbo.cases(id) ON DELETE CASCADE,
    template_id             INT NULL CONSTRAINT FK_reports_template REFERENCES dbo.report_templates(id),
    version                 INT NOT NULL,
    status                  VARCHAR(30) NOT NULL CONSTRAINT CK_reports_status CHECK (status IN ('Draft', 'Finalized')),
    workflow_status         VARCHAR(30) NOT NULL CONSTRAINT DF_reports_workflow DEFAULT ('Draft'),
    specimen_clinical       NVARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_specimen DEFAULT (''),
    gross_description       NVARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_gross DEFAULT (''),
    microscopic_findings    NVARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_micro DEFAULT (''),
    diagnosis               NVARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_diagnosis DEFAULT (''),
    comments                NVARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_comments DEFAULT (''),
    reviewer_name           NVARCHAR(255) NOT NULL CONSTRAINT DF_reports_reviewer DEFAULT (''),
    approved_by             VARCHAR(255) NULL,
    approved_at             DATETIME2 NULL,
    review_comment          NVARCHAR(4000) NULL,
    created_at              DATETIME2 NOT NULL CONSTRAINT DF_reports_created DEFAULT (SYSUTCDATETIME()),
    finalized_at            DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.report_reviews', 'U') IS NULL
CREATE TABLE dbo.report_reviews (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_report_reviews PRIMARY KEY,
    report_id       INT NOT NULL CONSTRAINT FK_report_reviews_report REFERENCES dbo.reports(id) ON DELETE CASCADE,
    previous_status VARCHAR(30) NULL,
    new_status      VARCHAR(30) NOT NULL,
    comment         NVARCHAR(4000) NULL,
    actor           VARCHAR(255) NOT NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_report_reviews_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.report_findings', 'U') IS NULL
CREATE TABLE dbo.report_findings (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_report_findings PRIMARY KEY,
    report_id       INT NOT NULL CONSTRAINT FK_report_findings_report REFERENCES dbo.reports(id) ON DELETE CASCADE,
    slide_id        INT NULL CONSTRAINT FK_report_findings_slide REFERENCES dbo.slides(id) ON DELETE SET NULL,
    annotation_id   INT NULL CONSTRAINT FK_report_findings_annotation REFERENCES dbo.annotations(id) ON DELETE SET NULL,
    text            NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_findings_text DEFAULT (''),
    sort_order      INT NOT NULL CONSTRAINT DF_report_findings_order DEFAULT (0)
);
GO

-- ------------------------------------------------------------------------------
-- 6. SCANNER INGESTION & RECONCILIATION
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.ingestion_jobs', 'U') IS NULL
CREATE TABLE dbo.ingestion_jobs (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ingestion_jobs PRIMARY KEY,
    correlation_id      VARCHAR(64) NOT NULL CONSTRAINT UQ_ingestion_correlation UNIQUE,
    source_path         VARCHAR(2048) NULL,
    source_device       VARCHAR(255) NULL,
    adapter_name        VARCHAR(120) NOT NULL CONSTRAINT DF_ingestion_adapter DEFAULT ('manual'),
    original_filename   VARCHAR(512) NOT NULL,
    checksum_sha256     VARCHAR(64) NULL,
    file_size           BIGINT NULL,
    status              VARCHAR(30) NOT NULL,
    attempts            INT NOT NULL CONSTRAINT DF_ingestion_attempts DEFAULT (0),
    progress            INT NOT NULL CONSTRAINT DF_ingestion_progress DEFAULT (0),
    error_detail        NVARCHAR(MAX) NULL,
    quarantine_path     VARCHAR(2048) NULL,
    validation_json     NVARCHAR(MAX) NULL,
    match_identifier    VARCHAR(255) NULL,
    matched_case_id     INT NULL CONSTRAINT FK_ingestion_case REFERENCES dbo.cases(id),
    match_method        VARCHAR(80) NULL,
    stored_filename     VARCHAR(512) NULL,
    imported_by         VARCHAR(255) NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_ingestion_created DEFAULT (SYSUTCDATETIME()),
    started_at          DATETIME2 NULL,
    completed_at        DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.reconciliation_items', 'U') IS NULL
CREATE TABLE dbo.reconciliation_items (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_reconciliation_items PRIMARY KEY,
    ingestion_job_id    INT NULL CONSTRAINT FK_reconciliation_job REFERENCES dbo.ingestion_jobs(id) ON DELETE SET NULL,
    proposed_case_id    VARCHAR(255) NULL,
    reason              NVARCHAR(MAX) NOT NULL,
    status              VARCHAR(30) NOT NULL CONSTRAINT DF_reconciliation_status DEFAULT ('Open'),
    resolved_case_id    INT NULL CONSTRAINT FK_reconciliation_case REFERENCES dbo.cases(id),
    resolved_by         VARCHAR(255) NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_reconciliation_created DEFAULT (SYSUTCDATETIME()),
    resolved_at         DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.scanner_compatibility', 'U') IS NULL
CREATE TABLE dbo.scanner_compatibility (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_scanner_compatibility PRIMARY KEY,
    manufacturer        VARCHAR(120) NOT NULL,
    model               VARCHAR(120) NOT NULL,
    software_version    VARCHAR(120) NULL,
    format              VARCHAR(80) NOT NULL,
    import_method       VARCHAR(120) NOT NULL,
    metadata_coverage   NVARCHAR(MAX) NULL,
    limitations         NVARCHAR(MAX) NULL,
    validation_status   VARCHAR(30) NOT NULL CONSTRAINT DF_scanner_validation DEFAULT ('Unverified'),
    tested_at           DATETIME2 NULL
);
GO

-- ------------------------------------------------------------------------------
-- 7. AI INFERENCE & ANALYSIS (HoVer-Net FAST)
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.ai_models', 'U') IS NULL
CREATE TABLE dbo.ai_models (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_models PRIMARY KEY,
    name                VARCHAR(255) NOT NULL,
    version             VARCHAR(120) NOT NULL,
    checksum            VARCHAR(128) NOT NULL,
    task                VARCHAR(255) NOT NULL,
    intended_use        NVARCHAR(MAX) NULL,
    compatible_inputs   NVARCHAR(MAX) NULL,
    limitations         NVARCHAR(MAX) NULL,
    approval_status     VARCHAR(30) NOT NULL CONSTRAINT DF_ai_models_approval DEFAULT ('Research'),
    adapter_name        VARCHAR(120) NOT NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_ai_models_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_ai_models_name_version UNIQUE (name, version)
);
GO

IF OBJECT_ID('dbo.ai_jobs', 'U') IS NULL
CREATE TABLE dbo.ai_jobs (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_jobs PRIMARY KEY,
    correlation_id      VARCHAR(64) NOT NULL CONSTRAINT UQ_ai_jobs_correlation UNIQUE,
    model_id            INT NOT NULL CONSTRAINT FK_ai_jobs_model REFERENCES dbo.ai_models(id),
    slide_id            INT NOT NULL CONSTRAINT FK_ai_jobs_slide REFERENCES dbo.slides(id),
    image_version_id    INT NULL CONSTRAINT FK_ai_jobs_image_version REFERENCES dbo.image_versions(id),
    status              VARCHAR(30) NOT NULL,
    progress            INT NOT NULL CONSTRAINT DF_ai_jobs_progress DEFAULT (0),
    parameters_json     NVARCHAR(MAX) NULL,
    environment_json    NVARCHAR(MAX) NULL,
    error_detail        NVARCHAR(MAX) NULL,
    requested_by        VARCHAR(255) NOT NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_ai_jobs_created DEFAULT (SYSUTCDATETIME()),
    started_at          DATETIME2 NULL,
    completed_at        DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.ai_analyses', 'U') IS NULL
CREATE TABLE dbo.ai_analyses (
    id                      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_analyses PRIMARY KEY,
    slide_id                INT NOT NULL CONSTRAINT FK_ai_analyses_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
    case_id                 INT NOT NULL CONSTRAINT FK_ai_analyses_case REFERENCES dbo.cases(id),
    requested_by            VARCHAR(255) NOT NULL,
    model_name              VARCHAR(255) NOT NULL CONSTRAINT DF_ai_analyses_model_name DEFAULT ('HoVer-Net FAST'),
    model_version           VARCHAR(120) NOT NULL CONSTRAINT DF_ai_analyses_model_version DEFAULT ('PanNuke-Fold1'),
    status                  VARCHAR(30) NOT NULL CONSTRAINT DF_ai_analyses_status DEFAULT ('Completed'),
    roi_x                   INT NOT NULL,
    roi_y                   INT NOT NULL,
    roi_width               INT NOT NULL,
    roi_height              INT NOT NULL,
    roi_level               INT NOT NULL CONSTRAINT DF_ai_analyses_level DEFAULT (0),
    total_nuclei            INT NOT NULL CONSTRAINT DF_ai_analyses_total DEFAULT (0),
    neoplastic_count        INT NOT NULL CONSTRAINT DF_ai_analyses_neopla DEFAULT (0),
    inflammatory_count      INT NOT NULL CONSTRAINT DF_ai_analyses_inflam DEFAULT (0),
    connective_count        INT NOT NULL CONSTRAINT DF_ai_analyses_connec DEFAULT (0),
    necrotic_count          INT NOT NULL CONSTRAINT DF_ai_analyses_necros DEFAULT (0),
    non_neoplastic_count    INT NOT NULL CONSTRAINT DF_ai_analyses_noneo DEFAULT (0),
    no_label_count          INT NOT NULL CONSTRAINT DF_ai_analyses_nolabe DEFAULT (0),
    nuclear_density         FLOAT NULL,
    avg_confidence          FLOAT NULL,
    summary_json            NVARCHAR(MAX) NULL,
    predictions_json        NVARCHAR(MAX) NULL,
    gallery_json            NVARCHAR(MAX) NULL,
    roi_image_filename      VARCHAR(255) NULL,
    overlay_image_filename  VARCHAR(255) NULL,
    report_pdf_filename     VARCHAR(255) NULL,
    error_detail            NVARCHAR(MAX) NULL,
    disclaimer              VARCHAR(500) NOT NULL CONSTRAINT DF_ai_analyses_disclaimer DEFAULT ('Research Use Only — Not for Clinical Diagnosis'),
    created_at              DATETIME2 NOT NULL CONSTRAINT DF_ai_analyses_created DEFAULT (SYSUTCDATETIME()),
    completed_at            DATETIME2 NULL
);
GO

-- ------------------------------------------------------------------------------
-- 8. SYSTEM CONFIGURATION & EXTERNAL INTEGRATIONS
-- ------------------------------------------------------------------------------

IF OBJECT_ID('dbo.system_settings', 'U') IS NULL
CREATE TABLE dbo.system_settings (
    setting_key     VARCHAR(255) NOT NULL CONSTRAINT PK_system_settings PRIMARY KEY,
    setting_value   NVARCHAR(MAX) NULL,
    updated_by      VARCHAR(255) NULL,
    updated_at      DATETIME2 NOT NULL CONSTRAINT DF_system_settings_updated DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.hl7_channels', 'U') IS NULL
CREATE TABLE dbo.hl7_channels (
    id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_hl7_channels PRIMARY KEY,
    name            VARCHAR(255) NOT NULL CONSTRAINT UQ_hl7_channels_name UNIQUE,
    direction       VARCHAR(20) NOT NULL,
    version         VARCHAR(20) NOT NULL CONSTRAINT DF_hl7_channels_version DEFAULT ('2.5.1'),
    transport       VARCHAR(30) NOT NULL CONSTRAINT DF_hl7_channels_transport DEFAULT ('MLLP'),
    host            VARCHAR(255) NULL,
    port            INT NULL,
    enabled         BIT NOT NULL CONSTRAINT DF_hl7_channels_enabled DEFAULT (0),
    mapping_json    NVARCHAR(MAX) NULL,
    created_at      DATETIME2 NOT NULL CONSTRAINT DF_hl7_channels_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.integration_events', 'U') IS NULL
CREATE TABLE dbo.integration_events (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_integration_events PRIMARY KEY,
    channel_id          INT NULL CONSTRAINT FK_integration_events_channel REFERENCES dbo.hl7_channels(id) ON DELETE SET NULL,
    correlation_id      VARCHAR(64) NOT NULL CONSTRAINT UQ_integration_events_correlation UNIQUE,
    message_control_id  VARCHAR(255) NULL,
    message_type        VARCHAR(40) NULL,
    direction           VARCHAR(20) NOT NULL,
    raw_payload         NVARCHAR(MAX) NOT NULL,
    normalized_json     NVARCHAR(MAX) NULL,
    status              VARCHAR(30) NOT NULL,
    acknowledgement     NVARCHAR(MAX) NULL,
    attempts            INT NOT NULL CONSTRAINT DF_integration_attempts DEFAULT (0),
    error_detail        NVARCHAR(MAX) NULL,
    created_at          DATETIME2 NOT NULL CONSTRAINT DF_integration_created DEFAULT (SYSUTCDATETIME()),
    processed_at        DATETIME2 NULL
);
GO

-- ------------------------------------------------------------------------------
-- 9. PERFORMANCE INDEXES
-- ------------------------------------------------------------------------------

-- Users & Auditing
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_audit_log_created_at' AND object_id=OBJECT_ID('dbo.audit_log'))
    CREATE INDEX idx_audit_log_created_at ON dbo.audit_log(created_at DESC);

-- Clinical Hierarchy & Cases
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_external' AND object_id=OBJECT_ID('dbo.patients'))
    CREATE UNIQUE INDEX UQ_patients_external ON dbo.patients(external_id) WHERE external_id IS NOT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_accessions_case_identifiers' AND object_id=OBJECT_ID('dbo.accessions'))
    CREATE INDEX idx_accessions_case_identifiers ON dbo.accessions(case_id, accession_number, order_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_accessions_patient' AND object_id=OBJECT_ID('dbo.accessions'))
    CREATE INDEX idx_accessions_patient ON dbo.accessions(patient_id, case_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_specimens_accession' AND object_id=OBJECT_ID('dbo.specimens'))
    CREATE INDEX idx_specimens_accession ON dbo.specimens(accession_id, specimen_type);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_blocks_specimen' AND object_id=OBJECT_ID('dbo.blocks'))
    CREATE INDEX idx_blocks_specimen ON dbo.blocks(specimen_id, block_code);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_links_case' AND object_id=OBJECT_ID('dbo.case_links'))
    CREATE INDEX idx_case_links_case ON dbo.case_links(case_id, related_case_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_review_notes_case_id' AND object_id=OBJECT_ID('dbo.case_review_notes'))
    CREATE INDEX idx_case_review_notes_case_id ON dbo.case_review_notes(case_id, id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_review_history_case_id' AND object_id=OBJECT_ID('dbo.case_review_history'))
    CREATE INDEX idx_case_review_history_case_id ON dbo.case_review_history(case_id, id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_consultations_case' AND object_id=OBJECT_ID('dbo.case_consultations'))
    CREATE INDEX idx_consultations_case ON dbo.case_consultations(case_id, status);

-- Slides & QC
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_slides_case_id' AND object_id=OBJECT_ID('dbo.slides'))
    CREATE INDEX idx_slides_case_id ON dbo.slides(case_id, id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_slide_qc_history_slide_id' AND object_id=OBJECT_ID('dbo.slide_qc_history'))
    CREATE INDEX idx_slide_qc_history_slide_id ON dbo.slide_qc_history(slide_id, id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_viewer_bookmarks_slide' AND object_id=OBJECT_ID('dbo.viewer_bookmarks'))
    CREATE INDEX idx_viewer_bookmarks_slide ON dbo.viewer_bookmarks(slide_id, id DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_viewer_preferences_user' AND object_id=OBJECT_ID('dbo.viewer_preferences'))
    CREATE INDEX idx_viewer_preferences_user ON dbo.viewer_preferences(username, updated_at DESC);

-- Annotations
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id=OBJECT_ID('dbo.annotations'))
    CREATE INDEX idx_annotations_slide_id ON dbo.annotations(slide_id, id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_annotation_revisions_annotation' AND object_id=OBJECT_ID('dbo.annotation_revisions'))
    CREATE INDEX idx_annotation_revisions_annotation ON dbo.annotation_revisions(annotation_id, revision_number DESC);

-- Reports
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_reports_case_id' AND object_id=OBJECT_ID('dbo.reports'))
    CREATE INDEX idx_reports_case_id ON dbo.reports(case_id, version DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_report_findings_report_id' AND object_id=OBJECT_ID('dbo.report_findings'))
    CREATE INDEX idx_report_findings_report_id ON dbo.report_findings(report_id, sort_order);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_report_reviews_report' AND object_id=OBJECT_ID('dbo.report_reviews'))
    CREATE INDEX idx_report_reviews_report ON dbo.report_reviews(report_id, id);

-- Ingestion & AI
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ingestion_checksum' AND object_id=OBJECT_ID('dbo.ingestion_jobs'))
    CREATE INDEX idx_ingestion_checksum ON dbo.ingestion_jobs(checksum_sha256, status);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_jobs_status' AND object_id=OBJECT_ID('dbo.ai_jobs'))
    CREATE INDEX idx_ai_jobs_status ON dbo.ai_jobs(status, created_at);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_analyses_slide_id' AND object_id=OBJECT_ID('dbo.ai_analyses'))
    CREATE INDEX idx_ai_analyses_slide_id ON dbo.ai_analyses(slide_id, id DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_analyses_case_id' AND object_id=OBJECT_ID('dbo.ai_analyses'))
    CREATE INDEX idx_ai_analyses_case_id ON dbo.ai_analyses(case_id, id DESC);

-- Integrations
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_integration_events_status' AND object_id=OBJECT_ID('dbo.integration_events'))
    CREATE INDEX idx_integration_events_status ON dbo.integration_events(status, created_at);
GO
