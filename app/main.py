from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from app.api import router
from app.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Synchronous endpoints wait for a thread instead of failing on pool checkout.
    # The limiter can be changed only from the event loop.
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().db_pool_size
    yield


def create_app() -> FastAPI:
    application = FastAPI(lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()
