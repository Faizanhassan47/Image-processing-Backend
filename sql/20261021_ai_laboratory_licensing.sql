/*
  Additive Hello Lica laboratory AI licensing schema.
  Pre-flight backup: take and verify a full SQL Server backup before execution.
  This script never updates or deletes dbo.user_subscriptions.
*/
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.schema_migrations','U') IS NULL
CREATE TABLE dbo.schema_migrations (
  migration_key VARCHAR(120) NOT NULL PRIMARY KEY,
  applied_at DATETIME2(3) NOT NULL CONSTRAINT DF_schema_migrations_applied DEFAULT SYSUTCDATETIME()
);

GO

  IF OBJECT_ID('dbo.laboratories','U') IS NULL
  CREATE TABLE dbo.laboratories (
    id INT IDENTITY PRIMARY KEY, laboratory_code VARCHAR(40) NOT NULL,
    laboratory_name NVARCHAR(200) NOT NULL, description NVARCHAR(1000) NULL,
    contact_email VARCHAR(320) NULL, contact_phone VARCHAR(40) NULL,
    status VARCHAR(20) NOT NULL CONSTRAINT DF_laboratories_status DEFAULT 'Active',
    created_by INT NULL REFERENCES dbo.users(id), created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_laboratories_code UNIQUE(laboratory_code),
    CONSTRAINT CK_laboratories_status CHECK(status IN ('Active','Inactive','Suspended'))
  );

  IF OBJECT_ID('dbo.user_laboratory_memberships','U') IS NULL
  CREATE TABLE dbo.user_laboratory_memberships (
    id INT IDENTITY PRIMARY KEY, user_id INT NOT NULL REFERENCES dbo.users(id),
    laboratory_id INT NOT NULL REFERENCES dbo.laboratories(id), is_active BIT NOT NULL DEFAULT 1,
    joined_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(), created_by INT NULL REFERENCES dbo.users(id),
    CONSTRAINT UQ_user_laboratory_membership UNIQUE(user_id,laboratory_id)
  );

  IF COL_LENGTH('dbo.ai_features','implementation_status') IS NULL
    ALTER TABLE dbo.ai_features ADD implementation_status VARCHAR(20) NOT NULL CONSTRAINT DF_ai_features_impl DEFAULT 'ComingSoon';
GO
  IF COL_LENGTH('dbo.ai_features','display_order') IS NULL
    ALTER TABLE dbo.ai_features ADD display_order INT NOT NULL CONSTRAINT DF_ai_features_order DEFAULT 100;
GO
  IF COL_LENGTH('dbo.ai_features','updated_at') IS NULL
    ALTER TABLE dbo.ai_features ADD updated_at DATETIME2(3) NOT NULL CONSTRAINT DF_ai_features_updated DEFAULT SYSUTCDATETIME();
