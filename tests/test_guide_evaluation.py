import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_guide import guide as guide

from app.guide_config import GuideSettings
from scripts.evaluate_guide import EvaluationCase, evaluate, external_file, load_cases


def test_evaluation_refuses_workspace_files_and_invalid_case_sets(tmp_path):
    with pytest.raises(ValueError):
        external_file(str(Path(__file__).resolve()))
    path = tmp_path / "cases.json"
    case = dict(id="synthetic", request=dict(schema_version=1, question="Instrumento sintético"), expected_status="answered", expected_citations=["synthetic-import"])
    for value in [{"schema_version": True, "cases": [case]}, {"schema_version": 1, "cases": []}, {"schema_version": 1, "cases": [case, case]}, {"schema_version": 1, "cases": [case | {"expected_citations": []}]}]:
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises((ValueError, ValidationError)):
            load_cases(path)


def test_evaluation_reads_selected_database_without_changing_index_or_saving_answers(guide):
    url = guide[0]
    database = Path(url.removeprefix("sqlite:///"))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    cases = [EvaluationCase.model_validate(dict(id="plural", request=dict(schema_version=1, question="¿Qué instrumentos producen sonido?"), expected_status="answered", expected_citations=["synthetic-import"]))]
    report = evaluate(database, GuideSettings(), cases)
    assert report['matched'] == 1 and report['database_unchanged']
    assert report['database_sha256_before'] == before == report['database_sha256_after']
    assert 'answer' not in report['results'][0] and 'request' not in report['results'][0]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError):
        evaluate(database, GuideSettings(generation='disabled'), cases)
