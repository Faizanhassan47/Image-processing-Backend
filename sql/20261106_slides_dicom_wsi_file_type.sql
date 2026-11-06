-- ==============================================================================
-- 20261106_slides_dicom_wsi_file_type.sql
--
-- Allow DICOM VL Whole Slide Microscopy rows to use file_type = 'dicom-wsi'.
-- Existing raster and wsi values are preserved. No tables or rows are dropped.
--
-- Review and apply this script to each environment manually.
-- Do not run it from application startup.
-- ==============================================================================

SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

BEGIN TRANSACTION;

IF OBJECT_ID('dbo.slides', 'U') IS NULL
BEGIN
    THROW 51010, 'dbo.slides does not exist. Apply the base schema before this migration.', 1;
END;

-- Drop the named schema.sql constraint when present.
IF EXISTS (
    SELECT 1
    FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('dbo.slides')
      AND name = 'CK_slides_file_type'
)
BEGIN
    ALTER TABLE dbo.slides DROP CONSTRAINT CK_slides_file_type;
END;

-- Drop unnamed CHECK constraints created by older init_tables installs that
-- still restrict file_type to raster/wsi only.
DECLARE @constraint_name SYSNAME;
DECLARE constraint_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT cc.name
    FROM sys.check_constraints AS cc
    WHERE cc.parent_object_id = OBJECT_ID('dbo.slides')
      AND cc.definition LIKE '%file_type%'
      AND cc.definition LIKE '%raster%'
      AND cc.definition LIKE '%wsi%'
      AND cc.definition NOT LIKE '%dicom-wsi%';

OPEN constraint_cursor;
FETCH NEXT FROM constraint_cursor INTO @constraint_name;
WHILE @@FETCH_STATUS = 0
BEGIN
    DECLARE @drop_sql NVARCHAR(400) = N'ALTER TABLE dbo.slides DROP CONSTRAINT ' + QUOTENAME(@constraint_name);
    EXEC sp_executesql @drop_sql;
    FETCH NEXT FROM constraint_cursor INTO @constraint_name;
END;
CLOSE constraint_cursor;
DEALLOCATE constraint_cursor;

ALTER TABLE dbo.slides WITH CHECK
ADD CONSTRAINT CK_slides_file_type
CHECK (file_type IN ('raster', 'wsi', 'dicom-wsi'));

-- Confirm existing clinical rows still satisfy the new constraint.
IF EXISTS (
    SELECT 1
    FROM dbo.slides
    WHERE file_type NOT IN ('raster', 'wsi', 'dicom-wsi')
)
BEGIN
    THROW 51011, 'slides.file_type contains values outside raster, wsi, and dicom-wsi.', 1;
END;

IF OBJECT_ID('dbo.schema_migrations', 'U') IS NOT NULL
BEGIN
    IF NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE filename = '20261106_slides_dicom_wsi_file_type.sql')
        INSERT INTO dbo.schema_migrations (filename) VALUES ('20261106_slides_dicom_wsi_file_type.sql');
END;

COMMIT TRANSACTION;
GO
