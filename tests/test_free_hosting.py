import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest
from test_guide import guide as guide

from scripts.package_content import package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_content", ROOT / "deployment/fetch_content.py")
fetch_content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_content)


def test_package_holds_every_referenced_file_verified(guide, tmp_path):
    report = package(guide[0], tmp_path / "private", tmp_path / "out/content.tar.gz")
    assert report["files"] >= 1 and len(report["sha256"]) == 64
    with tarfile.open(report["archive"]) as bundle:
        names = bundle.getnames()
        first = bundle.extractfile(names[0]).read()
    assert hashlib.sha256(first).hexdigest() in {path.name.split(".")[0] for path in (tmp_path / "private").rglob("*.*")}
    with pytest.raises(ValueError):
        package(guide[0], tmp_path / "private", tmp_path / "out/content.tar.gz")


def test_package_refuses_altered_files(guide, tmp_path):
    stored = next((tmp_path / "private").rglob("*.*"))
    stored.write_bytes(stored.read_bytes() + b"x")
    with pytest.raises(ValueError, match="Altered"):
        package(guide[0], tmp_path / "private", tmp_path / "content.tar.gz")


def archive_bytes():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = b"contenido"
        info = tarfile.TarInfo("objects/ab/abc.mp3")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_start_restores_verified_content_once(tmp_path, monkeypatch):
    data = archive_bytes()
    calls = []
    monkeypatch.setattr(fetch_content.urllib.request, "urlopen", lambda request, timeout: calls.append(request) or Response(data))
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_URL", "https://storage.example/content.tar.gz")
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setenv("MUSIYO_STORAGE_ROOT", str(tmp_path / "storage"))
    assert fetch_content.main() == 0
    assert (tmp_path / "storage/objects/ab/abc.mp3").read_bytes() == b"contenido"
    assert fetch_content.main() == 0 and len(calls) == 1


def test_start_refuses_unverified_or_insecure_archives(tmp_path, monkeypatch):
    data = archive_bytes()
    monkeypatch.setattr(fetch_content.urllib.request, "urlopen", lambda request, timeout: Response(data))
    monkeypatch.setenv("MUSIYO_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_URL", "https://storage.example/content.tar.gz")
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_SHA256", "0" * 64)
    assert fetch_content.main() == 1 and not (tmp_path / "storage/objects").exists()
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_URL", "http://storage.example/content.tar.gz")
    assert fetch_content.main() == 1
    monkeypatch.delenv("MUSIYO_CONTENT_ARCHIVE_URL")
    assert fetch_content.main() == 0


def test_archive_in_parts_is_joined_before_the_single_check(guide, tmp_path, monkeypatch):
    report = package(guide[0], tmp_path / "private", tmp_path / "out/content.tar.gz", part_size=100)
    parts = [Path(part).read_bytes() for part in report["parts"]]
    assert len(parts) >= 2 and b"".join(parts) == Path(report["archive"]).read_bytes()
    served = {f"https://storage.example/part{index}": data for index, data in enumerate(parts, start=1)}
    monkeypatch.setattr(fetch_content.urllib.request, "urlopen", lambda request, timeout: Response(served[request.full_url]))
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_URL", " ".join(served) + "\n")
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_SHA256", report["sha256"])
    monkeypatch.setenv("MUSIYO_STORAGE_ROOT", str(tmp_path / "storage"))
    assert fetch_content.main() == 0 and any((tmp_path / "storage").rglob("*.*"))
    monkeypatch.setenv("MUSIYO_CONTENT_ARCHIVE_URL", ",".join(reversed(list(served))))
    monkeypatch.setenv("MUSIYO_STORAGE_ROOT", str(tmp_path / "other"))
    assert fetch_content.main() == 1
