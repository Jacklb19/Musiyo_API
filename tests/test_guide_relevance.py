import json

import pytest
import sqlalchemy as sa
from test_guide import guide as guide
from test_guide import question

from app.content_operations import import_package, revoke
from app.content_tables import CONTENT
from app.guide_adapters import LocalHashEmbeddings, terms
from app.guide_relevance import (
    coverage,
    inflections_match,
    is_identity_question,
    question_terms,
)


@pytest.mark.parametrize("singular,plural", [("instrumento", "instrumentos"), ("personaje", "personajes"), ("motor", "motores"), ("actriz", "actrices"), ("produce", "producen")])
def test_regular_plural_matching_is_symmetric(singular, plural):
    assert inflections_match(singular, plural)
    assert inflections_match(plural, singular)
    assert not inflections_match(singular, plural + "extra")


def test_question_grammar_and_accents_do_not_change_indexed_tokens_or_vectors():
    assert question_terms("¿Quiénes son los personajes?") == {"personajes"}
    assert question_terms("¿Qué hace el personaje?") == {"personaje"}
    assert is_identity_question("¿Quiénes son los personajes?")
    assert not is_identity_question("¿Dónde están los personajes?")
    assert "quienes" in terms("¿Quiénes son los personajes?")
    adapter = LocalHashEmbeddings()
    assert adapter.model == "local-hash-v1"
    assert adapter.embed(["personaje"]) != adapter.embed(["personajes"])
    assert coverage({"personajes", "marte"}, "El personaje sintético") == 0.5


def test_plural_questions_reuse_existing_vectors_and_prefer_identity_summary(guide, tmp_path):
    _, factory, service, manifest, value = guide
    record = value['elements'][0]['record']
    record.update(title="El personaje sintético", description="El personaje sintético es una figura de prueba sin contenido cultural.")
    record['blocks'][0]['text'] = "El personaje sintético participa en una prueba de sonido."
    manifest.write_text(json.dumps(value), encoding='utf-8')
    # Explicit synthetic fixture import only, never an existing preview database.
    import_package(manifest, guide[0], private_storage=tmp_path / 'private')
    with factory() as db:
        before = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
    for text in ["¿Quién es el personaje?", "¿Quiénes son los personajes?", "¿Qué es el personaje?"]:
        result = service.ask(question(text))
        assert result.status == 'answered'
        assert "figura de prueba" in result.answer
        assert [citation.element_slug for citation in result.citations] == ['synthetic-import']
    with factory() as db:
        after = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
    assert before == after
    revoke('synthetic-import', database_url=guide[0], reason='Synthetic revocation')
    assert service.ask(question("¿Quiénes son los personajes?", 'synthetic-import')).status == 'abstained'


@pytest.mark.parametrize("text", ["¿El instrumento produce sonido en Marte?", "¿Cuál es el precio del instrumento?", "¿Quiénes son los astronautas?", "¿Cuántos años tiene el instrumento?"])
def test_partial_topic_overlap_does_not_answer_unsupported_qualifiers(guide, text):
    service = guide[2]
    result = service.ask(question(text))
    assert result.status == 'abstained'
    assert result.answer is None and result.citations == []


def test_plural_context_never_grants_access_to_an_unknown_record(guide):
    assert guide[2].ask(question("¿Qué instrumentos producen sonido?", "unknown")).status == 'abstained'


def test_regular_present_number_inflection_supports_a_natural_plural_question(guide):
    result = guide[2].ask(question("¿Qué instrumentos producen sonido?"))
    assert result.status == 'answered'
    assert 'instrumento sintético produce sonido' in result.answer
    assert [citation.element_slug for citation in result.citations] == ['synthetic-import']
