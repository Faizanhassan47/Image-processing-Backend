"""
Centralized SQL Queries and Schema Definitions.
Organized by domain: Auth/Users, Cases & Hierarchy, Slides & QC, Annotations,
Reports, AI Jobs, Audit Logging, and System Governance.
"""

# ==============================================================================
# AUTHENTICATION & USERS
# ==============================================================================
GET_USER_BY_USERNAME = """
    SELECT id, username, display_name, role, password_hash, is_active,
           must_change_password, failed_login_attempts, locked_until
    FROM users
    WHERE username = ?
"""

GET_USER_BY_ID = """
    SELECT id, username, display_name, role, is_active,
           must_change_password, failed_login_attempts, locked_until
    FROM users
    WHERE id = ?
"""

LIST_ALL_USERS = """
    SELECT id, username, display_name, role, is_active,
           must_change_password, failed_login_attempts, locked_until
    FROM users
    ORDER BY username ASC
"""

INSERT_USER = """
    INSERT INTO users (username, display_name, role, password_hash, is_active, must_change_password)
    VALUES (?, ?, ?, ?, ?, ?)
"""

UPDATE_USER_PROFILE = """
    UPDATE users
    SET display_name = ?, role = ?, is_active = ?
    WHERE id = ?
"""

UPDATE_USER_PASSWORD = """
    UPDATE users
    SET password_hash = ?, must_change_password = 0, failed_login_attempts = 0, locked_until = NULL
    WHERE id = ?
"""

INCREMENT_FAILED_LOGIN = """
    UPDATE users
    SET failed_login_attempts = failed_login_attempts + 1,
        locked_until = ?
    WHERE username = ?
"""

RESET_FAILED_LOGIN = """
    UPDATE users
    SET failed_login_attempts = 0, locked_until = NULL
    WHERE username = ?
"""

DELETE_USER_BY_ID = """
    DELETE FROM users
    WHERE id = ?
"""


# ==============================================================================
# AUDIT LOGGING
# ==============================================================================
INSERT_AUDIT_LOG = """
    INSERT INTO audit_log (
        user_id, username, action, entity_type, entity_id,
        details, ip_address, user_agent
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

LIST_AUDIT_LOGS = """
    SELECT TOP (?) id, user_id, username, action, entity_type, entity_id,
           details, ip_address, user_agent, created_at
    FROM audit_log
    ORDER BY created_at DESC
"""


# ==============================================================================
# CASES, ACCESSIONS, SPECIMENS & BLOCKS
# ==============================================================================
LIST_CASES = """
    SELECT c.id, c.case_id, c.patient_id, c.patient_name, c.age, c.gender,
           c.status, c.priority, c.assigned_pathologist, c.clinical_history,
           c.created_at, c.updated_at
    FROM cases c
    ORDER BY c.updated_at DESC
"""

GET_CASE_BY_ID = """
    SELECT id, case_id, patient_id, patient_name, age, gender,
           status, priority, assigned_pathologist, clinical_history,
           created_at, updated_at
    FROM cases
    WHERE id = ?
"""

GET_CASE_BY_CASE_ID = """
    SELECT id, case_id, patient_id, patient_name, age, gender,
           status, priority, assigned_pathologist, clinical_history,
           created_at, updated_at
    FROM cases
    WHERE case_id = ?
