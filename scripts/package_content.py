"""Pack the private museum files a database references into one verified archive for free hosts.

Every resource file (current or revoked, so a later revocation never needs a new archive) is checked
against its SHA-256 and stored under its private path. The archive and its digest go outside the
repository; the server restores it at start with deployment/fetch_content.py. --part-size-mb also writes
numbered parts (.part1, .part2, ...) for hosts that limit upload size; the digest covers the whole archive.
"""
import argparse
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import sqlalchemy as sa

from app.db import make_session_factory


def package(database_url: str, storage: Path, output: Path, part_size: int = 0) -> dict:
    if output.exists():
        raise ValueError("Choose a new archive path")
    if output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Content archives must remain outside the repository")
    factory = make_session_factory(database_url)
    try:
        with factory() as db:
            rows = db.execute(sa.text("SELECT id, private_path, sha256 FROM resources ORDER BY private_path")).mappings().all()
    finally:
        factory.kw["bind"].dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total = 0
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for row in rows:
            key = row["private_path"]
            if key in seen:
                continue
            path = (storage / key).resolve()
            if not path.is_relative_to(storage.resolve()) or not path.is_file():
                raise ValueError(f"Missing private file for {row['id']}")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError(f"Altered private file for {row['id']}")
            info = tarfile.TarInfo(key)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            bundle.addfile(info, io.BytesIO(data))
            seen.add(key)
            total += len(data)
    data = output.read_bytes()
    report = dict(archive=str(output), sha256=hashlib.sha256(data).hexdigest(), files=len(seen), content_bytes=total,
                  archive_bytes=len(data), parts=split(output, part_size) if part_size else [])
    return report


def split(archive: Path, part_size: int) -> list[str]:
    """Numbered parts that, joined in order, give back the archive byte for byte."""
    data, parts = archive.read_bytes(), []
    for index, start in enumerate(range(0, len(data), part_size), start=1):
        part = archive.with_name(f"{archive.name}.part{index}")
        part.write_bytes(data[start:start + part_size])
        parts.append(str(part))
    return parts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--storage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--part-size-mb", type=int, default=0, help="Also write parts of at most this size (Supabase free: 45)")
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(package(arguments.database_url, arguments.storage.resolve(strict=True), arguments.output.resolve(),
                             arguments.part_size_mb * 1024 * 1024), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
