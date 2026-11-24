import logging
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

logger = logging.getLogger(__name__)

# Resolved from this file, not from alembic.ini: its script_location is relative to the
# working directory, which differs between the image, the host and live_server.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class SchemaRevisionMismatch(RuntimeError):
    """The database is not at the head revision of the migrations this build ships with."""


def migrations_head() -> str | None:
    """Return the head revision in MIGRATIONS_DIR; None only if there are no migrations."""
    return ScriptDirectory(MIGRATIONS_DIR).get_current_head()


def check_schema_revision(connection: Connection) -> None:
    """Raise SchemaRevisionMismatch unless the database is at the migrations' head.

    A database without an alembic_version table, or without a row in it, does not match.
    The error names both revisions and is logged at ERROR before it is raised. The query
    runs in the connection's current transaction, which is left to the caller.
    """
    head = migrations_head()
    current = MigrationContext.configure(connection).get_current_revision()
    if current == head:
        return
    found = "no revision" if current is None else f"revision {current}"
    message = (
        f"the database schema is at {found}, but this build expects revision {head}: "
        "run the migrations (alembic upgrade head), or deploy the build that matches"
        " the database"
    )
    logger.error(message)
    raise SchemaRevisionMismatch(message)


def check_engine_schema(engine: Engine) -> None:
    """Run check_schema_revision on one short connection, returned to the pool after."""
    with engine.connect() as connection:
        check_schema_revision(connection)
