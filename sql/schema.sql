/*
 Digital Pathology SQL Server schema
 -----------------------------------
 Run this script while connected to the database selected for the application.
 It does not create, select, drop, or clear a database, and it is safe to rerun.
 Application startup also performs idempotent schema initialization.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.slides','U') IS NOT NULL AND COL_LENGTH('dbo.slides', 'qc_unusual_staining') IS NULL
    ALTER TABLE dbo.slides ADD qc_unusual_staining INT NULL CONSTRAINT DF_slides_qc_staining_existing DEFAULT (0);
IF OBJECT_ID('dbo.slide_qc_history','U') IS NOT NULL AND COL_LENGTH('dbo.slide_qc_history', 'qc_unusual_staining') IS NULL
    ALTER TABLE dbo.slide_qc_history ADD qc_unusual_staining BIT NULL;
GO

IF OBJECT_ID('dbo.users', 'U') IS NULL
CREATE TABLE dbo.users (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_users PRIMARY KEY,
    username VARCHAR(255) NOT NULL CONSTRAINT UQ_users_username UNIQUE,
    display_name VARCHAR(MAX) NOT NULL,
    role VARCHAR(MAX) NOT NULL,
    password_hash VARCHAR(MAX) NOT NULL,
    is_active INT NOT NULL CONSTRAINT DF_users_active DEFAULT (1),
    created_at VARCHAR(MAX) NULL CONSTRAINT DF_users_created DEFAULT (CURRENT_TIMESTAMP),
    last_login DATETIME NULL
);
GO

-- Idempotent migration to the approved three-role catalogue.
UPDATE dbo.users SET role = 'System Administrator' WHERE role = 'Admin';
UPDATE dbo.users SET role = 'Laboratory Manager' WHERE role IN ('Technician', 'Histotechnologist');
UPDATE dbo.users SET role = 'Pathologist' WHERE role = 'AI Analyst';
-- Ambiguous retired read-only/integration accounts are disabled for explicit administrator review.
UPDATE dbo.users SET role = 'Laboratory Manager', is_active = 0 WHERE role IN ('Viewer', 'Auditor', 'Integration Engineer');
IF NOT EXISTS (SELECT 1 FROM dbo.users WHERE LOWER(username) = 'etdadmin')
    UPDATE dbo.users SET username = 'ETDadmin' WHERE LOWER(username) = 'eidadmin';
UPDATE dbo.users SET role = 'System Administrator', is_active = 1 WHERE LOWER(username) IN ('etddev', 'etdadmin');
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_users_super_admin_reserved')
    ALTER TABLE dbo.users DROP CONSTRAINT CK_users_super_admin_reserved;
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_users_approved_roles')
    ALTER TABLE dbo.users ADD CONSTRAINT CK_users_approved_roles
        CHECK (role IN ('System Administrator', 'Laboratory Manager', 'Pathologist'));
GO

IF OBJECT_ID('dbo.cases', 'U') IS NULL
CREATE TABLE dbo.cases (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_cases PRIMARY KEY,
    case_id VARCHAR(255) NOT NULL CONSTRAINT UQ_cases_case_id UNIQUE,
    description VARCHAR(MAX) NOT NULL,
    filename VARCHAR(255) NOT NULL CONSTRAINT DF_cases_filename DEFAULT (''),
    created_at DATETIME NOT NULL CONSTRAINT DF_cases_created DEFAULT (CURRENT_TIMESTAMP),
    file_type VARCHAR(MAX) NOT NULL CONSTRAINT DF_cases_file_type DEFAULT ('raster'),
    format VARCHAR(MAX) NULL,
    width INT NULL,
    height INT NULL,
    file_size INT NULL,
    status VARCHAR(MAX) NOT NULL CONSTRAINT DF_cases_status DEFAULT ('New'),
    reviewer VARCHAR(MAX) NOT NULL CONSTRAINT DF_cases_reviewer DEFAULT ('')
);
GO

IF OBJECT_ID('dbo.slides', 'U') IS NULL
CREATE TABLE dbo.slides (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slides PRIMARY KEY,
    case_id INT NOT NULL CONSTRAINT FK_slides_cases REFERENCES dbo.cases(id),
    label VARCHAR(MAX) NOT NULL,
    stain VARCHAR(MAX) NOT NULL CONSTRAINT DF_slides_stain DEFAULT (''),
    specimen_description VARCHAR(MAX) NOT NULL CONSTRAINT DF_slides_specimen DEFAULT (''),
    filename VARCHAR(255) NOT NULL CONSTRAINT UQ_slides_filename UNIQUE,
    file_type VARCHAR(MAX) NOT NULL CONSTRAINT CK_slides_file_type CHECK (file_type IN ('raster','wsi')),
    format VARCHAR(MAX) NOT NULL,
    file_size INT NOT NULL,
    width INT NOT NULL,
    height INT NOT NULL,
    level_count INT NOT NULL CONSTRAINT DF_slides_levels DEFAULT (1),
    thumbnail VARBINARY(MAX) NULL,
    upload_state VARCHAR(MAX) NOT NULL CONSTRAINT DF_slides_upload_state DEFAULT ('uploading'),
    created_at DATETIME NOT NULL CONSTRAINT DF_slides_created DEFAULT (CURRENT_TIMESTAMP),
    qc_status VARCHAR(MAX) NULL,
    qc_missing_tissue INT NULL CONSTRAINT DF_slides_qc_missing DEFAULT (0),
    qc_blur INT NULL CONSTRAINT DF_slides_qc_blur DEFAULT (0),
    qc_scan_artifact INT NULL CONSTRAINT DF_slides_qc_artifact DEFAULT (0),
    qc_incorrect_label INT NULL CONSTRAINT DF_slides_qc_label DEFAULT (0),
    qc_unusual_staining INT NULL CONSTRAINT DF_slides_qc_staining DEFAULT (0),
    qc_note VARCHAR(MAX) NULL CONSTRAINT DF_slides_qc_note DEFAULT (''),
    qc_actor VARCHAR(MAX) NULL CONSTRAINT DF_slides_qc_actor DEFAULT (''),
    qc_timestamp DATETIME NULL,
    CONSTRAINT CK_slides_upload_state CHECK (upload_state IN ('uploading','processing','ready','error'))
);
GO

IF OBJECT_ID('dbo.slide_qc_history', 'U') IS NULL
CREATE TABLE dbo.slide_qc_history (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slide_qc_history PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_slide_qc_history_slide REFERENCES dbo.slides(id),
    qc_status VARCHAR(MAX) NULL,
    qc_missing_tissue BIT NULL,
    qc_blur BIT NULL,
    qc_scan_artifact BIT NULL,
    qc_incorrect_label BIT NULL,
    qc_unusual_staining BIT NULL,
    qc_note VARCHAR(MAX) NULL,
    actor VARCHAR(MAX) NOT NULL,
    created_at VARCHAR(MAX) NULL CONSTRAINT DF_slide_qc_history_created DEFAULT (CURRENT_TIMESTAMP)
);
GO

IF COL_LENGTH('dbo.slides', 'qc_unusual_staining') IS NULL
    ALTER TABLE dbo.slides ADD qc_unusual_staining INT NULL CONSTRAINT DF_slides_qc_staining_existing DEFAULT (0);
IF COL_LENGTH('dbo.slide_qc_history', 'qc_unusual_staining') IS NULL
    ALTER TABLE dbo.slide_qc_history ADD qc_unusual_staining BIT NULL;
GO

IF OBJECT_ID('dbo.annotations', 'U') IS NULL
CREATE TABLE dbo.annotations (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_annotations PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_annotations_slide REFERENCES dbo.slides(id),
    annotation_type VARCHAR(MAX) NOT NULL CONSTRAINT DF_annotations_type DEFAULT ('rectangle'),
    label VARCHAR(MAX) NOT NULL,
    note VARCHAR(MAX) NOT NULL CONSTRAINT DF_annotations_note DEFAULT (''),
    color VARCHAR(MAX) NOT NULL CONSTRAINT DF_annotations_color DEFAULT ('#e85d75'),
    x REAL NULL, y REAL NULL, width REAL NULL, height REAL NULL,
    points VARCHAR(MAX) NULL,
    created_at DATETIME NOT NULL CONSTRAINT DF_annotations_created DEFAULT (CURRENT_TIMESTAMP),
    updated_at VARCHAR(MAX) NOT NULL CONSTRAINT DF_annotations_updated DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT CK_annotations_type CHECK (annotation_type IN ('point','line','polygon','rectangle','ellipse','freehand','arrow'))
);
GO

IF OBJECT_ID('dbo.case_review_notes', 'U') IS NULL
CREATE TABLE dbo.case_review_notes (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_review_notes PRIMARY KEY,
    case_id INT NOT NULL CONSTRAINT FK_case_review_notes_case REFERENCES dbo.cases(id),
    author VARCHAR(MAX) NOT NULL,
    note VARCHAR(MAX) NOT NULL,
    created_at DATETIME NOT NULL CONSTRAINT DF_case_review_notes_created DEFAULT (CURRENT_TIMESTAMP)
);
GO

IF OBJECT_ID('dbo.case_review_history', 'U') IS NULL
CREATE TABLE dbo.case_review_history (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_review_history PRIMARY KEY,
    case_id INT NOT NULL CONSTRAINT FK_case_review_history_case REFERENCES dbo.cases(id),
    change_type VARCHAR(MAX) NOT NULL,
    previous_value VARCHAR(MAX) NOT NULL,
    new_value VARCHAR(MAX) NOT NULL,
    actor VARCHAR(MAX) NOT NULL,
    created_at DATETIME NOT NULL CONSTRAINT DF_case_review_history_created DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT CK_case_review_history_type CHECK (change_type IN ('status','reviewer'))
);
GO

IF OBJECT_ID('dbo.reports', 'U') IS NULL
CREATE TABLE dbo.reports (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_reports PRIMARY KEY,
    case_id INT NOT NULL CONSTRAINT FK_reports_case REFERENCES dbo.cases(id),
    version INT NOT NULL,
    status VARCHAR(MAX) NOT NULL,
    specimen_clinical VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_specimen DEFAULT (''),
    gross_description VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_gross DEFAULT (''),
    microscopic_findings VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_micro DEFAULT (''),
    diagnosis VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_diagnosis DEFAULT (''),
    comments VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_comments DEFAULT (''),
    reviewer_name VARCHAR(MAX) NOT NULL CONSTRAINT DF_reports_reviewer DEFAULT (''),
    created_at DATETIME NOT NULL CONSTRAINT DF_reports_created DEFAULT (CURRENT_TIMESTAMP),
    finalized_at VARCHAR(MAX) NULL,
    CONSTRAINT CK_reports_status CHECK (status IN ('Draft','Finalized'))
);
GO

IF OBJECT_ID('dbo.audit_log', 'U') IS NULL
CREATE TABLE dbo.audit_log (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_audit_log PRIMARY KEY,
    actor_username VARCHAR(MAX) NOT NULL,
    action VARCHAR(MAX) NOT NULL,
    target_type VARCHAR(MAX) NOT NULL,
    target_id VARCHAR(MAX) NOT NULL CONSTRAINT DF_audit_target DEFAULT (''),
    details VARCHAR(MAX) NOT NULL CONSTRAINT DF_audit_details DEFAULT (''),
    ip_address VARCHAR(45) NULL,
    device_name VARCHAR(255) NULL,
    device_location VARCHAR(255) NULL,
    user_agent VARCHAR(1000) NULL,
    created_at DATETIME NOT NULL CONSTRAINT DF_audit_created DEFAULT (CURRENT_TIMESTAMP)
);
GO
IF COL_LENGTH('dbo.audit_log','ip_address') IS NULL ALTER TABLE dbo.audit_log ADD ip_address VARCHAR(45) NULL;
IF COL_LENGTH('dbo.audit_log','device_name') IS NULL ALTER TABLE dbo.audit_log ADD device_name VARCHAR(255) NULL;
IF COL_LENGTH('dbo.audit_log','device_location') IS NULL ALTER TABLE dbo.audit_log ADD device_location VARCHAR(255) NULL;
IF COL_LENGTH('dbo.audit_log','user_agent') IS NULL ALTER TABLE dbo.audit_log ADD user_agent VARCHAR(1000) NULL;
GO

IF OBJECT_ID('dbo.report_findings', 'U') IS NULL
CREATE TABLE dbo.report_findings (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_report_findings PRIMARY KEY,
    report_id INT NOT NULL CONSTRAINT FK_report_findings_report REFERENCES dbo.reports(id),
    slide_id INT NULL CONSTRAINT FK_report_findings_slide REFERENCES dbo.slides(id) ON DELETE SET NULL,
    annotation_id INT NULL CONSTRAINT FK_report_findings_annotation REFERENCES dbo.annotations(id) ON DELETE SET NULL,
    text VARCHAR(MAX) NOT NULL CONSTRAINT DF_report_findings_text DEFAULT (''),
    sort_order INT NOT NULL CONSTRAINT DF_report_findings_order DEFAULT (0)
);
GO

IF OBJECT_ID('dbo.patients', 'U') IS NULL
CREATE TABLE dbo.patients (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patients PRIMARY KEY,
    external_id VARCHAR(255) NULL CONSTRAINT UQ_patients_external UNIQUE,
    display_label VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patients_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.accessions', 'U') IS NULL
CREATE TABLE dbo.accessions (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_accessions PRIMARY KEY,
    case_id INT NULL CONSTRAINT FK_accessions_case REFERENCES dbo.cases(id),
    patient_id INT NULL CONSTRAINT FK_accessions_patient REFERENCES dbo.patients(id),
    accession_number VARCHAR(255) NOT NULL CONSTRAINT UQ_accessions_number UNIQUE,
    order_id VARCHAR(255) NULL,
    priority VARCHAR(30) NOT NULL CONSTRAINT DF_accessions_priority DEFAULT ('Routine'),
    due_at DATETIME2 NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_accessions_created DEFAULT (SYSUTCDATETIME())
);
GO

IF COL_LENGTH('dbo.accessions', 'case_id') IS NULL
    ALTER TABLE dbo.accessions ADD case_id INT NULL CONSTRAINT FK_accessions_case_existing REFERENCES dbo.cases(id);
GO

IF OBJECT_ID('dbo.specimens', 'U') IS NULL
CREATE TABLE dbo.specimens (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_specimens PRIMARY KEY,
    accession_id INT NOT NULL CONSTRAINT FK_specimens_accession REFERENCES dbo.accessions(id),
    specimen_code VARCHAR(255) NOT NULL,
    specimen_type VARCHAR(255) NULL,
    description VARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_specimens_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.blocks', 'U') IS NULL
CREATE TABLE dbo.blocks (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_blocks PRIMARY KEY,
    specimen_id INT NOT NULL CONSTRAINT FK_blocks_specimen REFERENCES dbo.specimens(id),
    block_code VARCHAR(255) NOT NULL,
    description VARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_blocks_created DEFAULT (SYSUTCDATETIME())
);
GO

IF COL_LENGTH('dbo.cases', 'priority') IS NULL ALTER TABLE dbo.cases ADD priority VARCHAR(30) NOT NULL CONSTRAINT DF_cases_priority_existing DEFAULT ('Routine');
IF COL_LENGTH('dbo.cases', 'due_at') IS NULL ALTER TABLE dbo.cases ADD due_at DATETIME2 NULL;
IF COL_LENGTH('dbo.cases', 'exception_reason') IS NULL ALTER TABLE dbo.cases ADD exception_reason VARCHAR(2000) NULL;
IF COL_LENGTH('dbo.slides', 'block_id') IS NULL ALTER TABLE dbo.slides ADD block_id INT NULL CONSTRAINT FK_slides_block REFERENCES dbo.blocks(id);
IF COL_LENGTH('dbo.slides', 'slide_identifier') IS NULL ALTER TABLE dbo.slides ADD slide_identifier VARCHAR(255) NULL;
IF COL_LENGTH('dbo.slides', 'scanner_name') IS NULL ALTER TABLE dbo.slides ADD scanner_name VARCHAR(255) NULL;
GO

IF OBJECT_ID('dbo.case_links', 'U') IS NULL
CREATE TABLE dbo.case_links (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_links PRIMARY KEY,
    case_id INT NOT NULL CONSTRAINT FK_case_links_case REFERENCES dbo.cases(id),
    related_case_id INT NOT NULL CONSTRAINT FK_case_links_related REFERENCES dbo.cases(id),
    link_type VARCHAR(30) NOT NULL, reason VARCHAR(2000) NULL, created_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_case_links_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_case_links UNIQUE(case_id, related_case_id, link_type)
);
GO

IF OBJECT_ID('dbo.research_folders', 'U') IS NULL
CREATE TABLE dbo.research_folders (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_research_folders PRIMARY KEY,
    name VARCHAR(255) NOT NULL, description VARCHAR(2000) NULL, created_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_research_folders_created DEFAULT (SYSUTCDATETIME())
);
IF OBJECT_ID('dbo.research_folder_members', 'U') IS NULL
CREATE TABLE dbo.research_folder_members (
    folder_id INT NOT NULL CONSTRAINT FK_research_members_folder REFERENCES dbo.research_folders(id),
    case_id INT NOT NULL CONSTRAINT FK_research_members_case REFERENCES dbo.cases(id), added_by VARCHAR(255) NOT NULL,
    added_at DATETIME2 NOT NULL CONSTRAINT DF_research_members_added DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_research_folder_members PRIMARY KEY(folder_id, case_id)
);
IF OBJECT_ID('dbo.research_folder_access', 'U') IS NULL
CREATE TABLE dbo.research_folder_access (
    folder_id INT NOT NULL CONSTRAINT FK_research_access_folder REFERENCES dbo.research_folders(id),
    role_name VARCHAR(80) NOT NULL, granted_by VARCHAR(255) NOT NULL,
    granted_at DATETIME2 NOT NULL CONSTRAINT DF_research_access_granted DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_research_folder_access PRIMARY KEY(folder_id, role_name)
);
GO

IF OBJECT_ID('dbo.case_operations', 'U') IS NULL
CREATE TABLE dbo.case_operations (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_case_operations PRIMARY KEY,
    operation_type VARCHAR(30) NOT NULL, source_case_id INT NOT NULL CONSTRAINT FK_case_operations_source REFERENCES dbo.cases(id),
    target_case_id INT NULL CONSTRAINT FK_case_operations_target REFERENCES dbo.cases(id), details_json VARCHAR(MAX) NOT NULL,
    authorized_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL CONSTRAINT DF_case_operations_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.viewer_bookmarks', 'U') IS NULL
CREATE TABLE dbo.viewer_bookmarks (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_viewer_bookmarks PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_viewer_bookmarks_slide REFERENCES dbo.slides(id), name VARCHAR(120) NOT NULL,
    viewport_x FLOAT NOT NULL, viewport_y FLOAT NOT NULL, viewport_width FLOAT NOT NULL, viewport_height FLOAT NOT NULL,
    rotation FLOAT NOT NULL CONSTRAINT DF_viewer_bookmarks_rotation DEFAULT (0), created_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_viewer_bookmarks_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.viewer_preferences', 'U') IS NULL
CREATE TABLE dbo.viewer_preferences (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_viewer_preferences PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_viewer_preferences_slide REFERENCES dbo.slides(id),
    username VARCHAR(255) NOT NULL, settings_json VARCHAR(MAX) NOT NULL,
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_viewer_preferences_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_viewer_preferences UNIQUE(slide_id,username),
    CONSTRAINT CK_viewer_preferences_json CHECK (ISJSON(settings_json)=1)
);
GO

IF COL_LENGTH('dbo.reports', 'workflow_status') IS NULL ALTER TABLE dbo.reports ADD workflow_status VARCHAR(30) NOT NULL CONSTRAINT DF_reports_workflow_existing DEFAULT ('Draft'), approved_by VARCHAR(255) NULL, approved_at DATETIME2 NULL, review_comment VARCHAR(4000) NULL, template_id INT NULL;
GO
IF OBJECT_ID('dbo.report_templates','U') IS NULL CREATE TABLE dbo.report_templates (id INT IDENTITY PRIMARY KEY,name VARCHAR(255) UNIQUE NOT NULL,sections_json VARCHAR(MAX) NOT NULL,active BIT NOT NULL DEFAULT 1,created_by VARCHAR(255) NOT NULL,created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());
IF OBJECT_ID('dbo.report_reviews','U') IS NULL CREATE TABLE dbo.report_reviews (id INT IDENTITY PRIMARY KEY,report_id INT NOT NULL REFERENCES dbo.reports(id),previous_status VARCHAR(30),new_status VARCHAR(30) NOT NULL,comment VARCHAR(4000),actor VARCHAR(255) NOT NULL,created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());
IF OBJECT_ID('dbo.case_consultations','U') IS NULL CREATE TABLE dbo.case_consultations (id INT IDENTITY PRIMARY KEY,case_id INT NOT NULL REFERENCES dbo.cases(id),requested_by VARCHAR(255) NOT NULL,consultant_username VARCHAR(255) NOT NULL,message VARCHAR(4000),status VARCHAR(30) NOT NULL DEFAULT 'Open',created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),completed_at DATETIME2 NULL);
GO

IF OBJECT_ID('dbo.ingestion_jobs', 'U') IS NULL
CREATE TABLE dbo.ingestion_jobs (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ingestion_jobs PRIMARY KEY,
    correlation_id VARCHAR(64) NOT NULL CONSTRAINT UQ_ingestion_correlation UNIQUE,
    source_path VARCHAR(2048) NULL,
    source_device VARCHAR(255) NULL,
    adapter_name VARCHAR(120) NOT NULL CONSTRAINT DF_ingestion_adapter DEFAULT ('manual'),
    original_filename VARCHAR(512) NOT NULL,
    checksum_sha256 VARCHAR(64) NULL,
    file_size BIGINT NULL,
    status VARCHAR(30) NOT NULL,
    attempts INT NOT NULL CONSTRAINT DF_ingestion_attempts DEFAULT (0),
    progress INT NOT NULL CONSTRAINT DF_ingestion_progress DEFAULT (0),
    error_detail VARCHAR(MAX) NULL,
    quarantine_path VARCHAR(2048) NULL,
    validation_json VARCHAR(MAX) NULL,
    match_identifier VARCHAR(255) NULL,
    matched_case_id INT NULL CONSTRAINT FK_ingestion_case REFERENCES dbo.cases(id),
    match_method VARCHAR(80) NULL,
    stored_filename VARCHAR(512) NULL,
    imported_by VARCHAR(255) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_ingestion_created DEFAULT (SYSUTCDATETIME()),
    started_at DATETIME2 NULL,
    completed_at DATETIME2 NULL
);
GO

IF COL_LENGTH('dbo.ingestion_jobs', 'validation_json') IS NULL
    ALTER TABLE dbo.ingestion_jobs ADD validation_json VARCHAR(MAX) NULL;
GO
IF COL_LENGTH('dbo.ingestion_jobs', 'match_identifier') IS NULL
    ALTER TABLE dbo.ingestion_jobs ADD match_identifier VARCHAR(255) NULL, matched_case_id INT NULL CONSTRAINT FK_ingestion_case_existing REFERENCES dbo.cases(id), match_method VARCHAR(80) NULL, stored_filename VARCHAR(512) NULL, imported_by VARCHAR(255) NULL;
GO

IF OBJECT_ID('dbo.image_versions', 'U') IS NULL
CREATE TABLE dbo.image_versions (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_image_versions PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_image_versions_slide REFERENCES dbo.slides(id),
    version INT NOT NULL,
    checksum_sha256 VARCHAR(64) NOT NULL,
    source_path VARCHAR(2048) NULL,
    source_device VARCHAR(255) NULL,
    scanner_make VARCHAR(120) NULL,
    scanner_model VARCHAR(120) NULL,
    magnification FLOAT NULL,
    pixel_size_microns FLOAT NULL,
    acquisition_time DATETIME2 NULL,
    metadata_json VARCHAR(MAX) NULL,
    stored_filename VARCHAR(512) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_image_versions_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_image_versions_slide_version UNIQUE (slide_id, version)
);
GO

IF OBJECT_ID('dbo.reconciliation_items', 'U') IS NULL
CREATE TABLE dbo.reconciliation_items (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_reconciliation_items PRIMARY KEY,
    ingestion_job_id INT NULL CONSTRAINT FK_reconciliation_job REFERENCES dbo.ingestion_jobs(id),
    proposed_case_id VARCHAR(255) NULL,
    reason VARCHAR(MAX) NOT NULL,
    status VARCHAR(30) NOT NULL CONSTRAINT DF_reconciliation_status DEFAULT ('Open'),
    resolved_case_id INT NULL CONSTRAINT FK_reconciliation_case REFERENCES dbo.cases(id),
    resolved_by VARCHAR(255) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_reconciliation_created DEFAULT (SYSUTCDATETIME()),
    resolved_at DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.image_metadata_revisions', 'U') IS NULL
CREATE TABLE dbo.image_metadata_revisions (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_image_metadata_revisions PRIMARY KEY,
    image_version_id INT NOT NULL CONSTRAINT FK_metadata_revision_version REFERENCES dbo.image_versions(id),
    field_name VARCHAR(120) NOT NULL,
    original_value VARCHAR(MAX) NULL,
    corrected_value VARCHAR(MAX) NULL,
    editor VARCHAR(255) NOT NULL,
    reason VARCHAR(2000) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_metadata_revision_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.scanner_compatibility', 'U') IS NULL
CREATE TABLE dbo.scanner_compatibility (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_scanner_compatibility PRIMARY KEY,
    manufacturer VARCHAR(120) NOT NULL,
    model VARCHAR(120) NOT NULL,
    software_version VARCHAR(120) NULL,
    format VARCHAR(80) NOT NULL,
    import_method VARCHAR(120) NOT NULL,
    metadata_coverage VARCHAR(MAX) NULL,
    limitations VARCHAR(MAX) NULL,
    validation_status VARCHAR(30) NOT NULL CONSTRAINT DF_scanner_validation DEFAULT ('Unverified'),
    tested_at DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.ai_models', 'U') IS NULL
CREATE TABLE dbo.ai_models (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_models PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    version VARCHAR(120) NOT NULL,
    checksum VARCHAR(128) NOT NULL,
    task VARCHAR(255) NOT NULL,
    intended_use VARCHAR(MAX) NULL,
    compatible_inputs VARCHAR(MAX) NULL,
    limitations VARCHAR(MAX) NULL,
    approval_status VARCHAR(30) NOT NULL CONSTRAINT DF_ai_models_approval DEFAULT ('Research'),
    adapter_name VARCHAR(120) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_ai_models_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_ai_models_name_version UNIQUE (name, version)
);
GO

IF OBJECT_ID('dbo.ai_jobs', 'U') IS NULL
CREATE TABLE dbo.ai_jobs (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_jobs PRIMARY KEY,
    correlation_id VARCHAR(64) NOT NULL CONSTRAINT UQ_ai_jobs_correlation UNIQUE,
    model_id INT NOT NULL CONSTRAINT FK_ai_jobs_model REFERENCES dbo.ai_models(id),
    slide_id INT NOT NULL CONSTRAINT FK_ai_jobs_slide REFERENCES dbo.slides(id),
    image_version_id INT NULL CONSTRAINT FK_ai_jobs_image_version REFERENCES dbo.image_versions(id),
    status VARCHAR(30) NOT NULL,
    progress INT NOT NULL CONSTRAINT DF_ai_jobs_progress DEFAULT (0),
    parameters_json VARCHAR(MAX) NULL,
    environment_json VARCHAR(MAX) NULL,
    error_detail VARCHAR(MAX) NULL,
    requested_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_ai_jobs_created DEFAULT (SYSUTCDATETIME()),
    started_at DATETIME2 NULL,
    completed_at DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.ai_results', 'U') IS NULL
CREATE TABLE dbo.ai_results (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_results PRIMARY KEY,
    ai_job_id INT NOT NULL CONSTRAINT FK_ai_results_job REFERENCES dbo.ai_jobs(id),
    result_type VARCHAR(80) NOT NULL,
    geometry_json VARCHAR(MAX) NULL,
    measurements_json VARCHAR(MAX) NULL,
    confidence FLOAT NULL,
    raw_output_json VARCHAR(MAX) NULL,
    review_status VARCHAR(30) NOT NULL CONSTRAINT DF_ai_results_review DEFAULT ('Unreviewed'),
    reviewed_by VARCHAR(255) NULL,
    review_note VARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_ai_results_created DEFAULT (SYSUTCDATETIME()),
    reviewed_at DATETIME2 NULL
);
GO

IF COL_LENGTH('dbo.annotations', 'image_version_id') IS NULL ALTER TABLE dbo.annotations ADD image_version_id INT NULL CONSTRAINT FK_annotations_image_version REFERENCES dbo.image_versions(id);
IF COL_LENGTH('dbo.annotations', 'category') IS NULL ALTER TABLE dbo.annotations ADD category VARCHAR(120) NULL, annotation_status VARCHAR(30) NULL, author VARCHAR(255) NULL, source_type VARCHAR(30) NOT NULL CONSTRAINT DF_annotations_source_existing DEFAULT ('Human'), source_ai_result_id INT NULL CONSTRAINT FK_annotations_ai_result REFERENCES dbo.ai_results(id), measurements_json VARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('dbo.annotations') AND definition LIKE '%annotation_type%' AND name<>'CK_annotations_supported_type')
BEGIN
    DECLARE @annotation_check SYSNAME;
    SELECT TOP 1 @annotation_check=name FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('dbo.annotations') AND definition LIKE '%annotation_type%' AND name<>'CK_annotations_supported_type';
    EXEC('ALTER TABLE dbo.annotations DROP CONSTRAINT [' + @annotation_check + ']');
END;
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_annotations_supported_type')
    ALTER TABLE dbo.annotations ADD CONSTRAINT CK_annotations_supported_type CHECK (annotation_type IN ('point','line','polygon','rectangle','ellipse','freehand','arrow'));
GO
IF OBJECT_ID('dbo.annotation_revisions', 'U') IS NULL
CREATE TABLE dbo.annotation_revisions (
    id INT IDENTITY(1,1) PRIMARY KEY, annotation_id INT NOT NULL REFERENCES dbo.annotations(id), revision_number INT NOT NULL,
    geometry_json VARCHAR(MAX) NOT NULL, label VARCHAR(120) NOT NULL, note VARCHAR(2000) NULL, category VARCHAR(120) NULL,
    annotation_status VARCHAR(30) NULL, changed_by VARCHAR(255) NOT NULL, change_reason VARCHAR(2000) NULL,
    created_at DATETIME2 NOT NULL DEFAULT (SYSUTCDATETIME()), CONSTRAINT UQ_annotation_revision UNIQUE(annotation_id,revision_number)
);
GO

IF OBJECT_ID('dbo.hl7_channels', 'U') IS NULL
CREATE TABLE dbo.hl7_channels (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_hl7_channels PRIMARY KEY,
    name VARCHAR(255) NOT NULL CONSTRAINT UQ_hl7_channels_name UNIQUE,
    direction VARCHAR(20) NOT NULL,
    version VARCHAR(20) NOT NULL CONSTRAINT DF_hl7_channels_version DEFAULT ('2.5.1'),
    transport VARCHAR(30) NOT NULL CONSTRAINT DF_hl7_channels_transport DEFAULT ('MLLP'),
    host VARCHAR(255) NULL,
    port INT NULL,
    enabled BIT NOT NULL CONSTRAINT DF_hl7_channels_enabled DEFAULT (0),
    mapping_json VARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_hl7_channels_created DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.integration_events', 'U') IS NULL
CREATE TABLE dbo.integration_events (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_integration_events PRIMARY KEY,
    channel_id INT NULL CONSTRAINT FK_integration_events_channel REFERENCES dbo.hl7_channels(id),
    correlation_id VARCHAR(64) NOT NULL CONSTRAINT UQ_integration_events_correlation UNIQUE,
    message_control_id VARCHAR(255) NULL,
    message_type VARCHAR(40) NULL,
    direction VARCHAR(20) NOT NULL,
    raw_payload VARCHAR(MAX) NOT NULL,
    normalized_json VARCHAR(MAX) NULL,
    status VARCHAR(30) NOT NULL,
    acknowledgement VARCHAR(MAX) NULL,
    attempts INT NOT NULL CONSTRAINT DF_integration_attempts DEFAULT (0),
    error_detail VARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_integration_created DEFAULT (SYSUTCDATETIME()),
    processed_at DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.system_settings', 'U') IS NULL
CREATE TABLE dbo.system_settings (
    setting_key VARCHAR(255) NOT NULL CONSTRAINT PK_system_settings PRIMARY KEY,
    setting_value VARCHAR(MAX) NULL,
    updated_by VARCHAR(255) NULL,
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_system_settings_updated DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.backup_runs', 'U') IS NULL
CREATE TABLE dbo.backup_runs (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_backup_runs PRIMARY KEY,
    correlation_id VARCHAR(64) NOT NULL CONSTRAINT UQ_backup_runs_correlation UNIQUE,
    status VARCHAR(30) NOT NULL,
    destination VARCHAR(2048) NULL,
    manifest_json VARCHAR(MAX) NULL,
    error_detail VARCHAR(MAX) NULL,
    started_at DATETIME2 NOT NULL CONSTRAINT DF_backup_runs_started DEFAULT (SYSUTCDATETIME()),
    completed_at DATETIME2 NULL
);
IF OBJECT_ID('dbo.ai_analyses', 'U') IS NULL
CREATE TABLE dbo.ai_analyses (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_analyses PRIMARY KEY,
    slide_id INT NOT NULL CONSTRAINT FK_ai_analyses_slide REFERENCES dbo.slides(id),
    case_id INT NOT NULL CONSTRAINT FK_ai_analyses_case REFERENCES dbo.cases(id),
    requested_by VARCHAR(255) NOT NULL,
    model_name VARCHAR(255) NOT NULL CONSTRAINT DF_ai_analyses_model_name DEFAULT ('HoVer-Net FAST'),
    model_version VARCHAR(120) NOT NULL CONSTRAINT DF_ai_analyses_model_version DEFAULT ('PanNuke-Fold1'),
    status VARCHAR(30) NOT NULL CONSTRAINT DF_ai_analyses_status DEFAULT ('Completed'),
    roi_x INT NOT NULL,
    roi_y INT NOT NULL,
    roi_width INT NOT NULL,
    roi_height INT NOT NULL,
    roi_level INT NOT NULL CONSTRAINT DF_ai_analyses_level DEFAULT (0),
    total_nuclei INT NOT NULL CONSTRAINT DF_ai_analyses_total DEFAULT (0),
    neoplastic_count INT NOT NULL CONSTRAINT DF_ai_analyses_neopla DEFAULT (0),
    inflammatory_count INT NOT NULL CONSTRAINT DF_ai_analyses_inflam DEFAULT (0),
    connective_count INT NOT NULL CONSTRAINT DF_ai_analyses_connec DEFAULT (0),
    necrotic_count INT NOT NULL CONSTRAINT DF_ai_analyses_necros DEFAULT (0),
    non_neoplastic_count INT NOT NULL CONSTRAINT DF_ai_analyses_noneo DEFAULT (0),
    no_label_count INT NOT NULL CONSTRAINT DF_ai_analyses_nolabe DEFAULT (0),
    nuclear_density FLOAT NULL,
    avg_confidence FLOAT NULL,
    summary_json VARCHAR(MAX) NULL,
    predictions_json VARCHAR(MAX) NULL,
    gallery_json VARCHAR(MAX) NULL,
    roi_image_filename VARCHAR(255) NULL,
    overlay_image_filename VARCHAR(255) NULL,
    report_pdf_filename VARCHAR(255) NULL,
    error_detail VARCHAR(MAX) NULL,
    disclaimer VARCHAR(500) NOT NULL CONSTRAINT DF_ai_analyses_disclaimer DEFAULT ('Research Use Only — Not for Clinical Diagnosis'),
    created_at DATETIME2 NOT NULL CONSTRAINT DF_ai_analyses_created DEFAULT (SYSUTCDATETIME()),
    completed_at DATETIME2 NULL
);
GO

/* Patient profile and longitudinal clinical history. */
IF COL_LENGTH('dbo.patients','patient_identifier') IS NULL ALTER TABLE dbo.patients ADD patient_identifier VARCHAR(64) NULL;
IF COL_LENGTH('dbo.patients','medical_record_number') IS NULL ALTER TABLE dbo.patients ADD medical_record_number VARCHAR(120) NULL;
IF COL_LENGTH('dbo.patients','first_name') IS NULL ALTER TABLE dbo.patients ADD first_name NVARCHAR(120) NULL;
IF COL_LENGTH('dbo.patients','middle_name') IS NULL ALTER TABLE dbo.patients ADD middle_name NVARCHAR(120) NULL;
IF COL_LENGTH('dbo.patients','last_name') IS NULL ALTER TABLE dbo.patients ADD last_name NVARCHAR(120) NULL;
IF COL_LENGTH('dbo.patients','date_of_birth') IS NULL ALTER TABLE dbo.patients ADD date_of_birth DATE NULL;
IF COL_LENGTH('dbo.patients','sex') IS NULL ALTER TABLE dbo.patients ADD sex VARCHAR(30) NULL;
IF COL_LENGTH('dbo.patients','gender_identity') IS NULL ALTER TABLE dbo.patients ADD gender_identity NVARCHAR(80) NULL;
IF COL_LENGTH('dbo.patients','blood_group') IS NULL ALTER TABLE dbo.patients ADD blood_group VARCHAR(10) NULL;
IF COL_LENGTH('dbo.patients','phone') IS NULL ALTER TABLE dbo.patients ADD phone VARCHAR(40) NULL;
IF COL_LENGTH('dbo.patients','address') IS NULL ALTER TABLE dbo.patients ADD address NVARCHAR(1000) NULL;
IF COL_LENGTH('dbo.patients','status') IS NULL ALTER TABLE dbo.patients ADD status VARCHAR(30) NOT NULL CONSTRAINT DF_patients_status DEFAULT ('Active');
IF COL_LENGTH('dbo.patients','demographics_json') IS NULL ALTER TABLE dbo.patients ADD demographics_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.patients','created_by') IS NULL ALTER TABLE dbo.patients ADD created_by VARCHAR(255) NULL;
IF COL_LENGTH('dbo.patients','updated_by') IS NULL ALTER TABLE dbo.patients ADD updated_by VARCHAR(255) NULL;
IF COL_LENGTH('dbo.patients','updated_at') IS NULL ALTER TABLE dbo.patients ADD updated_at DATETIME2 NOT NULL CONSTRAINT DF_patients_updated DEFAULT (SYSUTCDATETIME());
IF COL_LENGTH('dbo.patients','archived_at') IS NULL ALTER TABLE dbo.patients ADD archived_at DATETIME2 NULL;
GO
UPDATE dbo.patients SET patient_identifier=CONCAT('PAT-',RIGHT(CONCAT('000000',CAST(id AS VARCHAR(20))),6)) WHERE patient_identifier IS NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_identifier' AND object_id=OBJECT_ID('dbo.patients')) CREATE UNIQUE INDEX UQ_patients_identifier ON dbo.patients(patient_identifier) WHERE patient_identifier IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UQ_patients_mrn' AND object_id=OBJECT_ID('dbo.patients')) CREATE UNIQUE INDEX UQ_patients_mrn ON dbo.patients(medical_record_number) WHERE medical_record_number IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_patients_demographics_json') ALTER TABLE dbo.patients ADD CONSTRAINT CK_patients_demographics_json CHECK (demographics_json IS NULL OR ISJSON(demographics_json)=1);
GO

