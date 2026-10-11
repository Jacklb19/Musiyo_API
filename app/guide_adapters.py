"""Offline adapters. No network provider is installed or selected implicitly."""
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .interface_copy import GUIDE_QUALIFIERS

if TYPE_CHECKING:
    from .guide_repository import Evidence


class ProviderUnavailable(Exception):
    """An adapter cannot complete the operation within its configured availability."""


STOP_WORDS = frozenset("a al algo como cual cuando de del donde el ella en es esta este esto fue hay la las lo los me mi para por que quien se si sin sobre su un una y".split())


def terms(text: str) -> set[str]:
    normalized = ''.join(character for character in unicodedata.normalize('NFD', text.lower())
                         if unicodedata.category(character) != 'Mn')
    return {word for word in re.findall(r"[a-z0-9]+", normalized) if len(word) > 2 and word not in STOP_WORDS}


class Embeddings(Protocol):
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalHashEmbeddings:
    """A deterministic lexical baseline, not a semantic language model."""

    model = "local-hash-v1"
    dimension = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        output = []
        for text in texts:
            vector = [0.0] * self.dimension
            for word in sorted(terms(text)):
                digest = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(digest[:4], 'big') % self.dimension] += 1.0 if digest[4] & 1 else -1.0
            length = math.sqrt(sum(number * number for number in vector))
            output.append([number / length for number in vector] if length else vector)
        return output


@dataclass(frozen=True)
class GeneratedAnswer:
    sufficient: bool
    answer: str
    fragment_ids: tuple[str, ...]


class Generation(Protocol):
    def generate(self, question: str, evidence: list["Evidence"]) -> GeneratedAnswer: ...


class LocalExtractiveGeneration:
    """Quote retrieved text without instructions, new claims or external processing."""

    def generate(self, question: str, evidence: list["Evidence"]) -> GeneratedAnswer:
        if not evidence:
            return GeneratedAnswer(False, "", ())
        item = evidence[0]
        qualifier = GUIDE_QUALIFIERS[item.kind]
        words = item.text.split()
        excerpt = ' '.join(words[:90]) + (' …' if len(words) > 90 else '')
        community = f" ({item.community})" if item.community else ""
        return GeneratedAnswer(True, f'{qualifier}{community}: «{excerpt}»', (item.fragment_id,))


class DisabledGeneration:
    def generate(self, question: str, evidence: list["Evidence"]) -> GeneratedAnswer:
        raise ProviderUnavailable("Generation is disabled")
