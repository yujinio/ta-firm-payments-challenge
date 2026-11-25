from alembic import context
from sqlalchemy import create_engine

from app.config import get_settings

config = context.config
connectable = create_engine(get_settings().database_url)

with connectable.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
connectable.dispose()
