"""Verifiable backup and restore of the online (PostgreSQL) museum database.

backup   dumps the public schema with pg_dump (custom format) into a NEW folder outside the
         repositories, with a manifest holding the dump's SHA-256, the server and tool versions,
         the installed extensions, the schema revision, exact row counts and a content
         fingerprint of every table. Once validators correct online, this database is the
         source of truth: the private files still come from the verified content archive.
restore  verifies the dump against its manifest, restores it into an EMPTY database (one that
         already has tables is refused, so the live database can never be overwritten) and
         compares row counts and fingerprints with the manifest.

The connection string comes from an environment variable (MUSIYO_DATABASE_URL for backup,
MUSIYO_RESTORE_DATABASE_URL for restore) so it never reaches the shell history; the password
reaches pg_dump only through its environment. pg_dump/pg_restore run from PATH when their major
version is at least the server's, otherwise inside Docker with an image of that major version.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from scripts.local_backup import digest, new_external_folder

DUMP = "museum.dump"
MANIFEST = "online-backup-manifest.json"
KIND = "musiyo-online-postgres"
# Rows visitors create by signing in or answering the closing survey; they change between a backup and a correction without
# touching content, so they are recorded but not required to match.
EPHEMERAL = ("validator_sessions", "validator_login_attempts", "survey_responses")
# Images already used by this project; any other major falls back to the official image.
IMAGES = {16: "pgvector/pgvector:0.8.0-pg16", 17: "public.ecr.aws/supabase/postgres:17.11.0.004"}


def environment_url(name: str) -> str:
    url = os.environ.get(name, "").strip()
    if not url:
        raise ValueError(f"Set {name} first (Git Bash: read -rs {name} && export {name})")
    return url


def safe_label(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def engine_for(url: str):
    if make_url(url).get_backend_name() != "postgresql":
        raise ValueError("Online backups are for PostgreSQL databases; use scripts.local_backup for SQLite")
    return sa.create_engine(url, poolclass=sa.pool.NullPool)


def state(url: str) -> dict:
    """Revision, extensions, exact row counts and a collation-independent fingerprint per table."""
    engine = engine_for(url)
    try:
        with engine.connect() as db:
            # Fingerprints compare text renderings, so fix every setting that shapes them.
            db.execute(sa.text("SET TIME ZONE 'UTC'"))
            db.execute(sa.text("SET datestyle = 'ISO, YMD'"))
            db.execute(sa.text("SET extra_float_digits = 3"))
            db.execute(sa.text("SET intervalstyle = 'postgres'"))
            tables = list(db.scalars(sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")))
            counts, fingerprints = {}, {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                counts[table] = db.scalar(sa.text(f"SELECT count(*) FROM public.{quoted}"))
                fingerprints[table] = db.scalar(sa.text(
                    "SELECT encode(sha256(convert_to(coalesce(string_agg(t::text, E'\\n' "
                    f"ORDER BY convert_to(t::text, 'UTF8')), ''), 'UTF8')), 'hex') FROM public.{quoted} t"))
            revision = db.scalar(sa.text("SELECT version_num FROM public.alembic_version")) \
                if "alembic_version" in tables else None
            revoked = db.scalar(sa.text("SELECT count(*) FROM public.authorizations WHERE revoked_at IS NOT NULL")) \
                if "authorizations" in tables else None
            extensions = [dict(name=row.extname, schema=row.nspname, version=row.extversion) for row in db.execute(sa.text(
                "SELECT e.extname, n.nspname, e.extversion FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname IN ('vector', 'unaccent', 'pg_trgm') ORDER BY e.extname"))]
            server = db.scalar(sa.text("SHOW server_version_num"))
    finally:
        engine.dispose()
    return dict(revision=revision, server_version_num=int(server), extensions=extensions,
                row_counts=counts, fingerprints=fingerprints, revoked_authorizations=revoked)


def comparable(summary: dict) -> tuple:
    """What a correction's backup must still match: everything except visitor sign-in rows."""
    keep = lambda values: {name: value for name, value in values.items() if name not in EPHEMERAL}  # noqa: E731
    return summary["revision"], keep(summary["row_counts"]), keep(summary["fingerprints"]), summary["revoked_authorizations"]


