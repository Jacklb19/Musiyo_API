"""Copy canonical API contracts to the Web and Unity repositories."""

import hashlib
import shutil
from pathlib import Path
from scripts.generate_unity_contracts import generate


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts"
TARGETS = (
    ROOT.parent / "Musiyo_Web" / "contracts",
    ROOT.parent / "Musiyo_Bëtsknaté" / "Assets" / "Contracts",
)


def main() -> None:
    files = sorted(path for path in SOURCE.rglob("*.json") if path.is_file())
    if not files:
        raise SystemExit("No generated contracts found. Run scripts/build_contracts.py first.")
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(SOURCE).as_posix()}\n"
        for path in files
    )
    for target in TARGETS:
        target.mkdir(parents=True, exist_ok=True)
        for source in files:
            destination = target / source.relative_to(SOURCE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (target / "source.sha256").write_text(manifest, encoding="utf-8", newline="\n")
        print(target)
    destination = TARGETS[1].parent / "Scripts/Museo/PublicContractModelsV1.cs"
    destination.write_text(generate(), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
