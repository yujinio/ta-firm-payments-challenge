from typing import ClassVar, NotRequired, TypedDict


class ErrorDetail(TypedDict):
    """One entry of `details` in a validation error: where, what and which rule failed."""

    loc: list[str | int]
    msg: str
    type: str


class ErrorPayload(TypedDict):
    """The `error` object of an error response; `details` only for validation errors."""

    code: str
    message: str
    details: NotRequired[list[ErrorDetail]]


class ErrorBody(TypedDict):
    """The JSON body of every error response from the API's handlers.

    /healthz is the exception: it answers 503 with {"status": "unavailable"}.
    """

    error: ErrorPayload


class DomainError(Exception):
    """A rejection with a stable error code; the HTTP layer maps it to a response."""

    code: ClassVar[str]
    status_code: ClassVar[int] = 422

    def __init__(self, message: str) -> None:
        """Keep the message sent to the client as `error.message`."""
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    """A request that breaks a rule the schema cannot express, such as a settings limit."""

    code = "validation_error"

    def __init__(self, message: str, details: list[ErrorDetail]) -> None:
        """Keep the message and the entries sent to the client as `error.details`."""
        super().__init__(message)
        self.details = details


class SelfPayment(DomainError):
    """A payment whose payee is the payer."""

    code = "self_payment"


class FirmNotFound(DomainError):
    """The payer or a payee does not exist."""

    code = "firm_not_found"


class InsufficientFunds(DomainError):
    """The payer's balance does not cover the batch total."""

    code = "insufficient_funds"


class BalanceOverflow(DomainError):
    """A payee's balance would exceed INT32_MAX cents."""

    code = "balance_overflow"


class IdempotencyKeyReuse(DomainError):
    """The Idempotency-Key was already used by this payer for a different request."""

    code = "idempotency_key_reuse"


class ServiceBusy(DomainError):
    """A lock wait timed out, a deadlock was detected or no pooled connection was free.

    The client should retry; the response carries `Retry-After: 1`.
    """

    code = "busy"
    status_code = 503
