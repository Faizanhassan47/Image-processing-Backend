"""Laboratory-owned AI licensing, seats, plans, and payment orchestration."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json

from fastapi import HTTPException

from core.security import is_protected_super_admin


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AILicensingService:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def is_super_admin(user):
        return user.get("role") == "Super Administrator" or is_protected_super_admin(user.get("username"))

    def memberships(self, user_id):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""SELECT m.laboratory_id,l.laboratory_code,l.laboratory_name,l.status
                FROM user_laboratory_memberships m JOIN laboratories l ON l.id=m.laboratory_id
                WHERE m.user_id=? AND m.is_active=1 ORDER BY l.laboratory_name""", (user_id,)).fetchall()]

    def require_laboratory(self, user, laboratory_id, administrative=False):
        if self.is_super_admin(user):
            return
        if administrative and user.get("role") != "System Administrator":
            raise HTTPException(403, detail={"code": "LABORATORY_ACCESS_DENIED", "message": "Laboratory administration is required"})
        with closing(self.connect()) as db:
            row = db.execute("""SELECT 1 FROM user_laboratory_memberships m JOIN laboratories l ON l.id=m.laboratory_id
                WHERE m.user_id=? AND m.laboratory_id=? AND m.is_active=1 AND l.status='Active'""",
                (user["id"], laboratory_id)).fetchone()
        if not row:
            raise HTTPException(403, detail={"code": "LABORATORY_ACCESS_DENIED", "message": "You do not belong to this laboratory"})

    def list_laboratories(self, user):
        with closing(self.connect()) as db:
            if self.is_super_admin(user):
                rows = db.execute("SELECT * FROM laboratories ORDER BY laboratory_name").fetchall()
            else:
                rows = db.execute("""SELECT l.* FROM laboratories l JOIN user_laboratory_memberships m ON m.laboratory_id=l.id
                    WHERE m.user_id=? AND m.is_active=1 ORDER BY l.laboratory_name""", (user["id"],)).fetchall()
            return [dict(row) for row in rows]

    def create_laboratory(self, item, actor):
        if not self.is_super_admin(actor):
            raise HTTPException(403, "Only a Super Administrator may create laboratories")
        with closing(self.connect()) as db:
            now = utcnow()
            try:
                cur = db.execute("""INSERT laboratories(laboratory_code,laboratory_name,description,contact_email,contact_phone,status,created_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,'Active',?,?,?)""", (item["laboratory_code"].strip().upper(), item["laboratory_name"].strip(),
                    item.get("description"), item.get("contact_email"), item.get("contact_phone"), actor["id"], now, now))
                lab_id = cur.lastrowid
                self._audit(db, actor["id"], lab_id, None, "laboratory.created", "laboratory", lab_id, None, item)
                db.commit()
            except Exception as exc:
                db.rollback()
                if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                    raise HTTPException(409, "Laboratory code already exists") from exc
                raise
        return self.get_laboratory(lab_id)

    def get_laboratory(self, laboratory_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM laboratories WHERE id=?", (laboratory_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Laboratory not found")
            return dict(row)

    def list_plans(self, include_inactive=False):
        with closing(self.connect()) as db:
            clause = "" if include_inactive else "WHERE p.is_active=1 AND p.purchasing_enabled=1"
            rows = db.execute(f"""SELECT p.*,f.feature_code,f.feature_name,f.is_implemented,f.is_active AS feature_active
                FROM ai_subscription_plans p JOIN ai_features f ON f.id=p.feature_id {clause}
                ORDER BY f.display_order,p.duration_months,p.price_per_seat""").fetchall()
            return [dict(row) for row in rows]

    def create_plan(self, item, actor):
        if not self.is_super_admin(actor):
            raise HTTPException(403, "Only a Super Administrator may configure prices")
        with closing(self.connect()) as db:
            now = utcnow()
            cur = db.execute("""INSERT ai_subscription_plans(feature_id,plan_name,billing_period,duration_months,price_per_seat,currency,minimum_seats,maximum_seats,is_active,purchasing_enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,1,1,?,?)""", (item["feature_id"], item["plan_name"].strip(), item["billing_period"],
                item["duration_months"], item["price_per_seat"], item["currency"].upper(), item["minimum_seats"], item.get("maximum_seats"), now, now))
            plan_id = cur.lastrowid
            self._audit(db, actor["id"], None, None, "plan.created", "ai_subscription_plan", plan_id, None, item)
            db.commit()
            return dict(db.execute("SELECT * FROM ai_subscription_plans WHERE id=?", (plan_id,)).fetchone())

    def subscriptions(self, laboratory_id, user):
        self.require_laboratory(user, laboratory_id)
        with closing(self.connect()) as db:
            rows = db.execute("""SELECT s.*,f.feature_code,f.feature_name,p.plan_name,
                (SELECT COUNT(*) FROM ai_seat_assignments a WHERE a.laboratory_subscription_id=s.id AND a.status='Active') assigned_seats
                FROM laboratory_subscriptions s JOIN ai_features f ON f.id=s.feature_id
                LEFT JOIN ai_subscription_plans p ON p.id=s.plan_id WHERE s.laboratory_id=?
                ORDER BY s.created_at DESC""", (laboratory_id,)).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                item["available_seats"] = max(0, item["seat_quantity"] - item["assigned_seats"])
                output.append(item)
            return output

    def grant(self, laboratory_id, item, actor):
        if not self.is_super_admin(actor):
            raise HTTPException(403, "Only a Super Administrator may grant complimentary licenses")
        start, end = item["start_date"], item["end_date"]
        if end <= start:
            raise HTTPException(400, "End date must be after start date")
        with closing(self.connect()) as db:
            now = utcnow()
            cur = db.execute("""INSERT laboratory_subscriptions(laboratory_id,plan_id,feature_id,seat_quantity,start_date,end_date,status,payment_status,subscription_source,created_by,created_at,updated_at)
                VALUES(?,NULL,?,?,?,?,'Active','NotRequired','SuperAdminGrant',?,?,?)""",
                (laboratory_id, item["feature_id"], item["seat_quantity"], start, end, actor["id"], now, now))
            subscription_id = cur.lastrowid
            self._audit(db, actor["id"], laboratory_id, None, "license.granted", "laboratory_subscription", subscription_id, None, item)
            db.commit()
        return next(row for row in self.subscriptions(laboratory_id, actor) if row["id"] == subscription_id)

    def assign_seat(self, laboratory_id, subscription_id, target_user_id, actor):
        self.require_laboratory(actor, laboratory_id, administrative=True)
        with closing(self.connect()) as db:
            try:
                sub = db.execute("""SELECT s.*,f.feature_code FROM laboratory_subscriptions s WITH (UPDLOCK,HOLDLOCK)
                    JOIN ai_features f ON f.id=s.feature_id WHERE s.id=? AND s.laboratory_id=?""", (subscription_id, laboratory_id)).fetchone()
                if not sub:
                    raise HTTPException(404, "Laboratory subscription not found")
                now = utcnow()
                if sub["status"] != "Active" or not (sub["start_date"] <= now < sub["end_date"]):
                    raise HTTPException(409, detail={"code": "SUBSCRIPTION_EXPIRED", "message": "Subscription is not active"})
                member = db.execute("SELECT 1 FROM user_laboratory_memberships WHERE laboratory_id=? AND user_id=? AND is_active=1", (laboratory_id, target_user_id)).fetchone()
                user = db.execute("SELECT is_active FROM users WHERE id=?", (target_user_id,)).fetchone()
                if not member or not user or not user["is_active"]:
                    raise HTTPException(400, "Target user must be an active laboratory member")
                assigned = db.execute("SELECT COUNT(*) AS count FROM ai_seat_assignments WITH (UPDLOCK,HOLDLOCK) WHERE laboratory_subscription_id=? AND status='Active'", (subscription_id,)).fetchone()["count"]
                if assigned >= sub["seat_quantity"]:
                    raise HTTPException(409, detail={"code": "NO_SEATS_AVAILABLE", "message": "All purchased seats are assigned"})
                cur = db.execute("""INSERT ai_seat_assignments(laboratory_subscription_id,laboratory_id,user_id,feature_id,assigned_by,assigned_at,status)
                    VALUES(?,?,?,?,?,?,'Active')""", (subscription_id, laboratory_id, target_user_id, sub["feature_id"], actor["id"], now))
                assignment_id = cur.lastrowid
                self._audit(db, actor["id"], laboratory_id, target_user_id, "seat.assigned", "ai_seat_assignment", assignment_id, None, {"subscription_id": subscription_id})
                db.commit()
            except HTTPException:
                db.rollback()
                raise
        return {"id": assignment_id, "status": "Active"}

    def revoke_seat(self, laboratory_id, assignment_id, actor):
        self.require_laboratory(actor, laboratory_id, administrative=True)
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM ai_seat_assignments WHERE id=? AND laboratory_id=? AND status='Active'", (assignment_id, laboratory_id)).fetchone()
            if not row:
                raise HTTPException(404, "Active assignment not found")
            now = utcnow()
            db.execute("UPDATE ai_seat_assignments SET status='Revoked',revoked_by=?,revoked_at=? WHERE id=?", (actor["id"], now, assignment_id))
            self._audit(db, actor["id"], laboratory_id, row["user_id"], "seat.revoked", "ai_seat_assignment", assignment_id, dict(row), {"status": "Revoked"})
            db.commit()
        return {"id": assignment_id, "status": "Revoked"}

    def access(self, user, laboratory_id, feature_code):
        if not user.get("is_active"):
            return self._denied("USER_INACTIVE")
        with closing(self.connect()) as db:
            feature = db.execute("SELECT * FROM ai_features WHERE feature_code=?", (feature_code,)).fetchone()
            if not feature or not feature["is_active"] or not feature["is_implemented"]:
                return self._denied("FEATURE_UNAVAILABLE")
            if self.is_super_admin(user):
                return {"allowed": True, "reason": None, "source": "SuperAdministrator"}
            membership = db.execute("SELECT 1 FROM user_laboratory_memberships WHERE user_id=? AND laboratory_id=? AND is_active=1", (user["id"], laboratory_id)).fetchone()
            if not membership:
                return self._denied("LABORATORY_ACCESS_DENIED")
            now = utcnow()
            row = db.execute("""SELECT TOP 1 s.status,s.end_date FROM ai_seat_assignments a
                JOIN laboratory_subscriptions s ON s.id=a.laboratory_subscription_id
                WHERE a.user_id=? AND a.laboratory_id=? AND a.feature_id=? AND a.status='Active'
                ORDER BY s.end_date DESC""", (user["id"], laboratory_id, feature["id"])).fetchone()
            if not row:
                owns = db.execute("SELECT 1 FROM laboratory_subscriptions WHERE laboratory_id=? AND feature_id=? AND status='Active' AND start_date<=? AND end_date>?", (laboratory_id, feature["id"], now, now)).fetchone()
                return self._denied("FEATURE_NOT_ASSIGNED" if owns else "SUBSCRIPTION_REQUIRED")
            if row["status"] == "Suspended": return self._denied("SUBSCRIPTION_SUSPENDED")
            if row["status"] != "Active" or row["end_date"] <= now: return self._denied("SUBSCRIPTION_EXPIRED")
            return {"allowed": True, "reason": None, "source": "LaboratorySeat", "expires_at": row["end_date"]}

    @staticmethod
    def _denied(reason):
        return {"allowed": False, "reason": reason}

    @staticmethod
    def _audit(db, actor, laboratory, target, action, resource_type, resource_id, before, after):
        db.execute("""INSERT ai_subscription_audit_logs(actor_user_id,laboratory_id,target_user_id,action,resource_type,resource_id,before_metadata,after_metadata)
            VALUES(?,?,?,?,?,?,?,?)""", (actor, laboratory, target, action, resource_type, str(resource_id),
            json.dumps(before, default=str) if before else None, json.dumps(after, default=str) if after else None))
