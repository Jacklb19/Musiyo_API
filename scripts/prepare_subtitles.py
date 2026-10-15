"""Build a private additive manifest from human-reviewed timing evidence."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from app.content_package import PublicationDecision
from app.resource_additions import ResourceAdditions
from app.timed_text import parse_vtt


def prepare_subtitles(reviews: list[Path], decision_file: Path, database: Path, output: Path):
    if output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Cultural timing outputs must remain outside the repository")
    decision = PublicationDecision.model_validate_json(decision_file.read_text(encoding="utf-8"))
    if not decision.approved_culturally:
        raise ValueError("A reviewed resource decision is required")
    additions, links, copies = [], [], []
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for review_path in reviews:
            review = json.loads(review_path.read_text(encoding="utf-8"))
            if review.get("status") != "TIMING-REVIEWED-PUBLICATION-SEPARATE" or not review.get("cues") or not all(
                cue.get("phrase_identity_reviewed") is True and cue.get("reviewer") and cue.get("reviewed_at") for cue in review["cues"]):
                raise ValueError("Every phrase requires explicit listening and timing review")
            resource_id = review["resource_id"]
            narration = db.execute("SELECT * FROM resources WHERE id=? AND kind='narration'", (resource_id,)).fetchone()
            if narration is None or narration["sha256"] != review["audio_sha256"] or narration["transcription"] != review["exact_transcription"]:
                raise ValueError("The reviewed audio or exact transcript differs from the selected database")
            vtt = review_path.parent / (resource_id + ".reviewed.vtt")
            sha = hashlib.sha256(vtt.read_bytes()).hexdigest()
            if sha != review.get("reviewed_vtt_sha256"):
                raise ValueError("Reviewed WebVTT hash differs")
            cues = parse_vtt(vtt.read_text(encoding="utf-8-sig"))
            if [cue[2] for cue in cues] != [cue["text"] for cue in review["cues"]] or any(
                abs(start - source["start"]) > .0006 or abs(end - source["end"]) > .0006
                for (start, end, _), source in zip(cues, review["cues"], strict=True)):
                raise ValueError("WebVTT differs from the reviewed cue evidence")
            if " ".join(cue[2] for cue in cues).split() != narration["transcription"].split() or cues[-1][1] > narration["duration_seconds"] + .05:
                raise ValueError("WebVTT differs from the transcript or exceeds the audio")
            subtitle_id = resource_id.replace("narration-", "subtitles-", 1)
            relative = Path("files") / (subtitle_id + ".vtt")
            additions.append({"element_slug": narration["element_id"], "resource": {
                "id": subtitle_id, "kind": "subtitles", "mime": "text/vtt", "byte_count": vtt.stat().st_size,
                "credit": narration["credit"], "provenance": "Alineación local revisada por " + ", ".join(sorted({cue["reviewer"] for cue in review["cues"]})) + ". Transcripción y MP3 originales conservados."},
                "file": {"file": relative.as_posix(), "sha256": sha, "profile": "general"}})
            links.append({"narration_id": resource_id, "expected_sha256": narration["sha256"], "subtitles_id": subtitle_id,
                "expected_subtitles_id": narration["subtitles_resource_id"]})
            copies.append((vtt, output.parent / relative))
    package = ResourceAdditions.model_validate_json(json.dumps({"operation_version": 1, "decision": decision.model_dump(mode="json"),
        "additions": additions, "subtitle_links": links}))
    if output.exists():
        raise ValueError("Choose a new output manifest; existing evidence is preserved")
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("Output subtitle file already differs")
        shutil.copyfile(source, target)
    output.write_text(package.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(output), "subtitles": len(additions), "imported": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, action="append", required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_subtitles(args.review, args.decision, args.database, args.output)))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
