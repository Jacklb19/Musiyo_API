"""Explicit read-only guide checks against an external question set and database."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from statistics import median
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
    category: str = Field(default="unspecified", min_length=1, max_length=60, pattern=r"^[a-z0-9_-]+$")

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


class GenerationTrace:
    """Capture actual adapter inputs only for an explicitly selected external review."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.evidence = []
        self.output = None

    def generate(self, question, evidence):
        self.evidence = evidence
        self.output = self.adapter.generate(question, evidence)
        return self.output


def latency_summary(values):
    ordered = sorted(values)
    return dict(samples=len(ordered), median_seconds=round(median(ordered), 4),
                maximum_seconds=round(ordered[-1], 4)) if ordered else None


def evaluate(database: Path, settings: GuideSettings, cases: list[EvaluationCase], *,
             repeats: int = 1, review: list | None = None, allow_generation: bool = False,
             pause_seconds: float = 0) -> dict:
    if not 1 <= repeats <= 5:
        raise ValueError("Select between one and five sequential observations per case")
    if settings.generation != "local" and not (allow_generation and settings.generation in {"ollama", "groq"}):
        raise ValueError("Generative evaluation requires an explicit flag and pinned local configuration")
    # SQLite URI mode=ro prevents creation, import, migration or index writes.
    factory = make_session_factory("sqlite:///" + database.as_uri() + "?mode=ro&uri=true")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    try:
        with factory() as db:
            if not PublicRepository(db).ready():
                raise ValueError("Selected database needs an explicit migration")
        service = GuideService(factory, settings)
        results: list[dict] = []
        trace = GenerationTrace(service.generation)
        if review is not None:
            service.generation = trace
        for case in cases:
            for observation in range(1, repeats + 1):
                if pause_seconds and results:
                    time.sleep(pause_seconds)  # stay under external per-minute token limits
                trace.evidence, trace.output = [], None
                started = time.monotonic()
                response = service.ask(case.request)
                seconds = round(time.monotonic() - started, 4)
                citations = sorted(citation.element_slug for citation in response.citations)
                results.append(dict(id=case.id, category=case.category, observation=observation,
                                    expected_status=case.expected_status, actual_status=response.status,
                                    citations=citations,
                                    expectations_match=response.status == case.expected_status and citations == sorted(case.expected_citations),
                                    seconds=seconds))
                if review is not None:
                    review.append(dict(id=case.id, category=case.category, observation=observation,
                                       request=case.request.model_dump(), response=response.model_dump(),
                                       seconds=seconds,
                                       retrieved_evidence=[dict(fragment_id=item.fragment_id, element_slug=item.element_slug,
                                                                text=item.text, kind=item.kind, text_hash=item.text_hash,
                                                                source_reference=item.source_reference, attribution=item.attribution,
                                                                context=item.context) for item in trace.evidence],
                                       selected_fragment_ids=list(trace.output.fragment_ids) if trace.output else [],
                                       generation_output=trace.output.__dict__ if trace.output else None,
                                       human_review=dict(relevant=None, every_claim_supported=None,
                                                         attribution_preserved=None, appropriate_abstention=None,
                                                         reviewer=None, notes=None)))
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        return dict(schema_version=1, generation=settings.generation, embeddings=service.embeddings.model,
                    database_sha256_before=before, database_sha256_after=after, database_unchanged=before == after,
                    cases=len(cases), observations=len(results), repeats=repeats,
                    latency=dict(first_observed=latency_summary([item["seconds"] for item in results if item["observation"] == 1]),
                                 repeated=latency_summary([item["seconds"] for item in results if item["observation"] > 1]),
                                 note="First observed is not proof of a cold model; record provider residency separately."),
                    generation_model={"ollama": settings.ollama_generation_model, "groq": settings.groq_model}.get(settings.generation, "extractive"),
                    categories={category: dict(observations=sum(item["category"] == category for item in results),
                                               matched=sum(item["category"] == category and item["expectations_match"] for item in results))
                                for category in sorted({case.category for case in cases})},
                    matched=sum(item["expectations_match"] for item in results), results=results,
                    review="Status and citation checks are not a review of factual support or answer relevance.")
    finally:
        factory.kw["bind"].dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--guide-config", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--pause-seconds", type=float, default=0, help="Wait between questions (external provider limits)")
    parser.add_argument("--review-output", help="New external private worksheet containing questions, responses and evidence")
    parser.add_argument("--allow-generative-evaluation", action="store_true",
                        help="Evaluate an explicitly authorized installed pinned local generator; never downloads or selects one")
    arguments = parser.parse_args()
    database, config, cases_path = [external_file(value) for value in (arguments.database, arguments.guide_config, arguments.cases)]
    if config.stat().st_size > 65536:
        raise ValueError("Guide configuration is too large")
    output = Path(arguments.output).resolve()
    if output.is_relative_to(WORKSPACE) or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output file in an existing external directory")
    settings = GuideSettings.model_validate_json(config.read_text(encoding="utf-8-sig"))
    review_path = Path(arguments.review_output).resolve() if arguments.review_output else None
    if review_path and (review_path == output or review_path.is_relative_to(WORKSPACE)
                       or review_path.exists() or not review_path.parent.is_dir()):
        raise ValueError("Choose a separate new external review file")
    review: list | None = [] if review_path else None
    report = evaluate(database, settings, load_cases(cases_path), repeats=arguments.repeats,
                      review=review, allow_generation=arguments.allow_generative_evaluation,
                      pause_seconds=min(max(arguments.pause_seconds, 0), 60))
    if review_path:
        with review_path.open("x", encoding="utf-8") as stream:
            json.dump(dict(schema_version=1, review=review, publication="Private evaluation only; not approval"), stream,
                      ensure_ascii=False, indent=2)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("cases", "matched", "database_unchanged", "review")}))
    return 0 if report["database_unchanged"] and report["observations"] == report["matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
