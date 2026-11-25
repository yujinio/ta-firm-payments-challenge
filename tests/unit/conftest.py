import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings for code that never opens a connection; override fields with model_copy."""
    return Settings(database_url="postgresql+psycopg://unused")