IF OBJECT_ID('dbo.patient_identity_mappings','U') IS NULL
CREATE TABLE dbo.patient_identity_mappings (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_identity_mappings PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_identity_patient REFERENCES dbo.patients(id),
    authority VARCHAR(120) NOT NULL, identifier_value VARCHAR(255) NOT NULL,
    identifier_type VARCHAR(80) NOT NULL CONSTRAINT DF_patient_identity_type DEFAULT ('External'),
    is_primary BIT NOT NULL CONSTRAINT DF_patient_identity_primary DEFAULT (0),
    created_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_identity_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_patient_identity_mapping UNIQUE(authority,identifier_value)
);
GO

IF OBJECT_ID('dbo.patient_medical_history','U') IS NULL
CREATE TABLE dbo.patient_medical_history (
    patient_id INT NOT NULL CONSTRAINT PK_patient_medical_history PRIMARY KEY CONSTRAINT FK_patient_history_patient REFERENCES dbo.patients(id),
    schema_version VARCHAR(20) NOT NULL CONSTRAINT DF_patient_history_schema DEFAULT ('1.0'),
    history_json NVARCHAR(MAX) NOT NULL CONSTRAINT DF_patient_history_json DEFAULT ('{}'),
    revision_number INT NOT NULL CONSTRAINT DF_patient_history_revision DEFAULT (1),
    created_by VARCHAR(255) NOT NULL, updated_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_history_created DEFAULT (SYSUTCDATETIME()),
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_patient_history_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_patient_medical_history_json CHECK (ISJSON(history_json)=1)
);
GO

