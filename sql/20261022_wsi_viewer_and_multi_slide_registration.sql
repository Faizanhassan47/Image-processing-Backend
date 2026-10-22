-- ==============================================================================
-- 20261022_wsi_viewer_and_multi_slide_registration.sql
-- Schema migration for:
-- 1. slide_viewing_trails (Review Coverage Tracking)
-- 2. slide_registrations (Pairwise Multi-Slide Tissue Registration)
-- 3. slide_registration_history (Audit Trail for Registrations)
-- 4. Extend viewer_bookmarks with viewpoint attributes (flip, brightness, contrast, gamma, etc.)
-- ==============================================================================

-- 1. Slide Viewing Trails Table
IF OBJECT_ID('dbo.slide_viewing_trails', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.slide_viewing_trails (
        id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slide_viewing_trails PRIMARY KEY,
        slide_id        INT NOT NULL CONSTRAINT FK_slide_viewing_trails_slide REFERENCES dbo.slides(id) ON DELETE CASCADE,
        username        VARCHAR(255) NOT NULL,
        coverage_json   NVARCHAR(MAX) NOT NULL CONSTRAINT CK_slide_viewing_trails_json CHECK (ISJSON(coverage_json) = 1),
        created_at      DATETIME2 NOT NULL CONSTRAINT DF_slide_viewing_trails_created DEFAULT (SYSUTCDATETIME()),
        updated_at      DATETIME2 NOT NULL CONSTRAINT DF_slide_viewing_trails_updated DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_slide_viewing_trails UNIQUE (slide_id, username)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_slide_viewing_trails_user' AND object_id = OBJECT_ID('dbo.slide_viewing_trails'))
BEGIN
    CREATE INDEX idx_slide_viewing_trails_user ON dbo.slide_viewing_trails(username, updated_at DESC);
END;
GO

-- 2. Slide Registrations Table
IF OBJECT_ID('dbo.slide_registrations', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.slide_registrations (
        id                          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slide_registrations PRIMARY KEY,
        case_id                     INT NOT NULL CONSTRAINT FK_slide_registrations_case REFERENCES dbo.cases(id) ON DELETE CASCADE,
        reference_slide_id          INT NOT NULL CONSTRAINT FK_slide_registrations_ref_slide REFERENCES dbo.slides(id),
        moving_slide_id             INT NOT NULL CONSTRAINT FK_slide_registrations_mov_slide REFERENCES dbo.slides(id),
        reference_image_version_id  INT NULL CONSTRAINT FK_slide_registrations_ref_ver REFERENCES dbo.image_versions(id),
        moving_image_version_id     INT NULL CONSTRAINT FK_slide_registrations_mov_ver REFERENCES dbo.image_versions(id),
        registration_method         VARCHAR(60) NOT NULL CONSTRAINT DF_slide_reg_method DEFAULT ('manual')
                                    CONSTRAINT CK_slide_reg_method CHECK (registration_method IN ('manual', 'automatic')),
        transform_type              VARCHAR(60) NOT NULL CONSTRAINT DF_slide_reg_transform DEFAULT ('affine')
                                    CONSTRAINT CK_slide_reg_transform CHECK (transform_type IN ('translation', 'rigid', 'similarity', 'affine')),
        transform_matrix_json       NVARCHAR(MAX) NULL CONSTRAINT CK_slide_reg_matrix CHECK (transform_matrix_json IS NULL OR ISJSON(transform_matrix_json) = 1),
        landmarks_json              NVARCHAR(MAX) NOT NULL CONSTRAINT DF_slide_reg_landmarks DEFAULT ('[]')
                                    CONSTRAINT CK_slide_reg_landmarks CHECK (ISJSON(landmarks_json) = 1),
        quality_metrics_json        NVARCHAR(MAX) NULL CONSTRAINT CK_slide_reg_metrics CHECK (quality_metrics_json IS NULL OR ISJSON(quality_metrics_json) = 1),
        status                      VARCHAR(30) NOT NULL CONSTRAINT DF_slide_reg_status DEFAULT ('Draft')
                                    CONSTRAINT CK_slide_reg_status CHECK (status IN ('Draft', 'Calculated', 'Reviewed', 'Rejected')),
        review_notes                NVARCHAR(1000) NULL,
        created_by                  VARCHAR(255) NOT NULL,
        created_at                  DATETIME2 NOT NULL CONSTRAINT DF_slide_reg_created DEFAULT (SYSUTCDATETIME()),
        updated_at                  DATETIME2 NOT NULL CONSTRAINT DF_slide_reg_updated DEFAULT (SYSUTCDATETIME()),
        approved_by                 VARCHAR(255) NULL,
        approved_at                 DATETIME2 NULL
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_slide_registrations_case' AND object_id = OBJECT_ID('dbo.slide_registrations'))
BEGIN
    CREATE INDEX idx_slide_registrations_case ON dbo.slide_registrations(case_id, id DESC);
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_slide_registrations_slides' AND object_id = OBJECT_ID('dbo.slide_registrations'))
BEGIN
    CREATE INDEX idx_slide_registrations_slides ON dbo.slide_registrations(reference_slide_id, moving_slide_id);
END;
GO

-- 3. Slide Registration History Table
IF OBJECT_ID('dbo.slide_registration_history', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.slide_registration_history (
        id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_slide_registration_history PRIMARY KEY,
        registration_id     INT NOT NULL CONSTRAINT FK_slide_reg_history_reg REFERENCES dbo.slide_registrations(id) ON DELETE CASCADE,
        action              VARCHAR(60) NOT NULL,
        actor               VARCHAR(255) NOT NULL,
        details_json        NVARCHAR(MAX) NULL,
        created_at          DATETIME2 NOT NULL CONSTRAINT DF_slide_reg_history_created DEFAULT (SYSUTCDATETIME())
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_slide_reg_history_reg' AND object_id = OBJECT_ID('dbo.slide_registration_history'))
BEGIN
    CREATE INDEX idx_slide_reg_history_reg ON dbo.slide_registration_history(registration_id, id DESC);
END;
GO

-- 4. Extend viewer_bookmarks with Viewpoint Attributes
IF COL_LENGTH('dbo.viewer_bookmarks', 'flip_h') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD flip_h BIT NOT NULL CONSTRAINT DF_viewer_bookmarks_flip_h DEFAULT (0);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'flip_v') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD flip_v BIT NOT NULL CONSTRAINT DF_viewer_bookmarks_flip_v DEFAULT (0);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'brightness') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD brightness INT NOT NULL CONSTRAINT DF_viewer_bookmarks_brightness DEFAULT (100);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'contrast') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD contrast INT NOT NULL CONSTRAINT DF_viewer_bookmarks_contrast DEFAULT (100);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'gamma') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD gamma FLOAT NOT NULL CONSTRAINT DF_viewer_bookmarks_gamma DEFAULT (1.0);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'annotations_visible') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD annotations_visible BIT NOT NULL CONSTRAINT DF_viewer_bookmarks_annotations_visible DEFAULT (1);
END;
GO

IF COL_LENGTH('dbo.viewer_bookmarks', 'minimap_visible') IS NULL
BEGIN
    ALTER TABLE dbo.viewer_bookmarks ADD minimap_visible BIT NOT NULL CONSTRAINT DF_viewer_bookmarks_minimap_visible DEFAULT (1);
END;
GO
