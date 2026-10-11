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
from .guide_config import GuideSettings
from .guide_contracts import GuideCitation, GuideQuestion, GuideResponse
from .guide_ollama import REQUEST_DEADLINE
from .guide_providers import configured_embeddings, configured_generation
from .guide_repository import GuideRepository


class GuideService:
    def __init__(self, factory, settings: GuideSettings | None = None, embeddings: Embeddings | None = None,
                 generation: Generation | None = None, now=utc_now):
        self.factory, self.now = factory, now
        self.model_lock = threading.Lock()
        self.settings = settings or GuideSettings.from_environment()
        self.embeddings = embeddings or configured_embeddings(self.settings)
        self.generation = generation or configured_generation(self.settings)

    def ask(self, question: GuideQuestion) -> GuideResponse:
        local_model = self.settings.embeddings == 'ollama' or self.settings.generation == 'ollama'
        if local_model and not self.model_lock.acquire(blocking=False):
            return GuideResponse(schema_version=1, status='provider_unavailable', answer=None,
                                 citations=[], reason='provider_unavailable')
        token = REQUEST_DEADLINE.set(time.monotonic() + self.settings.ollama_timeout_seconds)
        try:
            return self._ask(question)
        finally:
            REQUEST_DEADLINE.reset(token)
            if local_model:
                self.model_lock.release()

    def _ask(self, question: GuideQuestion) -> GuideResponse:
        def unavailable():
            return GuideResponse(schema_version=1, status='provider_unavailable', answer=None, citations=[], reason='provider_unavailable')

        def abstain():
            return GuideResponse(schema_version=1, status='abstained', answer=None, citations=[], reason='insufficient_evidence')

        if self.settings.generation == 'disabled':
            return unavailable()
        with self.factory() as db:
            repository = GuideRepository(db, self.now)
            try:
                evidence = repository.retrieve(question.question, question.element_slug, self.embeddings, self.settings)
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
            cited = {item.element_slug: item.title for item in used}
            citations = [GuideCitation(element_slug=slug, title=title) for slug, title in cited.items()]
            return GuideResponse(schema_version=1, status='answered',
                                 answer=output.answer, citations=citations, reason=None)
