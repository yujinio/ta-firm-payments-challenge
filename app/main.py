import email.message
import json
import logging
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TypedDict

import anyio.to_thread
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import BULK_PAYMENTS_PATH, router
from app.config import get_settings
from app.db import get_engine
from app.errors import (
    DomainError,
    ErrorBody,
    ErrorDetail,
    ErrorPayload,
    ServiceBusy,
    ValidationFailed,
)
from app.schema_revision import check_engine_schema
from app.schemas import format_cents

INSTANCE = socket.gethostname()

request_logger = logging.getLogger("app.requests")

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}

# Pydantic messages that quote part of the input ("found `z` at 0"), by error type.
_FIXED_MESSAGES = {"uuid_parsing": "Input should be a valid UUID"}


class RequestLogRecord(TypedDict):
    """The JSON record logged once per bulk payment request; it never holds a description.

    payer, payments and total stay None when the request fails before the endpoint sets
    them: payer and payments are set once the body is valid, total once build_command passes.
    """

    request_id: str
    instance: str
    payer: str | None
    payments: int | None
    total: str | None
    outcome: str
    status: int
    duration_ms: float


def _configure_logging() -> None:
    """Send INFO records of the `app` loggers to stderr, unless `app` already has a handler."""
    # Uvicorn configures only its own loggers; without a handler INFO records are dropped.
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Configure logging, cap the thread pool at the pool size, build the engine, check the schema.

    A database that is not at the migrations' head fails startup, so the replica never serves.
    """
    _configure_logging()
    # Synchronous endpoints wait for a thread instead of failing on pool checkout.
    # The limiter can be changed only from the event loop.
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().db_pool_size
    # Built before the first request, so requests never race to create it.
    engine = get_engine()
    # A replica never serves against a schema it was not built for (spec section 5.6).
    check_engine_schema(engine)
    yield


async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Write one record per bulk payment request; descriptions are never logged."""
    if request.url.path != BULK_PAYMENTS_PATH:
        return await call_next(request)
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        state = request.state
        # request.state is untyped: these getattr values are Any at this boundary.
        total_cents = getattr(state, "total_cents", None)
        record: RequestLogRecord = {
            "request_id": request_id,
            "instance": INSTANCE,
            "payer": getattr(state, "payer", None),
            "payments": getattr(state, "payment_count", None),
            "total": None if total_cents is None else format_cents(total_cents),
            # Unset only when an exception escaped to the 500 handler.
            "outcome": getattr(state, "outcome", "internal_error"),
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        request_logger.info(json.dumps(record))


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: list[ErrorDetail] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the error body and record `code` as the outcome for the request log."""
    request.state.outcome = code
    error: ErrorPayload = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    body: ErrorBody = {"error": error}
    return JSONResponse(body, status_code=status_code, headers=headers)


async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Answer a DomainError with its status and code; a busy rejection adds Retry-After: 1."""
    details = exc.details if isinstance(exc, ValidationFailed) else None
    headers = {"Retry-After": "1"} if isinstance(exc, ServiceBusy) else None
    return _error_response(
        request, exc.status_code, exc.code, exc.message, details=details, headers=headers
    )


def _is_json_content_type(content_type: str | None) -> bool:
    """FastAPI's rule: only application/json and application/*+json bodies are parsed.

    Without the header the body is not parsed either (strict_content_type).
    """
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (
        subtype == "json" or subtype.endswith("+json")
    )


async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer a request FastAPI rejected before the endpoint ran with 422 validation_error."""
    # Only loc, msg and type: input and ctx carry the rejected value, which may be
    # sensitive or impossible to encode (a lone surrogate from a \ud800 escape).
    # exc.errors() is Sequence[Any]: FastAPI does not type Pydantic's raw error dicts.
    details: list[ErrorDetail] = [
        {
            "loc": list(error["loc"]),
            "msg": _FIXED_MESSAGES.get(error["type"], error["msg"]),
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    if _is_json_content_type(request.headers.get("content-type")):
        message = "the request is invalid"
    else:
        # The body reached validation as raw bytes, for example from `curl -d`.
        message = "the request body must be JSON sent with Content-Type: application/json"
    return _error_response(request, 422, "validation_error", message, details=details)


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Answer an HTTP error raised by the framework in the service's error format.

    A 400, a body that cannot be read as JSON, becomes 422 validation_error.
    """
    if exc.status_code == 400:
        # FastAPI raises 400 only when it cannot read the body as JSON, for
        # example invalid UTF-8; spec section 8.1 makes that a validation error.
        message = "the request body is not valid JSON"
        detail: ErrorDetail = {"loc": ["body"], "msg": message, "type": "json_invalid"}
        return _error_response(request, 422, "validation_error", message, details=[detail])
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    return _error_response(request, exc.status_code, code, str(exc.detail), headers=exc.headers)


async def handle_internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Answer any unhandled exception with 500 internal_error and no detail."""
    # Uvicorn logs the traceback; the client learns nothing about the cause.
    return _error_response(request, 500, "internal_error", "internal server error")


def create_app() -> FastAPI:
    """Build the application with its routes, request log and error handlers."""
    application = FastAPI(lifespan=lifespan)
    application.include_router(router)
    application.middleware("http")(log_request)
    application.exception_handler(DomainError)(handle_domain_error)
    application.exception_handler(RequestValidationError)(handle_request_validation_error)
    application.exception_handler(StarletteHTTPException)(handle_http_exception)
    application.exception_handler(Exception)(handle_internal_error)
    return application


app = create_app()
