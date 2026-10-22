"""Where a reviewed correction runs: the local SQLite museum or the online PostgreSQL database.

--database selects a local SQLite file, whose backup comes from scripts.local_backup.
--database-url selects the online database; given without a value it reads MUSIYO_DATABASE_URL,
so the connection string never reaches the shell history. Its backup comes from
scripts.online_backup. Applying always needs a backup folder that matches the target's current
state; a dry run needs none. Online, the transaction runs at REPEATABLE READ: the guard that
compares every other row reads one snapshot, so a validator saving another ficha meanwhile
neither slips into the comparison nor is overwritten.
"""
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db import (
    PointElementRecord,
    PointRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
)
from scripts.evaluate_guide import external_file
from scripts.local_backup import MANIFEST
from scripts.online_backup import MANIFEST as ONLINE_MANIFEST
from scripts.online_backup import matching_online_backup, safe_label

ENVIRONMENT = "MUSIYO_DATABASE_URL"


def add_arguments(parser):
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--database", help="Local SQLite museum file")
    where.add_argument("--database-url", nargs="?", const=ENVIRONMENT, metavar="URL",
                       help=f"Online PostgreSQL database; without a value, read from ${ENVIRONMENT}")
    parser.add_argument("--backup-folder", help=f"Folder holding {MANIFEST} (local) or {ONLINE_MANIFEST} (online) "
                                                "for this exact database state")
    parser.add_argument("--dry-run", action="store_true")


def selected(arguments) -> str:
    if arguments.database:
        return "sqlite:///" + external_file(arguments.database).as_posix()
    url = os.environ.get(ENVIRONMENT, "").strip() if arguments.database_url == ENVIRONMENT else arguments.database_url
    if not url:
        raise ValueError(f"Set {ENVIRONMENT} first (Git Bash: read -rs {ENVIRONMENT} && export {ENVIRONMENT})")
    if make_url(url).get_backend_name() != "postgresql":
        raise ValueError("--database-url is for the online PostgreSQL database; use --database for SQLite")
    return url


def url_of(target: Path | str) -> str:
    return "sqlite:///" + target.as_posix() if isinstance(target, Path) else target


def online(target: Path | str) -> bool:
    return make_url(url_of(target)).get_backend_name() == "postgresql"


def label(target: Path | str) -> str:
    return safe_label(url_of(target)) if online(target) else str(target)


def session_factory(target: Path | str) -> sessionmaker:
    factory = make_session_factory(url_of(target))
    if online(target):
        factory.configure(bind=factory.kw["bind"].execution_options(isolation_level="REPEATABLE READ"))
    return factory


def require_backup(arguments, target: str):
    if arguments.dry_run:
        return
    if not arguments.backup_folder:
        tool = "scripts.online_backup" if online(target) else "scripts.local_backup"
        raise ValueError(f"Applying requires --backup-folder from {tool}")
    folder = Path(arguments.backup_folder).resolve(strict=True)
    if online(target):
        matching_online_backup(target, folder / ONLINE_MANIFEST)
    else:
        from scripts.update_credits import matching_backup
        matching_backup(Path(make_url(target).database or ""), folder / MANIFEST)


def bump_tours(db, slugs, stamp) -> dict[str, str]:
    """Give every tour that shows one of these fichas a new revision, so clients refresh."""
    keys = set(db.scalars(sa.select(RoomRecord.tour_id).join(PointRecord, PointRecord.room_id == RoomRecord.id)
                          .join(PointElementRecord, PointElementRecord.point_id == PointRecord.id)
                          .where(PointElementRecord.element_id.in_(sorted(slugs)))))
    revisions = {}
    for key in sorted(keys):
        db.get(TourRecord, key).revision = stamp
        revisions[key] = stamp.isoformat()
    return revisions
