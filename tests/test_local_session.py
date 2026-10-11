import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("pwsh")
WEB = ROOT.parent / "Musiyo_Web"
pytestmark = pytest.mark.skipif(os.name != "nt" or not SHELL or not (WEB / "node_modules/vite/bin/vite.js").exists(), reason="Windows session integration needs installed API/Web dependencies")


@pytest.fixture
def session_config(tmp_path):
    database = tmp_path / "selected.db"
    with sqlite3.connect(database) as db:
        db.executescript("create table alembic_version(version_num text); insert into alembic_version values('0005'); create table elements(id text); create view v_current_elements as select * from elements;")
    (tmp_path / "private").mkdir()
    (tmp_path / "guide.json").write_text(json.dumps({"generation": "local", "embeddings": "local"}))
    config = dict(schema_version=1, database_path="selected.db", storage_root="private", guide_config="guide.json", api_origin="http://127.0.0.1:18000", web_origin="http://localhost:18001", web_root=str(WEB), state_directory="state", reuse_existing_api=False)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(config))
    return path, config


def run_session(path, action):
    return subprocess.run([SHELL, "-NoProfile", "-File", str(ROOT / "scripts/local-session.ps1"), "-Action", action, "-Config", str(path)], capture_output=True, text=True, timeout=20)


def test_selected_configuration_rejects_remote_origins_and_unknown_fields(session_config):
    path, config = session_config
    assert run_session(path, "validate").returncode == 0
    for patch in [{"api_origin": "http://example.com:8000"}, {"web_origin": "http://localhost:5174/path"}, {"reuse_existing_api": "true"}, {"extra": "unknown"}]:
        path.write_text(json.dumps(config | patch))
        assert run_session(path, "validate").returncode != 0


def test_occupied_api_is_not_adopted_or_stopped_without_explicit_reuse(session_config):
    path, config = session_config
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config["api_origin"] = "http://127.0.0.1:" + str(listener.getsockname()[1])
        path.write_text(json.dumps(config))
        result = run_session(path, "start")
        assert result.returncode != 0
        assert "Reuse requires explicit configuration" in result.stderr
        assert listener.fileno() != -1
        assert not (path.parent / "state/session-state.json").exists()


def test_stop_rejects_reused_pid_identity_before_stopping_any_process(session_config):
    path, _ = session_config
    state = path.parent / "state"
    state.mkdir()
    (state / "session-state.json").write_text(json.dumps({"config_sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(), "api_reused": True, "owned_processes": [{"process_id": os.getpid(), "executable": "unexpected.exe", "started_at": "2000-01-01T00:00:00Z"}]}))
    result = run_session(path, "stop")
    assert result.returncode != 0
    assert "Process identity changed" in result.stderr
    assert (state / "session-state.json").exists()
