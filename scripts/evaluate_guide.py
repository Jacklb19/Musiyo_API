"""Explicit read-only guide checks against an external question set and database."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import make_session_factory
from app.guide_config import GuideSettings
from app.guide_contracts import GuideQuestion
from app.guide_service import GuideService
from app.public_repository import PublicRepository

WORKSPACE = Path(__file__).resolve().parents[2]


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    request: GuideQuestion
    expected_status: Literal["answered", "abstained", "provider_unavailable"]
    expected_citations: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def citation_expectation(self):
        if (self.expected_status == "answered") != bool(self.expected_citations):
            raise ValueError("Only an expected answer may have citation expectations")
        return self


def external_file(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    if path.is_relative_to(WORKSPACE) or not path.is_file():
        raise ValueError("Select an existing file outside the repository workspace")
    return path


def load_cases(path: Path) -> list[EvaluationCase]:
    if path.stat().st_size > 1_000_000:
        raise ValueError("Question set is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"schema_version", "cases"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Question set needs its evaluation schema version 1")
    if not isinstance(value["cases"], list) or not 1 <= len(value["cases"]) <= 200:
        raise ValueError("Select between one and 200 explicit cases")
    cases = [EvaluationCase.model_validate(case) for case in value["cases"]]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case identifiers must be unique")
    return cases


def evaluate(database: Path, settings: GuideSettings, cases: list[EvaluationCase]) -> dict:
    if settings.generation != "local":
        raise ValueError("This evaluation uses accepted extractive generation only")
    # SQLite URI mode=ro prevents creation, import, migration or index writes.
    factory = make_session_factory("sqlite:///" + database.as_uri() + "?mode=ro&uri=true")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    try:
        with factory() as db:
            if not PublicRepository(db).ready():
                raise ValueError("Selected database needs an explicit migration")
        service = GuideService(factory, settings)
        results = []
        for case in cases:
            started = time.monotonic()
            response = service.ask(case.request)
            citations = sorted(citation.element_slug for citation in response.citations)
            results.append(dict(id=case.id, expected_status=case.expected_status,
                                actual_status=response.status, citations=citations,
                                expectations_match=response.status == case.expected_status and citations == sorted(case.expected_citations),
                                seconds=round(time.monotonic() - started, 4)))
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        return dict(schema_version=1, generation=settings.generation, embeddings=service.embeddings.model,
                    database_sha256_before=before, database_sha256_after=after, database_unchanged=before == after,
                    cases=len(results), matched=sum(item["expectations_match"] for item in results), results=results,
                    review="Status and citation checks are not a review of factual support or answer relevance.")
    finally:
        factory.kw["bind"].dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--guide-config", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    database, config, cases_path = [external_file(value) for value in (arguments.database, arguments.guide_config, arguments.cases)]
    if config.stat().st_size > 65536:
        raise ValueError("Guide configuration is too large")
    output = Path(arguments.output).resolve()
    if output.is_relative_to(WORKSPACE) or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output file in an existing external directory")
    settings = GuideSettings.model_validate_json(config.read_text(encoding="utf-8-sig"))
    report = evaluate(database, settings, load_cases(cases_path))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("cases", "matched", "database_unchanged", "review")}))
    return 0 if report["database_unchanged"] and report["cases"] == report["matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
