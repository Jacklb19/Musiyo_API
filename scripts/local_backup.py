"""Verifiable backup and restore for the local SQLite museum and its private storage.

backup  reads the selected database through SQLite's online backup API (source opened read-only)
        and copies the private storage into a NEW external folder with a SHA-256 manifest.
restore verifies every hash and rebuilds the snapshot into a NEW, empty destination. It never
        writes to the source or to an existing database; selecting the restored copy for a
        session is a separate, explicit decision after inspection.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.evaluate_guide import WORKSPACE

DATABASE = "museum.db"
STORAGE = "private"
MANIFEST = "backup-manifest.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def new_external_folder(value: str) -> Path:
    folder = Path(value).resolve()
    if folder.is_relative_to(WORKSPACE) or folder.exists() or not folder.parent.is_dir():
        raise ValueError("Choose a new folder outside the repository workspace in an existing directory")
    return folder


def summary(database: Path) -> dict:
    """Schema revision, integrity and permission-bearing row counts, read-only."""
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [row[0] for row in db.execute("select name from sqlite_master where type='table' order by name")]
        counts = {table: db.execute(f'select count(*) from "{table}"').fetchone()[0] for table in tables}
        revoked = (db.execute("select count(*) from authorizations where revoked_at is not null").fetchone()[0]
                   if "authorizations" in tables else None)
        return dict(revision=db.execute("select version_num from alembic_version").fetchone()[0],
                    integrity=db.execute("pragma integrity_check").fetchone()[0], row_counts=counts,
                    revoked_authorizations=revoked)


def files(root: Path) -> dict[str, dict]:
    return {path.relative_to(root).as_posix(): dict(sha256=digest(path), bytes=path.stat().st_size)
            for path in sorted(root.rglob("*")) if path.is_file()}


def backup(database: Path, storage: Path, folder: Path) -> dict:
    database, storage = database.resolve(strict=True), storage.resolve(strict=True)
    if not storage.is_dir():
        raise ValueError("Select the private storage directory that matches the database")
    folder.mkdir()
    target = folder / DATABASE
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    shutil.copytree(storage, folder / STORAGE)
    copied = dict(sha256=digest(target), bytes=target.stat().st_size, **summary(target))
    if copied["integrity"] != "ok":
        raise ValueError("Backup copy failed SQLite integrity check")
    manifest = dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
                    source=dict(database=str(database), storage=str(storage)),
                    database=copied, storage=files(folder / STORAGE))
    (folder / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def restore(snapshot: Path, destination: Path) -> dict:
    snapshot = snapshot.resolve(strict=True)
    manifest = json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unknown backup manifest")
    if digest(snapshot / DATABASE) != manifest["database"]["sha256"]:
        raise ValueError("Database hash does not match the manifest")
    if files(snapshot / STORAGE) != manifest["storage"]:
        raise ValueError("Private storage does not match the manifest")
    destination.mkdir()
    shutil.copyfile(snapshot / DATABASE, destination / DATABASE)
    shutil.copytree(snapshot / STORAGE, destination / STORAGE)
    restored = summary(destination / DATABASE)
    checks = dict(
        database_hash=digest(destination / DATABASE) == manifest["database"]["sha256"],
        storage_hashes=files(destination / STORAGE) == manifest["storage"],
        integrity=restored["integrity"] == "ok",
        revision=restored["revision"] == manifest["database"]["revision"],
        row_counts=restored["row_counts"] == manifest["database"]["row_counts"],
        revoked_authorizations=restored["revoked_authorizations"] == manifest["database"]["revoked_authorizations"])
    return dict(schema_version=1, restored_at=datetime.now(timezone.utc).isoformat(), snapshot=str(snapshot),
                destination=str(destination), checks=checks, passed=all(checks.values()),
                activation="Not selected by any session. Inspect, then select it explicitly in a new session file.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("backup")
    make.add_argument("--database", required=True)
    make.add_argument("--storage", required=True)
    make.add_argument("--backup-folder", required=True)
    rebuild = commands.add_parser("restore")
    rebuild.add_argument("--snapshot", required=True)
    rebuild.add_argument("--destination", required=True)
    arguments = parser.parse_args()
    if arguments.command == "backup":
        result = backup(Path(arguments.database), Path(arguments.storage), new_external_folder(arguments.backup_folder))
        print(json.dumps(dict(database_sha256=result["database"]["sha256"], files=len(result["storage"]),
                              integrity=result["database"]["integrity"])))
        return 0
    report = restore(Path(arguments.snapshot), new_external_folder(arguments.destination))
    (Path(arguments.destination) / "restore-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["checks"]))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
