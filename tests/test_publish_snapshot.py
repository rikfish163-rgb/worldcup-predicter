from __future__ import annotations

import json
import hashlib
from pathlib import Path
import unicodedata

import pytest

import league_platform.publish_snapshot as publisher
import league_platform.maturity_validator as maturity_validator
from league_platform.publish_intelligence import ProducerAttestationContext


_REAL_REQUIRE_PUBLICATION_MATURITY = publisher.require_publication_maturity
_REAL_BUILD_PRODUCER_ATTESTATION_CONTEXT = (
    publisher.build_producer_attestation_context
)
_REAL_REQUIRE_OPENFOOTBALL_RAW_PRODUCER_RECEIPT = (
    publisher.require_openfootball_raw_producer_receipt
)


@pytest.fixture(autouse=True)
def _validated_unit_maturity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        publisher,
        "require_publication_maturity",
        lambda *_args, **_kwargs: {"status": "pass", "receiptSha256": "d" * 64},
    )
    monkeypatch.setattr(
        publisher,
        "build_producer_attestation_context",
        lambda *_args, **kwargs: None
        if kwargs.get("dry_run")
        else ProducerAttestationContext(
            key_id="test-key",
            signing_secret=b"s" * 32,
            stream="fixture_registration",
            rights_use_case="redistribution",
            source_policy_ids=("openfootball_current",),
            publication_epoch="test",
            maturity_receipt_raw_sha256="d" * 64,
            live_snapshot_raw_sha256="e" * 64,
        ),
    )
    monkeypatch.setattr(
        publisher,
        "require_openfootball_raw_producer_receipt",
        lambda _snapshot, *, dry_run: (
            "candidate_only_unverified_raw_provenance"
            if dry_run
            else "verified_test_fixture"
        ),
    )


def _fixture(index: int = 0) -> dict:
    return {
        "id": f"espn:{1000 + index}",
        "competition_id": "premier-league",
        "season": "2026",
        "kickoff_at": "2026-08-16T17:00:00+00:00",
        "home_team": f"Home {index}",
        "away_team": f"Away {index}",
        "home_provider_team_id": str(10 + index * 2),
        "away_provider_team_id": str(11 + index * 2),
        "venue": {"name": "Stadium"},
        "status": "upcoming",
        "score": {"home": 9, "away": 9},
        "source": {
            "name": "ESPN",
            "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
            "native_fixture_id": str(1000 + index),
            "retrieved_at": "2026-08-16T16:00:00+00:00",
            "raw_sha256": "a" * 64,
            "commercial_reuse_verified_by_code": True,
        },
    }


def _snapshot(fixtures: list[dict] | None = None) -> dict:
    return {
        "schema_version": 5,
        "as_of": "2026-08-16T16:01:00+00:00",
        "espn": {
            "provider": "ESPN",
            "rights_status": "operator_authorization_reference_supplied",
            "access_allowed": True,
            "authorization_reference": "test-contract:espn-current-serving",
            "commercial_reuse_verified_by_code": True,
            "fixtures": fixtures or [_fixture()],
        },
    }


def _openligadb_fixture() -> dict:
    fixture = _fixture()
    fixture["source"] = {
        "name": "OpenLigaDB",
        "url": "https://api.openligadb.de/getmatchdata/1000",
        "native_fixture_id": "1000",
        "retrieved_at": "2026-08-16T16:00:00+00:00",
        "raw_sha256": "f" * 64,
        "license": "ODbL-1.0",
        "license_url": "https://www.openligadb.de/lizenz",
        "attribution_required": True,
    }
    return fixture


def _openfootball_fixture_id(
    source_id: str,
    competition_id: str,
    season: str,
    home: str,
    away: str,
) -> str:
    normalized_home = " ".join(unicodedata.normalize("NFKC", home).casefold().split())
    normalized_away = " ".join(unicodedata.normalize("NFKC", away).casefold().split())
    return f"openfootball:{competition_id}:" + hashlib.sha256(
        f"{source_id}|{season}|{normalized_home}|{normalized_away}".encode("utf-8")
    ).hexdigest()[:24]


