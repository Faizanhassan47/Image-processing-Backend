"""Payment boundary. No transaction is trusted until a provider verifies it server-side."""

from abc import ABC, abstractmethod
import os

from fastapi import HTTPException


class PaymentProvider(ABC):
    @abstractmethod
    def create_checkout_session(self, payment): ...

    @abstractmethod
    def verify_payment(self, provider_transaction_id): ...

    @abstractmethod
    def process_webhook(self, raw_body: bytes, headers: dict): ...

    @abstractmethod
    def get_transaction_status(self, provider_transaction_id): ...

    def refund_payment(self, provider_transaction_id, amount=None):
        raise HTTPException(501, "Refunds are not supported by this provider")


class UnconfiguredPaymentProvider(PaymentProvider):
    def _unavailable(self):
        raise HTTPException(503, detail={"code": "PAYMENT_PROVIDER_NOT_CONFIGURED", "message": "Merchant onboarding and payment credentials are required"})

    create_checkout_session = lambda self, payment: self._unavailable()
    verify_payment = lambda self, provider_transaction_id: self._unavailable()
    process_webhook = lambda self, raw_body, headers: self._unavailable()
    get_transaction_status = lambda self, provider_transaction_id: self._unavailable()


def configured_payment_provider():
    provider_name = os.getenv("AI_PAYMENT_PROVIDER", "disabled").strip().lower()
    if provider_name in {"", "disabled", "none"}:
        return UnconfiguredPaymentProvider()
    # Deliberately fail closed: adapters are registered here only after official
    # signature verification and merchant credentials are available.
    raise RuntimeError(f"Unsupported AI_PAYMENT_PROVIDER: {provider_name}")
