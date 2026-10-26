"""Textual RAG with current evidence, strict citations and no response cache."""
import threading
import time

from .db import utc_now
from .guide_adapters import (
    Embeddings,
    GeneratedAnswer,
    Generation,
    ProviderUnavailable,
)
from .guide_clarification import (
    ambiguous_records,
    clarification_text,
    current_titles,
    named_record,
)
from .guide_config import GuideSettings
from .guide_contracts import GuideCitation, GuideQuestion, GuideResponse
from .guide_conversation import (
    GREETING_PREFIX,
    REDIRECT,
    WELCOME,
    split_courtesy,
    suggestions,
    topics_text,
)
from .guide_ollama import REQUEST_DEADLINE
from .guide_providers import configured_embeddings, configured_generation
from .guide_repository import Evidence, GuideRepository


class GuideService:
    def __init__(self, factory, settings: GuideSettings | None = None, embeddings: Embeddings | None = None,
                 generation: Generation | None = None, now=utc_now):
        self.factory, self.now = factory, now
        self.model_lock = threading.Lock()
        self.settings = settings or GuideSettings.from_environment()
        self.embeddings = embeddings or configured_embeddings(self.settings)
        self.generation = generation or configured_generation(self.settings)

    def ask(self, question: GuideQuestion) -> GuideResponse:
        return self.ask_with_evidence(question)[0]

    def converse(self, question: GuideQuestion) -> tuple[GuideResponse, list[Evidence]]:
        """ask_with_evidence with manners: courtesy answered by the guide itself, and three current records
        offered when the fichas cannot answer. The grounded answer itself is unchanged."""
        greeted, core = split_courtesy(question.question)
        if not core:
            return self.offer(WELCOME, question.question), []
        response, evidence = self.ask_with_evidence(question.model_copy(update={'question': core}))
        if response.status == 'abstained':
            return self.offer(REDIRECT, core), []
        if response.status == 'answered' and greeted and response.answer:
            response = response.model_copy(update={'answer': GREETING_PREFIX + response.answer})
        return response, evidence

    def offer(self, template: str, seed: str) -> GuideResponse:
        with self.factory() as db:
            titles = current_titles(db)
        slugs = suggestions(titles, seed)
        if not slugs:
            return GuideResponse(schema_version=1, status='abstained', answer=None, citations=[], reason='insufficient_evidence')
        return GuideResponse(schema_version=1, status='answered', answer=template.format(topics=topics_text(slugs, titles)),
                             citations=[GuideCitation(element_slug=slug, title=titles[slug]) for slug in slugs], reason=None)

    def ask_with_evidence(self, question: GuideQuestion):
        evidence: list[Evidence] = []
        local_model = self.settings.embeddings == 'ollama' or self.settings.generation == 'ollama'
        if local_model and not self.model_lock.acquire(blocking=False):
            return GuideResponse(schema_version=1, status='provider_unavailable', answer=None,
                                 citations=[], reason='provider_unavailable'), evidence
        token = REQUEST_DEADLINE.set(time.monotonic() + self.settings.ollama_timeout_seconds)
        try:
            return self._ask(question, evidence), evidence
        finally:
            REQUEST_DEADLINE.reset(token)
            if local_model:
                self.model_lock.release()

    def _ask(self, question: GuideQuestion, used_evidence: list[Evidence]) -> GuideResponse:
        def unavailable():
            return GuideResponse(schema_version=1, status='provider_unavailable', answer=None, citations=[], reason='provider_unavailable')

        def abstain():
            return GuideResponse(schema_version=1, status='abstained', answer=None, citations=[], reason='insufficient_evidence')

        if self.settings.generation == 'disabled':
            return unavailable()
        with self.factory() as db:
            repository = GuideRepository(db, self.now)
            if question.element_slug is None:
                titles = current_titles(db)
                options = ambiguous_records(question.question, titles)
                if options:
                    # Rule-based, no provider: list the current matching records and ask which one.
                    return GuideResponse(schema_version=1, status='answered', answer=clarification_text(options, titles),
                                         citations=[GuideCitation(element_slug=slug, title=titles[slug]) for slug in options],
                                         reason=None)
            # A question that names exactly one ficha is answered from that ficha only.
            scope = question.element_slug or named_record(question.question, titles)
            try:
                evidence = repository.retrieve(question.question, scope, self.embeddings, self.settings)
                if not evidence:
                    return abstain()
                output = self.generation.generate(question.question, evidence)
            except (ProviderUnavailable, TimeoutError, ConnectionError):
                return unavailable()
            if not isinstance(output, GeneratedAnswer) or type(output.sufficient) is not bool:
                return abstain()
            if not output.sufficient:
                return abstain()
            if (not isinstance(output.answer, str) or not output.answer.strip() or len(output.answer) > 2000
                    or len(output.answer.split()) > 120 or not isinstance(output.fragment_ids, tuple)
                    or not output.fragment_ids or any(not isinstance(key, str) for key in output.fragment_ids)):
                return abstain()
            indexed = {item.fragment_id: item for item in evidence}
            if len(set(output.fragment_ids)) != len(output.fragment_ids) or not set(output.fragment_ids) <= indexed.keys():
                return abstain()
            used = [indexed[key] for key in output.fragment_ids]
            if not repository.revalidate(used):
                return abstain()
            used_evidence.extend(used)
            cited = {item.element_slug: item.title for item in used}
            citations = [GuideCitation(element_slug=slug, title=title) for slug, title in cited.items()]
            return GuideResponse(schema_version=1, status='answered',
                                 answer=output.answer, citations=citations, reason=None)