def _openfootball_fixture(index: int = 0) -> dict:
    fixture = _fixture(index)
    source_id = "openfootball:football.json:2026-27:en.1"
    fixture_id = _openfootball_fixture_id(
        source_id,
        "premier-league",
        "2026-27",
        str(fixture["home_team"]),
        str(fixture["away_team"]),
    )
    fixture.update(
        {
            "id": fixture_id,
            "season": "2026-27",
            "source": {
                "name": "OpenFootball",
                "source_id": source_id,
                "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "b" * 64,
                "license": "CC0-1.0",
            },
        }
    )
    fixture.pop("home_provider_team_id")
    fixture.pop("away_provider_team_id")
    return fixture


def _openfootball_snapshot(fixtures: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0.0",
        "as_of": "2026-08-16T16:01:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": fixtures or [_openfootball_fixture()],
        },
    }


@pytest.mark.parametrize(
    "rights_metadata",
    [
        {},
        {
            "rights_status": "blocked_pending_express_written_permission",
            "access_allowed": False,
        },
    ],
)
def test_dry_run_rejects_unlicensed_legacy_espn_snapshot(
    tmp_path: Path,
    rights_metadata: dict,
) -> None:
    snapshot_path = tmp_path / "current.json"
    payload = {
        "schema_version": 5,
        "as_of": "2026-08-16T16:01:00+00:00",
        "espn": {
            "provider": "ESPN",
            "fixtures": [_fixture()],
            **rights_metadata,
        },
    }
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="legacy_espn_serving_rights_missing"):
        publisher.publish_snapshot(snapshot_path, dry_run=True)

    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == payload


