import hashlib
import json

from test_content_operations import package_fixture

from scripts.audit_narrations import audit


def test_narration_audit_checks_actual_audio_without_importing_or_changing_inputs(tmp_path):
    _, manifest, value = package_fixture(tmp_path)
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    report = audit(manifest)
    assert report["state"] == "review_required"
    recording = report["recordings"][0]
    assert recording["measured_seconds"] == 1.0 and recording["measurement_issues"] == []
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
    value["elements"][0]["record"]["resources"][0]["duration_seconds"] = 2
    manifest.write_text(json.dumps(value), encoding="utf-8")
    assert audit(manifest)["recordings"][0]["measurement_issues"]

    value["elements"][0]["record"]["resources"][0].update(transcription=" ", credit=" ", provenance=" ")
    manifest.write_text(json.dumps(value), encoding="utf-8")
    issues = audit(manifest)["recordings"][0]["measurement_issues"]
    assert any("transcript" in issue for issue in issues)
    assert any("credit" in issue for issue in issues)