IF OBJECT_ID('dbo.patient_medical_history_revisions','U') IS NULL
CREATE TABLE dbo.patient_medical_history_revisions (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_medical_history_revisions PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_history_revision_patient REFERENCES dbo.patients(id),
    revision_number INT NOT NULL, schema_version VARCHAR(20) NOT NULL, history_json NVARCHAR(MAX) NOT NULL,
    changed_by VARCHAR(255) NOT NULL, change_reason NVARCHAR(1000) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_history_revision_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_patient_history_revision UNIQUE(patient_id,revision_number),
    CONSTRAINT CK_patient_history_revision_json CHECK (ISJSON(history_json)=1)
);
GO

IF OBJECT_ID('dbo.patient_encounters','U') IS NULL
CREATE TABLE dbo.patient_encounters (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_encounters PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_encounter_patient REFERENCES dbo.patients(id),
    encounter_identifier VARCHAR(120) NOT NULL, encounter_type VARCHAR(80) NOT NULL,
    status VARCHAR(30) NOT NULL CONSTRAINT DF_patient_encounter_status DEFAULT ('Open'),
    occurred_at DATETIME2 NOT NULL, ended_at DATETIME2 NULL, location NVARCHAR(255) NULL, clinician VARCHAR(255) NULL,
    metadata_json NVARCHAR(MAX) NULL, created_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_encounter_created DEFAULT (SYSUTCDATETIME()),
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_patient_encounter_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_patient_encounter UNIQUE(patient_id,encounter_identifier),
    CONSTRAINT CK_patient_encounter_json CHECK (metadata_json IS NULL OR ISJSON(metadata_json)=1)
);
GO

