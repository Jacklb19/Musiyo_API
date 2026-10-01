"""Shared online migration environment for PostgreSQL and SQLite."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import Base

config = context.config
url = config.attributes.get("database_url") or os.getenv("MUSIYO_DATABASE_URL", "sqlite:///./musiyo.db")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
