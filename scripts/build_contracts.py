"""Regenerate the versioned public contract from the FastAPI models."""

import json
from pathlib import Path

from app.contracts import (
    CatalogFacet,
    CatalogPage,
    Element,
    PointPresence,
    ResourceAccess,
    ReturnToCatalog,
    SelectionCleared,
    SelectionConfirmed,
    ValidatorSession,
)
from app.main import Tour, app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    write_json(CONTRACTS / "openapi.json", app.openapi())
    write_json(CONTRACTS / "tour.v1.schema.json", Tour.model_json_schema())
    write_json(CONTRACTS / "element.v1.schema.json", Element.model_json_schema())
    write_json(CONTRACTS / "bridge.v1.schema.json", SelectionConfirmed.model_json_schema())
    write_json(CONTRACTS / "bridge-clear.v1.schema.json", SelectionCleared.model_json_schema())
    write_json(CONTRACTS / "bridge-return.v1.schema.json", ReturnToCatalog.model_json_schema())
    write_json(CONTRACTS / "bridge-presence.v1.schema.json", PointPresence.model_json_schema())
    write_json(CONTRACTS / "catalog.v1.schema.json", CatalogPage.model_json_schema())
    write_json(CONTRACTS / "catalog-facet.v1.schema.json", CatalogFacet.model_json_schema())
    write_json(CONTRACTS / "resource-access.v1.schema.json", ResourceAccess.model_json_schema())
    write_json(CONTRACTS / "validator-session.v1.schema.json", ValidatorSession.model_json_schema())


if __name__ == "__main__":
    main()