IF OBJECT_ID('dbo.patient_tests','U') IS NULL
CREATE TABLE dbo.patient_tests (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_tests PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_test_patient REFERENCES dbo.patients(id),
    encounter_id INT NULL CONSTRAINT FK_patient_test_encounter REFERENCES dbo.patient_encounters(id),
    case_id INT NULL CONSTRAINT FK_patient_test_case REFERENCES dbo.cases(id),
    report_id INT NULL CONSTRAINT FK_patient_test_report REFERENCES dbo.reports(id),
    test_identifier VARCHAR(120) NOT NULL, test_name NVARCHAR(255) NOT NULL, test_category VARCHAR(100) NOT NULL,
    order_date DATETIME2 NULL, collection_date DATETIME2 NULL, result_date DATETIME2 NULL,
    status VARCHAR(30) NOT NULL CONSTRAINT DF_patient_test_status DEFAULT ('Ordered'),
    summary_result NVARCHAR(2000) NULL, source_system VARCHAR(120) NOT NULL CONSTRAINT DF_patient_test_source DEFAULT ('Digital Pathology'),
    provenance_json NVARCHAR(MAX) NULL, created_by VARCHAR(255) NOT NULL, updated_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_test_created DEFAULT (SYSUTCDATETIME()),
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_patient_test_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_patient_test_identifier UNIQUE(patient_id,test_identifier),
    CONSTRAINT CK_patient_test_provenance_json CHECK (provenance_json IS NULL OR ISJSON(provenance_json)=1)
);
GO

