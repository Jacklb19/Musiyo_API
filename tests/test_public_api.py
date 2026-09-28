from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, Elemento, Punto, PuntoElemento, Recurso, Sala
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
            Elemento(
                id="visible", titulo="Elemento sintético", descripcion="Solo prueba",
                interpretacion="", estado_aprobacion="aprobado", aprobado_en=NOW,
                autorizado_desde=NOW - timedelta(days=1),
            ),
            Elemento(
                id="borrador", titulo="Borrador", descripcion="No publicar",
                interpretacion="", estado_aprobacion="borrador", aprobado_en=None,
                autorizado_desde=NOW - timedelta(days=1),
            ),
            Elemento(
                id="vencido", titulo="Vencido", descripcion="No publicar",
                interpretacion="", estado_aprobacion="aprobado", aprobado_en=NOW,
                autorizado_desde=NOW - timedelta(days=2),
                autorizado_hasta=NOW - timedelta(days=1),
            ),
            Elemento(
                id="revocado", titulo="Revocado", descripcion="No publicar",
                interpretacion="", estado_aprobacion="aprobado", aprobado_en=NOW,
                autorizado_desde=NOW - timedelta(days=1), revocado_en=NOW,
            ),
            Elemento(
                id="sin-autorizacion", titulo="Sin autorización",
                descripcion="No publicar", interpretacion="",
                estado_aprobacion="aprobado", aprobado_en=NOW,
            ),
            Sala(id="sala-prueba", recorrido_id="recorrido-prueba", orden=0),
            Punto(id="punto-01", sala_id="sala-prueba", orden=0),
            Punto(id="punto-02", sala_id="sala-prueba", orden=1),
            PuntoElemento(punto_id="punto-01", elemento_id="visible", orden=0),
            PuntoElemento(punto_id="punto-01", elemento_id="borrador", orden=1),
            PuntoElemento(punto_id="punto-02", elemento_id="visible", orden=0),
        ])
        db.commit()
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "archivo.txt").write_text("contenido sintético", encoding="utf-8")
    with factory() as db:
        db.add_all([
            Recurso(
                id="autorizado", elemento_id="visible", nombre="archivo.txt",
                tipo_mime="text/plain", ruta_privada="archivo.txt", aprobado=True,
                autorizado_desde=NOW - timedelta(days=1),
            ),
            Recurso(
                id="sin-aprobar", elemento_id="visible", nombre="archivo.txt",
                tipo_mime="text/plain", ruta_privada="archivo.txt", aprobado=False,
                autorizado_desde=NOW - timedelta(days=1),
            ),
            Recurso(
                id="de-borrador", elemento_id="borrador", nombre="archivo.txt",
                tipo_mime="text/plain", ruta_privada="archivo.txt", aprobado=True,
                autorizado_desde=NOW - timedelta(days=1),
            ),
            Recurso(
                id="fuera", elemento_id="visible", nombre="archivo.txt",
                tipo_mime="text/plain", ruta_privada="../archivo.txt", aprobado=True,
                autorizado_desde=NOW - timedelta(days=1),
            ),
        ])
        db.commit()
    return TestClient(create_app(factory, now=lambda: NOW, private_storage=storage))


def test_publication_fail_closed(tmp_path):
    client = client_with_data(tmp_path)
    listado = client.get("/api/v1/elementos")
    assert listado.status_code == 200
    assert [item["id"] for item in listado.json()] == ["visible"]
    for denied in ("borrador", "vencido", "revocado", "sin-autorizacion"):
        assert client.get(f"/api/v1/elementos/{denied}").status_code == 404
    assert client.get("/api/v1/elementos/visible").json()["titulo"] == "Elemento sintético"


def test_contract_filters_denied_elements_and_preserves_repetition(tmp_path):
    client = client_with_data(tmp_path)
    response = client.get("/api/v1/recorridos/recorrido-prueba")
    assert response.status_code == 200
    assert response.json() == {
        "schemaVersion": 1,
        "recorridoId": "recorrido-prueba",
        "salas": [{
            "id": "sala-prueba",
            "orden": 0,
            "puntos": [
                {"anclajeId": "punto-01", "elementoIds": ["visible"]},
                {"anclajeId": "punto-02", "elementoIds": ["visible"]},
            ],
        }],
    }
    assert client.get("/api/v1/recorridos/inexistente").status_code == 404


def test_resources_require_both_authorizations_and_safe_storage(tmp_path):
    client = client_with_data(tmp_path)
    base = "/api/v1/elementos"
    allowed = client.get(f"{base}/visible/recursos/autorizado")
    assert allowed.status_code == 200
    assert allowed.text == "contenido sintético"
    assert allowed.headers["cache-control"] == "private, no-store"
    for element, resource in [
        ("visible", "sin-aprobar"),
        ("borrador", "de-borrador"),
        ("visible", "fuera"),
        ("visible", "de-borrador"),
    ]:
        assert client.get(f"{base}/{element}/recursos/{resource}").status_code == 404
