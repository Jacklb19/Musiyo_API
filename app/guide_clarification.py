"""Ask which record is meant when a question names a term shared by several fichas (no AI).

"¿Qué es la máscara?" matches four mask titles and names none of them completely, so the guide
lists the current, authorized titles instead of silently answering about one. The list comes
from the catalogue itself, so new or revoked records change it automatically.
"""
import sqlalchemy as sa

from .content_tables import CONTENT
from .guide_adapters import terms
from .guide_relevance import inflections_match, question_terms
from .public_repository import ASSISTANT_ELEMENTS

MAXIMUM_OPTIONS = 6


def current_titles(db) -> dict[str, str]:
    details = CONTENT["element_details"]
    rows = db.execute(sa.select(ASSISTANT_ELEMENTS.c.id, sa.func.coalesce(details.c.title, ASSISTANT_ELEMENTS.c.title))
                      .outerjoin(details, details.c.element_id == ASSISTANT_ELEMENTS.c.id)).all()
    return {row[0]: row[1] for row in rows}


def matches(word: str, title_terms: set[str]) -> bool:
    return any(inflections_match(word, candidate) for candidate in title_terms)


def ambiguous_records(question: str, titles: dict[str, str]) -> list[str]:
    """Slugs to offer, or [] when the question names one record or is not about a shared term."""
    asked = question_terms(question)
    if not asked:
        return []
    title_terms = {slug: terms(title) for slug, title in titles.items()}
    # A record whose whole title is named ("Máscara del Matachín", "El bombo") is not ambiguous.
    if any(words and all(matches(word, asked) for word in words) for words in title_terms.values()):
        return []
    candidates: set[str] | None = None
    for word in asked:
        found = {slug for slug, words in title_terms.items() if matches(word, words)}
        if found:
            candidates = found if candidates is None else candidates & found
    if not candidates or not 2 <= len(candidates) <= MAXIMUM_OPTIONS:
        return []
    return sorted(candidates, key=lambda slug: titles[slug])


def clarification_text(slugs: list[str], titles: dict[str, str]) -> str:
    names = [titles[slug] for slug in slugs]
    listed = "; ".join(names[:-1]) + " y " + names[-1]
    return f"Hay {len(names)} fichas que coinciden con tu pregunta: {listed}. ¿Sobre cuál quieres saber?"
