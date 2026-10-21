SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO
IF COL_LENGTH('dbo.ai_features','catalog_category') IS NULL ALTER TABLE dbo.ai_features ADD catalog_category NVARCHAR(80) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_summary') IS NULL ALTER TABLE dbo.ai_features ADD catalog_summary NVARCHAR(500) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_purpose') IS NULL ALTER TABLE dbo.ai_features ADD catalog_purpose NVARCHAR(1000) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_value') IS NULL ALTER TABLE dbo.ai_features ADD catalog_value NVARCHAR(1000) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_outputs_json') IS NULL ALTER TABLE dbo.ai_features ADD catalog_outputs_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.ai_features','is_catalog_visible') IS NULL ALTER TABLE dbo.ai_features ADD is_catalog_visible BIT NOT NULL CONSTRAINT DF_ai_features_catalog_visible DEFAULT 1;
IF COL_LENGTH('dbo.ai_features','is_featured') IS NULL ALTER TABLE dbo.ai_features ADD is_featured BIT NOT NULL CONSTRAINT DF_ai_features_featured DEFAULT 0;
IF COL_LENGTH('dbo.ai_features','catalog_title') IS NULL ALTER TABLE dbo.ai_features ADD catalog_title NVARCHAR(160) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_display_order') IS NULL ALTER TABLE dbo.ai_features ADD catalog_display_order INT NOT NULL CONSTRAINT DF_ai_features_catalog_display_order DEFAULT 0;
IF COL_LENGTH('dbo.ai_features','is_catalog_deleted') IS NULL ALTER TABLE dbo.ai_features ADD is_catalog_deleted BIT NOT NULL CONSTRAINT DF_ai_features_catalog_deleted DEFAULT 0;
IF COL_LENGTH('dbo.ai_features','catalog_session_price_usd') IS NULL ALTER TABLE dbo.ai_features ADD catalog_session_price_usd DECIMAL(12,2) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_usd_to_pkr_rate') IS NULL ALTER TABLE dbo.ai_features ADD catalog_usd_to_pkr_rate DECIMAL(12,4) NULL;
IF COL_LENGTH('dbo.ai_features','catalog_session_price_pkr') IS NULL ALTER TABLE dbo.ai_features ADD catalog_session_price_pkr DECIMAL(14,2) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name='CK_ai_features_catalog_outputs_json')
  ALTER TABLE dbo.ai_features ADD CONSTRAINT CK_ai_features_catalog_outputs_json CHECK(catalog_outputs_json IS NULL OR ISJSON(catalog_outputs_json)=1);
GO
