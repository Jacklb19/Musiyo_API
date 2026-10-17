"""Reviewed external quiz bundles with current publication and evidence checks."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import Field, model_validator

from .contracts import ContractModel, Identifier
from .db import utc_now
from .guide_repository import record_evidence
from .public_repository import ASSISTANT_ELEMENTS, PublicRepository
from .quiz_contracts import (
    GuideQuiz,
    GuideQuizAnswer,
    GuideQuizAnswerResult,
    QuizCitation,
    QuizQuestion,
)


class QuizDraft(ContractModel):
    question: QuizQuestion
    state: Literal['draft', 'reviewed'] = 'draft'
    option_elements: dict[str, Identifier]
    correct_option_id: Identifier
    explanation: str = Field(min_length=1, max_length=1200)
    evidence_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    reviewed_by: str | None = None
    reviewed_at: str | None = None

    @model_validator(mode='after')
    def validate_review(self):
        if set(self.option_elements) != {option.id for option in self.question.options}:
            raise ValueError('Each option needs a current record')
        if self.option_elements.get(self.correct_option_id) != self.question.element_slug:
            raise ValueError('Correct option must identify the cited record')
        if self.correct_option_id not in {option.id for option in self.question.options}:
            raise ValueError('Correct option must exist')
        if self.state == 'reviewed':
            if not self.reviewed_by or not self.reviewed_by.strip() or not self.reviewed_at:
                raise ValueError('Human review requires a reviewer and timestamp')
            instant = datetime.fromisoformat(self.reviewed_at)
            if instant.tzinfo is None or instant > datetime.now(timezone.utc):
                raise ValueError('Review timestamp must be timezone-aware and not in the future')
        return self


class QuizBundle(ContractModel):
    schema_version: Literal[1]
    tour_key: Identifier
    questions: list[QuizDraft] = Field(max_length=50)

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [item.question.id for item in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError('Repeated question id')
        return self


class QuizService:
    def __init__(self, factory, bundle_path: Path | None = None, now=utc_now):
        self.factory, self.bundle_path, self.now = factory, bundle_path, now

    def bundle(self):
        if self.bundle_path is None:
            return None
        # Read on every request; replacing a reviewed bundle never leaves a stale cache.
        return QuizBundle.model_validate_json(self.bundle_path.read_text(encoding='utf-8'))

    def current(self, db, draft, tour_key, room_key=None):
        public = PublicRepository(db, self.now)
        tour = public.tour(tour_key)
        if tour is None:
            return None
        slugs = {element.slug for room in tour.rooms if room_key is None or room.key == room_key
                 for point in room.points for element in point.elements}
        question = draft.question
        if question.element_slug not in slugs or draft.state != 'reviewed':
            return None
        if question.element_slug not in set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id))):
            return None
        allowed = set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)))
        for option in question.options:
            slug = draft.option_elements[option.id]
            candidate = public.element(slug) if slug in allowed else None
            if candidate is None or candidate.title != option.text:
                return None
        record = public.element(question.element_slug)
        if record is None:
            return None
        evidence = next((item for item in record_evidence(record)
                         if item.kind == 'documented_fact' and item.source_id == question.source_id
                         and item.source_reference and item.text_hash == draft.evidence_hash), None)
        source = next((item for item in record.sources if item.id == question.source_id), None)
        return (evidence, source) if evidence and source else None

    def list(self, tour_key, room_key=None):
        bundle = self.bundle()
        questions = []
        if bundle and bundle.tour_key == tour_key:
            with self.factory() as db:
                questions = [item.question for item in bundle.questions if self.current(db, item, tour_key, room_key)]
        return GuideQuiz(schema_version=1, tour_key=tour_key, room_key=room_key, questions=questions)

    def answer(self, answer: GuideQuizAnswer):
        bundle = self.bundle()
        if bundle is None:
            return None
        draft = next((item for item in bundle.questions if item.question.id == answer.id), None)
        if draft is None:
            return None
        with self.factory() as db:
            current = self.current(db, draft, bundle.tour_key)
            if current is None:
                return None
            if answer.option_id not in {item.id for item in draft.question.options}:
                raise ValueError('Unknown option')
            evidence, source = current
            return GuideQuizAnswerResult(schema_version=1, id=answer.id,
                correct=answer.option_id == draft.correct_option_id, explanation=draft.explanation,
                citation=QuizCitation(element_slug=evidence.element_slug, source_id=evidence.source_id,
                                      quote=evidence.text), source=source)


def generate_drafts(factory, tour_key, now=utc_now):
    """Extract recognition questions without generating cultural claims or answers."""
    result = []
    with factory() as db:
        public = PublicRepository(db, now)
        tour = public.tour(tour_key)
        if tour is None:
            raise ValueError('Unknown published tour')
        slugs = {item.slug for room in tour.rooms for point in room.points for item in point.elements}
        allowed = set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id))) & slugs
        candidates = [public.element(slug) for slug in sorted(allowed)]
        records = [item for item in candidates if item is not None]
        for record in records:
            others = [item for item in records if item.title != record.title]
            distinct = {item.title: item for item in others}
            if len(distinct) < 2:
                continue
            for evidence in record_evidence(record):
                if evidence.kind != 'documented_fact' or not evidence.source_id or not evidence.source_reference:
                    continue
                from .quiz_contracts import QuizOption
                selected = [record, *list(distinct.values())[:3]]
                offset = int(evidence.text_hash[:2], 16) % len(selected)
                selected = selected[offset:] + selected[:offset]
                options = [QuizOption(id=chr(97 + index), text=item.title) for index, item in enumerate(selected)]
                option_elements = {option.id: item.slug for option, item in zip(options, selected, strict=True)}
                correct_id = next(key for key, slug in option_elements.items() if slug == record.slug)
                question = QuizQuestion(id='quiz-' + evidence.text_hash[:24],
                    text=f'¿Qué ficha contiene este fragmento documentado? «{evidence.text}»',
                    options=options, element_slug=record.slug, source_id=evidence.source_id)
                result.append(QuizDraft(question=question, correct_option_id=correct_id, option_elements=option_elements,
                    explanation=evidence.text, evidence_hash=evidence.text_hash))
                if len(result) >= 50:
                    break
            if len(result) >= 50:
                break
    return QuizBundle(schema_version=1, tour_key=tour_key, questions=result)


def write_new_bundle(path, bundle):
    with path.open('x', encoding='utf-8', newline='\n') as output:
        json.dump(bundle.model_dump(), output, ensure_ascii=False, indent=2)
        output.write('\n')
