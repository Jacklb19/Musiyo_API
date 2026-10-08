import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.content_tables import CONTENT, EmbeddingVector
from app.db import ElementRecord, ResourceRecord, TourRecord, make_session_factory
from app.migrations import upgrade_database
from app.public_repository import ASSISTANT_ELEMENTS, PublicRepository
from tests.test_public_api import NOW, client_with_data


def test_public_routes_cannot_query_persistence_tables():
    source = Path(__file__).parents[1] / "app/main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"select", "text", "ElementRecord", "ResourceRecord", "PointElementRecord", "RoomRecord", "TourRecord"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, f"Public route accesses persistence directly: {node.id}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"execute", "scalars", "scalar", "get"} or not isinstance(node.func.value, ast.Name) \
                or node.func.value.id != "db", "Public route bypasses the publication repository"


def test_public_queries_use_real_database_views(tmp_path):
    client = client_with_data(tmp_path)
    url = f"sqlite:///{tmp_path / 'test.db'}"
    factory = make_session_factory(url)
    inspector = sa.inspect(factory.kw["bind"])
    assert {"v_current_elements", "v_current_resources", "v_public_tours", "v_public_rooms", "v_public_points"} <= set(inspector.get_view_names())
    with factory.begin() as db:
        db.execute(CONTENT["authorizations"].delete().where(CONTENT["authorizations"].c.element_id == "visible"))
    assert client.get("/api/v1/elements").json() == []
    assert client.get("/api/v1/elements/visible").status_code == 404
    assert client.get("/api/v1/elements/visible/resources/autorizado").status_code == 404
    assert all(not point["elements"] for room in client.get("/api/v1/tours/recorrido-prueba").json()["rooms"] for point in room["points"])


@pytest.mark.parametrize("state", ["future", "expires_now", "revoked", "not_approved", "no_record"])
def test_authorization_boundaries_are_fail_closed(tmp_path, state):
    client = client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    authorizations = CONTENT["authorizations"]
    values = {"future": {"authorized_from": NOW + timedelta(microseconds=1)},
        "expires_now": {"authorized_until": NOW}, "revoked": {"revoked_at": NOW},
        "not_approved": {"approved_culturally": False}}
    with factory.begin() as db:
        statement = authorizations.delete() if state == "no_record" else authorizations.update().values(**values[state])
        db.execute(statement.where(authorizations.c.element_id == "visible"))
    assert client.get("/api/v1/elements/visible").status_code == 404


def test_resource_parent_retirement_and_transcription_metadata(tmp_path):
    client = client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.add(ResourceRecord(id="narration-test", element_id="visible", room_id="room-prueba", name="test.mp3",
            mime="audio/mpeg", private_path="not-public/test.mp3", kind="narration", transcription="TEST TRANSCRIPT",
            credit="Test credit", provenance="Synthetic generator", byte_count=10, duration_seconds=1.0))
        db.flush()
        db.execute(CONTENT["authorizations"].insert().values(resource_id="narration-test", approved_culturally=True,
            authorized_from=NOW - timedelta(days=1)))
    element = client.get("/api/v1/elements/visible").json()
    assert element["resources"][0]["transcription"] == "TEST TRANSCRIPT"
    assert "not-public" not in str(element) and "private_path" not in str(element)
    assert client.get("/api/v1/tours/recorrido-prueba").json()["rooms"][0]["points"][0]["elements"][0]["has_narration"]
    with factory.begin() as db:
        db.get(TourRecord, "recorrido-prueba").published = False
    assert client.get("/api/v1/elements/visible").json()["resources"] == []
    assert client.get("/api/v1/tours/recorrido-prueba").status_code == 404


def test_legacy_migration_moves_metadata_without_fabricating_decisions(tmp_path):
    from alembic import command
    from alembic.config import Config
    root = Path(__file__).parents[1]
    url = f"sqlite:///{tmp_path / 'old.db'}"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes["database_url"] = url
    command.upgrade(config, "0002")
    # The old schema predates the current ORM columns.
    engine = sa.create_engine(url)
    with engine.begin() as db:
        db.execute(sa.text("""INSERT INTO elements (id,title,description,interpretation,approval_status,approved_at,
            authorized_from,credits,sources,restrictions) VALUES ('old','Test','Original description','Original interpretation',
            'approved','2026-01-01','2026-01-01','Original credit','Original source','Original restriction')"""))
    upgrade_database(url)
    factory = make_session_factory(url)
    with factory() as db:
        item = PublicRepository(db, lambda: NOW).element("old")
        assert item.description == "Original description" and item.blocks[0].text == "Original interpretation"
        assert item.sources[0].reference == "Original source" and item.credits[0].name == "Original credit"
        approval = db.execute(sa.select(CONTENT["authorizations"])).mappings().one()
        assert approval["approved_by"] is None and approval["decision_reference"] is None


