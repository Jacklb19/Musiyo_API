"""Copy the curated local museum (SQLite + private files) into an empty, migrated deployment database.

The local preview is where content is reviewed and corrected; a deployment (Postgres, local volume or
S3) starts from an exact copy of it. The source is opened read-only. The target must be at the same
migration revision and hold no content. All base tables are copied in one transaction (dependency
order, identical keys); derived tables are rebuilt by the target itself: catalogue search through
its triggers and the guide index through reindex. Every private file is copied or uploaded only after
its SHA-256 matches the database. The result compares the public API views of source and target
(elements, tours, catalogue facets, credits) and reports any difference.
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

from app.content_operations import reindex_in_session
from app.db import Base, make_session_factory
from app.museum_credits import museum_credits
from app.public_repository import PublicRepository
from app.resource_storage import configured_s3

# Rebuilt by the target (search triggers, guide reindex) or meaningless after a move (sessions).
SKIPPED = {"element_search", "index_fragments", "validator_sessions", "validator_login_attempts"}


def tables():
    return [table for table in Base.metadata.sorted_tables if table.name not in SKIPPED]


def referenced_first(table, rows: list[dict]) -> list[dict]:
    """Order rows so a row that points to another row of the same table (a subtitle, a parent model) follows it."""
    links = [(element.parent.name, element.column.name) for element in table.foreign_keys if element.column.table is table]
    if not links:
        return rows
    ordered: list[dict] = []
    placed: set = set()
    pending = list(rows)
    while pending:
        ready = [row for row in pending if all(row[column] is None or row[column] in placed for column, _ in links)]
        if not ready:
            raise ValueError(f"{table.name}: circular or missing self references")
        for row in ready:
            ordered.append(row)
            placed.update(row[key] for _, key in links)
        pending = [row for row in pending if row not in ready]
    return ordered


def revision(connection) -> str | None:
    try:
        return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    except sa.exc.DBAPIError:
        return None


def public_view(factory) -> dict:
    with factory() as db:
        repository = PublicRepository(db)
        elements = repository.elements()
        tours = [row[0] for row in db.execute(sa.text("SELECT id FROM tours ORDER BY id"))]
        return dict(
            # Keyed by slug: SQLite and Postgres collate hyphenated slugs differently, which only changes list order.
            elements={element.slug: element.model_dump(mode="json", exclude={"last_modified"}) for element in elements},
            tours={key: (tour.model_dump(mode="json", exclude={"revision"}) if (tour := repository.tour(key)) else None)
                   for key in tours},
            categories=[facet.model_dump(mode="json") for facet in repository.facets("categories")],
            collections=[facet.model_dump(mode="json") for facet in repository.facets("collections")],
            credits=museum_credits(sorted(elements, key=lambda element: element.slug)).model_dump(mode="json"))


def differences(left, right, path="") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        return [item for key in sorted(set(left) | set(right))
                for item in differences(left.get(key), right.get(key), f"{path}/{key}")]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [item for index, (a, b) in enumerate(zip(left, right)) for item in differences(a, b, f"{path}[{index}]")]
    return [] if left == right or same_instant(left, right) else [path or "/"]


def same_instant(left, right) -> bool:
    # SQLite returns stored UTC times without an offset; Postgres returns them with one.
    if not (isinstance(left, str) and isinstance(right, str)):
        return False
    try:
        a, b = datetime.fromisoformat(left), datetime.fromisoformat(right)
    except ValueError:
        return False
    return a.replace(tzinfo=a.tzinfo or timezone.utc) == b.replace(tzinfo=b.tzinfo or timezone.utc)


def transfer(source_database: Path, source_storage: Path, target_url: str, target_storage: Path | None,
             *, dry_run: bool) -> dict:
    source = make_session_factory("sqlite:///file:" + source_database.as_posix() + "?mode=ro&uri=true")
    target = make_session_factory(target_url)
    remote = configured_s3()
    if remote is None and target_storage is None:
        raise ValueError("Select --target-storage for local files, or MUSIYO_STORAGE_BACKEND=s3")
    report: dict = dict(schema_version=1, dry_run=dry_run, tables={}, files=dict(copied=0, bytes=0))
    try:
        with source() as reader, target.begin() as writer:
            source_revision, target_revision = revision(reader.connection()), revision(writer.connection())
            if source_revision is None or source_revision != target_revision:
                raise ValueError(f"Migrate the target first: source {source_revision}, target {target_revision}")
            occupied = [table.name for table in tables()
                        if writer.execute(sa.select(sa.func.count()).select_from(table)).scalar()]
            if occupied:
                raise ValueError(f"The target already holds content: {occupied}")
            resources = reader.execute(sa.text("SELECT id, private_path, sha256, mime FROM resources")).mappings().all()
            for resource in resources:
                path = source_storage / resource["private_path"]
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != resource["sha256"]:
                    raise ValueError(f"Missing or altered private file for {resource['id']}")
            for table in tables():
                rows = referenced_first(table, [dict(row) for row in reader.execute(sa.select(table)).mappings()])
                if rows:
                    writer.execute(table.insert(), rows)
                report["tables"][table.name] = len(rows)
            if not dry_run:
                for resource in resources:
                    path, key = source_storage / resource["private_path"], resource["private_path"]
                    if remote is not None:
                        remote.upload_local(path, key, resource["mime"], resource["sha256"])
                    else:
                        assert target_storage is not None
                        destination = (target_storage / key).resolve()
                        if not destination.is_relative_to(target_storage.resolve()):
                            raise ValueError("Unsafe private path")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != resource["sha256"]:
                            raise ValueError(f"A different file already exists at {key}")
                        shutil.copyfile(path, destination)
                    report["files"]["copied"] += 1
                    report["files"]["bytes"] += path.stat().st_size
            report["guide_fragments"] = reindex_in_session(writer)
            if dry_run:
                writer.rollback()
        if not dry_run:
            mismatches = differences(public_view(source), public_view(target))
            report["public_view_identical"] = not mismatches
            report["public_view_differences"] = mismatches[:50]
    finally:
        source.kw["bind"].dispose()
        target.kw["bind"].dispose()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", required=True, type=Path)
    parser.add_argument("--source-storage", required=True, type=Path)
    parser.add_argument("--target-database-url", required=True, help="Migrated, empty deployment database")
    parser.add_argument("--target-storage", type=Path, help="Local private storage root (omit with MUSIYO_STORAGE_BACKEND=s3)")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    report = transfer(arguments.source_database.resolve(strict=True), arguments.source_storage.resolve(strict=True),
                      arguments.target_database_url, arguments.target_storage, dry_run=arguments.dry_run)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("public_view_identical", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
