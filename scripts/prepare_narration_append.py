"""Draft a resources-only narration manifest from an owner's recording folder (read-only on the database).

The external plan maps each recording stem ("01 bombo") to one or more existing elements and new
resource IDs. Every "<stem>.mp3" must sit beside "<stem>.txt" holding the exact words spoken. The
audio is measured, hashed and copied into a new output folder; each parent record is taken from the
current public record, as `import --resources-only` requires. Stems without audio are reported, and
elements that do not exist yet are left for their own new-element import. The decision stays
unapproved: owner listening and an explicit decision come before any import.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from app.content_package import ContentPackage
from app.content_tables import CONTENT
from app.db import PointElementRecord, make_session_factory, utc_now
from app.public_repository import PublicRepository
from scripts.evaluate_guide import external_file
from scripts.media_probe import probe_audio

MAX_BYTES = 3 * 1024 * 1024


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    replaces: str | None = None


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stem: str = Field(min_length=1, max_length=200)
    script_id: str = Field(min_length=1, max_length=100)
    targets: list[Target] = Field(min_length=1, max_length=4)


class NarrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int = Field(ge=1, le=1)
    voice_label: str = Field(min_length=1, max_length=100)
    credit: str = Field(min_length=1, max_length=500)
    provenance: str = Field(min_length=1, max_length=1000)
    recordings: list[Recording] = Field(min_length=1, max_length=40)


def prepare(folder: Path, plan: NarrationPlan, database: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Choose a new output folder")
    if output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Cultural packages must remain outside the repository")
    factory = make_session_factory("sqlite:///" + database.as_posix())
    elements: list[dict[str, Any]] = []
    files: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = dict(ready=[], missing_audio=[], new_elements_pending=[], replaces=[], issues=[])
    try:
        with factory() as db:
            public = PublicRepository(db)
            for recording in plan.recordings:
                audio, script = folder / f"{recording.stem}.mp3", folder / f"{recording.stem}.txt"
                if not audio.is_file():
                    report["missing_audio"].append(recording.stem)
                    continue
                text = script.read_text(encoding="utf-8-sig").strip() if script.is_file() else ""
                size = audio.stat().st_size
                probe = probe_audio(audio, "audio/mpeg")
                problems = [message for check, message in (
                    (bool(text), "missing or empty transcript (.txt)"), (size <= MAX_BYTES, "audio larger than 3 MB"),
                    (probe is not None, "audio could not be measured"),
                    (probe is None or 0 < probe["duration_seconds"] <= 180, "narration must last 0-180 s")) if not check]
                if problems or probe is None:
                    report["issues"].append(dict(stem=recording.stem, problems=problems))
                    continue
                digest = hashlib.sha256(audio.read_bytes()).hexdigest()
                for target in recording.targets:
                    current = public.element(target.slug)
                    if current is None:
                        report["new_elements_pending"].append(dict(stem=recording.stem, slug=target.slug))
                        continue
                    points = sorted(db.scalars(sa.select(PointElementRecord.point_id)
                                               .where(PointElementRecord.element_id == target.slug)))
                    record = current.model_dump(mode="json", exclude={"last_modified"})
                    record["resources"] = [dict(id=target.resource_id, kind="narration", mime="audio/mpeg", byte_count=size,
                        duration_seconds=round(probe["duration_seconds"], 6), alternative_text=None, credit=plan.credit,
                        provenance=plan.provenance, variants=[], transcription=text, subtitles_resource_id=None)]
                    kind = db.scalar(sa.select(CONTENT["element_metadata"].c.kind)
                                     .where(CONTENT["element_metadata"].c.element_id == target.slug))
                    elements.append(dict(record=record, kind=kind, point_keys=points))
                    files[target.resource_id] = dict(file=f"files/{target.resource_id}.mp3", sha256=digest, profile="general",
                        point_key=points[0] if points else None, paradata=dict(
                            source_filename=audio.name, script_id=recording.script_id,
                            script_sha256=hashlib.sha256(text.encode()).hexdigest(), voice_label=plan.voice_label,
                            listening_acceptance="pending_owner", literal_transcript_match="pending_owner_listening",
                            pronunciation="pending_owner_listening", webvtt="not_created_without_actual_timing",
                            replaces=target.replaces), metadata=probe)
                    report["ready"].append(dict(stem=recording.stem, slug=target.slug, resource_id=target.resource_id,
                                                seconds=round(probe["duration_seconds"], 2), bytes=size))
                    if target.replaces:
                        report["replaces"].append(dict(new=target.resource_id, revoke_after_import=target.replaces))
    finally:
        factory.kw["bind"].dispose()
    if not elements:
        return report
    now = utc_now()
    package = ContentPackage.model_validate_json(json.dumps(dict(manifest_version=1, elements=elements, files=files, decision=dict(
        reference="PENDIENTE-escucha-y-decision-del-propietario", responsible="PENDIENTE", decision_date=now.date().isoformat(),
        approved_culturally=False, authorized_from=now.isoformat()))))
    (output / "files").mkdir(parents=True)
    for recording in plan.recordings:
        for target in recording.targets:
            if target.resource_id in files:
                shutil.copyfile(folder / f"{recording.stem}.mp3", output / files[target.resource_id]["file"])
    (output / "narrations.DRAFT.json").write_text(package.model_dump_json(indent=1), encoding="utf-8")
    report["manifest"] = str(output / "narrations.DRAFT.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True, help="New external folder for the draft package")
    arguments = parser.parse_args()
    plan = NarrationPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
    report = prepare(Path(arguments.folder).resolve(strict=True), plan, external_file(arguments.database),
                     Path(arguments.output).resolve())
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