def matching_online_backup(url: str, manifest_path: Path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != KIND or manifest.get("schema_version") != 1:
        raise ValueError("Select a folder made by scripts.online_backup for the online database")
    if comparable(manifest["database"]) != comparable(state(url)):
        raise ValueError("The backup does not match the online database's current state; make a new backup")


def tool_major(command: list[str]) -> int | None:
    try:
        output = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"(\d+)(?:\.\d+)?", output)
    return int(found.group(1)) if found else None


def docker_image(major: int, override: str | None) -> str:
    if override:
        return override
    known = IMAGES.get(major)
    if known and subprocess.run(["docker", "image", "inspect", known], capture_output=True).returncode == 0:
        return known
    return f"postgres:{major}-alpine"


def pg_command(tool: str, major: int, folder: Path, image: str | None) -> tuple[list[str], Path, str | None]:
    """The tool to run, the folder as the tool sees it, and the Docker image it runs in (None: from PATH)."""
    local = shutil.which(tool)
    if local and not image and (tool_major([local]) or 0) >= major:
        return [local], folder, None
    chosen = docker_image(major, image)
    command = ["docker", "run", "--rm", "-e", "PGPASSWORD", "-e", "PGSSLMODE",
               "-v", f"{folder}:/backup", chosen, tool]
    found = tool_major(["docker", "run", "--rm", chosen, tool])
    if found is None or found < major:
        raise ValueError(f"{tool} {major} is needed; Docker image {chosen} has {found}")
    return command, Path("/backup"), chosen


def connection(url: str, in_docker: bool) -> tuple[list[str], dict]:
    parsed = make_url(url)
    host = parsed.host or "localhost"
    if in_docker and host in ("localhost", "127.0.0.1", "::1"):
        host = "host.docker.internal"
    arguments = ["--host", host, "--port", str(parsed.port or 5432), "--username", parsed.username or "postgres",
                 "--dbname", parsed.database or "postgres"]
    environment = dict(os.environ, PGPASSWORD=parsed.password or "",
                       PGSSLMODE=str(parsed.query.get("sslmode", "prefer")))
    return arguments, environment


def run(command: list[str], environment: dict, what: str):
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        # libpq never prints the password; the URL itself is not part of the command line.
        raise RuntimeError(f"{what} failed: {result.stderr.strip()[-2000:]}")
    return result


