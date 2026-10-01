import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.cli import import_test_data
from app.contracts import Tour
from app.db import PointRecord, TourRecord, make_session_factory
from app.main import create_app
from app.migrations import upgrade_database


def test_full_museum_tour_and_unpublished_access(tmp_path):
    url = f"sqlite:///{tmp_path / 'museum.db'}"
    upgrade_database(url)
    assert import_test_data(url, "museum-test-data") == {"rooms": 6, "points": 16}
    assert import_test_data(url, "museum-test-data") == {"rooms": 0, "points": 0}
    factory = make_session_factory(url)
    client = TestClient(create_app(factory))
    response = client.get("/api/v1/tours/museum-main")
    assert response.status_code == 200
    tour = Tour.model_validate_json(response.content)
    assert [room.name for room in tour.rooms] == [
        "Llegada", "Bienvenida", "Personajes", "Instrumentos y Danza", "Fogón", "Mirador"
    ]
    assert sum(len(room.points) for room in tour.rooms) == 16
    assert all(point.elements == [] for room in tour.rooms for point in room.points)
    assert tour.guide.room_key == "room.hearth" and not tour.guide.available
    with factory.begin() as db:
        db.get(TourRecord, "museum-main").published = False
    assert client.get("/api/v1/tours/museum-main").status_code == 404
    assert client.get("/api/v1/tours/unknown").status_code == 404


def test_prototype_and_full_museum_can_coexist(tmp_path):
    url = f"sqlite:///{tmp_path / 'both.db'}"
    upgrade_database(url)
    import_test_data(url)
    import_test_data(url, "museum-test-data")
    factory = make_session_factory(url)
    with factory() as db:
        assert len(db.scalars(select(PointRecord)).all()) == 19
    client = TestClient(create_app(factory))
    assert client.get("/api/v1/tours/recorrido-prueba").status_code == 200
    assert client.get("/api/v1/tours/museum-main?schema_version=2").status_code == 409


@pytest.mark.parametrize("missing", ["name", "room", "unknown_room"])
def test_incomplete_guide_metadata_does_not_break_public_tour(tmp_path, missing):
    url = f"sqlite:///{tmp_path / 'incomplete.db'}"
    upgrade_database(url)
    import_test_data(url, "museum-test-data")
    factory = make_session_factory(url)
    with factory.begin() as db:
        tour = db.get(TourRecord, "museum-main")
        if missing == "name":
            tour.guide_name = None
        else:
            tour.guide_room_key = None if missing == "room" else "unknown"
    response = TestClient(create_app(factory)).get("/api/v1/tours/museum-main")
    assert response.status_code == 200
    assert Tour.model_validate_json(response.content).guide is None