def test_dry_run_rejects_self_authorized_section_scoped_legacy_espn(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture["source"].pop("commercial_reuse_verified_by_code")
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(json.dumps(_snapshot([fixture])), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_schedule_summary:redistribution",
    ):
        publisher.publish_snapshot(snapshot_path, dry_run=True)


def test_unknown_provider_cannot_self_authorize_with_payload_booleans() -> None:
    fixture = _fixture()
    fixture["source"].update(
        {
            "name": "Attacker feed",
            "source_id": "attacker_self_issued",
            "url": "https://attacker.example/fixture.json",
            "commercial_reuse_verified": True,
            "commercial_reuse_verified_by_code": True,
        }
    )

    with pytest.raises(ValueError, match="source_rights_unknown_provider:Attacker feed"):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_dry_run_accepts_openfootball_cc0_current_snapshot(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture.update(
        {
            "id": _openfootball_fixture_id(
                "openfootball:football.json:2026-27:en.1",
                "premier-league",
                "2026-27",
                "Arsenal FC",
                "Manchester City FC",
            ),
            "season": "2026-27",
            "home_team": "Arsenal FC",
            "away_team": "Manchester City FC",
            "source": {
                "name": "OpenFootball",
                "source_id": "openfootball:football.json:2026-27:en.1",
                "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "b" * 64,
                "license": "CC0-1.0",
            },
        }
    )
    fixture.pop("home_provider_team_id")
    fixture.pop("away_provider_team_id")
    snapshot_path = tmp_path / "current.json"
    snapshot = {
        "schema_version": "1.0.0",
        "as_of": "2026-08-16T16:01:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball",
            "source_contract": {
                "fact_source": "OpenFootball",
                "license": "CC0-1.0",
            },
            "fixtures": [fixture],
        },
    }
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = publisher.publish_snapshot(snapshot_path, dry_run=True)
    payload = next(publisher.iter_fixture_batches(snapshot))

    assert result["status"] == "dry_run"
    assert result["requests"] == 0
    assert result["fixtures"] == 1
    assert payload["sourceRun"]["provider"] == "OpenFootball"
    item = payload["fixtures"][0]
    assert item["source"]["name"] == "OpenFootball"
    assert item["source"]["tier"] == "candidate_only_unverified_raw_provenance"
    for entity in (item["homeTeam"], item["awayTeam"], item["fixture"]):
        assert entity["provider"] == "Matchline canonical alias"
        assert entity["identityKind"] == "synthetic_alias"
    assert item["homeTeam"]["providerId"] == "premier-league:arsenal"
    assert item["awayTeam"]["providerId"] == "premier-league:man-city"
    assert item["homeTeam"]["canonicalName"] == "Arsenal"
    assert item["awayTeam"]["canonicalName"] == "Man City"
    assert item["fixture"]["providerFixtureId"] == fixture["id"]


def test_wikidata_venue_provenance_is_registered_for_redistribution(
    tmp_path: Path,
) -> None:
    fixture = _openfootball_fixture()
    fixture["field_sources"] = {
        "venue": {
            "name": "Wikidata structured data",
            "source_id": "wikidata_entities",
            "url": "https://www.wikidata.org/w/api.php",
            "license": "CC0-1.0",
            "license_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        }
    }
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([fixture])),
        encoding="utf-8",
    )

    result = publisher.publish_snapshot(snapshot_path, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["requests"] == 0
    assert result["fixtures"] == 1


def test_openfootball_name_and_license_cannot_authorize_an_unregistered_url() -> None:
    fixture = _fixture()
    fixture.update(
        {
            "id": "openfootball:premier-league:forged",
            "season": "2026-27",
            "source": {
                "name": "OpenFootball",
                "source_id": "openfootball:football.json:2026-27:en.1",
                "url": "https://attacker.example/forged.json",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "0" * 64,
                "license": "CC0-1.0",
            },
        }
    )
    fixture.pop("home_provider_team_id")
    fixture.pop("away_provider_team_id")

    with pytest.raises(ValueError, match="openfootball_source_contract_unverified"):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_allowlisted_openfootball_metadata_cannot_authorize_forged_facts() -> None:
    fixture = _openfootball_fixture()
    fixture["id"] = "openfootball:premier-league:invented"
    fixture["home_team"] = "Invented FC"
    fixture["away_team"] = "Fabricated United"
    fixture["source"]["raw_sha256"] = "0" * 64

    with pytest.raises(
        ValueError,
        match="openfootball_fixture_identity_source_mismatch",
    ):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_allowlisted_openfootball_facts_stay_blocked_without_raw_producer_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _openfootball_fixture()
    fixture["home_team"] = "Invented FC"
    fixture["away_team"] = "Fabricated United"
    fixture["source"]["raw_sha256"] = "0" * 64
    fixture["id"] = _openfootball_fixture_id(
        fixture["source"]["source_id"],
        fixture["competition_id"],
        fixture["season"],
        fixture["home_team"],
        fixture["away_team"],
    )
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([fixture])),
        encoding="utf-8",
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )
    monkeypatch.setattr(
        publisher,
        "require_openfootball_raw_producer_receipt",
        _REAL_REQUIRE_OPENFOOTBALL_RAW_PRODUCER_RECEIPT,
    )

    with pytest.raises(ValueError, match="raw_producer_receipt_required"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://example.com/api/fixtures/register",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_cfl_native_identity_is_blocked_when_commercial_reuse_is_unverified(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    fixture.update(
        {
            "id": "cfl-official:csl:official-fixture-id",
            "competition_id": "csl",
            "native_fixture_id": "official-fixture-id",
            "home_team": "上海申花",
            "away_team": "山东泰山",
            "home_provider_team_id": "home-official-id",
            "away_provider_team_id": "away-official-id",
            "source": {
                "name": "CFL official",
                "url": "https://api.cfl-china.cn/frontweb/api/matches/page",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "c" * 64,
                "rights_status": "official_public_frontend_no_reuse_terms_published",
                "commercial_reuse_verified": False,
            },
        }
    )
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "as_of": "2026-08-16T16:01:00+00:00",
                "fixture_feed": {
                    "provider": "CFL official",
                    "fixtures": [fixture],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source_rights_blocked:cfl_official_current:redistribution"):
        publisher.publish_snapshot(snapshot_path, dry_run=True)


def test_canonical_fixture_rights_are_preflighted_before_building_any_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openfootball = _fixture(1)
    openfootball.update(
        {
            "id": "openfootball:premier-league:1001",
            "season": "2026-27",
            "source": {
                "name": "OpenFootball",
                "source_id": "openfootball:football.json:2026-27:en.1",
                "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "b" * 64,
                "license": "CC0-1.0",
            },
        }
    )
    openfootball.pop("home_provider_team_id")
    openfootball.pop("away_provider_team_id")
    blocked = _fixture(2)
    blocked.update(
        {
            "id": "cfl-official:csl:1002",
            "competition_id": "csl",
            "source": {
                "name": "CFL official",
                "url": "https://api.cfl-china.cn/frontweb/api/matches/page",
                "native_fixture_id": "1002",
                "retrieved_at": "2026-08-16T16:00:00+00:00",
                "raw_sha256": "c" * 64,
                "commercial_reuse_verified": False,
            },
        }
    )
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "as_of": "2026-08-16T16:01:00+00:00",
                "fixture_feed": {
                    "provider": "OpenFootball + CFL official",
                    "fixtures": [openfootball, blocked],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    built: list[str] = []
    original = publisher.build_fixture_registration_item

    def recording_builder(fixture, **kwargs):
        built.append(str(fixture.get("id")))
        return original(fixture, **kwargs)

    monkeypatch.setattr(publisher, "build_fixture_registration_item", recording_builder)

    with pytest.raises(ValueError, match="source_rights_blocked:cfl_official_current:redistribution"):
        publisher.publish_snapshot(snapshot_path, dry_run=True)

    assert built == []


def test_openligadb_current_display_is_not_implicitly_redistributable(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "as_of": "2026-08-16T16:01:00+00:00",
                "fixture_feed": {
                    "provider": "OpenLigaDB",
                    "fixtures": [_openligadb_fixture()],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:openligadb_secondary_results:redistribution",
    ):
        publisher.publish_snapshot(snapshot_path, dry_run=True)


def test_blocked_schedule_overlay_cannot_launder_fields_through_openfootball(
    tmp_path: Path,
) -> None:
    fixture = _openfootball_fixture()
    fixture["kickoff_at"] = "2099-01-01T00:00:00+00:00"
    fixture["venue"] = {"name": "ESPN Arena"}
    espn_source = {
        "name": "ESPN",
        "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
        "retrieved_at": "2026-08-16T16:00:00+00:00",
        "raw_sha256": "e" * 64,
    }
    fixture["field_sources"] = {
        "kickoff_at": espn_source,
        "venue": espn_source,
    }
    fixture["schedule_overlay"] = {
        "provider": "ESPN",
        "provider_fixture_id": "forged-overlay",
        "fields": ["kickoff_at", "venue"],
    }
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([fixture])),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:espn_schedule_summary:redistribution",
    ):
        publisher.publish_snapshot(snapshot_path, dry_run=True)


def test_native_provider_identity_is_preserved_for_allowed_local_projection() -> None:
    fixture = _openligadb_fixture()

    item = publisher.build_fixture_registration_item(
        fixture,
        snapshot_as_of="2026-08-16T16:01:00+00:00",
    )

    assert item["source"]["name"] == "OpenLigaDB"
    for entity in (item["homeTeam"], item["awayTeam"], item["fixture"]):
        assert entity["provider"] == "OpenLigaDB"
        assert entity["identityKind"] == "native"
    assert item["fixture"]["providerFixtureId"] == "1000"


@pytest.mark.parametrize(
    ("competition_id", "competition_name", "country"),
    [
        ("championship", "英冠", "England"),
        ("ligue-1", "法甲", "France"),
    ],
)
def test_expanded_current_competitions_keep_known_country_metadata(
    competition_id: str,
    competition_name: str,
    country: str,
) -> None:
    fixture = _openligadb_fixture()
    fixture["competition_id"] = competition_id

    item = publisher.build_fixture_registration_item(
        fixture,
        snapshot_as_of="2026-08-16T16:01:00+00:00",
    )

    assert item["competition"]["name"] == competition_name
    assert item["competition"]["country"] == country
    assert item["homeTeam"]["country"] == country


def test_fixture_without_native_or_explicit_synthetic_identity_is_quarantined() -> None:
    fixture = _openligadb_fixture()
    fixture["id"] = ""
    fixture["source"].pop("native_fixture_id")

    with pytest.raises(ValueError, match="fixture_identity_unavailable"):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_native_fixture_without_provider_team_identity_is_quarantined() -> None:
    fixture = _openligadb_fixture()
    fixture.pop("away_provider_team_id")

    with pytest.raises(ValueError, match="team_identity_unavailable"):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_unknown_provider_without_verified_commercial_rights_is_quarantined() -> None:
    fixture = _fixture()
    fixture["source"] = {
        "name": "Unknown public provider",
        "url": "https://example.com/fixtures",
        "native_fixture_id": "native-1000",
        "retrieved_at": "2026-08-16T16:00:00+00:00",
        "raw_sha256": "e" * 64,
    }

    with pytest.raises(
        ValueError,
        match="source_rights_unknown_provider:Unknown public provider",
    ):
        publisher.build_fixture_registration_item(
            fixture,
            snapshot_as_of="2026-08-16T16:01:00+00:00",
        )


def test_registration_payload_strips_scores_and_is_bounded() -> None:
    item = publisher.build_fixture_registration_item(
        _openfootball_fixture(),
        snapshot_as_of="2026-08-16T16:01:00+00:00",
    )
    assert "score" not in item
    assert "score" not in item["fixture"]
    payload = publisher.build_fixture_batch_payload([item])
    encoded = json.dumps(payload)
    assert '"score"' not in encoded
    with pytest.raises(ValueError, match="1-100"):
        publisher.build_fixture_batch_payload([item] * 101)


def test_receipts_resume_completed_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([_openfootball_fixture(1), _openfootball_fixture(2)])),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def fake_publish(payload, **_kwargs):
        calls.append(payload)
        return {
            "status": "ok",
            "accepted": len(payload["fixtures"]),
            "created": len(payload["fixtures"]),
            "updated": 0,
            "observationsAccepted": len(payload["fixtures"]),
            "observationsSkippedDuplicate": 0,
        }

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    receipts = tmp_path / "receipts.jsonl"
    first = publisher.publish_snapshot(snapshot_path, endpoint="https://example.com/register", token="secret", receipts_path=receipts, publication_epoch="production-v1")
    second = publisher.publish_snapshot(snapshot_path, endpoint="https://example.com/register", token="secret", receipts_path=receipts, publication_epoch="production-v1")
    assert first["requests"] == 1
    assert second["requests"] == 0
    assert second["receiptsSkipped"] == 1
    assert len(calls) == 1


