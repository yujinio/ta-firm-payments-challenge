from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Service configuration from environment variables (spec section 10)."""

    database_url: str
    max_payments_per_request: int = 1000
    max_description_length: int = 500
    lock_timeout_ms: int = 5000
    db_pool_size: int = 10
    db_pool_timeout_s: int = 5


@lru_cache
def get_settings() -> Settings:
    """Return the process's settings, read from the environment on the first call."""
    return Settings()
