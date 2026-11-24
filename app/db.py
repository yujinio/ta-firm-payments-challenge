import threading
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# One engine per process: its pool bounds the process's connections (spec section 9.3).
# lru_cache would not do: concurrent first calls each run the wrapped function.
_engine: Engine | None = None
_engine_lock = threading.Lock()


def _create_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=0,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,
        # Error text must not carry bound parameters: they include payment
        # descriptions, which never go to logs (spec section 12).
        hide_parameters=True,
    )


def get_engine() -> Engine:
    global _engine
    engine = _engine
    if engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _create_engine()
            engine = _engine
    return engine


def get_session() -> Iterator[Session]:
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        yield session