def test_forged_completed_receipt_without_ack_never_skips_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot = _openfootball_snapshot([_openfootball_fixture(1)])
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    payload = next(publisher.iter_fixture_batches(snapshot))
    batch_digest, _payload_digest = publisher._batch_identity(payload)
    identity = publisher.publication_receipt_identity(
        stream="fixtures",
        destinations={"register": "https://example.com/register"},
        publication_epoch_value="production-v1",
        publication_gate_sha256="d" * 64,
        outbound_payload=payload,
    )
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        json.dumps({
            **identity,
            "batchDigest": batch_digest,
            "status": "completed",
        })
        + "\n",
        encoding="utf-8",
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="scoped identity missing"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://example.com/register",
            token="secret",
            receipts_path=receipts,
            publication_epoch="production-v1",
        )

    assert calls == []


def test_receipt_does_not_skip_a_different_destination_or_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([_openfootball_fixture(1)])),
        encoding="utf-8",
    )
    receipts = tmp_path / "receipts.jsonl"
    calls: list[str] = []

    def fake_publish(payload, *, endpoint, **_kwargs):
        calls.append(endpoint)
        return {
            "status": "ok",
            "accepted": len(payload["fixtures"]),
            "created": 1,
            "observationsAccepted": len(payload["fixtures"]),
            "observationsSkippedDuplicate": 0,
        }

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    for endpoint, epoch in (
        ("https://staging.example/register", "deploy-1"),
        ("https://production.example/register", "deploy-1"),
        ("https://production.example/register", "deploy-2"),
    ):
        result = publisher.publish_snapshot(
            snapshot_path,
            endpoint=endpoint,
            token="secret",
            receipts_path=receipts,
            publication_epoch=epoch,
        )
        assert result["requests"] == 1

    assert calls == [
        "https://staging.example/register",
        "https://production.example/register",
        "https://production.example/register",
    ]


