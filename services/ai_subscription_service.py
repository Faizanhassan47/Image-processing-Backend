"""Database-backed AI feature subscriptions and effective entitlements."""

from contextlib import closing
from datetime import datetime, timezone

from fastapi import HTTPException

HOVERNET = "HOVERNET"
VALID_STATUSES = {"Active", "Expired", "Cancelled", "Suspended"}
PROTECTED_SUPER_ADMINS = {"etddev", "etdadmin"}


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AISubscriptionService:
    """IsPremium is a synchronized cache; authorization always uses joined subscription data."""

    def __init__(self, connect):
        self.connect = connect

    def expire_due_subscriptions(self, db=None):
        owns_connection = db is None
        context = closing(self.connect()) if owns_connection else None
        conn = context.__enter__() if context else db
        try:
            now = utcnow_naive()
            conn.execute("UPDATE user_subscriptions SET status='Expired', updated_at=? WHERE status='Active' AND end_date<=?", (now, now))
            conn.execute("""UPDATE users SET is_premium=CASE WHEN EXISTS (
                SELECT 1 FROM user_subscriptions s WHERE s.user_id=users.id AND s.status='Active'
                AND s.start_date<=? AND s.end_date>?) THEN 1 ELSE 0 END""", (now, now))
            if owns_connection:
                conn.commit()
        finally:
            if context:
                context.__exit__(None, None, None)

    def list_features(self, user_id=None):
        with closing(self.connect()) as db:
            self.expire_due_subscriptions(db)
            params = [user_id, user_id] if user_id is not None else [None, None]
            rows = db.execute("""SELECT f.id, f.feature_code, f.feature_name, f.description,
                       f.is_implemented, f.is_active, f.created_at, f.catalog_category,
                       f.catalog_summary, f.catalog_purpose, f.catalog_value,
                       f.catalog_outputs_json, f.is_catalog_visible, f.is_featured, f.catalog_title,
                       f.catalog_display_order, f.is_catalog_deleted, f.catalog_session_price_usd,
                       f.catalog_usd_to_pkr_rate, f.catalog_session_price_pkr,
                       COALESCE(p.is_enabled,1) AS user_enabled,
                       CASE WHEN sf.is_enabled=1 AND s.status='Active' AND s.start_date<=SYSUTCDATETIME()
                         AND s.end_date>SYSUTCDATETIME() THEN 1 ELSE 0 END AS is_entitled
                FROM ai_features f
                LEFT JOIN user_ai_feature_preferences p ON p.feature_id=f.id AND p.user_id=?
                LEFT JOIN user_subscription_features sf ON sf.feature_id=f.id
                LEFT JOIN user_subscriptions s ON s.id=sf.subscription_id AND s.user_id=?
                WHERE (? IS NULL OR s.id IS NOT NULL OR NOT EXISTS (
                    SELECT 1 FROM user_subscription_features x JOIN user_subscriptions y ON y.id=x.subscription_id
                    WHERE x.feature_id=f.id AND y.user_id=?))
                ORDER BY f.catalog_display_order, f.id""", [user_id, user_id, user_id, user_id] if user_id is not None else [None, None, None, None]).fetchall()
            db.commit()
            unique = {}
            for row in rows:
                item = dict(row)
                code = item["feature_code"]
                if code not in unique or item["is_entitled"]:
                    unique[code] = item
            return list(unique.values())

    def subscription_for_user(self, user_id, include_history=False):
        with closing(self.connect()) as db:
            self.expire_due_subscriptions(db)
            limit = "" if include_history else "TOP 1"
            rows = db.execute(f"""SELECT {limit} s.*, u.username, u.display_name, u.role, u.is_premium
                FROM user_subscriptions s JOIN users u ON u.id=s.user_id
                WHERE s.user_id=? ORDER BY CASE s.status WHEN 'Active' THEN 0 ELSE 1 END, s.created_at DESC""", (user_id,)).fetchall()
            output = [self._with_features(db, dict(row)) for row in rows]
            db.commit()
            return output if include_history else (output[0] if output else None)

    def list_subscriptions(self):
        with closing(self.connect()) as db:
            self.expire_due_subscriptions(db)
            users = db.execute("SELECT id,username,display_name,role,is_active,is_premium FROM users ORDER BY display_name,username").fetchall()
            result = []
            for user in users:
                item = dict(user)
                subscriptions = db.execute("SELECT * FROM user_subscriptions WHERE user_id=? ORDER BY created_at DESC", (item["id"],)).fetchall()
                item["subscriptions"] = [self._with_features(db, dict(row)) for row in subscriptions]
                result.append(item)
            db.commit()
            return result

    def create(self, user_id, plan_name, start_date, end_date, feature_codes, actor):
        if end_date <= start_date:
            raise HTTPException(400, "End date must be after start date")
        with closing(self.connect()) as db:
            user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise HTTPException(404, "User not found")
            conflict = db.execute("SELECT id FROM user_subscriptions WHERE user_id=? AND status='Active'", (user_id,)).fetchone()
            if conflict:
                raise HTTPException(409, "A user may have only one Active subscription")
            now = utcnow_naive()
            status = "Expired" if end_date <= now else "Active"
            cur = db.execute("""INSERT INTO user_subscriptions
                (user_id,plan_name,start_date,end_date,status,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", (user_id, plan_name.strip(), start_date, end_date, status, actor, now, now))
            subscription_id = cur.lastrowid
            self._replace_features(db, subscription_id, feature_codes, now)
            self._enable_features_for_user(db, user_id, feature_codes, now)
            self._sync_user(db, user_id, now)
            db.commit()
            return self._get(db, subscription_id)

    def update(self, subscription_id, plan_name=None, start_date=None, end_date=None, status=None):
        with closing(self.connect()) as db:
            current = self._get(db, subscription_id)
            new_start = start_date or current["start_date"]
            new_end = end_date or current["end_date"]
            new_status = status or current["status"]
            if new_status not in VALID_STATUSES:
                raise HTTPException(400, "Invalid subscription status")
            if new_end <= new_start:
                raise HTTPException(400, "End date must be after start date")
            if new_status == "Active":
                conflict = db.execute("SELECT id FROM user_subscriptions WHERE user_id=? AND status='Active' AND id<>?", (current["user_id"], subscription_id)).fetchone()
                if conflict:
                    raise HTTPException(409, "A user may have only one Active subscription")
            now = utcnow_naive()
            db.execute("UPDATE user_subscriptions SET plan_name=?,start_date=?,end_date=?,status=?,updated_at=? WHERE id=?",
                       ((plan_name or current["plan_name"]).strip(), new_start, new_end, new_status, now, subscription_id))
            self._sync_user(db, current["user_id"], now)
            db.commit()
            return self._get(db, subscription_id)

    def set_features(self, subscription_id, feature_codes):
        with closing(self.connect()) as db:
            current = self._get(db, subscription_id)
            now = utcnow_naive()
            self._replace_features(db, subscription_id, feature_codes, now)
            self._enable_features_for_user(db, current["user_id"], feature_codes, now)
            db.commit()
            return self._get(db, subscription_id)

    def effective_entitlements(self, user_id, username=None):
        subscription = self.subscription_for_user(user_id)
        features = self.list_features(user_id)
        has_administrative_access = (username or "").casefold() in PROTECTED_SUPER_ADMINS
        if has_administrative_access:
            for feature in features:
                feature["is_entitled"] = bool(feature["is_active"] and feature["is_implemented"])
        entitled = [f["feature_code"] for f in features if f["is_entitled"] and f["is_active"] and f["is_implemented"] and f["user_enabled"]]
        return {"subscription": subscription, "features": features, "effective_feature_codes": entitled, "administrative_access": has_administrative_access}

    def set_user_preference(self, user, feature_id, is_enabled):
        entitlements = self.effective_entitlements(user["id"], user.get("username"))
        feature = next((item for item in entitlements["features"] if item["id"] == feature_id), None)
        if not feature:
            raise HTTPException(404, "AI feature not found")
        if not feature["is_entitled"] or not feature["is_active"] or not feature["is_implemented"]:
            raise HTTPException(403, "This AI feature is not available to your account")
        with closing(self.connect()) as db:
            db.execute("""MERGE user_ai_feature_preferences AS target
                USING (SELECT ? AS user_id, ? AS feature_id) AS source
                ON target.user_id=source.user_id AND target.feature_id=source.feature_id
                WHEN MATCHED THEN UPDATE SET is_enabled=?,updated_at=?
                WHEN NOT MATCHED THEN INSERT(user_id,feature_id,is_enabled,updated_at) VALUES(?,?,?,?);""",
                (user["id"], feature_id, 1 if is_enabled else 0, utcnow_naive(), user["id"], feature_id, 1 if is_enabled else 0, utcnow_naive()))
            db.commit()
        return self.effective_entitlements(user["id"], user.get("username"))

    def require_entitlement(self, user, feature_code):
        if not user.get("is_active"):
            raise HTTPException(401, "Inactive user")
        entitlements = self.effective_entitlements(user["id"], user.get("username"))
        if feature_code not in entitlements["effective_feature_codes"]:
            feature = next((f for f in entitlements["features"] if f["feature_code"] == feature_code), None)
            if feature and not feature["is_implemented"]:
                raise HTTPException(409, "This AI feature is coming soon")
            raise HTTPException(403, "An active subscription including this AI feature is required")
        return entitlements

    def _replace_features(self, db, subscription_id, codes, now):
        normalized = sorted(set(codes or []))
        rows = db.execute("SELECT id,feature_code FROM ai_features").fetchall()
        by_code = {row["feature_code"]: row["id"] for row in rows}
        invalid = [code for code in normalized if code not in by_code]
        if invalid:
            raise HTTPException(400, f"Unknown AI feature: {invalid[0]}")
        db.execute("DELETE FROM user_subscription_features WHERE subscription_id=?", (subscription_id,))
        for code in normalized:
            db.execute("INSERT INTO user_subscription_features (subscription_id,feature_id,is_enabled,granted_at) VALUES (?,?,1,?)",
                       (subscription_id, by_code[code], now))

    def _sync_user(self, db, user_id, now):
        db.execute("""UPDATE users SET is_premium=CASE WHEN EXISTS (SELECT 1 FROM user_subscriptions
            WHERE user_id=? AND status='Active' AND start_date<=? AND end_date>?) THEN 1 ELSE 0 END WHERE id=?""",
                   (user_id, now, now, user_id))

    @staticmethod
    def _enable_features_for_user(db, user_id, feature_codes, now):
        normalized = sorted(set(feature_codes or []))
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        rows = db.execute(f"SELECT id FROM ai_features WHERE feature_code IN ({placeholders})", normalized).fetchall()
        for row in rows:
            db.execute("""MERGE user_ai_feature_preferences AS target
                USING (SELECT ? AS user_id, ? AS feature_id) AS source
                ON target.user_id=source.user_id AND target.feature_id=source.feature_id
                WHEN MATCHED THEN UPDATE SET is_enabled=1,updated_at=?
                WHEN NOT MATCHED THEN INSERT(user_id,feature_id,is_enabled,updated_at) VALUES(?,?,1,?);""",
                (user_id, row["id"], now, user_id, row["id"], now))

    def _get(self, db, subscription_id):
        row = db.execute("SELECT * FROM user_subscriptions WHERE id=?", (subscription_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Subscription not found")
        return self._with_features(db, dict(row))

    @staticmethod
    def _with_features(db, item):
        item["features"] = db.execute("""SELECT f.id,f.feature_code,f.feature_name,f.is_implemented,f.is_active,sf.is_enabled,sf.granted_at
            FROM user_subscription_features sf JOIN ai_features f ON f.id=sf.feature_id
            WHERE sf.subscription_id=? ORDER BY f.id""", (item["id"],)).fetchall()
        return item