"""

INSERT_CASE = """
    INSERT INTO cases (
        case_id, patient_id, patient_name, age, gender,
        status, priority, assigned_pathologist, clinical_history
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

UPDATE_CASE_STATUS = """
    UPDATE cases
    SET status = ?, assigned_pathologist = COALESCE(?, assigned_pathologist), updated_at = GETUTCDATE()
    WHERE id = ?
"""

DELETE_CASE_BY_ID = """
    DELETE FROM cases
    WHERE id = ?
"""

# Accessions
LIST_ACCESSIONS_BY_CASE = """
    SELECT id, case_id, accession_number, received_at, status, notes
    FROM accessions
    WHERE case_id = ?
    ORDER BY received_at DESC
"""

# Specimens
LIST_SPECIMENS_BY_CASE = """
    SELECT id, case_id, accession_id, specimen_id, specimen_type,
           collection_site, fixation_type, gross_description, status
    FROM specimens
    WHERE case_id = ?
    ORDER BY specimen_id ASC
"""

# Blocks
LIST_BLOCKS_BY_CASE = """
    SELECT b.id, b.case_id, b.specimen_id, b.block_id, b.embedding_medium,
           b.section_count, b.status
    FROM blocks b
    WHERE b.case_id = ?
    ORDER BY b.block_id ASC
"""


# ==============================================================================
# SLIDES & QUALITY CONTROL (QC)
# ==============================================================================
LIST_SLIDES_BY_CASE = """
    SELECT s.id, s.case_id, s.block_id, s.slide_id, s.stain_type,
           s.image_path, s.thumbnail_path, s.width, s.height, s.mpp_x, s.mpp_y,
           s.magnification, s.status, s.qc_status, s.uploaded_at
    FROM slides s
    WHERE s.case_id = ?
    ORDER BY s.slide_id ASC
"""

GET_SLIDE_BY_ID = """
    SELECT s.id, s.case_id, s.block_id, s.slide_id, s.stain_type,
           s.image_path, s.thumbnail_path, s.width, s.height, s.mpp_x, s.mpp_y,
           s.magnification, s.status, s.qc_status, s.uploaded_at
    FROM slides s
    WHERE s.id = ?
"""

INSERT_SLIDE = """
    INSERT INTO slides (
        case_id, block_id, slide_id, stain_type, image_path, thumbnail_path,
        width, height, mpp_x, mpp_y, magnification, status, qc_status
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

UPDATE_SLIDE_QC = """
    UPDATE slides
    SET qc_status = ?, status = ?
    WHERE id = ?
"""

INSERT_SLIDE_QC_HISTORY = """
    INSERT INTO slide_qc_history (
        slide_id, reviewer_id, reviewer_name, previous_status, new_status,
        qc_score, comments, focus_quality, staining_quality, artifact_detected
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

LIST_SLIDE_QC_HISTORY = """
    SELECT id, slide_id, reviewer_id, reviewer_name, previous_status, new_status,
           qc_score, comments, focus_quality, staining_quality, artifact_detected, created_at
    FROM slide_qc_history
    WHERE slide_id = ?
    ORDER BY created_at DESC
"""


# ==============================================================================
# ANNOTATIONS & REVISIONS
# ==============================================================================
LIST_ANNOTATIONS_BY_SLIDE = """
    SELECT id, slide_id, user_id, author_name, label, geometry_type,
           coordinates, measurements, notes, is_locked, created_at, updated_at
    FROM annotations
    WHERE slide_id = ?
    ORDER BY created_at ASC
"""

GET_ANNOTATION_BY_ID = """
    SELECT id, slide_id, user_id, author_name, label, geometry_type,
           coordinates, measurements, notes, is_locked, created_at, updated_at
    FROM annotations
    WHERE id = ?
"""

INSERT_ANNOTATION = """
    INSERT INTO annotations (
        slide_id, user_id, author_name, label, geometry_type,
        coordinates, measurements, notes, is_locked
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

UPDATE_ANNOTATION = """
    UPDATE annotations
    SET label = ?, coordinates = ?, measurements = ?, notes = ?, is_locked = ?, updated_at = GETUTCDATE()
    WHERE id = ?
"""

DELETE_ANNOTATION = """
    DELETE FROM annotations
    WHERE id = ?
"""

INSERT_ANNOTATION_REVISION = """
    INSERT INTO annotation_revisions (
        annotation_id, revision_number, label, geometry_type,
        coordinates, measurements, notes, modified_by, change_summary
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

LIST_ANNOTATION_REVISIONS = """
    SELECT id, annotation_id, revision_number, label, geometry_type,
           coordinates, measurements, notes, modified_by, change_summary, created_at
    FROM annotation_revisions
    WHERE annotation_id = ?
    ORDER BY revision_number DESC
"""


# ==============================================================================
# REPORTS & REPORT FINDINGS
# ==============================================================================
GET_LATEST_REPORT_FOR_CASE = """
    SELECT TOP 1 id, case_id, version, status, reviewer_id, reviewer_name,
           specimen_clinical, gross_description, microscopic_findings,
           diagnosis, comments, finalized_at, created_at, updated_at
    FROM reports
    WHERE case_id = ?
    ORDER BY version DESC
"""

GET_REPORT_BY_ID = """
    SELECT id, case_id, version, status, reviewer_id, reviewer_name,
           specimen_clinical, gross_description, microscopic_findings,
           diagnosis, comments, finalized_at, created_at, updated_at
    FROM reports
    WHERE id = ?
"""

INSERT_REPORT = """
    INSERT INTO reports (
        case_id, version, status, reviewer_id, reviewer_name,
        specimen_clinical, gross_description, microscopic_findings,
        diagnosis, comments, finalized_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

UPDATE_REPORT = """
    UPDATE reports
    SET status = ?, reviewer_id = ?, reviewer_name = ?,
        specimen_clinical = ?, gross_description = ?, microscopic_findings = ?,
        diagnosis = ?, comments = ?, finalized_at = ?, updated_at = GETUTCDATE()
    WHERE id = ?
"""

LIST_REPORT_FINDINGS = """
    SELECT id, report_id, slide_id, annotation_id, text, sort_order
    FROM report_findings
    WHERE report_id = ?
    ORDER BY sort_order ASC
"""

INSERT_REPORT_FINDING = """
    INSERT INTO report_findings (report_id, slide_id, annotation_id, text, sort_order)
    VALUES (?, ?, ?, ?, ?)
"""

DELETE_REPORT_FINDINGS = """
    DELETE FROM report_findings
    WHERE report_id = ?
"""


# ==============================================================================
# AI JOBS & RESULTS
# ==============================================================================
INSERT_AI_JOB = """
    INSERT INTO ai_jobs (slide_id, model_name, model_version, status, requested_by)
    VALUES (?, ?, ?, ?, ?)
"""

GET_AI_JOB = """
    SELECT id, slide_id, model_name, model_version, status, requested_by,
           error_message, started_at, completed_at, created_at
    FROM ai_jobs
    WHERE id = ?
"""

UPDATE_AI_JOB_STATUS = """
    UPDATE ai_jobs
    SET status = ?, error_message = ?, started_at = COALESCE(?, started_at), completed_at = ?
    WHERE id = ?
"""

INSERT_AI_RESULT = """
    INSERT INTO ai_results (job_id, slide_id, result_type, summary, raw_output)
    VALUES (?, ?, ?, ?, ?)
"""

GET_AI_RESULTS_BY_SLIDE = """
    SELECT r.id, r.job_id, r.slide_id, r.result_type, r.summary, r.raw_output, r.created_at,
           j.model_name, j.model_version, j.status AS job_status
    FROM ai_results r
    JOIN ai_jobs j ON r.job_id = j.id
    WHERE r.slide_id = ?
    ORDER BY r.created_at DESC
"""
