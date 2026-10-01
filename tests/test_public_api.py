from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ElementRecord, PointRecord, PointElementRecord, ResourceRecord, RoomRecord
from app.main import create_app


NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def client_with_data(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            ElementRecord(
                id="visible", title="ElementRecord sintético", description="Solo prueba",
                interpretation="Interpretación sintética", sources="Fuente sintética", credits="Autor de prueba",
                restrictions="Uso de prueba", approval_status="approved", approved_at=NOW,
                authorized_from=NOW - timedelta(days=1),
            ),
            ElementRecord(
                id="draft", title="Borrador", description="No publicar",
                interpretation="", approval_status="draft", approved_at=None,
                authorized_from=NOW - timedelta(days=1),
            ),
            ElementRecord(
                id="vencido", title="Vencido", description="No publicar",
                interpretation="", approval_status="approved", approved_at=NOW,
                authorized_from=NOW - timedelta(days=2),
                authorized_until=NOW - timedelta(days=1),
            ),
            ElementRecord(
                id="revoked", title="Revocado", description="No publicar",
                interpretation="", approval_status="approved", approved_at=NOW,
                authorized_from=NOW - timedelta(days=1), revoked_at=NOW,
            ),
            ElementRecord(
                id="sin-autorizacion", title="Sin autorización",
                description="No publicar", interpretation="",
                approval_status="approved", approved_at=NOW,
            ),
            RoomRecord(id="room-prueba", tour_id="recorrido-prueba", order=0),
            PointRecord(id="point-01", room_id="room-prueba", order=0),
            PointRecord(id="point-02", room_id="room-prueba", order=1),
            PointElementRecord(point_id="point-01", element_id="visible", order=0),
            PointElementRecord(point_id="point-01", element_id="draft", order=1),
            PointElementRecord(point_id="point-02", element_id="visible", order=0),
        ])
        db.commit()
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "archivo.txt").write_text("contenido sintético", encoding="utf-8")
    with factory() as db:
        db.add_all([
            ResourceRecord(
                id="autorizado", element_id="visible", name="archivo.txt",
                mime="text/plain", private_path="archivo.txt", approved=True,
                authorized_from=NOW - timedelta(days=1),
            ),
            ResourceRecord(
                id="sin-aprobar", element_id="visible", name="archivo.txt",
                mime="text/plain", private_path="archivo.txt", approved=False,
                authorized_from=NOW - timedelta(days=1),
            ),
            ResourceRecord(
                id="de-draft", element_id="draft", name="archivo.txt",
                mime="text/plain", private_path="archivo.txt", approved=True,
                authorized_from=NOW - timedelta(days=1),
            ),
            ResourceRecord(
                id="fuera", element_id="visible", name="archivo.txt",
                mime="text/plain", private_path="../archivo.txt", approved=True,
                authorized_from=NOW - timedelta(days=1),
            ),
        ])
        db.commit()
    return TestClient(create_app(factory, now=lambda: NOW, private_storage=storage))


def test_publication_fail_closed(tmp_path):
    client = client_with_data(tmp_path)
    listing = client.get("/api/v1/elements")
    assert listing.status_code == 200
    assert [item["slug"] for item in listing.json()] == ["visible"]
    for denied in ("draft", "vencido", "revoked", "sin-autorizacion"):
        assert client.get(f"/api/v1/elements/{denied}").status_code == 404
    assert client.get("/api/v1/elements/visible").json()["title"] == "ElementRecord sintético"


def test_legacy_adapter_preserves_text_and_source_links(tmp_path):
    from app.contracts import Element
    response = client_with_data(tmp_path).get("/api/v1/elements/visible")
    element = Element.model_validate_json(response.content)
    assert element.blocks[0].kind == "interpretation"
    assert element.blocks[0].text == "Interpretación sintética"
    assert element.blocks[0].source_id == element.sources[0].id
    assert element.sources[0].reference == "Fuente sintética"
    assert element.credits[0].name == "Autor de prueba"
    assert element.restrictions[0].description == "Uso de prueba"
    assert element.community is None and element.resources == []


def test_contract_filters_denied_elements_and_preserves_repetition(tmp_path):
    client = client_with_data(tmp_path)
    response = client.get("/api/v1/tours/recorrido-prueba")
    assert response.status_code == 200
    from app.contracts import Tour
    contract = Tour.model_validate_json(response.content)
    assert contract.schema_version == 1
    assert contract.tour.key == "recorrido-prueba"
    assert contract.rooms[0].key == "room-prueba"
    assert [point.key for point in contract.rooms[0].points] == ["point-01", "point-02"]
    assert [[item.slug for item in point.elements] for point in contract.rooms[0].points] == [["visible"], ["visible"]]
    assert contract.guide is None
    assert client.get("/api/v1/tours/inexistente").status_code == 404
    assert client.get("/api/v1/tours/recorrido-prueba?schema_version=2").status_code == 409
    incompatible = client.get("/api/v1/tours/recorrido-prueba?schema_version=2")
    assert incompatible.headers["content-type"] == "application/problem+json"
    assert incompatible.json()["code"] == "schema_incompatible"
    assert client.get("/api/v1/tours/recorrido-prueba?schema_version=invalid").json()["code"] == "validation"


def test_resources_require_both_authorizations_and_safe_storage(tmp_path):
    client = client_with_data(tmp_path)
    base = "/api/v1/elements"
    allowed = client.get(f"{base}/visible/resources/autorizado")
    assert allowed.status_code == 200
    assert allowed.text == "contenido sintético"
    assert allowed.headers["cache-control"] == "private, no-store"
    for element, resource in [
        ("visible", "sin-aprobar"),
        ("draft", "de-draft"),
        ("visible", "fuera"),
        ("visible", "de-draft"),
    ]:
        assert client.get(f"{base}/{element}/resources/{resource}").status_code == 404
