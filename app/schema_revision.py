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
    """The database is not at the head revisions of the migrations this build ships with."""


def migrations_heads() -> list[str]:
    """Return the sorted head revisions in MIGRATIONS_DIR; empty if it has no migrations."""
    return sorted(ScriptDirectory(MIGRATIONS_DIR).get_heads())


def check_schema_revision(connection: Connection) -> None:
    """Raise SchemaRevisionMismatch unless the database is at the migrations' heads.

    The revisions in alembic_version, sorted, must equal the non-empty sorted heads of the
    migrations. A missing or empty table, an extra row, another revision and a build with
    no migrations are all refused; the error names both lists and is logged at ERROR before
    it is raised. The query runs in the connection's current transaction, which is left to
    the caller.
    """
    expected = migrations_heads()
    current = sorted(MigrationContext.configure(connection).get_current_heads())
    if expected and current == expected:
        return
    message = (
        f"the database's alembic_version holds {current}, but the migrations in"
        f" {MIGRATIONS_DIR} have the heads {expected}: run the migrations"
        " (alembic upgrade head), or deploy the build that matches the database"
    )
    logger.error(message)
    raise SchemaRevisionMismatch(message)


def check_engine_schema(engine: Engine) -> None:
    """Run check_schema_revision on one short connection, returned to the pool after."""
    with engine.connect() as connection:
        check_schema_revision(connection)
