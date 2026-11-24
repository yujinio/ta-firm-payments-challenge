import json
import logging
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio.to_thread
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import BULK_PAYMENTS_PATH, router
from app.config import get_settings
from app.errors import DomainError, ServiceBusy, ValidationFailed
from app.schemas import format_cents

INSTANCE = socket.gethostname()

request_logger = logging.getLogger("app.requests")

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def _configure_logging() -> None:
    # Uvicorn configures only its own loggers; without a handler INFO records are dropped.
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    # Synchronous endpoints wait for a thread instead of failing on pool checkout.
    # The limiter can be changed only from the event loop.
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().db_pool_size
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
        total_cents = getattr(state, "total_cents", None)
        record = {
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
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request.state.outcome = code
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    details = exc.details if isinstance(exc, ValidationFailed) else None
    headers = {"Retry-After": "1"} if isinstance(exc, ServiceBusy) else None
    return _error_response(
        request, exc.status_code, exc.code, exc.message, details=details, headers=headers
    )


async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Only loc, msg and type: input and ctx carry the rejected value, which may be
    # sensitive or impossible to encode (a lone surrogate from a \ud800 escape).
    details = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return _error_response(
        request, 422, "validation_error", "the request is invalid", details=details
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 400:
        # FastAPI raises 400 only when it cannot read the body as JSON, for
        # example invalid UTF-8; spec section 8.1 makes that a validation error.
        message = "the request body is not valid JSON"
        detail = {"loc": ["body"], "msg": message, "type": "json_invalid"}
        return _error_response(request, 422, "validation_error", message, details=[detail])
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    return _error_response(request, exc.status_code, code, str(exc.detail), headers=exc.headers)


async def handle_internal_error(request: Request, exc: Exception) -> JSONResponse:
    # Uvicorn logs the traceback; the client learns nothing about the cause.
    return _error_response(request, 500, "internal_error", "internal server error")


def create_app() -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    application.include_router(router)
    application.middleware("http")(log_request)
    application.exception_handler(DomainError)(handle_domain_error)
    application.exception_handler(RequestValidationError)(handle_request_validation_error)
    application.exception_handler(StarletteHTTPException)(handle_http_exception)
    application.exception_handler(Exception)(handle_internal_error)
    return application


app = create_app()
