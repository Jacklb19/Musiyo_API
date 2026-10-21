from fastapi.testclient import TestClient
from test_guide import guide as guide

from app.contracts import Element
from app.main import create_app
from app.museum_credits import museum_credits


def record(slug, **extra):
    return Element.model_validate({"slug": slug, "title": slug, "description": "Texto.", **extra})


def test_credits_gather_authorized_records_once_and_in_film_order():
    project = record("creditos-del-museo", credits=[{"role": "Autor del prototipo", "name": "Autor"}])
    mask = record("mascara", credits=[{"role": "Pertenencia colectiva", "name": "Cabildo indígena"},
                                      {"role": "Creador de la pieza", "name": "Desconocido"}],
                  sources=[{"id": "q", "kind": "publication", "reference": "Quiñones Triana, Y. (2019). Betsknate, el carnaval del perdón del pueblo kamsá: Interpretaciones sobre el contacto. Maguaré, 33(1), 109-138."}])
    other = record("otra", credits=[{"role": "Pertenencia colectiva", "name": "Cabildo indígena"}],
                   blocks=[{"id": "t", "kind": "testimony", "text": "Relato.", "source_id": "q2", "attribution": "Mayor de la comunidad"}],
                   sources=[{"id": "q2", "kind": "publication", "reference": "Quiñones Triana, Y. (2019). Betsknate, el carnaval del perdón del pueblo kamsá. Interpretaciones sobre el contacto. Maguaré, 33(1), 109–138. Pasaje de p. 122."}])
    credits = museum_credits([mask, project, other])
    sections = {section.key: section for section in credits.sections}
    assert [section.key for section in credits.sections] == ["project", "pieces", "testimonies", "sources", "technology"]
    assert [(line.role, line.text) for line in sections["pieces"].lines] == [("Pertenencia colectiva", "Cabildo indígena")]
    assert [line.text for line in sections["testimonies"].lines] == ["Mayor de la comunidad"]
    assert len(sections["sources"].lines) == 1
    assert any("CC BY-SA 4.0" in line.text for line in sections["technology"].lines)


def test_credits_endpoint_follows_the_public_contract(guide):
    client = TestClient(create_app(guide[1]))
    body = client.get("/api/v1/credits").json()
    assert body["schema_version"] == 1 and body["title"] == "Musiyo Bëtsknaté" and body["subtitle"]
    pieces = next(section for section in body["sections"] if section["key"] == "pieces")
    assert {"role": "testing", "text": "Synthetic role"} in pieces["lines"]
    assert client.get("/api/v1/credits", params={"schema_version": 2}).status_code == 409
