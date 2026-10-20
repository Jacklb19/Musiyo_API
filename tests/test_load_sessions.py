import asyncio
import hashlib
from pathlib import Path

import httpx
from test_guide import guide as guide

from app.main import create_app
from scripts.load_sessions import run, summary


def test_concurrent_sessions_check_flows_and_database_without_storing_content(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    app = create_app(guide[1], private_storage=tmp_path / "private", guide_service=guide[2])
    transport = httpx.ASGITransport(app=app)
    report = asyncio.run(run("http://testserver/api/v1", 4, (0, 0), database, transport, tour="recorrido-prueba"))
    assert report["failures"] == [] and report["sessions_without_failures"] == 4 and report["database_unchanged"]
    assert report["database_sha256_before"] == before and set(report["status_counts"]) == {"200"}
    assert {"catalog", "element", "resource_delivery", "tour", "guide_question"} <= set(report["endpoints"])
    assert "answer" not in str(report) and "Instrumento" not in str(report)


def test_latency_summary_reports_percentiles():
    result = summary([float(value) for value in range(1, 21)])
    assert result == dict(samples=20, median_seconds=10.5, p95_seconds=19.0, maximum_seconds=20.0)
