"""Conservative question matching, independent of stored embedding fingerprints."""
import re

from .catalog_search import fold
from .guide_adapters import STOP_WORDS, terms

# These grammatical forms belong to question parsing, never to indexed vectors.
QUESTION_FUNCTION_WORDS = STOP_WORDS | frozenset("son quien quienes cual cuales hace hacen tiene tienen cumple cumplen".split())


def inflections_match(left: str, right: str) -> bool:
    """Match exact tokens or limited regular Spanish number inflections."""
    if left == right:
        return True
    singular, plural = sorted((left, right), key=len)
    if len(singular) < 4:
        return False
    if singular[-1] in "ae" and plural == singular + "n":
        return True
    if singular[-1] in "aeiou":
        return plural == singular + "s"
    if singular.endswith("z"):
        return plural == singular[:-1] + "ces"
    return plural == singular + "es"


def question_terms(question: str) -> set[str]:
    return {word for word in terms(question)
            if not any(inflections_match(word, function) for function in QUESTION_FUNCTION_WORDS)}


def matching_terms(query: set[str], text: str) -> set[str]:
    available = terms(text)
    return {word for word in query if any(inflections_match(word, candidate) for candidate in available)}


def coverage(query: set[str], text: str) -> float:
    return len(matching_terms(query, text)) / len(query) if query else 0.0


def is_identity_question(question: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", fold(question)).strip()
    return bool(re.match(r"^(?:que (?:es|son)|quien es|quienes son)\b", normalized))
