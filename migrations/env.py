"""Shared online migration environment for PostgreSQL and SQLite."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import Base, utc_now

config = context.config
url = config.attributes.get("database_url") or os.getenv("MUSIYO_DATABASE_URL", "sqlite:///./musiyo.db")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    if connection.dialect.name == "sqlite":
        # SQLite validates dependent views during a table rebuild.
        connection.connection.driver_connection.create_function("musiyo_now", 0,
            lambda: utc_now().replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds"))
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
