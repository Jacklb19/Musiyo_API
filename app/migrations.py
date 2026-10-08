"""Explicit database upgrades; importing the API never modifies a schema."""
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(database_url: str | None = None) -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["database_url"] = database_url or os.getenv("MUSIYO_DATABASE_URL", "sqlite:///./musiyo.db")
    command.upgrade(config, "head")
