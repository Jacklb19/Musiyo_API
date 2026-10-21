"""End-of-visit credits gathered from the currently authorized records (owner decision 21/10/2026).

Fichas keep their reading text clean and cite in short; the full acknowledgement lives here, in
the order a film's closing credits would show it. Nothing is typed by hand except the technology
lines, which describe services the guide may use and the attribution its fallback voice requires.
"""
import re
from typing import Literal

from pydantic import Field

from .contracts import ContractModel, Element

PROJECT_RECORD = "creditos-del-museo"
TECHNOLOGY = [
    ("Respuestas del guía", "Groq (redacción a partir de las fichas autorizadas)"),
    ("Voz del guía", "ElevenLabs"),
    ("Voz de respaldo del guía", "Piper «daniela» (larcanio), entrenada con OpenSLR SLR61, "
                                 "Guevara-Rukoz y otros (2020), licencia CC BY-SA 4.0"),
]


class CreditLine(ContractModel):
    role: str | None = None
    text: str


class CreditSection(ContractModel):
    key: Literal["project", "pieces", "testimonies", "media", "sources", "technology"]
    title: str
    lines: list[CreditLine]


class MuseumCredits(ContractModel):
    schema_version: Literal[1] = 1
    title: str
    sections: list[CreditSection] = Field(default_factory=list)


def _unique(values):
    seen: dict[tuple, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def museum_credits(elements: list[Element]) -> MuseumCredits:
    project = next((element for element in elements if element.slug == PROJECT_RECORD), None)
    records = [element for element in elements if element.slug != PROJECT_RECORD]
    roles: dict[str, list[str]] = {}
    for element in records:
        for credit in element.credits:
            names = roles.setdefault(credit.role or "", [])
            if credit.name not in names and credit.name.strip().lower() != "desconocido":
                names.append(credit.name)
    testimonies = _unique(block.attribution.strip() for element in records for block in element.blocks
                          if block.kind == "testimony" and block.attribution and block.attribution.strip())
    media = _unique(resource.credit.strip() for element in records for resource in element.resources
                    if resource.credit and resource.credit.strip() and resource.kind != "subtitles")
    # The same work may be recorded twice with different punctuation or a page note: show it once.
    sources_by_work: dict[str, str] = {}
    for element in elements:
        for source in element.sources:
            reference = source.reference.strip()
            work = re.sub(r"\W", "", reference.casefold())[:60]
            if work not in sources_by_work or len(reference) < len(sources_by_work[work]):
                sources_by_work[work] = reference
    sources = sorted(sources_by_work.values(), key=str.casefold)
    sections = [
        CreditSection(key="project", title="Musiyo Bëtsknaté",
                      lines=[CreditLine(role=credit.role, text=credit.name) for credit in (project.credits if project else [])]),
        CreditSection(key="pieces", title="Piezas y textos",
                      lines=[CreditLine(role=role or None, text=", ".join(names)) for role, names in roles.items() if names]),
        CreditSection(key="testimonies", title="Testimonios",
                      lines=[CreditLine(text=text) for text in testimonies]),
        CreditSection(key="media", title="Voces, modelos y medios",
                      lines=[CreditLine(text=text) for text in media]),
        CreditSection(key="sources", title="Fuentes", lines=[CreditLine(text=text) for text in sources]),
        CreditSection(key="technology", title="Tecnología del guía",
                      lines=[CreditLine(role=role, text=text) for role, text in TECHNOLOGY]),
    ]
    return MuseumCredits(title="Créditos", sections=[section for section in sections if section.lines])
