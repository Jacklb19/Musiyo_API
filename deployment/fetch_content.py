"""Container start step: restore the private museum files from a verified archive.

Free hosts (Render free) wipe the disk on every restart, so the API starts by downloading the content
archive made with scripts.package_content, checks its SHA-256 and unpacks it into MUSIYO_STORAGE_ROOT.
Hosts with an upload limit (Supabase free: 50 MB per file) get the archive in parts: list the part URLs
in order, separated by spaces or commas; they are joined before the single SHA-256 check.
Nothing happens when MUSIYO_CONTENT_ARCHIVE_URL is unset (Docker volume or S3 deployments).
"""
import hashlib
import os
import re
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def main() -> int:
    urls = [part for part in re.split(r"[\s,]+", os.getenv("MUSIYO_CONTENT_ARCHIVE_URL", "")) if part]
    if not urls:
        return 0
    expected = os.getenv("MUSIYO_CONTENT_ARCHIVE_SHA256", "").strip().lower()
    root = Path(os.getenv("MUSIYO_STORAGE_ROOT", "/storage"))
    if any(not url.startswith("https://") for url in urls) or len(expected) != 64:
        print("Content archive needs an https URL and its SHA-256", file=sys.stderr)
        return 1
    marker = root / ".content-sha256"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected:
        return 0
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
        for url in urls:
            request = urllib.request.Request(url, headers={"User-Agent": "Musiyo-API/1.0"})
            with urllib.request.urlopen(request, timeout=300) as response:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    handle.write(chunk)
        archive = Path(handle.name)
    try:
        if digest.hexdigest() != expected:
            print("Content archive checksum mismatch; refusing to start with unverified files", file=sys.stderr)
            return 1
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter="data")
        marker.write_text(expected, encoding="utf-8")
    finally:
        archive.unlink(missing_ok=True)
    print(f"Content archive restored into {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
