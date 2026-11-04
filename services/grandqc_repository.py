"""SQL Server persistence for GrandQC whole-slide quality control."""

from contextlib import closing
import json

FEATURE_CODE = "SLIDE_QUALITY_CONTROL"


def init_grandqc_schema(connect_func):
    """Create QC table and register the research-only AI feature."""
    with closing(connect_func()) as db:
        db.execute("""
            IF OBJECT_ID(N'dbo.qc_analyses', N'U') IS NULL
            BEGIN
                CREATE TABLE dbo.qc_analyses (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    slide_id INT NOT NULL REFERENCES dbo.slides(id),
                    case_id INT NOT NULL REFERENCES dbo.cases(id),
                    requested_by VARCHAR(255) NOT NULL,
                    job_id VARCHAR(64) NOT NULL UNIQUE,
                    model_name VARCHAR(80) NOT NULL,
                    model_version VARCHAR(80) NOT NULL,
                    status VARCHAR(30) NOT NULL,
                    polygon_count INT NOT NULL DEFAULT 0,
                    elapsed_seconds FLOAT NULL,
                    result_json NVARCHAR(MAX) NOT NULL,
                    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
                    completed_at DATETIME2 NULL
                );

                CREATE INDEX IX_qc_analyses_slide
                ON dbo.qc_analyses(slide_id, id DESC);
            END
        """)

        db.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM dbo.ai_features WHERE feature_code = ?
            )
            BEGIN
                INSERT INTO dbo.ai_features (
                    feature_code, feature_name, description,
                    is_implemented, is_active,
                    catalog_category, catalog_summary, catalog_purpose,
                    catalog_value, catalog_title, catalog_display_order
                )
                VALUES (
                    ?,
                    'GrandQC - Slide Quality Control',
                    'Research-only whole-slide artifact detection.',
                    1, 0,
                    'Slide quality',
                    'Detects tissue and preparation artifacts.',
                    'Whole-slide quality review.',
                    'Supports artifact inspection by researchers.',
                    'GrandQC - Whole-Slide Quality Control',
                    3
                );
            END
        """, (FEATURE_CODE, FEATURE_CODE))
        db.commit()



def save_grandqc_analysis(
    connect_func,
    *,
    slide_id,
    case_id,
    requested_by,
    job_id,
    result,
):
    """Save completed GrandQC results and return the analysis ID."""
    payload = json.dumps(result, ensure_ascii=False)

    with closing(connect_func()) as db:
        try:
            # Check whether this job has already been saved.
            existing = db.execute(
                "SELECT id FROM dbo.qc_analyses WHERE job_id = ?",
                (job_id,),
            ).fetchone()

            if existing is not None:
                return int(existing["id"])

            # Insert without relying on cursor output metadata.
            db.execute("""
                INSERT INTO dbo.qc_analyses (
                    slide_id,
                    case_id,
                    requested_by,
                    job_id,
                    model_name,
                    model_version,
                    status,
                    polygon_count,
                    elapsed_seconds,
                    result_json,
                    completed_at
                )
                VALUES (
                    ?, ?, ?, ?,
                    'GrandQC',
                    'MPP1.5',
                    'Completed',
                    ?, ?, ?,
                    SYSUTCDATETIME()
                )
            """, (
                slide_id,
                case_id,
                requested_by,
                job_id,
                int(result.get("polygon_count", 0)),
                float(result.get("elapsed_seconds", 0)),
                payload,
            ))

            db.commit()

            # Your database connection returns dictionary-style rows.
            saved = db.execute(
                "SELECT id FROM dbo.qc_analyses WHERE job_id = ?",
                (job_id,),
            ).fetchone()

            if saved is None:
                raise RuntimeError(
                    f"GrandQC analysis not found after insert: {job_id}"
                )

            return int(saved["id"])

        except Exception:
            db.rollback()
            raise

    """Persist completed QC results and return the analysis ID.

    Retrieve the identity using an OUTPUT table variable rather than assuming
    that the INSERT's first driver result set is a row. Repeated calls with the
    same job_id reuse the existing analysis instead of duplicating it.
    """
    payload = json.dumps(result, ensure_ascii=False)

    with closing(connect_func()) as db:
        try:
            existing = db.execute(
                "SELECT id FROM dbo.qc_analyses WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                return int(existing[0])

            cursor = db.execute("""
                SET NOCOUNT ON;
                DECLARE @new_ids TABLE (id INT NOT NULL);

                INSERT INTO dbo.qc_analyses (
                    slide_id, case_id, requested_by, job_id,
                    model_name, model_version, status,
                    polygon_count, elapsed_seconds, result_json, completed_at
                )
                OUTPUT INSERTED.id INTO @new_ids(id)
                VALUES (
                    ?, ?, ?, ?,
                    'GrandQC', 'MPP1.5', 'Completed',
                    ?, ?, ?, SYSUTCDATETIME()
                );

                SELECT id FROM @new_ids;
            """, (
                slide_id,
                case_id,
                requested_by,
                job_id,
                int(result.get("polygon_count", 0)),
                float(result.get("elapsed_seconds", 0)),
                payload,
            ))

            # Drivers may expose non-row result sets before the SELECT.
            while cursor.description is None:
                if not cursor.nextset():
                    raise RuntimeError("SQL Server did not return a GrandQC analysis ID")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("GrandQC INSERT produced no ID row")

            analysis_id = int(row[0])
            db.commit()
            return analysis_id
        except Exception:
            db.rollback()
            raise


def get_grandqc_analysis(connect_func, analysis_id):
    with closing(connect_func()) as db:
        row = db.execute(
            "SELECT * FROM dbo.qc_analyses WHERE id = ?",
            (analysis_id,),
        ).fetchone()
        return dict(row) if row else None


def list_grandqc_analyses(connect_func, slide_id):
    with closing(connect_func()) as db:
        rows = db.execute("""
            SELECT
                id, slide_id, case_id, requested_by, job_id,
                model_name, model_version, status, polygon_count,
                elapsed_seconds, created_at, completed_at
            FROM dbo.qc_analyses
            WHERE slide_id = ?
            ORDER BY id DESC
        """, (slide_id,)).fetchall()
        return [dict(row) for row in rows]