def test_missing_publication_epoch_fails_before_fixture_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(_openfootball_snapshot([_openfootball_fixture(1)])),
        encoding="utf-8",
    )
    calls: list[dict] = []
    monkeypatch.setattr(publisher, "publish_payload", lambda payload, **_kwargs: calls.append(payload))

    with pytest.raises(ValueError, match="publication_epoch"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://production.example/register",
            token="secret",
        )

    assert calls == []


def test_missing_maturity_context_fails_before_fixture_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(json.dumps(_openfootball_snapshot()), encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "require_publication_maturity",
        _REAL_REQUIRE_PUBLICATION_MATURITY,
    )
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="publication_maturity_context"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://production.example/register",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_missing_producer_signing_identity_fails_before_fixture_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(json.dumps(_openfootball_snapshot()), encoding="utf-8")
    maturity_context = publisher.PublicationMaturityContext(
        receipt_path=tmp_path / "maturity.json",
        artifact_paths={"live_snapshot": snapshot_path},
        runtime_root=tmp_path,
        repository_root=tmp_path,
    )
    monkeypatch.setattr(
        publisher,
        "build_producer_attestation_context",
        _REAL_BUILD_PRODUCER_ATTESTATION_CONTEXT,
    )
    monkeypatch.delenv("MATCHLINE_PRODUCER_KEY_ID", raising=False)
    monkeypatch.delenv("MATCHLINE_PRODUCER_SIGNING_SECRET", raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="producer signing identity"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://example.com/register",
            token="secret",
            publication_epoch="production-v1",
            maturity_context=maturity_context,
        )

    assert calls == []


