"""Generate the private content-package schema separately from public contracts."""
import argparse
import json
from pathlib import Path

from app.content_package import ContentPackage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = Path(__file__).parents[1] / "schemas/content-package.v1.schema.json"
    expected = json.dumps(ContentPackage.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not destination.exists() or destination.read_text(encoding="utf-8") != expected:
            raise SystemExit("Content-package schema differs; regenerate it")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(expected, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
