from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas import BulkPaymentRequest
from app.service import build_command, execute_bulk_payment

BULK_PAYMENTS_PATH = "/bulk-payments"

router = APIRouter()


@router.get("/healthz")
def healthz(
    response: Response, session: Annotated[Session, Depends(get_session)]
) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}


@router.post(BULK_PAYMENTS_PATH, status_code=201)
def create_bulk_payment(
    payload: BulkPaymentRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    # Fields for the per-request log record written in app.main.
    request.state.payer = str(payload.payer_firm_uuid)
    request.state.payment_count = len(payload.payments)
    command = build_command(payload, None, settings)
    request.state.total_cents = command.total_cents

    result = execute_bulk_payment(session, command, settings.lock_timeout_ms)
    request.state.outcome = "replayed" if result.replayed else "created"
    return JSONResponse(result.body, status_code=201)