def backup(url: str, folder: Path, *, image: str | None = None) -> dict:
    before = state(url)
    major = before["server_version_num"] // 10000
    folder.mkdir()
    command, inside, used = pg_command("pg_dump", major, folder, image)
    arguments, environment = connection(url, used is not None)
    run(command + ["--format=custom", "--schema=public", "--no-owner", "--no-privileges",
                   "--file", (inside / DUMP).as_posix()] + arguments, environment, "pg_dump")
    after = state(url)
    if comparable(after) != comparable(before):
        raise ValueError("The database changed while it was being copied; run the backup again")
    dump, dumped_with = folder / DUMP, tool_major(command) or major
    # pg_restore must be at least as new as the pg_dump that wrote the archive.
    manifest = dict(schema_version=1, kind=KIND, created_at=datetime.now(timezone.utc).isoformat(),
                    source=safe_label(url), tool=f"pg_dump {dumped_with}" + (f" (Docker {used})" if used else ""),
                    tool_major=dumped_with, dump=dict(file=DUMP, sha256=digest(dump), bytes=dump.stat().st_size),
                    database=after)
    (folder / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def empty(url: str) -> bool:
    engine = engine_for(url)
    try:
        with engine.connect() as db:
            return not db.scalar(sa.text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"))
    finally:
        engine.dispose()


def restore(snapshot: Path, url: str, *, image: str | None = None) -> dict:
    snapshot = snapshot.resolve(strict=True)
    manifest = json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("kind") != KIND or manifest.get("schema_version") != 1:
        raise ValueError("Unknown online backup manifest")
    if digest(snapshot / DUMP) != manifest["dump"]["sha256"]:
        raise ValueError("The dump does not match its manifest")
    if not empty(url):
        raise ValueError("The restore target must be an empty database; the live database is never overwritten")
    engine = engine_for(url)
    try:
        with engine.begin() as db:
            for extension in manifest["database"]["extensions"]:
                schema = '"' + extension["schema"].replace('"', '""') + '"'
                db.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
                db.execute(sa.text(f'CREATE EXTENSION IF NOT EXISTS "{extension["name"]}" WITH SCHEMA {schema}'))
    finally:
        engine.dispose()
    command, inside, used = pg_command("pg_restore", manifest["tool_major"], snapshot, image)
    arguments, environment = connection(url, used is not None)
    listing = run(command + ["--list", (inside / DUMP).as_posix()], environment, "pg_restore --list").stdout
    # The public schema and the extensions already exist in the target.
    kept = [line for line in listing.splitlines()
            if not re.search(r"\b(SCHEMA - public|EXTENSION - |COMMENT - EXTENSION )", line)]
    entries = snapshot / "restore-list.txt"
    entries.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        run(command + ["--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error",
                       "--use-list", (inside / entries.name).as_posix()] + arguments + [(inside / DUMP).as_posix()],
            environment, "pg_restore")
    finally:
        entries.unlink(missing_ok=True)
    restored = state(url)
    recorded = manifest["database"]
    checks = dict(dump_hash=True, revision=restored["revision"] == recorded["revision"],
                  row_counts=restored["row_counts"] == recorded["row_counts"],
                  fingerprints=restored["fingerprints"] == recorded["fingerprints"],
                  revoked_authorizations=restored["revoked_authorizations"] == recorded["revoked_authorizations"])
    return dict(schema_version=1, restored_at=datetime.now(timezone.utc).isoformat(), snapshot=str(snapshot),
                destination=safe_label(url), checks=checks, passed=all(checks.values()),
                differing_tables=sorted(name for name in recorded["fingerprints"]
                                        if restored["fingerprints"].get(name) != recorded["fingerprints"][name]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("backup")
    make.add_argument("--backup-folder", required=True, help="A NEW folder outside the repositories")
    make.add_argument("--docker-image", help="Image with pg_dump of the server's major version")
    rebuild = commands.add_parser("restore")
    rebuild.add_argument("--snapshot", required=True)
    rebuild.add_argument("--docker-image")
    check = commands.add_parser("verify", help="Re-check a backup folder's dump against its manifest")
    check.add_argument("--snapshot", required=True)
    arguments = parser.parse_args()
    if arguments.command == "backup":
        manifest = backup(environment_url("MUSIYO_DATABASE_URL"), new_external_folder(arguments.backup_folder),
                          image=arguments.docker_image)
        counts = manifest["database"]["row_counts"]
        print(json.dumps(dict(folder=str(Path(arguments.backup_folder).resolve()), dump_sha256=manifest["dump"]["sha256"],
                              dump_bytes=manifest["dump"]["bytes"], revision=manifest["database"]["revision"],
                              tables=len(counts), rows=sum(counts.values()))))
        return 0
    snapshot = Path(arguments.snapshot)
    if arguments.command == "verify":
        manifest = json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))
        ok = digest(snapshot / DUMP) == manifest["dump"]["sha256"]
        print(json.dumps(dict(dump_hash=ok, created_at=manifest["created_at"])))
        return 0 if ok else 1
    report = restore(snapshot, environment_url("MUSIYO_RESTORE_DATABASE_URL"), image=arguments.docker_image)
    print(json.dumps(dict(checks=report["checks"], passed=report["passed"], differing_tables=report["differing_tables"])))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
