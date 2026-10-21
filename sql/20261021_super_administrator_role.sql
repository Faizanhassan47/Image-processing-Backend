/* Add the distinct Super Administrator role and enforce the platform maximum. */
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

BEGIN TRANSACTION;

IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('dbo.users') AND name='CK_users_approved_roles')
  ALTER TABLE dbo.users DROP CONSTRAINT CK_users_approved_roles;

ALTER TABLE dbo.users WITH CHECK ADD CONSTRAINT CK_users_approved_roles CHECK (
  role IN ('Super Administrator','System Administrator','Laboratory Manager','Pathologist')
);

UPDATE dbo.users
SET role='Super Administrator'
WHERE LOWER(username) IN ('etddev','etdadmin');

IF (SELECT COUNT(*) FROM dbo.users WHERE role='Super Administrator') > 2
  THROW 51000, 'A maximum of two Super Administrators is allowed.', 1;

COMMIT TRANSACTION;
GO

CREATE OR ALTER TRIGGER dbo.TR_users_max_two_super_administrators
ON dbo.users
AFTER INSERT, UPDATE
AS
BEGIN
  SET NOCOUNT ON;
  IF (SELECT COUNT(*) FROM dbo.users WITH (UPDLOCK,HOLDLOCK) WHERE role='Super Administrator') > 2
  BEGIN
    ROLLBACK TRANSACTION;
    THROW 51001, 'A maximum of two Super Administrators is allowed.', 1;
  END
END;
GO

IF OBJECT_ID('dbo.schema_migrations','U') IS NOT NULL
AND NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE migration_key='20261021_super_administrator_role')
  INSERT dbo.schema_migrations(migration_key) VALUES('20261021_super_administrator_role');
GO

SELECT id,username,display_name,role,is_active
FROM dbo.users
WHERE LOWER(username) IN ('etddev','etdadmin') OR role='Super Administrator'
ORDER BY id;