def test_new_content_constraints_and_vector_dimensions(tmp_path):
    url = f"sqlite:///{tmp_path / 'constraints.db'}"
    upgrade_database(url)
    factory = make_session_factory(url)
    with factory.begin() as db:
        db.add(ElementRecord(id="item", title="Test", description="Test"))
        db.flush()
        db.execute(CONTENT["element_details"].insert().values(element_id="item", title="Test", description="Test"))
    for values in ({"source_id": None, "context": None}, {"source_id": None, "context": " "}):
        with pytest.raises(IntegrityError), factory.begin() as db:
            db.execute(CONTENT["detail_blocks"].insert().values(element_id="item", kind="interpretation", text="Test", order=0, **values))
    with pytest.raises(IntegrityError), factory.begin() as db:
        db.add(ResourceRecord(id="bad-narration", element_id="item", name="test.mp3", mime="audio/mpeg", private_path="test.mp3", kind="narration"))
        db.flush()
    with pytest.raises(IntegrityError), factory.begin() as db:
        db.execute(CONTENT["authorizations"].insert().values(approved_culturally=True, authorized_from=datetime.now(timezone.utc)))
    encoder = EmbeddingVector().bind_processor(None)
    with pytest.raises(ValueError):
        encoder([1.0, 2.0])
    with pytest.raises(ValueError):
        encoder([float('nan')] * 1024)
    assert len(EmbeddingVector().result_processor(None, None)(encoder([0.0] * 1024))) == 1024


def test_tour_and_assistant_restrictions_apply_without_removing_the_public_record(tmp_path):
    client = client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["restrictions"].insert().values(element_id="visible", kind="no_tour", description="Test restriction"))
        db.execute(CONTENT["restrictions"].insert().values(element_id="visible", kind="exclude_assistant", description="Test restriction"))
    assert client.get("/api/v1/elements/visible").status_code == 200
    assert all(not point["elements"] for room in client.get("/api/v1/tours/recorrido-prueba").json()["rooms"] for point in room["points"])
    with factory() as db:
        PublicRepository(db, lambda: NOW)
        assert db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)).all() == []


def test_expired_assistant_denial_does_not_override_a_current_grant(tmp_path):
    client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["authorizations"].insert().values(element_id="visible", approved_culturally=True,
            authorized_from=NOW - timedelta(days=2), authorized_until=NOW - timedelta(days=1), allow_assistant=False))
    with factory() as db:
        PublicRepository(db, lambda: NOW)
        assert db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)).all() == ["visible"]


def test_normalized_metadata_round_trip_uses_the_existing_wire_contract(tmp_path):
    client = client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["categories"].insert().values(id="category-test", slug="test-category", name="Test category"))
        db.execute(CONTENT["communities"].insert().values(id="community-test", name="Synthetic community", people="shared"))
        db.execute(CONTENT["collections"].insert().values(id="collection-test", slug="test-collection", name="Test collection"))
        db.execute(CONTENT["element_metadata"].insert().values(element_id="visible", category_id="category-test",
            community_id="community-test", technique="Test technique", materials=["Test material"]))
        db.execute(CONTENT["element_details"].insert().values(element_id="visible", title="Normalized title", description="Normalized description"))
        db.execute(CONTENT["sources"].insert().values(id="source-test", kind="publication", reference="Synthetic reference"))
        db.execute(CONTENT["element_sources"].insert().values(element_id="visible", source_id="source-test"))
        db.execute(CONTENT["detail_blocks"].insert().values(element_id="visible", kind="documented_fact", text="Synthetic fact", order=0, source_id="source-test"))
        db.execute(CONTENT["collection_elements"].insert().values(element_id="visible", collection_id="collection-test", order=0))
    item = client.get("/api/v1/elements/visible").json()
    assert item["title"] == "Normalized title" and item["description"] == "Normalized description"
    assert item["category"]["slug"] == "test-category" and item["community"]["people"] == "shared"
    assert item["materials"] == ["Test material"] and item["blocks"][0]["kind"] == "documented_fact"
    assert item["collections"][0]["slug"] == "test-collection"


def test_postgresql_metadata_compiles_with_vector_and_timestamp_types():
    from sqlalchemy.dialects.postgresql import dialect
    from sqlalchemy.schema import CreateTable
    statements = [str(CreateTable(table).compile(dialect=dialect())) for table in CONTENT.values()]
    assert any("VECTOR(1024)" in statement and "TSVECTOR" in statement for statement in statements)
    assert any("TIMESTAMP WITH TIME ZONE" in statement and "approved_culturally" in statement for statement in statements)
