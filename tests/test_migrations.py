from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.cli import import_test_data
from app.db import (
    ElementRecord,
    PointElementRecord,
    PointRecord,
    RoomRecord,
    make_session_factory,
)
from app.migrations import upgrade_database


def test_empty_database_upgrade_and_idempotent_import(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    upgrade_database(url)
    upgrade_database(url)
    assert import_test_data(url) == {"rooms": 1, "points": 3}
    assert import_test_data(url) == {"rooms": 0, "points": 0}
    with make_session_factory(url)() as db:
        assert db.scalars(select(PointRecord.id).order_by(PointRecord.order)).all() == [
            "punto-01", "punto-02", "punto-03"
        ]
        assert db.scalars(select(ElementRecord)).all() == []


def test_legacy_upgrade_preserves_content_and_relationships(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    schema = (Path(__file__).parents[1] / "fixtures" / "legacy_schema.sql").read_text(encoding="utf-8")
    with engine.begin() as connection:
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(text(statement))
        connection.execute(text("""INSERT INTO elementos
            (id,titulo,descripcion,interpretacion,estado_aprobacion,aprobado_en,autorizado_desde,creditos,fuentes,restricciones)
            VALUES ('preserved','Prueba','Descripción de prueba','Texto conservado','aprobado',
            '2026-01-01','2026-01-01','Crédito','Fuente','Restricción')"""))
        connection.execute(text("INSERT INTO salas (id,recorrido_id,orden) VALUES ('room','tour',0)"))
        connection.execute(text("INSERT INTO puntos (id,sala_id,orden) VALUES ('point','room',0)"))
        connection.execute(text("INSERT INTO punto_elementos (punto_id,elemento_id,orden) VALUES ('point','preserved',0)"))
        connection.execute(text("""INSERT INTO recursos (id,elemento_id,nombre,tipo_mime,ruta_privada,aprobado)
            VALUES ('resource','preserved','test.txt','text/plain','private/test.txt',0)"""))
    upgrade_database(url)
    with make_session_factory(url)() as db:
        element = db.get(ElementRecord, "preserved")
        assert element.title == "Prueba" and element.interpretation == "Texto conservado"
        assert element.approval_status == "approved" and element.authorized_from is not None
        assert element.sources == "Fuente" and element.credits == "Crédito"
        assert element.resources[0].private_path == "private/test.txt"
        assert not element.resources[0].approved
        assert db.get(PointElementRecord, ("point", "preserved")).order == 0
        assert db.get(RoomRecord, "room").tour_id == "tour"
        assert db.get(PointRecord, "point").room_id == "room"
    tables = set(inspect(engine).get_table_names())
    assert {"elements", "resources", "rooms", "points", "point_elements", "alembic_version"} <= tables
    assert not tables.intersection({"elementos", "recursos", "salas", "puntos", "punto_elementos"})
    assert {index["name"] for index in inspect(engine).get_indexes("rooms")} == {"ix_rooms_tour_id"}


def test_partial_schema_is_refused_without_deleting_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'partial.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE elementos (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO elementos VALUES ('preserved')"))
    with pytest.raises(RuntimeError, match="Incomplete or mixed"):
        upgrade_database(url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM elementos")) == "preserved"
    assert "elements" not in inspect(engine).get_table_names()


def test_conflicting_test_import_rolls_back_all_new_points(tmp_path):
    url = f"sqlite:///{tmp_path / 'conflict.db'}"
    upgrade_database(url)
    factory = make_session_factory(url)
    with factory.begin() as db:
        db.add(RoomRecord(id="sala-prueba", tour_id="recorrido-prueba", order=0))
        db.flush()
        db.add(PointRecord(id="existing", room_id="sala-prueba", order=1))
    with pytest.raises(ValueError, match="Point order already used"):
        import_test_data(url)
    with factory() as db:
        assert db.scalars(select(PointRecord.id)).all() == ["existing"]


def test_session_factory_does_not_modify_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'uninitialized.db'}"
    factory = make_session_factory(url)
    assert inspect(factory.kw["bind"]).get_table_names() == []
