# AI subscription policy

`users.is_premium` is a synchronized display/cache field. It is never an authorization source. Effective access is calculated from an active, date-valid `user_subscriptions` row, an enabled `user_subscription_features` grant, and an implemented and active `ai_features` row.

Business rules:

- A user can have only one subscription with status `Active`; SQL Server enforces this with a filtered unique index.
- Cancellation and suspension are immediate and block new AI execution.
- End dates are exclusive UTC timestamps. Expired active rows are changed to `Expired` when subscription data is read.
- An AI job authorized and started before expiry may finish. Expiration does not terminate an in-flight worker.
- Historical AI results, overlays, galleries, and reports remain readable after expiry, subject to normal role and assigned-case authorization.
- Planned features can be preassigned but cannot execute until both `is_implemented` and `is_active` are true.
- Only the protected `ETDDev` and `ETDadmin` accounts receive automatic access to implemented, active AI features without a paid subscription. Ordinary System Administrator accounts require a valid subscription and feature grant.
- Protected super administrators can globally enable or disable catalog features. An inactive feature cannot be executed by any account.
- Subscription management is restricted to the protected `ETDDev` and `ETDadmin` super-administrator accounts; ordinary System Administrator accounts cannot view or mutate subscription records.
