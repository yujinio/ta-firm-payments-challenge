from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    max_payments_per_request: int = 1000
    max_description_length: int = 500
    lock_timeout_ms: int = 5000
    db_pool_size: int = 10
    db_pool_timeout_s: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
