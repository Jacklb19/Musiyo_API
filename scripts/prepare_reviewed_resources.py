"""Materialize a reviewed external candidate without changing any database."""
import argparse
import hashlib
import json
from pathlib import Path

from app.content_package import ContentPackage, PublicationDecision
from app.resource_additions import ResourceAdditions


def prepare_reviewed(draft: Path, approval: Path, output: Path):
    if output.resolve().parent != draft.resolve().parent or output.exists():
        raise ValueError("Choose a new manifest beside the external draft")
    if output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Reviewed cultural packages must remain outside the repository")
    evidence = json.loads(approval.read_text(encoding="utf-8"))
    if evidence.get("draft_sha256") != hashlib.sha256(draft.read_bytes()).hexdigest():
        raise ValueError("Review does not identify this exact draft")
    decision = PublicationDecision.model_validate_json(json.dumps(evidence["decision"]))
    if not decision.approved_culturally:
        raise ValueError("An explicit effective local resource decision is required")
    data = json.loads(draft.read_text(encoding="utf-8"))
    data["decision"] = decision.model_dump(mode="json")
    result: ResourceAdditions | ContentPackage
    if "additions" in data:
        if data.get("subtitle_links") or any(item["resource"]["kind"] != "model_3d" for item in data["additions"]):
            raise ValueError("Use prepare_subtitles for reviewed narration timings")
        reviewed = evidence.get("visually_reviewed_resource_hashes", {})
        for item in data["additions"]:
            if reviewed.get(item["resource"]["id"]) != item["file"]["sha256"]:
                raise ValueError("Each model derivative requires concrete visual review")
            item["file"]["paradata"]["owner_visual_reviewed"] = True
        result = ResourceAdditions.model_validate_json(json.dumps(data))
    else:
        plan = draft.parent / "assignments.json"
        if evidence.get("text_and_assignment_reviewed") is not True or evidence.get("assignments_sha256") != hashlib.sha256(plan.read_bytes()).hexdigest():
            raise ValueError("The exact new text and assignment table require review")
        for item in data["elements"]:
            item["show_in_tour"] = True
            item["allow_assistant"] = False
            for restriction in item["record"].get("restrictions", []):
                if restriction["kind"] == "no_reuse":
                    restriction["description"] = "Reconstrucción para ensayo local del museo. Difusión externa y derechos de la fotografía de referencia sin acreditar. No reutilizar fuera de este alcance."
        result = ContentPackage.model_validate_json(json.dumps(data))
    output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(output), "imported": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_reviewed(args.draft, args.approval, args.output)))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