IF OBJECT_ID('dbo.patient_test_results','U') IS NULL
CREATE TABLE dbo.patient_test_results (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_test_results PRIMARY KEY,
    test_id INT NOT NULL CONSTRAINT FK_patient_test_result_test REFERENCES dbo.patient_tests(id),
    analyte_name NVARCHAR(255) NOT NULL, result_value NVARCHAR(1000) NULL,
    result_type VARCHAR(40) NOT NULL CONSTRAINT DF_patient_test_result_type DEFAULT ('Text'),
    unit NVARCHAR(80) NULL, reference_range NVARCHAR(255) NULL, abnormal_flag VARCHAR(30) NULL,
    result_json NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_test_result_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_patient_test_result_json CHECK (result_json IS NULL OR ISJSON(result_json)=1)
);
GO

IF OBJECT_ID('dbo.patient_clinical_notes','U') IS NULL
CREATE TABLE dbo.patient_clinical_notes (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_clinical_notes PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_note_patient REFERENCES dbo.patients(id),
    case_id INT NULL CONSTRAINT FK_patient_note_case REFERENCES dbo.cases(id),
    encounter_id INT NULL CONSTRAINT FK_patient_note_encounter REFERENCES dbo.patient_encounters(id),
    note_type VARCHAR(80) NOT NULL CONSTRAINT DF_patient_note_type DEFAULT ('Clinical'), note_text NVARCHAR(MAX) NOT NULL,
    revision_number INT NOT NULL CONSTRAINT DF_patient_note_revision DEFAULT (1), is_archived BIT NOT NULL CONSTRAINT DF_patient_note_archived DEFAULT (0),
    created_by VARCHAR(255) NOT NULL, updated_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_note_created DEFAULT (SYSUTCDATETIME()),
    updated_at DATETIME2 NOT NULL CONSTRAINT DF_patient_note_updated DEFAULT (SYSUTCDATETIME())
);
GO

