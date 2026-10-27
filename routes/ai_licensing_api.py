"""Validated APIs for laboratory AI licensing and seat administration."""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from services.ai_licensing_service import AILicensingService


class LaboratoryCreate(BaseModel):
    laboratory_code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    laboratory_name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(None, max_length=1000)
    contact_email: str | None = Field(None, max_length=320)
    contact_phone: str | None = Field(None, max_length=40)


class PlanCreate(BaseModel):
    feature_id: int
    plan_name: str = Field(min_length=2, max_length=120)
    billing_period: str
    duration_months: int = Field(ge=1, le=120)
    price_per_seat: Decimal = Field(ge=0, max_digits=19, decimal_places=4)
    currency: str = Field(min_length=3, max_length=3)
    minimum_seats: int = Field(default=1, ge=1)
    maximum_seats: int | None = Field(default=None, ge=1)

    @field_validator("billing_period")
    @classmethod
    def validate_period(cls, value):
        if value not in {"Monthly", "Quarterly", "Annual", "Custom"}:
            raise ValueError("Unsupported billing period")
        return value


class GrantCreate(BaseModel):
    feature_id: int
    seat_quantity: int = Field(ge=1, le=100000)
    start_date: datetime
    end_date: datetime

    
class SeatAssign(BaseModel):
    subscription_id: int
    user_id: int


class SeatRevoke(BaseModel):
    assignment_id: int


def create_ai_licensing_router(connect, get_current_user):
    router = APIRouter(tags=["AI Laboratory Licensing"])
    service = AILicensingService(connect)

    @router.get("/api/laboratories")
    def laboratories(user=Depends(get_current_user)):
        return service.list_laboratories(user)

    @router.post("/api/laboratories", status_code=201)
    def create_laboratory(item: LaboratoryCreate, user=Depends(get_current_user)):
        return service.create_laboratory(item.model_dump(), user)

    @router.get("/api/laboratories/{laboratory_id}")
    def laboratory(laboratory_id: int, user=Depends(get_current_user)):
        service.require_laboratory(user, laboratory_id)
        return service.get_laboratory(laboratory_id)

    @router.get("/api/ai/subscription-plans")
    def plans(user=Depends(get_current_user)):
        return service.list_plans(service.is_super_admin(user))

    @router.post("/api/admin/ai/subscription-plans", status_code=201)
    def create_plan(item: PlanCreate, user=Depends(get_current_user)):
        return service.create_plan(item.model_dump(), user)

    @router.get("/api/laboratories/{laboratory_id}/ai-subscriptions")
    def subscriptions(laboratory_id: int, user=Depends(get_current_user)):
        return service.subscriptions(laboratory_id, user)

    @router.post("/api/admin/laboratories/{laboratory_id}/ai-grants", status_code=201)
    def grant(laboratory_id: int, item: GrantCreate, user=Depends(get_current_user)):
        values = item.model_dump()
        values["start_date"] = values["start_date"].replace(tzinfo=None)
        values["end_date"] = values["end_date"].replace(tzinfo=None)
        return service.grant(laboratory_id, values, user)

    @router.post("/api/laboratories/{laboratory_id}/ai-seats/assign", status_code=201)
    def assign(laboratory_id: int, item: SeatAssign, user=Depends(get_current_user)):
        return service.assign_seat(laboratory_id, item.subscription_id, item.user_id, user)

    @router.post("/api/laboratories/{laboratory_id}/ai-seats/revoke")
    def revoke(laboratory_id: int, item: SeatRevoke, user=Depends(get_current_user)):
        return service.revoke_seat(laboratory_id, item.assignment_id, user)

    @router.get("/api/ai/features/{feature_code}/access")
    def access(feature_code: str, laboratory_id: int, user=Depends(get_current_user)):
        result = service.access(user, laboratory_id, feature_code.upper())
        if not result["allowed"]:
            raise HTTPException(403, detail=result)
        return result

    @router.post("/api/laboratories/{laboratory_id}/ai-subscriptions/checkout")
    def checkout_unavailable(laboratory_id: int, user=Depends(get_current_user)):
        service.require_laboratory(user, laboratory_id, administrative=True)
        raise HTTPException(503, detail={"code": "PAYMENT_PROVIDER_NOT_CONFIGURED", "message": "Live checkout is unavailable until an approved payment provider is configured"})

    return router
