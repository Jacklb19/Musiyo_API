import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_guide import guide as guide

from app.db import make_session_factory
from app.main import create_app
from app.migrations import upgrade_database
from scripts.transfer_museum import transfer


def test_copy_is_complete_rebuilds_derived_tables_and_matches_the_public_view(guide, tmp_path):
    source = Path(guide[0].removeprefix("sqlite:///"))
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    upgrade_database(target_url)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    preview = transfer(source, tmp_path / "private", target_url, tmp_path / "target-private", dry_run=True)
    assert preview["dry_run"] and preview["tables"]["elements"] >= 1
    with make_session_factory(target_url)() as db:
        assert db.execute(sa.text("SELECT count(*) FROM elements")).scalar() == 0
    report = transfer(source, tmp_path / "private", target_url, tmp_path / "target-private", dry_run=False)
    assert report["public_view_identical"], report["public_view_differences"]
    assert report["files"]["copied"] == report["tables"]["resources"] and report["guide_fragments"] > 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    client = TestClient(create_app(make_session_factory(target_url)))
    assert client.get("/api/v1/catalog", params={"query": "sintética"}).json()["items"]
    with pytest.raises(ValueError, match="already holds content"):
        transfer(source, tmp_path / "private", target_url, tmp_path / "target-private", dry_run=False)


def test_target_must_be_migrated_and_files_must_match(guide, tmp_path):
    source = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError, match="Migrate the target"):
        transfer(source, tmp_path / "private", f"sqlite:///{tmp_path / 'empty.db'}", tmp_path / "out", dry_run=True)
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    upgrade_database(target_url)
    stored = next((tmp_path / "private").rglob("*.*"))
    stored.write_bytes(stored.read_bytes() + b"x")
    with pytest.raises(ValueError, match="altered private file"):
        transfer(source, tmp_path / "private", target_url, tmp_path / "out", dry_run=True)
