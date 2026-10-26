"""Detect generated answers that reuse a qualified ficha sentence without its qualification.

Fichas often say "algunos intérpretes lo asocian…", "una de las versiones…", "según los
testimonios…" or "denominación institucional". A model can keep the claim and drop the qualifier,
turning one recorded view into a general fact. When that happens the guide quotes the ficha
instead (owner review 2026-10-20 against the museum's source document).
"""
import re
from typing import TYPE_CHECKING

from .catalog_search import fold
from .guide_adapters import terms

if TYPE_CHECKING:
    from .guide_repository import Evidence

# Folded (lowercase, unaccented) qualifiers that situate or attribute a statement.
MARKERS = ("algunos", "algunas", "algun ", "segun", "describe", "descrit", "una de las versiones", "version",
           "puede", "pueden", "podria", "habria", "marcaria", "seria", "tradicionalmente", "buena parte",
           "institucional", "asocia", "interpret", "testimonio", "aproximad", "suele", "suelen", "se cree",
           "se dice", "posiblemente", "probablemente", "distintas fuentes", "relato", "situa", "propone",
           "senala", "registra", "recoge", "en ocasiones")
CHECKED_KINDS = {"documented_fact", "summary"}  # testimony/interpretation already get explicit notes
# A pending status cannot be swapped for another qualifier: «sujetos a validación de la comunidad» must not
# become «validados por la comunidad» just because the answer says «interpretaciones» (found 26/10/2026).
STATUS = ("sujet", "pendient", "por documentar", "por validar", "sin validacion", "aun no", "todavia no")


def markers(text: str) -> set[str]:
    folded = " " + fold(text) + " "
    return {marker for marker in MARKERS if marker in folded}


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.;:])\s+", text) if part.strip()]


def lost_qualifications(question: str, answer: str, used: list["Evidence"]) -> list[str]:
    """Qualified sentences whose content the answer reuses (two or more shared content words
    beyond the question) while the answer carries no qualifier at all."""
    kept, folded = markers(answer), " " + fold(answer) + " "
    asked, said = terms(question), terms(answer)
    lost = []
    for item in used:
        if item.kind not in CHECKED_KINDS:
            continue
        for sentence in sentences(item.text):
            if len((terms(sentence) & said) - asked) < 2:
                continue
            status = {marker for marker in STATUS if marker in " " + fold(sentence) + " "}
            if (status and not any(marker in folded for marker in status)) or (markers(sentence) and not kept):
                lost.append(sentence)
    return lost
