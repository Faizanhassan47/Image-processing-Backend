"""
Database table initialization module.
Automatically and idempotently verifies and creates all tables, columns,
constraints, and indexes required by the application.
"""

from contextlib import closing
import pyodbc


def init_db(connect_fn):
    """Create the core database schema and idempotently lift legacy case files into it."""
    with closing(connect_fn()) as db:
        db.execute("""
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
        db.execute("UPDATE users SET username = 'ETDadmin' WHERE LOWER(username) = 'eidadmin' AND NOT EXISTS (SELECT 1 FROM users WHERE LOWER(username) = 'etdadmin')")
            
        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='cases' and xtype='U')
            CREATE TABLE cases (
                id INT IDENTITY(1,1) PRIMARY KEY,
                case_id VARCHAR(255) NOT NULL UNIQUE,
                description VARCHAR(MAX) NOT NULL,
                filename VARCHAR(255) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
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

        db.execute('''
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slides' and xtype='U')
            CREATE TABLE slides (
                id INT IDENTITY(1,1) PRIMARY KEY,
                case_id INT NOT NULL REFERENCES cases(id),
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
            )
        ''')
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
                
        db.execute('''
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='slide_qc_history' and xtype='U')
            CREATE TABLE slide_qc_history (
                id INT IDENTITY(1,1) PRIMARY KEY,
                slide_id INT NOT NULL REFERENCES slides(id),
                qc_status VARCHAR(MAX),
                qc_missing_tissue BIT,
                qc_blur BIT,
                qc_scan_artifact BIT,
                qc_incorrect_label BIT,
                qc_unusual_staining BIT,
                qc_note VARCHAR(MAX),
                actor VARCHAR(MAX) NOT NULL,
                created_at VARCHAR(MAX) DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_slide_qc_history_slide_id' AND object_id = OBJECT_ID('slide_qc_history')) CREATE INDEX idx_slide_qc_history_slide_id ON slide_qc_history(slide_id, id)")
        db.execute("IF COL_LENGTH('slide_qc_history','qc_unusual_staining') IS NULL ALTER TABLE slide_qc_history ADD qc_unusual_staining BIT NULL")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_slides_case_id' AND object_id = OBJECT_ID('slides')) CREATE INDEX idx_slides_case_id ON slides(case_id, id)")

        cursor = db.execute("SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='annotations'")
        columns = [row['name'] for row in cursor.fetchall()]
        if columns and 'points' not in columns:
            db.execute("""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='annotations_new' and xtype='U')
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
                )
            """)
            db.execute("""
                INSERT INTO annotations_new 
                (id, slide_id, annotation_type, label, note, color, x, y, width, height, created_at, updated_at)
                SELECT id, slide_id, annotation_type, label, note, color, x, y, width, height, created_at, updated_at 
                FROM annotations
            """)
            db.execute("DROP TABLE annotations")
            db.execute("ALTER TABLE annotations_new RENAME TO annotations")
            db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id = OBJECT_ID('annotations')) CREATE INDEX idx_annotations_slide_id ON annotations(slide_id, id)")
        else:
            db.execute("""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='annotations' and xtype='U')
                CREATE TABLE annotations (
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
                )
            """)
            db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_annotations_slide_id' AND object_id = OBJECT_ID('annotations')) CREATE INDEX idx_annotations_slide_id ON annotations(slide_id, id)")

        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_notes' and xtype='U')
            CREATE TABLE case_review_notes (
                id INT IDENTITY(1,1) PRIMARY KEY,
                case_id INT NOT NULL REFERENCES cases(id),
                author VARCHAR(MAX) NOT NULL,
                note VARCHAR(MAX) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_case_review_notes_case_id' AND object_id = OBJECT_ID('case_review_notes')) CREATE INDEX idx_case_review_notes_case_id ON case_review_notes(case_id, id)")

        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='case_review_history' and xtype='U')
            CREATE TABLE case_review_history (
                id INT IDENTITY(1,1) PRIMARY KEY,
                case_id INT NOT NULL REFERENCES cases(id),
                change_type VARCHAR(MAX) NOT NULL CHECK (change_type IN ('status', 'reviewer')),
                previous_value VARCHAR(MAX) NOT NULL,
                new_value VARCHAR(MAX) NOT NULL,
                actor VARCHAR(MAX) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_case_review_history_case_id' AND object_id = OBJECT_ID('case_review_history')) CREATE INDEX idx_case_review_history_case_id ON case_review_history(case_id, id)")

        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='reports' and xtype='U')
            CREATE TABLE reports (
                id INT IDENTITY(1,1) PRIMARY KEY,
                case_id INT NOT NULL REFERENCES cases(id),
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
            )
        """)
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_reports_case_id' AND object_id = OBJECT_ID('reports')) CREATE INDEX idx_reports_case_id ON reports(case_id, version DESC)")

        db.execute("""
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
            )
        """)
        db.execute("IF COL_LENGTH('audit_log','ip_address') IS NULL ALTER TABLE audit_log ADD ip_address VARCHAR(45) NULL")
        db.execute("IF COL_LENGTH('audit_log','device_name') IS NULL ALTER TABLE audit_log ADD device_name VARCHAR(255) NULL")
        db.execute("IF COL_LENGTH('audit_log','device_location') IS NULL ALTER TABLE audit_log ADD device_location VARCHAR(255) NULL")
        db.execute("IF COL_LENGTH('audit_log','user_agent') IS NULL ALTER TABLE audit_log ADD user_agent VARCHAR(1000) NULL")
        db.execute("IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name='idx_audit_log_created_at' AND object_id = OBJECT_ID('audit_log')) CREATE INDEX idx_audit_log_created_at ON audit_log(created_at DESC)")
        db.execute("""
            INSERT INTO audit_log (actor_username,action,target_type,target_id,details)
            SELECT 'system','user.role_migration','user',CAST(id AS VARCHAR(50)),'previous_role=' + CAST(role AS VARCHAR(255)) + '; account_disabled_pending_reassignment'
            FROM users u WHERE role IN ('Technician','Histotechnologist','Viewer','Auditor','AI Analyst','Integration Engineer')
            AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action='user.role_migration' AND a.target_type='user' AND a.target_id=CAST(u.id AS VARCHAR(50)))
        """)
        db.execute("IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_users_approved_roles') ALTER TABLE users DROP CONSTRAINT CK_users_approved_roles")
        db.execute("IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_users_super_admin_reserved') ALTER TABLE users DROP CONSTRAINT CK_users_super_admin_reserved")
        db.execute("UPDATE users SET role='Laboratory Manager', is_active=0 WHERE role IN ('Technician','Histotechnologist','Viewer','Auditor','AI Analyst','Integration Engineer')")
        db.execute("UPDATE users SET role='System Administrator' WHERE role='Admin'")
        db.execute("UPDATE users SET role='System Administrator', is_active=1 WHERE LOWER(username) IN ('etddev','etdadmin')")
        db.execute("ALTER TABLE users ADD CONSTRAINT CK_users_approved_roles CHECK (role IN ('System Administrator','Laboratory Manager','Pathologist'))")

        db.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='report_findings' and xtype='U')
            CREATE TABLE report_findings (
                id INT IDENTITY(1,1) PRIMARY KEY,
                report_id INT NOT NULL REFERENCES reports(id),
                slide_id INT REFERENCES slides(id) ON DELETE SET NULL,
                annotation_id INT REFERENCES annotations(id) ON DELETE SET NULL,
                text VARCHAR(MAX) NOT NULL DEFAULT '',
                sort_order INT NOT NULL DEFAULT 0
            )
        """)
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
        try:
            from services.ai_engine import init_ai_schema
            init_ai_schema(connect_fn)
        except Exception:
            pass
