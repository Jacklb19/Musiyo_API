import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_guide import guide as guide

from app.content_operations import import_package
from app.main import create_app
from scripts.prepare_narration_append import NarrationPlan, prepare

FRAME = b'\xff\xfb\x90\xc0' + bytes(413)


def plan(**extra):
    return NarrationPlan.model_validate(dict(operation_version=1, voice_label="Synthetic", credit="Synthetic credit",
        provenance="Synthetic provenance", recordings=[
            dict(stem="01 sintetico", script_id="s-01", targets=[dict(slug="synthetic-import", resource_id="narration-synthetic-v2")]),
            dict(stem="02 sin audio", script_id="s-02", targets=[dict(slug="synthetic-import", resource_id="narration-synthetic-v3")]),
            dict(stem="03 ficha nueva", script_id="s-03", targets=[dict(slug="ficha-que-no-existe", resource_id="narration-new-v1")]),
        ], **extra))


def recordings(folder: Path):
    folder.mkdir()
    for stem in ("01 sintetico", "03 ficha nueva"):
        (folder / f"{stem}.mp3").write_bytes(FRAME * 40)
        (folder / f"{stem}.txt").write_text("Texto exacto leído por la voz sintética.\n", encoding="utf-8")
    (folder / "02 sin audio.txt").write_text("Sin grabar todavía.", encoding="utf-8")


def test_draft_is_unapproved_and_imports_only_after_an_explicit_decision(guide, tmp_path):
    url, factory = guide[0], guide[1]
    recordings(tmp_path / "rec")
    report = prepare(tmp_path / "rec", plan(), Path(url.removeprefix("sqlite:///")), tmp_path / "out")
    assert [item["resource_id"] for item in report["ready"]] == ["narration-synthetic-v2"]
    assert report["missing_audio"] == ["02 sin audio"]
    assert report["new_elements_pending"] == [{"stem": "03 ficha nueva", "slug": "ficha-que-no-existe"}]
    draft = json.loads((tmp_path / "out/narrations.DRAFT.json").read_text(encoding="utf-8"))
    assert draft["decision"]["approved_culturally"] is False
    resource = draft["elements"][0]["record"]["resources"][0]
    assert resource["transcription"] == "Texto exacto leído por la voz sintética." and resource["duration_seconds"] > 0
    draft["decision"].update(approved_culturally=True, reference="synthetic", responsible="Synthetic owner")
    approved = tmp_path / "out/narrations.json"
    approved.write_text(json.dumps(draft), encoding="utf-8")
    import_package(approved, url, private_storage=tmp_path / "private", resources_only=True)
    body = TestClient(create_app(factory)).get("/api/v1/elements/synthetic-import").json()
    assert "narration-synthetic-v2" in [item["id"] for item in body["resources"]]


def test_output_must_be_new_and_outside_the_repository(guide, tmp_path):
    recordings(tmp_path / "rec")
    (tmp_path / "taken").mkdir()
    database = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError):
        prepare(tmp_path / "rec", plan(), database, tmp_path / "taken")
    with pytest.raises(ValueError):
        prepare(tmp_path / "rec", plan(), database, Path(__file__).resolve().parents[1] / "narrations-out")
