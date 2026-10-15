"""Read-only narration readiness audit using the existing private package schema."""
import argparse
import hashlib
import json
from pathlib import Path

from app.content_operations import checked_files
from app.content_package import ContentPackage
from scripts.evaluate_guide import WORKSPACE, external_file
from scripts.media_probe import probe_audio


def audit(manifest: Path):
    package = ContentPackage.model_validate_json(manifest.read_text(encoding="utf-8-sig"))
    files = checked_files(package, manifest.parent)
    recordings = []
    for item in package.elements:
        for resource in item.record.resources:
            if resource.kind not in {"audio", "narration"}:
                continue
            path, size, _ = files[resource.id]
            probe = probe_audio(path, resource.mime)
            measured = probe["duration_seconds"] if probe else None
            issues = []
            if not resource.transcription or not resource.transcription.strip():
                issues.append("A nonblank transcript of the actual recording is required")
            if not (resource.credit or "").strip() or not (resource.provenance or "").strip():
                issues.append("Actual nonblank credit and provenance are required")
            if measured is None:
                issues.append("Measure the final encoded audio with an independent media probe")
            elif resource.duration_seconds is None or abs(measured - resource.duration_seconds) > 0.1:
                issues.append("Declared duration differs from measured audio")
            if resource.kind == "narration" and (resource.duration_seconds is None or resource.duration_seconds <= 0
                                                 or measured is not None and not 0 < measured <= 180):
                issues.append("Narration must have a measured positive duration of at most 180 seconds")
            recordings.append(dict(element_slug=item.record.slug, resource_id=resource.id, kind=resource.kind,
                                   mime=resource.mime, byte_count=size, sha256=package.files[resource.id].sha256,
                                   transcription_sha256=hashlib.sha256((resource.transcription or "").encode()).hexdigest(),
                                   declared_seconds=resource.duration_seconds, measured_seconds=measured, probe=probe,
                                   subtitles_resource_id=resource.subtitles_resource_id,
                                   measurement_issues=issues))
    return dict(schema_version=1, operation="read_only_package_audit", element_count=len(package.elements),
                declared_files_verified=len(files), recordings=recordings,
                state="no_declared_audio" if not recordings else "review_required",
                limits="No provider activation, production, import, publication or listening acceptance is performed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = external_file(args.manifest)
    output = Path(args.output).resolve()
    if output.is_relative_to(WORKSPACE) or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new external output file")
    report = audit(manifest)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(state=report["state"], recordings=len(report["recordings"]),
                         declared_files_verified=report["declared_files_verified"])))


if __name__ == "__main__":
    main()
