from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_guide import guide as guide
from test_guide import question

from app.main import create_app
from scripts.add_blocks import BlockPlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-22", reference="synthetic")
FACT = dict(kind="documented_fact", source_id="test-source", text="El instrumento sintético se fabrica con un tallo vaciado y piel.")
TESTIMONY = dict(kind="testimony", source_id="test-source", attribution="Mayor sintética",
                 text="Cada quien toca el instrumento sintético según su gusto.")


def plan(count, *blocks):
    return BlockPlan.model_validate(dict(operation_version=1, decision=DECISION,
                                         elements=[dict(slug="synthetic-import", expected_block_count=count, blocks=list(blocks))]))


def record(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/elements/synthetic-import").json()


def test_blocks_are_appended_in_order_and_reach_the_guide(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    count = len(record(guide)["blocks"])
    assert not apply(database, plan(count, FACT, TESTIMONY), dry_run=True)["applied"]
    assert len(record(guide)["blocks"]) == count
    result = apply(database, plan(count, FACT, TESTIMONY), dry_run=False)
    assert [item["order"] for item in result["added"]] == [count, count + 1]
    blocks = record(guide)["blocks"]
    assert [b["kind"] for b in blocks[-2:]] == ["documented_fact", "testimony"] and blocks[-1]["attribution"] == "Mayor sintética"
    answer = guide[2].ask(question("¿Con qué se fabrica el instrumento sintético?"))
    assert answer.status == "answered" and "tallo vaciado" in answer.answer
    with pytest.raises(ValueError):
        apply(database, plan(count, FACT), dry_run=False)


def test_unknown_sources_and_unattributed_testimony_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    count = len(record(guide)["blocks"])
    with pytest.raises(ValueError):
        apply(database, plan(count, FACT | dict(source_id="no-existe")), dry_run=False)
    with pytest.raises(ValueError):
        plan(count, TESTIMONY | dict(attribution=None))
    assert len(record(guide)["blocks"]) == count
