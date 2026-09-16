import json
import importlib.util
from pathlib import Path

import pytest



_MODULE_PATH = Path(__file__).parents[1] / "deploy" / "remote-facts" / "publish_remote_facts.py"
_SPEC = importlib.util.spec_from_file_location("matchline_remote_facts_publisher", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
DEFAULT_ENDPOINT = _MODULE.DEFAULT_ENDPOINT
read_payload = _MODULE.read_payload
validate_payload = _MODULE.validate_payload


def _payload():
    return {
        "schema": "matchline.remote.facts.v1",
        "retrievedAt": "2026-09-03T15:23:43Z",
        "hostname": "matchline-vps",
        "factsOnly": True,
        "predictions": [],
        "odds": [],
        "sources": [],
        "openfootball": {"season": "2026-27", "sources": [], "matches": []},
        "openligadb": {"season": 2026, "leagues": [], "matches": []},
        "wikidata": {"entities": []},
        "metNorway": {"observations": []},
    }


def test_publisher_accepts_only_facts_only_schema(tmp_path: Path):
    path = tmp_path / "current.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    body, value = read_payload(path)
    assert body
    assert value["factsOnly"] is True
    assert validate_payload(value)["schema"] == "matchline.remote.facts.v1"


def test_publisher_accepts_bounded_openfootball_history_section():
    value = _payload()
    value["openfootball"]["history"] = {"seasons": ["2025-26"], "sources": [], "matches": []}
    assert validate_payload(value)["openfootball"]["history"]["seasons"] == ["2025-26"]


def test_publisher_rejects_predictions_before_network():
    value = _payload()
    value["predictions"] = [{"id": "not-allowed"}]
    with pytest.raises(ValueError, match="contains_model_fields"):
        validate_payload(value)


def test_endpoint_is_fixed_to_the_canonical_sites_origin():
    assert _MODULE.endpoint(DEFAULT_ENDPOINT) == DEFAULT_ENDPOINT
    with pytest.raises(ValueError, match="not_allowlisted"):
        _MODULE.endpoint("https://example.test/api/v1/remote-facts")
