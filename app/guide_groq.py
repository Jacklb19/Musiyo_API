"""Groq chat generation over retrieved, currently authorized evidence (owner decision 2026-10-20).

Only the question and the retrieved fragments are sent. The answer must cite supplied fragment
ids under a strict JSON schema; the service still re-validates citations and authorization.
"""
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .guide_adapters import (
    GeneratedAnswer,
    Generation,
    ProviderUnavailable,
    evidence_notes,
)
from .guide_config import GuideSettings
from .guide_hedges import lost_qualifications
from .guide_ollama import SYSTEM_INSTRUCTIONS, ModelAnswer
from .remote_http import post_json, read_secret

if TYPE_CHECKING:
    from .guide_repository import Evidence

CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
REPAIR = ("Tu respuesta usa estas frases de la evidencia pero omite su matiz o atribución. Reescríbela en una o dos oraciones conservando explícitamente esas palabras de matiz o atribución (por ejemplo «una de las versiones», «según», «algunos», «descrita como», «tradicionalmente», «buena parte», «denominación institucional», «puede») o deja fuera esas afirmaciones. Devuelve el mismo JSON. Frases: ")


class GroqGeneration:
    def __init__(self, settings: GuideSettings, post=post_json):
        self.settings = settings
        self.post = post

    def generate(self, question: str, evidence: list["Evidence"]) -> GeneratedAnswer:
        if not evidence:
            return GeneratedAnswer(False, "", ())
        references = {f"E{index + 1}": item.fragment_id for index, item in enumerate(evidence)}
        context = [{"fragment_id": reference, "title": item.title, "kind": item.kind,
                    "community": item.community, "text": item.text, "attribution": item.attribution,
                    "context": item.context, "source_reference": item.source_reference}
                   for reference, item in zip(references, evidence, strict=True)]
        schema = ModelAnswer.model_json_schema()
        schema["properties"]["fragment_ids"]["items"] = {"type": "string", "enum": list(references)}
        schema.pop("title", None)
        messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {"role": "user", "content": json.dumps({"question": question, "evidence": context}, ensure_ascii=False)}]
        output, content = self.request(messages, schema)
        lost = self.lost(question, output, references, evidence)
        if lost:
            # One targeted repair: name the qualified sentences the answer must keep.
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": REPAIR + json.dumps(lost, ensure_ascii=False)}]
            output, _ = self.request(messages, schema)
            if self.lost(question, output, references, evidence):
                # Never publish a claim stripped of its qualifier; the fallback quotes the ficha.
                raise ProviderUnavailable("Generated answer drops a recorded qualification")
        if any(reference not in references for reference in output.fragment_ids):
            return GeneratedAnswer(False, "", ())
        fragment_ids = tuple(references[reference] for reference in output.fragment_ids)
        used = [item for item in evidence if item.fragment_id in fragment_ids]
        notes = evidence_notes(used) if output.sufficient else ""
        return GeneratedAnswer(output.sufficient, output.answer + ("\n\n" + notes if notes else ""), fragment_ids)

    def request(self, messages: list[dict], schema: dict) -> tuple[ModelAnswer, str]:
        result = self.post(CHAT_URL, {
            "model": self.settings.groq_model, "temperature": 0,
            "max_completion_tokens": self.settings.groq_output_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "guide_answer", "strict": True, "schema": schema}},
            "messages": messages},
            {"Authorization": "Bearer " + read_secret(self.settings.groq_api_key_file)},
            self.settings.groq_timeout_seconds, 200_000)
        try:
            content = result["choices"][0]["message"]["content"]
            return ModelAnswer.model_validate_json(content), content
        except (KeyError, IndexError, TypeError, ValidationError) as error:
            raise ProviderUnavailable("External generation violates its schema") from error

    @staticmethod
    def lost(question: str, output: ModelAnswer, references: dict[str, str], evidence: list["Evidence"]) -> list[str]:
        if not output.sufficient:
            return []
        chosen = {references.get(reference) for reference in output.fragment_ids}
        return lost_qualifications(question, output.answer, [item for item in evidence if item.fragment_id in chosen])


class FallbackGeneration:
    """Use the local extractive guide only when the external provider is unavailable.

    A provider's own 'insufficient evidence' decision is respected and never overridden.
    """

    def __init__(self, primary: Generation, fallback: Generation):
        self.primary, self.fallback = primary, fallback

    def generate(self, question: str, evidence: list["Evidence"]) -> GeneratedAnswer:
        try:
            return self.primary.generate(question, evidence)
        except (ProviderUnavailable, TimeoutError, ConnectionError):
            return self.fallback.generate(question, evidence)
