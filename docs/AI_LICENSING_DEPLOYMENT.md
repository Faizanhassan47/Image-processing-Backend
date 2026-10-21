# AI licensing deployment and operations

## Current compatibility boundary

`dbo.user_subscriptions` remains the legacy, user-level subscription source. Rows are not copied, reclassified, or used as proof of payment. The laboratory model uses new tables and does not assign existing users to laboratories automatically. Existing HoVer-Net authorization continues through the legacy service until a laboratory and seat migration is explicitly approved.

The protected bootstrap usernames remain compatible with earlier deployments, but new Super Administrators receive the literal `Super Administrator` role through `PATCH /api/super-admin/users/{id}/role`. Ordinary System Administrators cannot call that endpoint or assign the protected role through normal user management.

## Before migration

1. Take a full SQL Server backup and verify it can be restored.
2. Export this immutable reconciliation query:

   ```sql
   SELECT id,user_id,plan_name,start_date,end_date,status,created_by,created_at,updated_at
   FROM dbo.user_subscriptions ORDER BY id;
   ```

3. Confirm no application job is writing schema changes.
4. Run `sql/20261021_ai_laboratory_licensing.sql` using an account permitted to create tables, indexes, constraints, and foreign keys.
5. Compare the reconciliation query byte-for-byte with the pre-migration export.

The migration is additive and idempotent. It creates laboratories, memberships, plans, laboratory subscriptions, historical seat assignments, payment records, idempotent webhook events, and AI-specific audit logs. It adds catalog metadata and the missing Mitotic Figure Detection entry. It does not remove the pre-existing Tissue Segmentation catalog entry because deleting established catalog data would violate backward compatibility.

## Configuration

Use `.env.example` as the configuration template. Payment is disabled by default. Do not set a provider name until its adapter implements official signature verification, server-side status verification, amount/currency matching, idempotent webhook processing, and refunds where applicable.

No browser redirect activates a license. With no configured provider, checkout returns HTTP 503 and `PAYMENT_PROVIDER_NOT_CONFIGURED`.

## Rollback

Restore the verified backup for a full rollback. The commented rollback block at the end of the migration may be used only if all new tables are confirmed empty. Never drop those tables after laboratories, payments, grants, assignments, or audit records have been created.

## Access policy

- Super Administrator: implemented and globally enabled AI features are exempt from subscription/payment checks.
- Other users: active user, active laboratory membership, active laboratory subscription, successful payment or complimentary source, and active seat are required.
- Subscription expiry and suspension are checked at request time; access does not depend on a scheduler.
- Seat allocation locks the parent subscription and current assignments with `UPDLOCK, HOLDLOCK`, preventing two requests from consuming the final seat.
- User preference is separate from licensing. Turning “Use this feature” off hides it from the user workflow but does not release the purchased seat.

## Known external blocker

A commercial payment provider has not been selected and no merchant credentials were supplied. The provider interface and database structures are ready, but live checkout/webhooks/refunds must remain disabled until an approved adapter is implemented and tested against that provider's official sandbox.