GO

  IF NOT EXISTS (SELECT 1 FROM dbo.ai_features WHERE feature_code='MITOTIC_DETECTION')
    INSERT dbo.ai_features(feature_code,feature_name,description,is_implemented,is_active,implementation_status,display_order)
    VALUES('MITOTIC_DETECTION','Mitotic Figure Detection','Identifies candidate mitotic figures to assist pathologist assessment.',0,1,'ComingSoon',30);

  IF OBJECT_ID('dbo.ai_subscription_plans','U') IS NULL
  CREATE TABLE dbo.ai_subscription_plans (
    id INT IDENTITY PRIMARY KEY, feature_id INT NOT NULL REFERENCES dbo.ai_features(id),
    plan_name NVARCHAR(120) NOT NULL, billing_period VARCHAR(20) NOT NULL, duration_months INT NOT NULL,
    price_per_seat DECIMAL(19,4) NOT NULL, currency CHAR(3) NOT NULL,
    minimum_seats INT NOT NULL DEFAULT 1, maximum_seats INT NULL, is_active BIT NOT NULL DEFAULT 1,
    purchasing_enabled BIT NOT NULL DEFAULT 1, created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_ai_plans_period CHECK(billing_period IN ('Monthly','Quarterly','Annual','Custom')),
    CONSTRAINT CK_ai_plans_values CHECK(duration_months>0 AND price_per_seat>=0 AND minimum_seats>0 AND (maximum_seats IS NULL OR maximum_seats>=minimum_seats)),
    CONSTRAINT UQ_ai_plan_name UNIQUE(feature_id,plan_name,currency)
  );

  IF OBJECT_ID('dbo.laboratory_subscriptions','U') IS NULL
  CREATE TABLE dbo.laboratory_subscriptions (
    id INT IDENTITY PRIMARY KEY, laboratory_id INT NOT NULL REFERENCES dbo.laboratories(id),
    plan_id INT NULL REFERENCES dbo.ai_subscription_plans(id), feature_id INT NOT NULL REFERENCES dbo.ai_features(id),
    seat_quantity INT NOT NULL, start_date DATETIME2(3) NOT NULL, end_date DATETIME2(3) NOT NULL,
    status VARCHAR(20) NOT NULL, payment_status VARCHAR(20) NOT NULL,
    subscription_source VARCHAR(30) NOT NULL, created_by INT NOT NULL REFERENCES dbo.users(id),
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(), updated_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    row_version ROWVERSION,
    CONSTRAINT CK_lab_sub_seats CHECK(seat_quantity>0), CONSTRAINT CK_lab_sub_dates CHECK(end_date>start_date),
    CONSTRAINT CK_lab_sub_status CHECK(status IN ('Pending','Active','Suspended','Expired','Cancelled')),
    CONSTRAINT CK_lab_sub_payment CHECK(payment_status IN ('NotRequired','Pending','Processing','Succeeded','Failed','Cancelled','Refunded')),
    CONSTRAINT CK_lab_sub_source CHECK(subscription_source IN ('Payment','SuperAdminGrant','Migration'))
  );

  IF OBJECT_ID('dbo.ai_seat_assignments','U') IS NULL
  CREATE TABLE dbo.ai_seat_assignments (
    id INT IDENTITY PRIMARY KEY, laboratory_subscription_id INT NOT NULL REFERENCES dbo.laboratory_subscriptions(id),
    laboratory_id INT NOT NULL REFERENCES dbo.laboratories(id), user_id INT NOT NULL REFERENCES dbo.users(id),
    feature_id INT NOT NULL REFERENCES dbo.ai_features(id), assigned_by INT NOT NULL REFERENCES dbo.users(id),
    assigned_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(), revoked_by INT NULL REFERENCES dbo.users(id),
    revoked_at DATETIME2(3) NULL, status VARCHAR(20) NOT NULL DEFAULT 'Active',
    CONSTRAINT CK_ai_seat_status CHECK(status IN ('Active','Revoked'))
  );
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.ai_seat_assignments') AND name='UX_ai_seat_active_user_feature')
    CREATE UNIQUE INDEX UX_ai_seat_active_user_feature ON dbo.ai_seat_assignments(laboratory_id,user_id,feature_id) WHERE status='Active';

  IF OBJECT_ID('dbo.subscription_payments','U') IS NULL
  CREATE TABLE dbo.subscription_payments (
    id INT IDENTITY PRIMARY KEY, laboratory_id INT NOT NULL REFERENCES dbo.laboratories(id),
    subscription_id INT NULL REFERENCES dbo.laboratory_subscriptions(id), plan_id INT NOT NULL REFERENCES dbo.ai_subscription_plans(id),
    seat_quantity INT NOT NULL, payment_provider VARCHAR(60) NOT NULL, provider_transaction_id VARCHAR(200) NULL,
    provider_checkout_id VARCHAR(200) NULL, amount DECIMAL(19,4) NOT NULL, currency CHAR(3) NOT NULL,
    payment_status VARCHAR(20) NOT NULL, payment_method VARCHAR(60) NULL, initiated_by INT NOT NULL REFERENCES dbo.users(id),
    created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(), paid_at DATETIME2(3) NULL,
    updated_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_subscription_payment_amount CHECK(amount>=0 AND seat_quantity>0),
    CONSTRAINT CK_subscription_payment_status CHECK(payment_status IN ('Pending','Processing','Succeeded','Failed','Cancelled','Refunded'))
  );
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.subscription_payments') AND name='UX_payment_provider_transaction')
    CREATE UNIQUE INDEX UX_payment_provider_transaction ON dbo.subscription_payments(payment_provider,provider_transaction_id) WHERE provider_transaction_id IS NOT NULL;
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.subscription_payments') AND name='UX_payment_provider_checkout')
    CREATE UNIQUE INDEX UX_payment_provider_checkout ON dbo.subscription_payments(payment_provider,provider_checkout_id) WHERE provider_checkout_id IS NOT NULL;

  IF OBJECT_ID('dbo.payment_webhook_events','U') IS NULL
  CREATE TABLE dbo.payment_webhook_events (
    id BIGINT IDENTITY PRIMARY KEY, payment_provider VARCHAR(60) NOT NULL, provider_event_id VARCHAR(200) NOT NULL,
    event_type VARCHAR(100) NOT NULL, processing_status VARCHAR(20) NOT NULL DEFAULT 'Received',
    payload_hash CHAR(64) NOT NULL, error_message NVARCHAR(1000) NULL,
    received_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(), processed_at DATETIME2(3) NULL,
    CONSTRAINT UQ_payment_webhook_event UNIQUE(payment_provider,provider_event_id),
    CONSTRAINT CK_webhook_status CHECK(processing_status IN ('Received','Processed','Rejected','Failed'))
  );

  IF OBJECT_ID('dbo.ai_subscription_audit_logs','U') IS NULL
  CREATE TABLE dbo.ai_subscription_audit_logs (
    id BIGINT IDENTITY PRIMARY KEY, actor_user_id INT NULL REFERENCES dbo.users(id), laboratory_id INT NULL REFERENCES dbo.laboratories(id),
    target_user_id INT NULL REFERENCES dbo.users(id), action VARCHAR(80) NOT NULL, resource_type VARCHAR(60) NOT NULL,
    resource_id VARCHAR(100) NULL, before_metadata NVARCHAR(MAX) NULL, after_metadata NVARCHAR(MAX) NULL,
    correlation_id VARCHAR(100) NULL, created_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_ai_audit_before_json CHECK(before_metadata IS NULL OR ISJSON(before_metadata)=1),
    CONSTRAINT CK_ai_audit_after_json CHECK(after_metadata IS NULL OR ISJSON(after_metadata)=1)
  );

  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.user_laboratory_memberships') AND name='IX_membership_laboratory_active') CREATE INDEX IX_membership_laboratory_active ON dbo.user_laboratory_memberships(laboratory_id,is_active,user_id);
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.laboratory_subscriptions') AND name='IX_lab_sub_access') CREATE INDEX IX_lab_sub_access ON dbo.laboratory_subscriptions(laboratory_id,feature_id,status,end_date);
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.ai_seat_assignments') AND name='IX_ai_seat_subscription_status') CREATE INDEX IX_ai_seat_subscription_status ON dbo.ai_seat_assignments(laboratory_subscription_id,status);
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.subscription_payments') AND name='IX_payment_lab_status') CREATE INDEX IX_payment_lab_status ON dbo.subscription_payments(laboratory_id,payment_status,created_at);
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.ai_subscription_audit_logs') AND name='IX_ai_audit_lab_created') CREATE INDEX IX_ai_audit_lab_created ON dbo.ai_subscription_audit_logs(laboratory_id,created_at DESC);

  IF NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE migration_key='20261021_ai_laboratory_licensing')
    INSERT dbo.schema_migrations(migration_key) VALUES('20261021_ai_laboratory_licensing');
GO

/* Verification: must return the same legacy IDs/dates/status values captured before migration. */
SELECT id,user_id,plan_name,start_date,end_date,status,created_by,created_at,updated_at
FROM dbo.user_subscriptions ORDER BY id;

/* Rollback: only after confirming no production data has entered the new tables. Drop in FK order.
DROP TABLE dbo.ai_subscription_audit_logs, dbo.payment_webhook_events, dbo.subscription_payments,
 dbo.ai_seat_assignments, dbo.laboratory_subscriptions, dbo.ai_subscription_plans,
 dbo.user_laboratory_memberships, dbo.laboratories;
DELETE dbo.schema_migrations WHERE migration_key='20261021_ai_laboratory_licensing';
*/
