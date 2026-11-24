from typing import Any, ClassVar


class DomainError(Exception):
    """A rejection with a stable error code; the HTTP layer maps it to a response."""

    code: ClassVar[str]
    status_code: ClassVar[int] = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    code = "validation_error"

    def __init__(self, message: str, details: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.details = details


class SelfPayment(DomainError):
    code = "self_payment"


class FirmNotFound(DomainError):
    code = "firm_not_found"


class InsufficientFunds(DomainError):
    code = "insufficient_funds"


class BalanceOverflow(DomainError):
    code = "balance_overflow"


class IdempotencyKeyReuse(DomainError):
    code = "idempotency_key_reuse"


class ServiceBusy(DomainError):
    code = "busy"
    status_code = 503