IF OBJECT_ID('dbo.patient_clinical_note_revisions','U') IS NULL
CREATE TABLE dbo.patient_clinical_note_revisions (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_clinical_note_revisions PRIMARY KEY,
    note_id INT NOT NULL CONSTRAINT FK_patient_note_revision_note REFERENCES dbo.patient_clinical_notes(id),
    revision_number INT NOT NULL, note_text NVARCHAR(MAX) NOT NULL, changed_by VARCHAR(255) NOT NULL,
    change_reason NVARCHAR(1000) NULL, created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_note_revision_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT UQ_patient_note_revision UNIQUE(note_id,revision_number)
);
GO

IF OBJECT_ID('dbo.patient_documents','U') IS NULL
CREATE TABLE dbo.patient_documents (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_documents PRIMARY KEY,
    patient_id INT NOT NULL CONSTRAINT FK_patient_document_patient REFERENCES dbo.patients(id),
    case_id INT NULL CONSTRAINT FK_patient_document_case REFERENCES dbo.cases(id),
    document_type VARCHAR(80) NOT NULL, original_filename NVARCHAR(512) NOT NULL,
    stored_filename VARCHAR(512) NOT NULL CONSTRAINT UQ_patient_document_stored UNIQUE,
    content_type VARCHAR(120) NOT NULL, file_size BIGINT NOT NULL, checksum_sha256 VARCHAR(64) NOT NULL,
    description NVARCHAR(1000) NULL, uploaded_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_document_created DEFAULT (SYSUTCDATETIME()), archived_at DATETIME2 NULL
);
GO

