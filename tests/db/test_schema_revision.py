"""A replica refuses to start unless the database is at the migrations' head (spec 5.6)."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.schema_revision
from app.db import get_engine
from app.main import app as application
from app.schema_revision import (
    SchemaRevisionMismatch,
    check_schema_revision,
    migrations_head,
)


def _error_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "app.schema_revision"
    ]


def test_check_passes_at_head_from_any_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The head comes from the package's migrations, not from the working directory."""
    monkeypatch.chdir(tmp_path)
    assert migrations_head() is not None
    with get_engine().connect() as conn:
        check_schema_revision(conn)


@pytest.mark.parametrize(
    ("statement", "found"),
    [
        ("UPDATE alembic_version SET version_num = 'bogus'", "revision bogus"),
        ("DELETE FROM alembic_version", "no revision"),
        ("DROP TABLE alembic_version", "no revision"),
    ],
    ids=["other_revision", "empty_table", "no_table"],
)
def test_check_rejects_a_schema_not_at_head(
    statement: str, found: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The change is made and checked in one transaction that is rolled back."""
    with get_engine().connect() as conn:
        try:
            conn.execute(text(statement))
            with pytest.raises(SchemaRevisionMismatch) as excinfo:
                check_schema_revision(conn)
        finally:
            conn.rollback()

    message = str(excinfo.value)
    assert f"is at {found}, but this build expects revision {migrations_head()}" in message
    assert "alembic upgrade head" in message
    assert _error_records(caplog) == [(logging.ERROR, message)]
    with get_engine().connect() as conn:
        check_schema_revision(conn)


def test_startup_fails_when_schema_is_not_at_head(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifespan runs the check on one pooled connection and returns it on failure."""
    monkeypatch.setattr(app.schema_revision, "migrations_head", lambda: "f00d")

    with pytest.raises(SchemaRevisionMismatch, match="expects revision f00d"):
        with TestClient(application):
            pytest.fail("the application started")

    assert [level for level, _ in _error_records(caplog)] == [logging.ERROR]
    assert get_engine().pool.checkedout() == 0
