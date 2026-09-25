import json
from pathlib import Path

from analytics_service.handoff import export_contract


def test_exported_handoff_is_current():
    saved = json.loads((Path(__file__).parents[1] / "model_contract.json").read_text())
    assert saved == json.loads(json.dumps(export_contract()))


def test_handoff_has_only_schema_and_definitions_not_observed_records():
    contract = export_contract()
    assert len(contract["schema"]["tables"]) == 12
    assert len(contract["metrics"]) == 6
    text = json.dumps(contract)
    for key in ('"example"', '"sample_values"', '"observed_values"', '"unique_values"'):
        assert key not in text