IF OBJECT_ID('dbo.patient_imports','U') IS NULL
CREATE TABLE dbo.patient_imports (
    id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_patient_imports PRIMARY KEY,
    patient_id INT NULL CONSTRAINT FK_patient_import_patient REFERENCES dbo.patients(id),
    schema_version VARCHAR(20) NOT NULL, source_identifier VARCHAR(255) NULL, payload_hash VARCHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL, summary_json NVARCHAR(MAX) NOT NULL, imported_by VARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL CONSTRAINT DF_patient_import_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_patient_import_summary_json CHECK (ISJSON(summary_json)=1)
);
GO

/* Query indexes used by the API. */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_analyses_slide_id' AND object_id=OBJECT_ID('dbo.ai_analyses')) CREATE INDEX idx_ai_analyses_slide_id ON dbo.ai_analyses(slide_id, id DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_analyses_case_id' AND object_id=OBJECT_ID('dbo.ai_analyses')) CREATE INDEX idx_ai_analyses_case_id ON dbo.ai_analyses(case_id, id DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_slides_case_id' AND object_id=OBJECT_ID('dbo.slides')) CREATE INDEX idx_slides_case_id ON dbo.slides(case_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_slide_qc_history_slide_id' AND object_id=OBJECT_ID('dbo.slide_qc_history')) CREATE INDEX idx_slide_qc_history_slide_id ON dbo.slide_qc_history(slide_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id=OBJECT_ID('dbo.annotations')) CREATE INDEX idx_annotations_slide_id ON dbo.annotations(slide_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_review_notes_case_id' AND object_id=OBJECT_ID('dbo.case_review_notes')) CREATE INDEX idx_case_review_notes_case_id ON dbo.case_review_notes(case_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_review_history_case_id' AND object_id=OBJECT_ID('dbo.case_review_history')) CREATE INDEX idx_case_review_history_case_id ON dbo.case_review_history(case_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_reports_case_id' AND object_id=OBJECT_ID('dbo.reports')) CREATE INDEX idx_reports_case_id ON dbo.reports(case_id,version DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_audit_log_created_at' AND object_id=OBJECT_ID('dbo.audit_log')) CREATE INDEX idx_audit_log_created_at ON dbo.audit_log(created_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_report_findings_report_id' AND object_id=OBJECT_ID('dbo.report_findings')) CREATE INDEX idx_report_findings_report_id ON dbo.report_findings(report_id,sort_order);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ingestion_checksum' AND object_id=OBJECT_ID('dbo.ingestion_jobs')) CREATE INDEX idx_ingestion_checksum ON dbo.ingestion_jobs(checksum_sha256,status);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ai_jobs_status' AND object_id=OBJECT_ID('dbo.ai_jobs')) CREATE INDEX idx_ai_jobs_status ON dbo.ai_jobs(status,created_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_integration_events_status' AND object_id=OBJECT_ID('dbo.integration_events')) CREATE INDEX idx_integration_events_status ON dbo.integration_events(status,created_at);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_accessions_case_identifiers' AND object_id=OBJECT_ID('dbo.accessions')) CREATE INDEX idx_accessions_case_identifiers ON dbo.accessions(case_id,accession_number,order_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_specimens_accession' AND object_id=OBJECT_ID('dbo.specimens')) CREATE INDEX idx_specimens_accession ON dbo.specimens(accession_id,specimen_type);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_blocks_specimen' AND object_id=OBJECT_ID('dbo.blocks')) CREATE INDEX idx_blocks_specimen ON dbo.blocks(specimen_id,block_code);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_case_links_case' AND object_id=OBJECT_ID('dbo.case_links')) CREATE INDEX idx_case_links_case ON dbo.case_links(case_id,related_case_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_viewer_bookmarks_slide' AND object_id=OBJECT_ID('dbo.viewer_bookmarks')) CREATE INDEX idx_viewer_bookmarks_slide ON dbo.viewer_bookmarks(slide_id,id DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_viewer_preferences_user' AND object_id=OBJECT_ID('dbo.viewer_preferences')) CREATE INDEX idx_viewer_preferences_user ON dbo.viewer_preferences(username,updated_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_annotation_revisions_annotation' AND object_id=OBJECT_ID('dbo.annotation_revisions')) CREATE INDEX idx_annotation_revisions_annotation ON dbo.annotation_revisions(annotation_id,revision_number DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_report_reviews_report' AND object_id=OBJECT_ID('dbo.report_reviews')) CREATE INDEX idx_report_reviews_report ON dbo.report_reviews(report_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_consultations_case' AND object_id=OBJECT_ID('dbo.case_consultations')) CREATE INDEX idx_consultations_case ON dbo.case_consultations(case_id,status);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_identity_patient' AND object_id=OBJECT_ID('dbo.patient_identity_mappings')) CREATE INDEX idx_patient_identity_patient ON dbo.patient_identity_mappings(patient_id,id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_encounters_timeline' AND object_id=OBJECT_ID('dbo.patient_encounters')) CREATE INDEX idx_patient_encounters_timeline ON dbo.patient_encounters(patient_id,occurred_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_tests_history' AND object_id=OBJECT_ID('dbo.patient_tests')) CREATE INDEX idx_patient_tests_history ON dbo.patient_tests(patient_id,order_date DESC,id DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_notes_history' AND object_id=OBJECT_ID('dbo.patient_clinical_notes')) CREATE INDEX idx_patient_notes_history ON dbo.patient_clinical_notes(patient_id,created_at DESC,id DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_patient_documents' AND object_id=OBJECT_ID('dbo.patient_documents')) CREATE INDEX idx_patient_documents ON dbo.patient_documents(patient_id,created_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_accessions_patient' AND object_id=OBJECT_ID('dbo.accessions')) CREATE INDEX idx_accessions_patient ON dbo.accessions(patient_id,case_id);
GO
