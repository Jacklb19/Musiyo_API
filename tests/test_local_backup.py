import hashlib
from pathlib import Path

import pytest
from test_guide import guide as guide

from app.content_operations import revoke
from scripts.local_backup import backup, new_external_folder, restore


def test_backup_and_restore_preserve_hashes_permissions_and_never_touch_source(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    revoke("synthetic-import", database_url=guide[0], reason="Synthetic revocation")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest = backup(database, tmp_path / "private", tmp_path / "snapshot")
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert manifest["database"]["integrity"] == "ok" and manifest["storage"]
    assert manifest["database"]["revoked_authorizations"] >= 1
    report = restore(tmp_path / "snapshot", tmp_path / "restored")
    assert report["passed"] and all(report["checks"].values())
    with pytest.raises(FileExistsError):
        restore(tmp_path / "snapshot", tmp_path / "restored")


def test_restore_refuses_tampered_snapshot_and_workspace_destinations(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    manifest = backup(database, tmp_path / "private", tmp_path / "snapshot")
    stored = tmp_path / "snapshot" / "private" / next(iter(manifest["storage"]))
    stored.write_bytes(stored.read_bytes() + b"tampered")
    with pytest.raises(ValueError):
        restore(tmp_path / "snapshot", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    with pytest.raises(ValueError):
        new_external_folder(str(Path(__file__).resolve().parent / "new-backup"))
    with pytest.raises(ValueError):
        new_external_folder(str(tmp_path / "snapshot"))
