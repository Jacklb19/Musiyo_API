from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.content_tables import CONTENT
from app.contracts import CatalogFacet, CatalogPage
from app.db import ElementRecord, make_session_factory
from app.main import create_app
from tests.test_public_api import NOW, client_with_data


def catalog_fixture(tmp_path):
    client = client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["categories"].insert(), [
            {"id": "cat-a", "slug": "test-objects", "name": "Objetos de prueba"},
            {"id": "cat-b", "slug": "test-hidden", "name": "Categoría privada"}])
        db.execute(CONTENT["collections"].insert(), [
            {"id": "col-a", "slug": "test-collection", "name": "Colección de prueba"},
            {"id": "col-b", "slug": "test-private", "name": "Colección privada"}])
        for slug in ("visible", "draft"):
            db.execute(CONTENT["element_metadata"].insert().values(element_id=slug,
                category_id="cat-a" if slug == "visible" else "cat-b", technique="Técnica sintética",
                materials=["Material de prueba"], highlighted=True))
            db.execute(CONTENT["element_details"].insert().values(element_id=slug,
                title="[PRUEBA] Kamëntsá", description="Descripción sintética"))
            db.execute(CONTENT["collection_elements"].insert().values(element_id=slug, collection_id="col-a", order=0))
        db.execute(CONTENT["collection_elements"].insert().values(element_id="draft", collection_id="col-b", order=0))
        db.execute(CONTENT["detail_blocks"].insert().values(id="catalog-test-block", element_id="visible",
            kind="interpretation", text="Contenido sintético: interpretación", context="Prueba técnica", order=0))
    return client, factory


@pytest.mark.parametrize("query", ["Kamëntsá", "kamentsa", "KAMENTSA", "kamentza", "interpretacion",
    "descripción", "Tecnica sintetica", "material", "coleccion", "objetos"])
def test_search_folds_accents_and_indexes_metadata_and_blocks(tmp_path, query):
    client, _ = catalog_fixture(tmp_path)
    response = client.get("/api/v1/catalog", params={"query": query})
    assert response.status_code == 200
    contract = CatalogPage.model_validate_json(response.content)
    Draft202012Validator(CatalogPage.model_json_schema()).validate(response.json())
    assert [item.slug for item in contract.items] == ["visible"]
    assert contract.total == 1


def test_filters_combine_with_query_and_unknown_terms_are_empty(tmp_path):
    client, _ = catalog_fixture(tmp_path)
    filters = {"query": "kamentsa", "category": "test-objects", "collection": "test-collection", "highlighted": "true"}
    assert client.get("/api/v1/catalog", params=filters).json()["total"] == 1
    for change in ({"category": "test-hidden"}, {"collection": "test-private"}, {"category": "unknown"}, {"query": "zzzzzzz"}):
        page = client.get("/api/v1/catalog", params=filters | change).json()
        assert page["items"] == [] and page["total"] == 0


@pytest.mark.parametrize("state", ["no_record", "not_approved", "future", "expired", "revoked"])
def test_counts_and_search_do_not_leak_unavailable_content(tmp_path, state):
    client, factory = catalog_fixture(tmp_path)
    categories = client.get("/api/v1/categories").json()
    assert categories == [{"slug": "test-objects", "name": "Objetos de prueba", "element_count": 1}]
    assert client.get("/api/v1/collections").json() == [{"slug": "test-collection", "name": "Colección de prueba", "element_count": 1}]
    CatalogFacet.model_validate(categories[0])
    values = {"not_approved": {"approved_culturally": False}, "future": {"authorized_from": NOW + timedelta(seconds=1)},
        "expired": {"authorized_until": NOW}, "revoked": {"revoked_at": NOW}}
    with factory.begin() as db:
        table = CONTENT["authorizations"]
        statement = table.delete() if state == "no_record" else table.update().values(**values[state])
        db.execute(statement.where(table.c.element_id == "visible"))
    assert client.get("/api/v1/catalog", params={"query": "kamentsa"}).json()["items"] == []
    assert client.get("/api/v1/categories").json() == client.get("/api/v1/collections").json() == []


def test_triggers_refresh_title_blocks_and_term_names_immediately(tmp_path):
    client, factory = catalog_fixture(tmp_path)
    with factory.begin() as db:
        db.execute(CONTENT["element_details"].update().where(CONTENT["element_details"].c.element_id == "visible")
            .values(title="[PRUEBA] Renombrado", description="Nueva descripción"))
        db.execute(CONTENT["detail_blocks"].delete().where(CONTENT["detail_blocks"].c.element_id == "visible"))
        db.execute(CONTENT["categories"].update().where(CONTENT["categories"].c.id == "cat-a").values(name="Ensayo único"))
        db.execute(CONTENT["collections"].update().where(CONTENT["collections"].c.id == "col-a").values(name="Ensamble"))
    for query in ("renombrado", "ensayo", "ensamble"):
        assert client.get("/api/v1/catalog", params={"query": query}).json()["total"] == 1
    for query in ("kamentsa", "interpretacion", "objetos", "coleccion"):
        assert client.get("/api/v1/catalog", params={"query": query}).json()["total"] == 0
    with factory.begin() as db:
        db.execute(CONTENT["collection_elements"].delete())
    assert client.get("/api/v1/catalog", params={"query": "ensamble"}).json()["total"] == 0


def test_stable_pagination_and_title_ranking(tmp_path):
    client, factory = catalog_fixture(tmp_path)
    with factory.begin() as db:
        for index in range(26):
            slug = f"page-{index:02}"
            db.add(ElementRecord(id=slug, title="Synthetic page", description="kamentsa"))
            db.flush()
            db.execute(CONTENT["authorizations"].insert().values(element_id=slug, approved_culturally=True, authorized_from=NOW))
    first = client.get("/api/v1/catalog", params={"limit": 24}).json()
    second = client.get("/api/v1/catalog", params={"limit": 24, "offset": 24}).json()
    assert first["total"] == second["total"] == 27 and len(first["items"]) == 24 and len(second["items"]) == 3
    assert len({item["slug"] for item in first["items"] + second["items"]}) == 27
    assert client.get("/api/v1/catalog", params={"query": "kamentsa"}).json()["items"][0]["slug"] == "visible"
    assert client.get("/api/v1/catalog", params={"offset": 100}).json()["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"query": "x" * 201}, {"category": ""}])
def test_invalid_catalog_parameters_use_problem_contract(tmp_path, params):
    client, _ = catalog_fixture(tmp_path)
    response = client.get("/api/v1/catalog", params=params)
    assert response.status_code == 422 and response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "validation"
    assert client.get("/api/v1/catalog?schema_version=2").status_code == 409


def test_catalog_uses_a_bounded_number_of_queries(tmp_path):
    _, factory = catalog_fixture(tmp_path)
    client = TestClient(create_app(factory, now=lambda: NOW))
    statements = []
    sa.event.listen(factory.kw["bind"], "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    assert client.get("/api/v1/catalog").status_code == 200
    assert len(statements) == 3
