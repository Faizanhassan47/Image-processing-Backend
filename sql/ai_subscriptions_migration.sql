SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH('users','is_premium') IS NULL
    ALTER TABLE users ADD is_premium BIT NOT NULL CONSTRAINT DF_users_is_premium DEFAULT 0;
GO

IF OBJECT_ID('ai_features','U') IS NULL
CREATE TABLE ai_features (
    id INT IDENTITY(1,1) PRIMARY KEY, feature_code VARCHAR(64) NOT NULL UNIQUE,
    feature_name VARCHAR(120) NOT NULL, description VARCHAR(1000) NOT NULL,
    is_implemented BIT NOT NULL DEFAULT 0, is_active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

MERGE ai_features AS target
USING (VALUES
 ('HOVERNET','HoVer-Net','Detects and segments cell nuclei, classifies nuclear types, and provides counts, density, and morphology measurements.',1),
 ('IMAGE_QC','Image Quality Control','Evaluates image quality, including blur, focus problems, tissue folds, and artifacts.',0),
 ('TUMOR_DETECTION','Tumor Detection','Identifies candidate tumor-containing regions for research review.',0),
 ('TISSUE_SEGMENTATION','Tissue Segmentation','Identifies tissue structures and regions, such as stroma and epithelium, when supported by an appropriate model.',0)
) AS source(feature_code,feature_name,description,is_implemented)
ON target.feature_code=source.feature_code
WHEN NOT MATCHED THEN INSERT(feature_code,feature_name,description,is_implemented,is_active)
VALUES(source.feature_code,source.feature_name,source.description,source.is_implemented,1);
GO

IF OBJECT_ID('user_subscriptions','U') IS NULL
CREATE TABLE user_subscriptions (
 id INT IDENTITY(1,1) PRIMARY KEY, user_id INT NOT NULL REFERENCES users(id), plan_name VARCHAR(120) NOT NULL,
 start_date DATETIME2 NOT NULL, end_date DATETIME2 NOT NULL, status VARCHAR(20) NOT NULL,
 created_by VARCHAR(255) NOT NULL, created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
 CONSTRAINT CK_user_subscriptions_status CHECK(status IN ('Active','Expired','Cancelled','Suspended')),
 CONSTRAINT CK_user_subscriptions_dates CHECK(end_date>start_date)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_user_subscriptions_one_active' AND object_id=OBJECT_ID('user_subscriptions'))
CREATE UNIQUE INDEX UX_user_subscriptions_one_active ON user_subscriptions(user_id) WHERE status='Active';
GO

IF OBJECT_ID('user_subscription_features','U') IS NULL
CREATE TABLE user_subscription_features (
 id INT IDENTITY(1,1) PRIMARY KEY, subscription_id INT NOT NULL REFERENCES user_subscriptions(id) ON DELETE CASCADE,
 feature_id INT NOT NULL REFERENCES ai_features(id), is_enabled BIT NOT NULL DEFAULT 1,
 granted_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(), CONSTRAINT UQ_user_subscription_features UNIQUE(subscription_id,feature_id)
);
GO

IF OBJECT_ID('user_ai_feature_preferences','U') IS NULL
CREATE TABLE user_ai_feature_preferences (
 id INT IDENTITY(1,1) PRIMARY KEY, user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 feature_id INT NOT NULL REFERENCES ai_features(id) ON DELETE CASCADE, is_enabled BIT NOT NULL DEFAULT 1,
 updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
 CONSTRAINT UQ_user_ai_feature_preferences UNIQUE(user_id,feature_id)
);
GO
