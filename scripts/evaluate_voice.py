"""Explicit read-only spoken-question checks: local synthesis -> local transcription -> guide.

Synthetic generic speech measures the technical pipeline only. It is not evidence about human
voices, accents, noise, microphones, Quest support or cultural adequacy of the answers.
"""
import argparse
import asyncio
import hashlib
import json
import time
import unicodedata
from pathlib import Path

from app.db import make_session_factory
from app.guide_adapters import ProviderUnavailable
from app.guide_config import GuideSettings
from app.guide_contracts import GuideQuestion
from app.guide_service import GuideService
from app.public_repository import PublicRepository
from app.voice_adapters import (
    Synthesizer,
    Transcriber,
    WhisperCppTranscriber,
    WindowsSapiSynthesizer,
    WindowsSapiTranscriber,
)
from app.voice_config import VoiceSettings
from app.voice_routes import catalogue_vocabulary
from scripts.evaluate_guide import (
    WORKSPACE,
    EvaluationCase,
    external_file,
    latency_summary,
    load_cases,
)


def words(text: str) -> list[str]:
    plain = "".join(char for char in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(char) != "Mn")
    return "".join(char if char.isalnum() else " " for char in plain).split()


def word_accuracy(expected: str, actual: str) -> float:
    reference, hypothesis = words(expected), words(actual)
    previous = list(range(len(hypothesis) + 1))
    for index, word in enumerate(reference, 1):
        current = [index]
        for position, other in enumerate(hypothesis, 1):
            current.append(min(previous[position] + 1, current[-1] + 1, previous[position - 1] + (word != other)))
        previous = current
    return round(max(0.0, 1 - previous[-1] / max(1, len(reference))), 4)


async def spoken(synthesizer: Synthesizer, transcriber: Transcriber, text: str):
    started = time.monotonic()
    audio = await synthesizer.synthesize(text)
    synthesized = time.monotonic()
    try:
        transcript = await transcriber.transcribe(audio)
    except ProviderUnavailable:
        transcript = None
    return transcript, round(synthesized - started, 4), round(time.monotonic() - synthesized, 4)


def evaluate(database: Path, settings: GuideSettings, voice: VoiceSettings, cases: list[EvaluationCase], *,
             synthesizer: Synthesizer | None = None, transcriber: Transcriber | None = None) -> dict:
    if settings.generation != "local":
        raise ValueError("Spoken evaluation uses the accepted local extractive guide")
    if voice.synthesis == "disabled" or voice.transcription == "disabled":
        raise ValueError("Select local synthesis and transcription explicitly")
    synthesizer = synthesizer or WindowsSapiSynthesizer(voice)
    factory = make_session_factory("sqlite:///" + database.as_uri() + "?mode=ro&uri=true")

    async def vocabulary():
        # Same published-title vocabulary the API uses, read from the selected database.
        return catalogue_vocabulary(factory)
    transcriber = transcriber or (WindowsSapiTranscriber(voice, vocabulary) if voice.transcription == "windows_sapi"
                                  else WhisperCppTranscriber(voice))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    try:
        with factory() as db:
            if not PublicRepository(db).ready():
                raise ValueError("Selected database needs an explicit migration")
        service = GuideService(factory, settings)
        results = []
        for case in cases:
            typed = service.ask(case.request)
            transcript, synthesis_seconds, transcription_seconds = asyncio.run(
                spoken(synthesizer, transcriber, case.request.question))
            started = time.monotonic()
            response = service.ask(GuideQuestion(schema_version=1, question=transcript, element_slug=case.request.element_slug)) \
                if transcript and len(transcript.strip()) >= 3 else None
            guide_seconds = round(time.monotonic() - started, 4)
            expected = (case.expected_status, sorted(case.expected_citations))
            citations = sorted(item.element_slug for item in response.citations) if response else []
            results.append(dict(
                id=case.id, category=case.category, expected_status=case.expected_status,
                typed_status=typed.status, typed_matches=(typed.status, sorted(c.element_slug for c in typed.citations)) == expected,
                transcribed=transcript is not None, word_accuracy=word_accuracy(case.request.question, transcript or ""),
                spoken_status=response.status if response else "not_transcribed", spoken_citations=citations,
                spoken_matches=response is not None and (response.status, citations) == expected,
                synthesis_seconds=synthesis_seconds, transcription_seconds=transcription_seconds, guide_seconds=guide_seconds))
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        accuracy = [item["word_accuracy"] for item in results]
        return dict(
            schema_version=1, synthesis=voice.synthesis, transcription=voice.transcription,
            voice=voice.windows_voice, recognizer_culture=voice.windows_recognizer_culture,
            database_sha256_before=before, database_sha256_after=after, database_unchanged=before == after,
            cases=len(cases), typed_matched=sum(item["typed_matches"] for item in results),
            spoken_matched=sum(item["spoken_matches"] for item in results),
            transcribed=sum(item["transcribed"] for item in results),
            mean_word_accuracy=round(sum(accuracy) / len(accuracy), 4) if accuracy else None,
            latency=dict(synthesis=latency_summary([item["synthesis_seconds"] for item in results]),
                         transcription=latency_summary([item["transcription_seconds"] for item in results]),
                         guide=latency_summary([item["guide_seconds"] for item in results])),
            categories={category: dict(cases=sum(item["category"] == category for item in results),
                                       typed_matched=sum(item["category"] == category and item["typed_matches"] for item in results),
                                       spoken_matched=sum(item["category"] == category and item["spoken_matches"] for item in results))
                        for category in sorted({case.category for case in cases})},
            results=results,
            review=("Synthetic generic speech; technical match only. Human voices, accents, noise, microphones, "
                    "Quest capture and cultural validation remain separate checks."))
    finally:
        factory.kw["bind"].dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--database", "--guide-config", "--voice-config", "--cases", "--output"):
        parser.add_argument(name, required=True)
    arguments = parser.parse_args()
    database, guide, voice, cases_path = [external_file(value) for value in (
        arguments.database, arguments.guide_config, arguments.voice_config, arguments.cases)]
    output = Path(arguments.output).resolve()
    if output.is_relative_to(WORKSPACE) or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output file in an existing external directory")
    for path in (guide, voice):
        if path.stat().st_size > 65536:
            raise ValueError("Configuration is too large")
    report = evaluate(database, GuideSettings.model_validate_json(guide.read_text(encoding="utf-8-sig")),
                      VoiceSettings.model_validate_json(voice.read_text(encoding="utf-8-sig")), load_cases(cases_path))
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("cases", "typed_matched", "spoken_matched", "mean_word_accuracy", "database_unchanged")}))
    return 0 if report["database_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
