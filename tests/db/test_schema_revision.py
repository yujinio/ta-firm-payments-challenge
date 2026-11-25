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
    migrations_heads,
)


def _error_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "app.schema_revision"
    ]


def _refused_in_rolled_back_transaction(statement: str | None) -> SchemaRevisionMismatch:
    """Run statement and the check in one transaction, roll it back, return the error."""
    with get_engine().connect() as conn:
        try:
            if statement is not None:
                conn.execute(text(statement))
            with pytest.raises(SchemaRevisionMismatch) as excinfo:
                check_schema_revision(conn)
        finally:
            conn.rollback()
    return excinfo.value


def test_check_passes_at_head_from_any_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The heads come from the package's migrations, not from the working directory."""
    monkeypatch.chdir(tmp_path)
    assert migrations_heads() != []
    with get_engine().connect() as conn:
        check_schema_revision(conn)


@pytest.mark.parametrize(
    ("statement", "current"),
    [
        ("UPDATE alembic_version SET version_num = 'bogus'", "['bogus']"),
        ("DELETE FROM alembic_version", "[]"),
        ("DROP TABLE alembic_version", "[]"),
        ("INSERT INTO alembic_version (version_num) VALUES ('second')", "['0001', 'second']"),
    ],
    ids=["other_revision", "empty_table", "no_table", "extra_row"],
)
def test_check_rejects_a_schema_not_at_head(
    statement: str, current: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The change is made and checked in one transaction that is rolled back."""
    message = str(_refused_in_rolled_back_transaction(statement))

    expected = migrations_heads()
    assert f"alembic_version holds {current}, but" in message
    assert f"have the heads {expected}" in message
    assert "alembic upgrade head" in message
    assert _error_records(caplog) == [(logging.ERROR, message)]
    with get_engine().connect() as conn:
        check_schema_revision(conn)


@pytest.mark.parametrize(
    ("statement", "current"),
    [(None, "['0001']"), ("DELETE FROM alembic_version", "[]")],
    ids=["database_at_0001", "empty_table"],
)
def test_check_rejects_a_build_without_migrations(
    statement: str | None,
    current: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No script heads never match, not even a database with no revision either."""
    (tmp_path / "versions").mkdir()
    monkeypatch.setattr(app.schema_revision, "MIGRATIONS_DIR", tmp_path)
    assert migrations_heads() == []

    message = str(_refused_in_rolled_back_transaction(statement))

    assert f"alembic_version holds {current}, but" in message
    assert f"the migrations in {tmp_path} have the heads []" in message
    assert _error_records(caplog) == [(logging.ERROR, message)]


def test_startup_fails_when_schema_is_not_at_head(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifespan runs the check on one pooled connection and returns it on failure."""
    monkeypatch.setattr(app.schema_revision, "migrations_heads", lambda: ["f00d"])

    with pytest.raises(SchemaRevisionMismatch, match=r"have the heads \['f00d'\]"):
        with TestClient(application):
            pytest.fail("the application started")

    assert [level for level, _ in _error_records(caplog)] == [logging.ERROR]
    assert get_engine().pool.checkedout() == 0
