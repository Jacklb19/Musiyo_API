"""The guide answers courtesy itself and offers current records instead of a technical refusal."""
from contextlib import contextmanager

import pytest

from app import guide_service
from app.guide_contracts import GuideCitation, GuideQuestion, GuideResponse
from app.guide_conversation import split_courtesy, suggestions, topics_text
from app.guide_service import GuideService

TITLES = {"matachin": "El Matachín", "matachin-mask": "Máscara del Matachín", "flauta": "La flauta",
          "sanjuanes": "Los Sanjuanes", "creditos-del-museo": "Créditos y fuentes del museo", "bombo": "El bombo"}


@pytest.mark.parametrize("question, greeted, core", [
    ("Hola, ¿qué tal? ¿Cómo estás? ¿Podrías contar acerca del museo?", True, "Podrías contar acerca del museo?"),
    ("hola, me podrías explicar de qué trata el museo", True, "me podrías explicar de qué trata el museo?"),
    ("Buenas tardes. ¿Qué es el bombo?", True, "Qué es el bombo?"),
    ("¿Qué es el bombo? Gracias", False, "Qué es el bombo?"),
    ("Hola, ¿cómo estás?", True, ""),
    ("Muchas gracias", False, ""),
    ("¿Quién es el Matachín?", False, "Quién es el Matachín?"),
])
def test_courtesy_is_split_from_the_question(question, greeted, core):
    assert split_courtesy(question) == (greeted, core)


def test_suggestions_leave_out_pieces_and_credits_and_read_naturally():
    slugs = suggestions(TITLES, "seed")
    assert len(slugs) == 3 and len(set(slugs)) == 3
    assert not {"matachin-mask", "creditos-del-museo"} & set(slugs)
    assert topics_text(["matachin", "flauta", "bombo"], TITLES) == "el Matachín, la flauta o el bombo"


def service(monkeypatch, answer: GuideResponse):
    instance = GuideService.__new__(GuideService)
    asked = []

    def ask_with_evidence(question):
        asked.append(question.question)
        return answer, []

    @contextmanager
    def factory():
        yield None

    instance.ask_with_evidence = ask_with_evidence
    instance.factory = factory
    monkeypatch.setattr(guide_service, "current_titles", lambda db: TITLES)
    return instance, asked


def test_greeting_alone_is_answered_without_a_provider(monkeypatch):
    instance, asked = service(monkeypatch, GuideResponse(schema_version=1, status="provider_unavailable", answer=None,
                                                         citations=[], reason="provider_unavailable"))
    response, _ = instance.converse(GuideQuestion(schema_version=1, question="Hola, ¿cómo estás?"))
    assert asked == [] and response.status == "answered" and response.answer.startswith("¡Hola! Soy tu guía")
    assert len(response.citations) == 3


def test_greeting_reaches_the_answer_and_not_the_search(monkeypatch):
    grounded = GuideResponse(schema_version=1, status="answered", answer="El bombo es un tambor.",
                             citations=[GuideCitation(element_slug="bombo", title="El bombo")], reason=None)
    instance, asked = service(monkeypatch, grounded)
    response, _ = instance.converse(GuideQuestion(schema_version=1, question="Hola, ¿qué tal? ¿Qué es el bombo?"))
    assert asked == ["Qué es el bombo?"]
    assert response.answer == "¡Hola! El bombo es un tambor." and response.citations == grounded.citations


def test_unanswerable_question_offers_current_records(monkeypatch):
    instance, _ = service(monkeypatch, GuideResponse(schema_version=1, status="abstained", answer=None, citations=[],
                                                     reason="insufficient_evidence"))
    response, evidence = instance.converse(GuideQuestion(schema_version=1, question="¿Quién ganó el mundial?"))
    assert response.status == "answered" and evidence == []
    assert response.answer.startswith("Eso no lo encuentro en las fichas del museo, y prefiero no inventarlo.")
    assert all(citation.element_slug in TITLES for citation in response.citations)


def test_provider_failure_is_not_hidden(monkeypatch):
    failure = GuideResponse(schema_version=1, status="provider_unavailable", answer=None, citations=[],
                            reason="provider_unavailable")
    instance, _ = service(monkeypatch, failure)
    assert instance.converse(GuideQuestion(schema_version=1, question="¿Qué es el bombo?"))[0] == failure