def test_blocked_maturity_verification_stops_fixture_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(json.dumps(_openfootball_snapshot()), encoding="utf-8")
    paths = {
        name: tmp_path / f"{name}.json"
        for name in (
            "platform_verification",
            "strict_report",
            "prospective_lock",
            "cycle",
            "evaluation",
            "offline_snapshot",
            "sites_build_evidence",
            "test_evidence",
            "publication_audit",
            "prediction_archive",
            "publication_diagnostic",
        )
    }
    for path in paths.values():
        path.write_text("{}", encoding="utf-8")
    receipt_path = tmp_path / "maturity-receipt.json"
    receipt_path.write_text("{}", encoding="utf-8")
    context = publisher.build_publication_maturity_context(
        receipt_path=receipt_path,
        platform_verification_path=paths["platform_verification"],
        strict_report_path=paths["strict_report"],
        prospective_lock_path=paths["prospective_lock"],
        cycle_path=paths["cycle"],
        evaluation_path=paths["evaluation"],
        live_snapshot_path=snapshot_path,
        offline_snapshot_path=paths["offline_snapshot"],
        sites_build_evidence_path=paths["sites_build_evidence"],
        test_evidence_path=paths["test_evidence"],
        publication_audit_path=paths["publication_audit"],
        prediction_archive_path=paths["prediction_archive"],
        publication_diagnostic_path=paths["publication_diagnostic"],
        runtime_root=tmp_path,
        repository_root=tmp_path,
    )
    assert set(context.artifact_paths) == {
        "platform_verification",
        "strict_report",
        "prospective_lock",
        "cycle",
        "evaluation",
        "offline_snapshot",
        "sites_build_evidence",
        "test_evidence",
        "publication_audit",
        "live_snapshot",
        "prediction_archive",
        "publication_diagnostic",
    }
    monkeypatch.setattr(
        publisher,
        "require_publication_maturity",
        _REAL_REQUIRE_PUBLICATION_MATURITY,
    )
    monkeypatch.setattr(
        maturity_validator,
        "validate_maturity_receipt",
        lambda *_args, **_kwargs: {
            "status": "blocked",
            "failures": ["platform_verification_status_blocked"],
        },
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="publication_maturity_blocked"):
        publisher.publish_snapshot(
            snapshot_path,
            endpoint="https://example.com/register",
            token="secret",
            publication_epoch="production-v1",
            maturity_context=context,
        )
    assert calls == []


def test_empty_fixture_response_never_writes_completed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(json.dumps(_openfootball_snapshot()), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    monkeypatch.setattr(publisher, "publish_payload", lambda *_args, **_kwargs: {})

    result = publisher.publish_snapshot(
        snapshot_path,
        endpoint="https://production.example/register",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert result["status"] == "partial"
    assert result["failed"] == 1
    assert not receipts.exists()


def test_invalid_source_provenance_is_rejected() -> None:
    invalid = _fixture()
    invalid["source"]["url"] = "http://example.com/scoreboard"
    with pytest.raises(ValueError, match="HTTPS"):
        publisher.build_fixture_registration_item(invalid, snapshot_as_of="2026-08-16T16:01:00+00:00")
    invalid_hash = _fixture()
    invalid_hash["source"]["raw_sha256"] = "not-a-hash"
    with pytest.raises(ValueError, match="SHA-256"):
        publisher.build_fixture_registration_item(invalid_hash, snapshot_as_of="2026-08-16T16:01:00+00:00")
