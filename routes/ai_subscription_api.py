"""AI feature catalog, self-service entitlements, and administrator subscriptions."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.security import is_protected_super_admin
from services.ai_subscription_service import AISubscriptionService


class SubscriptionCreate(BaseModel):
    user_id: int
    plan_name: str = Field(min_length=1, max_length=120)
    start_date: datetime
    end_date: datetime
    feature_codes: list[str] = Field(default_factory=list)


class SubscriptionUpdate(BaseModel):
    plan_name: str | None = Field(default=None, min_length=1, max_length=120)
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: str | None = None


class FeatureAssignment(BaseModel):
    feature_codes: list[str]


class FeatureStatusUpdate(BaseModel):
    is_active: bool | None = None
    is_catalog_visible: bool | None = None
    is_featured: bool | None = None
    is_catalog_deleted: bool | None = None
    catalog_title: str | None = Field(default=None, min_length=1, max_length=160)
    catalog_display_order: int | None = Field(default=None, ge=0, le=10000)
    catalog_session_price_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    catalog_usd_to_pkr_rate: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=4)
    catalog_category: str | None = Field(default=None, max_length=80)
    catalog_summary: str | None = Field(default=None, max_length=500)
    catalog_purpose: str | None = Field(default=None, max_length=1000)
    catalog_value: str | None = Field(default=None, max_length=1000)
    catalog_outputs: list[str] | None = Field(default=None, max_length=8)


class FeaturePreferenceUpdate(BaseModel):
    is_enabled: bool


def create_ai_subscription_router(connect, require_roles, get_current_user, audit):
    router = APIRouter(tags=["AI Subscriptions"])
    service = AISubscriptionService(connect)

    def is_super_admin(user: dict) -> bool:
        return user.get("role") == "Super Administrator" or is_protected_super_admin(user.get("username"))

    def require_subscription_admin(user=Depends(get_current_user)):
        if is_super_admin(user) or user.get("role") == "System Administrator":
            return user
        raise HTTPException(403, "Only an administrator may manage AI subscriptions")

    def require_admin(user=Depends(get_current_user)):
        if user.get("role") in ("System Administrator", "Super Administrator") or is_protected_super_admin(user.get("username")):
            return user
        raise HTTPException(403, "Only an administrator may manage AI features")

    def assert_can_grant_features(actor: dict, target_user_id: int, feature_codes: list[str]):
        if is_super_admin(actor):
            return
        # System Administrator cannot manage subscriptions for Super Admins
        with connect() as db:
            target = db.execute("SELECT id, username, role FROM users WHERE id=?", (target_user_id,)).fetchone()
            if not target:
                raise HTTPException(404, "Target user not found")
            if target["role"] == "Super Administrator" or is_protected_super_admin(target["username"]):
                raise HTTPException(403, "System Administrators cannot manage subscriptions for Super Administrators")
            if target["role"] not in {"Pathologist", "Laboratory Manager"}:
                raise HTTPException(403, "System Administrators may grant AI features only to Pathologists and Laboratory Managers")
        # System Administrator can only grant features they have active paid entitlements for
        admin_entitlements = service.effective_entitlements(actor["id"], actor.get("username"))
        allowed_codes = set(admin_entitlements.get("effective_feature_codes", []))
        unauthorized = [code for code in feature_codes if code not in allowed_codes]
        if unauthorized:
            raise HTTPException(403, f"You can only grant AI features included in your active subscription. Unauthorized: {', '.join(unauthorized)}")

    @router.get("/api/ai/features")
    def features(user=Depends(get_current_user)):
        return service.list_features(user["id"])

    @router.get("/api/ai/my-subscription")
    def my_subscription(user=Depends(get_current_user)):
        return service.subscription_for_user(user["id"])

    @router.get("/api/ai/my-entitlements")
    def my_entitlements(user=Depends(get_current_user)):
        return service.effective_entitlements(user["id"], user.get("username"))

    @router.put("/api/ai/features/{feature_id}/preference")
    def update_my_feature_preference(feature_id: int, item: FeaturePreferenceUpdate, user=Depends(get_current_user)):
        result = service.set_user_preference(user, feature_id, item.is_enabled)
        with connect() as db:
            audit(db, user["username"], "ai.feature.preference", "ai_feature", feature_id, f"is_enabled={item.is_enabled}")
            db.commit()
        return result

    @router.patch("/api/admin/ai/features/{feature_id}")
    def update_feature_status(feature_id: int, item: FeatureStatusUpdate, user=Depends(get_current_user)):
        if not is_super_admin(user):
            raise HTTPException(403, "Only a Super Administrator may manage the AI catalog")
        with connect() as db:
            feature = db.execute("SELECT id,feature_code FROM ai_features WHERE id=?", (feature_id,)).fetchone()
            if not feature:
                raise HTTPException(404, "AI feature not found")
            values = item.model_dump(exclude_none=True)
            if not values:
                raise HTTPException(400, "No catalog changes were supplied")
            import json
            if "catalog_outputs" in values:
                outputs = [output.strip() for output in values.pop("catalog_outputs") if output.strip()]
                values["catalog_outputs_json"] = json.dumps(outputs)
            if "catalog_session_price_usd" in values:
                rate = values.get("catalog_usd_to_pkr_rate", Decimal("280"))
                values["catalog_session_price_pkr"] = (values["catalog_session_price_usd"] * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for key in ("is_active", "is_catalog_visible", "is_featured", "is_catalog_deleted"):
                if key in values:
                    values[key] = 1 if values[key] else 0
            allowed = {"is_active", "is_catalog_visible", "is_featured", "is_catalog_deleted", "catalog_title", "catalog_display_order", "catalog_category", "catalog_summary", "catalog_purpose", "catalog_value", "catalog_outputs_json", "catalog_session_price_usd", "catalog_usd_to_pkr_rate", "catalog_session_price_pkr"}
            if set(values) - allowed:
                raise HTTPException(400, "Unsupported catalog field")
            assignments = ",".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE ai_features SET {assignments} WHERE id=?", (*values.values(), feature_id))
            audit(db, user["username"], "ai.feature.catalog_update", "ai_feature", feature_id, f"feature={feature['feature_code']}; fields={','.join(values)}")
            db.commit()
        return {"id": feature_id, "feature_code": feature["feature_code"], **item.model_dump(exclude_none=True)}

    @router.get("/api/admin/ai/features")
    def catalog_cards(user=Depends(get_current_user)):
        if not is_super_admin(user):
            raise HTTPException(403, "Only a Super Administrator may manage the AI catalog")
        return service.list_features(user["id"])

    @router.get("/api/admin/ai/subscriptions")
    def subscriptions(user=Depends(require_subscription_admin)):
        all_accounts = service.list_subscriptions()
        if is_super_admin(user):
            return all_accounts
        # System Administrator: hide protected super admins and their accounts from being modified
        return [acc for acc in all_accounts if not is_protected_super_admin(acc.get("username")) and acc.get("role") != "Super Administrator"]

    @router.get("/api/admin/ai/subscriptions/{user_id}")
    def subscription_history(user_id: int, user=Depends(require_subscription_admin)):
        if not is_super_admin(user):
            with connect() as db:
                target = db.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
                if not target:
                    raise HTTPException(404, "User not found")
                if target["role"] == "Super Administrator" or is_protected_super_admin(target["username"]):
                    raise HTTPException(403, "Cannot view subscription history for Super Administrators")
        return service.subscription_for_user(user_id, include_history=True)

    @router.post("/api/admin/ai/subscriptions", status_code=201)
    def create_subscription(item: SubscriptionCreate, user=Depends(require_subscription_admin)):
        assert_can_grant_features(user, item.user_id, item.feature_codes)
        result = service.create(item.user_id, item.plan_name, item.start_date.replace(tzinfo=None), item.end_date.replace(tzinfo=None), item.feature_codes, user["username"])
        with connect() as db:
            audit(db, user["username"], "ai.subscription.create", "subscription", result["id"], f"user_id={item.user_id}; plan={item.plan_name}")
            db.commit()
        return result

    @router.patch("/api/admin/ai/subscriptions/{subscription_id}")
    def update_subscription(subscription_id: int, item: SubscriptionUpdate, user=Depends(require_subscription_admin)):
        with connect() as db:
            sub = db.execute("SELECT user_id FROM user_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not sub:
                raise HTTPException(404, "Subscription not found")
            target_user_id = sub["user_id"]
        if not is_super_admin(user):
            with connect() as db:
                target = db.execute("SELECT id, username, role FROM users WHERE id=?", (target_user_id,)).fetchone()
                if target and (target["role"] == "Super Administrator" or is_protected_super_admin(target["username"])):
                    raise HTTPException(403, "Cannot update subscriptions for Super Administrators")
        values = item.model_dump(exclude_none=True)
        for key in ("start_date", "end_date"):
            if key in values:
                values[key] = values[key].replace(tzinfo=None)
        result = service.update(subscription_id, **values)
        with connect() as db:
            audit(db, user["username"], "ai.subscription.update", "subscription", subscription_id, ",".join(values.keys()))
            db.commit()
        return result

    @router.put("/api/admin/ai/subscriptions/{subscription_id}/features")
    def assign_features(subscription_id: int, item: FeatureAssignment, user=Depends(require_subscription_admin)):
        with connect() as db:
            sub = db.execute("SELECT user_id FROM user_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not sub:
                raise HTTPException(404, "Subscription not found")
            target_user_id = sub["user_id"]
        assert_can_grant_features(user, target_user_id, item.feature_codes)
        result = service.set_features(subscription_id, item.feature_codes)
        with connect() as db:
            audit(db, user["username"], "ai.subscription.features", "subscription", subscription_id, ",".join(item.feature_codes))
            db.commit()
        return result

    @router.post("/api/admin/ai/subscriptions/{subscription_id}/cancel")
    def cancel_subscription(subscription_id: int, user=Depends(require_subscription_admin)):
        with connect() as db:
            sub = db.execute("SELECT user_id FROM user_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not sub:
                raise HTTPException(404, "Subscription not found")
            target_user_id = sub["user_id"]
        if not is_super_admin(user):
            with connect() as db:
                target = db.execute("SELECT id, username, role FROM users WHERE id=?", (target_user_id,)).fetchone()
                if target and (target["role"] == "Super Administrator" or is_protected_super_admin(target["username"])):
                    raise HTTPException(403, "Cannot cancel subscriptions for Super Administrators")
        result = service.update(subscription_id, status="Cancelled")
        with connect() as db:
            audit(db, user["username"], "ai.subscription.cancel", "subscription", subscription_id)
            db.commit()
        return result

    return router
