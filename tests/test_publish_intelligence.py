import base64
import hashlib
import hmac
import io
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import urllib.error

import pytest

import league_platform.publish_intelligence as publisher
from league_platform.intelligence import ObservationLedger, observation_from_payload
from league_platform.live_sources.openfootball_live import OPENFOOTBALL_SOURCES
from league_platform.openfootball_raw_archive import (
    OpenFootballRawArchive,
    OpenFootballRawObservation,
    load_verified_openfootball_archive,
)
from league_platform.source_rights import SourceId
from league_platform.publish_intelligence import (
    build_prospective_gate_metadata,
    build_ingest_payload,
    build_lineup_diagnostics_payload,
    build_source_health_payloads,
    iter_ledger_batches,
    publish_ledger,
    publish_lineup_diagnostics,
    publish_source_health_diagnostics,
    publish_payload,
)


_REAL_REQUIRE_PUBLICATION_MATURITY = publisher.require_publication_maturity
_REAL_REQUIRE_RAW_SOURCE_PRODUCER_RECEIPT = (
    publisher.require_raw_source_producer_receipt
)
_MISSING_RUNTIME_EVIDENCE = object()


_RUNTIME_EVIDENCE_SOURCE_CASES = (
    (
        "MET Norway Locationforecast",
        "met_norway_weather",
        "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=51&lon=0",
    ),
    (
        "Wikidata structured data",
        "wikidata_entities",
        "https://www.wikidata.org/w/api.php?action=wbsearchentities",
    ),
)


_CRAWL4AI_OPERATOR_SOURCE_ALIASES = (
    (
        "Football-Data historical match archive operator source",
        SourceId.FOOTBALL_DATA_HISTORICAL,
    ),
    (
        "WhoScored public match centre operator source",
        SourceId.WHOSCORED_PUBLIC_PAGES,
    ),
    (
        "Premier League official public match pages operator source",
        SourceId.PREMIER_LEAGUE_PUBLIC_PAGES,
    ),
    (
        "LaLiga official public match pages operator source",
        SourceId.LALIGA_PUBLIC_PAGES,
    ),
    (
        "LaLiga official public news operator source",
        SourceId.LALIGA_OFFICIAL_NEWS,
    ),
    (
        "Bundesliga official public match pages operator source",
        SourceId.BUNDESLIGA_PUBLIC_PAGES,
    ),
    (
        "Serie A official public match pages operator source",
        SourceId.SERIEA_PUBLIC_PAGES,
    ),
    (
        "Ligue 1 official public news operator source",
        SourceId.LIGUE1_OFFICIAL_NEWS,
    ),
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
        else _attestation_context(
            stream=str(kwargs.get("stream") or "intelligence_ingest"),
            rights_use_case=str(
                kwargs.get("rights_use_case") or "redistribution"
            ),
            source_policy_ids=tuple(
                sorted(kwargs.get("source_policy_ids") or {"openfootball_current"})
            ),
        ),
    )
    monkeypatch.setattr(
        publisher,
        "require_raw_source_producer_receipt",
        lambda _source_ids, *, dry_run: (
            "candidate_only_unverified_raw_provenance"
            if dry_run
            else "verified_test_fixture"
        ),
    )


def _attestation_context(
    *,
    stream: str = "intelligence_ingest",
    rights_use_case: str = "redistribution",
    source_policy_ids: tuple[str, ...] = ("openfootball_current",),
) -> publisher.ProducerAttestationContext:
    return publisher.ProducerAttestationContext(
        key_id="test-key",
        signing_secret=b"s" * 32,
        stream=stream,
        rights_use_case=rights_use_case,
        source_policy_ids=source_policy_ids,
        publication_epoch="test",
        maturity_receipt_raw_sha256="d" * 64,
        live_snapshot_raw_sha256="e" * 64,
    )


def _row() -> dict:
    return {
        "entity_type": "fixture",
        "entity_id": "f-1",
        "kind": "fixture_schedule",
        "payload": {"kickoff_at": "2026-08-12T11:00:00+00:00"},
        "source_name": "ESPN",
        "source_url": "https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
        "source_tier": "authorized",
        "observed_at": datetime(2026, 8, 11, 12, tzinfo=timezone.utc).isoformat(),
        "effective_at": datetime(2026, 8, 11, 12, tzinfo=timezone.utc).isoformat(),
        "confidence": 0.98,
        "raw_hash": "a" * 64,
        "enters_model": True,
    }


def test_build_ingest_payload_preserves_provenance_and_wire_names():
    row = _row() | {"fixture_id": 17, "team_id": 23, "player_id": 31}
    payload = build_ingest_payload([row])
    assert payload["sourceRun"]["recordsSeen"] == 1
    assert payload["sourceRun"]["provider"] == "ESPN"
    assert payload["observations"][0]["sourceUrl"].startswith("https://")
    assert payload["observations"][0]["rawHash"] == "a" * 64
    assert payload["observations"][0]["fixtureId"] == 17
    assert payload["observations"][0]["teamId"] == 23
    assert payload["observations"][0]["playerId"] == 31


def test_build_ingest_payload_marks_boundary_batches_as_mixed_source():
    second = _row() | {
        "entity_id": "f-2",
        "source_name": "Understat",
        "source_url": "https://understat.com/league/EPL",
    }
    payload = build_ingest_payload([_row(), second])
    assert payload["sourceRun"]["provider"] == "Matchline local ledger (2 sources)"


def test_build_ingest_payload_allows_explicit_provider_override():
    payload = build_ingest_payload([_row()], provider="scheduled backfill")
    assert payload["sourceRun"]["provider"] == "scheduled backfill"


def test_build_ingest_payload_limits_request_size():
    with pytest.raises(ValueError, match="at most"):
        build_ingest_payload([_row()] * 201)


def test_build_ingest_payload_bounds_long_news_urls_without_losing_provenance():
    long_url = "https://news.google.com/rss/articles/" + ("a" * 240)
    row = _row() | {
        "entity_type": "news",
        "entity_id": long_url,
        "kind": "news_fact_candidate",
        "source_name": "Google News RSS",
        "source_url": "https://news.google.com/rss/search?q=football",
        "source_tier": "reliable_media",
        "enters_model": False,
    }
    payload = build_ingest_payload([row])
    entity_id = payload["observations"][0]["entityId"]
    assert entity_id.startswith("news-url-sha256:")
    assert len(entity_id) <= 160
    assert payload["observations"][0]["sourceUrl"] == row["source_url"]


def test_build_ingest_payload_rejects_overlong_non_news_ids():
    row = _row() | {"entity_id": "fixture-" + ("x" * 200)}
    with pytest.raises(ValueError, match="exceeds 160"):
        build_ingest_payload([row])


def test_build_ingest_payload_bounds_oversized_nested_text_with_digest_marker():
    markdown = "# Match report\n" + ("line with verified source facts\n" * 2_000)
    row = _row() | {
        "payload": {
            "markdown": markdown,
            "source_url": "https://source.example/report",
            "facts": ["injury", "suspension"],
        },
    }

    payload = build_ingest_payload([row])
    wire_markdown = payload["observations"][0]["payload"]["markdown"]

    assert len(wire_markdown) <= publisher.MAX_WIRE_PAYLOAD_STRING_LENGTH
    assert wire_markdown.startswith("[matchline-truncated sha256=")
    assert f"original_length={len(markdown)}" in wire_markdown
    assert payload["observations"][0]["payload"]["source_url"] == "https://source.example/report"
    assert payload["observations"][0]["payload"]["facts"] == ["injury", "suspension"]


def test_iter_ledger_batches_respects_bounded_prefix(tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(ledger_path)
    rows = []
    for index in range(5):
        rows.append(
            observation_from_payload(
                entity_type="fixture",
                entity_id=f"f-{index}",
                kind="fixture_schedule",
                payload={"kickoff_at": "2026-08-20T19:00:00+00:00"},
                source_name=f"source-{index}",
                source_url="https://source.example/feed",
                source_tier="authorized",
                observed_at="2026-08-17T10:00:00+00:00",
                published_at="2026-08-17T10:00:00+00:00",
                confidence=0.98,
                enters_model=True,
                raw_hash=(str(index) + "a") * 32,
            )
        )
    ledger.append(rows)

    batches = list(iter_ledger_batches(ledger, max_rows=2))

    assert sum(len(batch) for batch in batches) == 2
    assert [row["entity_id"] for batch in batches for row in batch] == ["f-0", "f-1"]


def test_iter_ledger_batches_can_limit_publication_to_the_prospective_window(tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(ledger_path)
    old = observation_from_payload(
        entity_type="fixture",
        entity_id="old",
        kind="fixture_schedule",
        payload={"kickoff_at": "2026-08-17T18:00:00+00:00"},
        source_name="ESPN",
        source_url="https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
        source_tier="authorized",
        observed_at="2026-08-17T17:00:00+00:00",
        published_at="2026-08-17T17:00:00+00:00",
        confidence=0.98,
        raw_hash="c" * 64,
    )
    current = observation_from_payload(
        entity_type="fixture",
        entity_id="current",
        kind="fixture_schedule",
        payload={"kickoff_at": "2026-08-17T18:00:00+00:00"},
        source_name="ESPN",
        source_url="https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
        source_tier="authorized",
        observed_at="2026-08-17T18:00:00+00:00",
        published_at="2026-08-17T18:00:00+00:00",
        confidence=0.98,
        raw_hash="d" * 64,
    )
    ledger.append([old, current])

    batches = list(iter_ledger_batches(ledger, batch_size=25, min_observed_at="2026-08-17T17:57:41+00:00"))

    assert [[row["entity_id"] for row in batch] for batch in batches] == [["current"]]


def test_publish_ledger_limit_does_not_claim_full_scan(tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(ledger_path)
    ledger.append(
        [_ledger_observation(f"f-{index}") for index in range(3)]
    )

    result = publish_ledger(ledger_path, dry_run=True, limit=2)

    assert result["rows"] == 2
    assert result["ledgerScanComplete"] is False


def test_publish_ledger_forwards_safe_batch_size_to_source_aware_iterator(tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    ledger = ObservationLedger(ledger_path)
    ledger.append([_ledger_observation(f"fixture-{index}") for index in range(3)])

    result = publish_ledger(ledger_path, dry_run=True, batch_size=2)

    assert result["batches"] == 2
    assert result["rows"] == 3


def test_publish_ledger_rejects_batch_size_above_wire_limit(tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    ObservationLedger(ledger_path).append([_ledger_observation("fixture-1")])

    with pytest.raises(ValueError, match="batch_size"):
        publish_ledger(ledger_path, dry_run=True, batch_size=201)


def test_publish_ledger_blocks_unlicensed_news_before_d1_ingest(monkeypatch, tmp_path):
    ledger_path = tmp_path / "data" / "live" / "intelligence" / "observations.jsonl"
    snapshot_path = tmp_path / "data" / "live" / "current.json"
    ledger = ObservationLedger(ledger_path)
    news = observation_from_payload(
        entity_type="news",
        entity_id="https://news.example/story",
        kind="news_fact_candidate",
        payload={
            "title": "Marseille predicted XI v Strasbourg",
            "summary": "Expected lineups",
            "link": "https://news.example/story",
            "published_at": "2026-08-20T10:00:00+00:00",
        },
        source_name="Google News RSS",
        source_url="https://news.google.com/rss/search?q=Marseille",
        source_tier="reliable_media",
        observed_at="2026-08-20T11:00:00+00:00",
        published_at="2026-08-20T10:00:00+00:00",
        confidence=0.45,
        enters_model=False,
        model_exclusion_reason="news_fact_not_structured_model_feature",
        raw_hash="b" * 64,
    )
    ledger.append([news])
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps({
            "espn": {"fixtures": [{
                "id": "espn:1",
                "kickoff_at": "2026-08-20T19:00:00+00:00",
                "home_team": "Marseille",
                "away_team": "Strasbourg",
            }]},
        }),
        encoding="utf-8",
    )
    captured = []

    def fake_publish(payload, **_kwargs):
        captured.append(payload)
        return {"accepted": 1, "skippedDuplicate": 0, "sourceRunId": 7, "projections": {"news": 1}}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    with pytest.raises(
        ValueError,
        match="source_rights_unknown_provider:Google News RSS",
    ):
        publish_ledger(
            ledger_path,
            snapshot_path=snapshot_path,
            endpoint="https://matchline.example/api/ingest",
            token="secret",
            receipts_path=tmp_path / "receipts.jsonl",
            publication_epoch="production-v1",
        )
    assert captured == []


def test_publish_ledger_cursor_reads_only_new_tail_after_completed_scan(monkeypatch, tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    cursor = tmp_path / "cursor.json"
    ledger = ObservationLedger(ledger_path)

    def row(entity_id: str, digest: str) -> object:
        return observation_from_payload(
            entity_type="fixture",
            entity_id=entity_id,
            kind="fixture_schedule",
            payload={"kickoff_at": "2026-08-20T19:00:00+00:00"},
            source_name="OpenFootball",
            source_url="https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
            source_tier="reliable_public_provider",
            observed_at="2026-08-17T10:00:00+00:00",
            published_at="2026-08-17T10:00:00+00:00",
            confidence=0.98,
            enters_model=True,
            raw_hash=digest * 64,
        )

    ledger.append([row("espn:1", "a"), row("espn:2", "b")])
    captured = []

    def fake_publish(payload, **_kwargs):
        captured.append(payload)
        return {"accepted": len(payload["observations"]), "skippedDuplicate": 0, "sourceRunId": 8}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    first = publish_ledger(
        ledger_path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        cursor_path=cursor,
        publication_epoch="production-v1",
    )
    assert first["ledgerCursorSaved"] is True
    assert first["ledgerCursorUsed"] is False
    assert len(captured) == 1

    ledger.append([row("espn:3", "c")])
    second = publish_ledger(
        ledger_path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        cursor_path=cursor,
        publication_epoch="production-v1",
    )

    assert second["ledgerCursorUsed"] is True
    assert second["rows"] == 1
    assert len(captured) == 2
    assert [item["entityId"] for item in captured[-1]["observations"]] == ["espn:3"]


def test_publish_ledger_dry_run_reads_cursor_without_advancing_it(monkeypatch, tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    cursor = tmp_path / "cursor.json"
    ledger = ObservationLedger(ledger_path)

    def row(entity_id: str, digest: str) -> object:
        return observation_from_payload(
            entity_type="fixture",
            entity_id=entity_id,
            kind="fixture_schedule",
            payload={"kickoff_at": "2026-08-20T19:00:00+00:00"},
            source_name="OpenFootball",
            source_url="https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
            source_tier="reliable_public_provider",
            observed_at="2026-08-17T10:00:00+00:00",
            published_at="2026-08-17T10:00:00+00:00",
            confidence=0.98,
            enters_model=True,
            raw_hash=digest * 64,
        )

    ledger.append([row("espn:1", "a"), row("espn:2", "b")])

    def fake_publish(payload, **_kwargs):
        return {"accepted": len(payload["observations"]), "skippedDuplicate": 0, "sourceRunId": 9}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    first = publish_ledger(
        ledger_path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        cursor_path=cursor,
        publication_epoch="production-v1",
    )
    assert first["ledgerCursorSaved"] is True
    before = cursor.read_text(encoding="utf-8")

    ledger.append([row("espn:3", "c")])
    dry_run = publish_ledger(
        ledger_path,
        endpoint="https://matchline.example/api/ingest",
        dry_run=True,
        cursor_path=cursor,
        publication_epoch="production-v1",
        limit=1,
    )

    assert dry_run["ledgerCursorUsed"] is True
    assert dry_run["ledgerStartOffset"] > 0
    assert dry_run["rows"] == 1
    assert dry_run["ledgerScanComplete"] is False
    assert cursor.read_text(encoding="utf-8") == before


def _lineup_snapshot() -> dict:
    return {
        "as_of": "2026-08-17T01:00:00+00:00",
        "lineup_poll_diagnostics": {
            "premier-league": {
                "provider": "ESPN",
                "source_key": "espn_lineups",
                "source_status": "ok",
                "state": "awaiting_publication",
                "reason_code": "no_public_lineup_yet",
                "requested": True,
                "reference_at": "2026-08-17T01:00:00+00:00",
                "horizon_hours": 48,
                "candidate_count": 2,
                "invalid_kickoff_count": 0,
                "source_lineup_count": 0,
                "confirmed_lineup_count": 0,
                "model_eligible_lineup_count": 0,
                "error_count": 0,
                "next_fixture": {
                    "fixture_id": "espn:secret",
                    "home_team": "Invented FC",
                    "away_team": "Private United",
                    "kickoff_at": "2099-01-01T00:00:00+00:00",
                },
                "fixture_plans": [{"fixture_id": "espn:secret", "stage": "lineup"}],
            },
            "csl": {
                "provider": "Matchline lineup poll",
                "source_key": None,
                "source_status": "not_configured",
                "state": "not_configured",
                "reason_code": "no_permitted_adapter",
                "requested": False,
                "reference_at": "2026-08-17T01:00:00+00:00",
                "horizon_hours": 48,
                "candidate_count": 1,
                "invalid_kickoff_count": 0,
                "source_lineup_count": 0,
                "confirmed_lineup_count": 0,
                "model_eligible_lineup_count": 0,
                "error_count": 0,
            },
        },
    }


def test_build_lineup_diagnostics_payload_is_empty_observation_audit_run():
    payload = build_lineup_diagnostics_payload(_lineup_snapshot())
    assert payload["observations"] == []
    assert payload["sourceRun"]["sourceType"] == "lineup_poll_diagnostics"
    assert payload["sourceRun"]["status"] == "degraded"
    assert payload["sourceRun"]["recordsSeen"] == 2
    assert payload["sourceRun"]["errorCode"] == "lineup_coverage_incomplete"
    assert payload["sourceRun"]["metadata"]["modelUse"] == "display_only_audit_metadata"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "Invented FC" not in encoded
    assert "Private United" not in encoded
    assert "espn:secret" not in encoded
    assert "2099-01-01" not in encoded


def test_publish_lineup_diagnostics_is_idempotent(monkeypatch, tmp_path):
    snapshot = tmp_path / "current.json"
    snapshot.write_text(json.dumps(_lineup_snapshot()), encoding="utf-8")
    receipts = tmp_path / "lineup-receipts.jsonl"
    calls = []

    def fake_publish(payload, **_kwargs):
        calls.append(payload)
        return {"accepted": 0, "skippedDuplicate": 0, "sourceRunId": 12}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    first = publish_lineup_diagnostics(
        snapshot,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    second = publish_lineup_diagnostics(
        snapshot,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    assert first["requests"] == 1
    assert second["requests"] == 0
    assert second["receiptsSkipped"] == 1
    assert second["status"] == "already_published"
    assert len(calls) == 1
    receipt = json.loads(receipts.read_text(encoding="utf-8"))
    assert receipt["stream"] == "lineup_diagnostics"
    assert receipt["rowCount"] == 0


def _source_registry_snapshot() -> dict:
    return {
        "as_of": "2026-08-17T01:00:00+00:00",
        "source_registry": [
            {
                "id": "espn_schedule_summary",
                "name": "ESPN 赛程、赛果与事件摘要",
                "status": "enabled",
                "source_tier": "public_provider",
                "access_policy": "public_https_bounded_requests",
                "model_policy": "eligible_for_fixture_identity_and_causal_results",
                "runtime": {
                    "status": "fresh",
                    "record_count": 331,
                    "error_count": 0,
                    "retrieved_at": "2026-08-17T00:59:00+00:00",
                    "errors": [],
                    "fixture_join": {
                        "status": "exact",
                        "joined_count": 10,
                        "unmatched_count": 0,
                        "model_admission": "display_only",
                    },
                },
            },
            {
                "id": "fbref_public_stats",
                "name": "FBref 公共球队与球员统计",
                "status": "quarantined",
                "source_tier": "public_stats_candidate",
                "access_policy": "public_https_current_probe_403",
                "model_policy": "display_only_until_access_and_parser_are_verified",
                "runtime": {
                    "status": "quarantined",
                    "record_count": 0,
                    "error_count": 0,
                    "errors": [],
                },
            },
            {
                "id": "sofascore_prematch",
                "name": "SofaScore 赛前事件、伤停与阵容线索",
                "status": "enabled",
                "source_tier": "public_provider",
                "access_policy": "public_https_bounded_requests",
                "model_policy": "eligible_only_high_confidence_provider_facts",
                "runtime": {
                    "status": "unavailable",
                    "record_count": 0,
                    "error_count": 1,
                    "errors": [{"stage": "schedule", "error_code": "http_403", "error": "Forbidden"}],
                },
            },
        ],
    }


def test_build_source_health_payloads_preserves_policy_and_failure_boundary():
    payloads = build_source_health_payloads(_source_registry_snapshot())
    assert [payload["sourceRun"]["provider"] for payload in payloads] == ["ESPN", "FBref", "SofaScore"]
    assert payloads[0]["observations"] == []
    assert payloads[0]["sourceRun"]["status"] == "ok"
    assert payloads[0]["sourceRun"]["recordsSeen"] == 331
    assert payloads[1]["sourceRun"]["status"] == "quarantined"
    assert payloads[1]["sourceRun"]["errorCode"] == "source_quarantined"
    assert payloads[2]["sourceRun"]["status"] == "failed"
    assert payloads[2]["sourceRun"]["errorCode"] == "http_403"
    assert payloads[2]["sourceRun"]["metadata"]["modelUse"] == "display_only_audit_metadata"
    assert "fixtureJoin" not in payloads[0]["sourceRun"]["metadata"]


def test_build_source_health_payloads_preserves_match_directory_diagnostics():
    snapshot = {
        "as_of": "2026-08-20T02:00:00+00:00",
        "source_registry": [{
            "id": "laliga_official_match_directory",
            "name": "西甲官方公开比赛目录",
            "status": "enabled",
            "source_tier": "official",
            "access_policy": "public_https_bounded_no_redirect",
            "model_policy": "eligible_for_fixture_identity_and_official_page_discovery_no_lineup_inference",
            "runtime": {
                "status": "fresh",
                "record_count": 380,
                "match_discovery": {
                    "status": "fresh",
                    "matched_count": 2,
                    "page_count": 4,
                    "truncated": False,
                    "endpoint": "https://apim.laliga.com/public-service/api/v1/matches",
                    "raw_sha256": "b" * 64,
                    "records_seen": 380,
                },
                "error_count": 0,
                "errors": [],
            },
        }],
    }
    payload = build_source_health_payloads(snapshot)[0]
    runtime = payload["sourceRun"]["metadata"]["runtime"]
    assert payload["sourceRun"]["provider"] == "LaLiga official match directory"
    assert "matchDiscovery" not in runtime
    assert set(runtime) <= {
        "status",
        "retrievedAt",
        "recordCount",
        "extractedRecordCount",
        "errorCount",
        "errorCodes",
        "executionCounts",
    }


def test_build_source_health_payloads_preserves_crawl4ai_fanout_without_promoting_it():
    snapshot = {
        "as_of": "2026-08-20T02:00:00+00:00",
        "source_registry": [{
            "id": "crawl4ai_allowlisted_pages",
            "name": "Crawl4AI 抓取引擎",
            "role": "fetch_runtime",
            "fact_source": False,
            "adapter": "league_platform.live_sources.crawl4ai",
            "status": "degraded",
            "source_tier": "fetch_runtime",
            "access_policy": "explicit_allowlist_robots_fail_closed_same_host_redirect",
            "model_policy": "runtime_only_never_a_fact_source",
            "runtime": {
                "status": "degraded",
                "record_count": 2,
                "extracted_record_count": 12,
                "error_count": 1,
                "errors": [{"stage": "crawl", "error_code": "network_error", "error": "connection closed"}],
                "fanout": {
                    "capture_engine": "Crawl4AI",
                    "declared_source_count": 2,
                    "declared_source_ids": ["whoscored_public_pages", "laliga_public_pages"],
                    "pages_by_source": {"whoscored_public_pages": 1, "laliga_public_pages": 1},
                    "unidentified_page_count": 0,
                },
                "execution": {
                    "strategy": "bounded_cross_host_parallel_per_host_serial",
                    "host_group_count": 2,
                    "max_host_workers": 2,
                    "configured_source_count": 2,
                    "index_page_count": 2,
                    "detail_page_count": 3,
                    "follow_up_candidate_count": 12,
                    "follow_up_requested_count": 3,
                    "follow_up_rejected_count": 1,
                    "follow_up_capped_count": 8,
                    "follow_up_rejections": [{"source_id": "laliga_public_pages", "enters_model": False}],
                    "duration_ms": 321.9876,
                },
            },
        }],
    }
    payload = build_source_health_payloads(snapshot)[0]
    metadata = payload["sourceRun"]["metadata"]
    assert metadata["sourceRole"] == "fetch_runtime"
    assert metadata["factSource"] is False
    assert "fanout" not in metadata["runtime"]
    assert metadata["runtime"]["errorCodes"] == ["network_error"]
    assert metadata["runtime"]["executionCounts"] == {
        "hostGroupCount": 2,
        "maxHostWorkers": 2,
        "configuredSourceCount": 2,
        "indexPageCount": 2,
        "detailPageCount": 3,
        "followUpCandidateCount": 12,
        "followUpRequestedCount": 3,
        "followUpRejectedCount": 1,
        "followUpCappedCount": 8,
        "durationMs": 321.988,
    }


def test_prospective_gate_metadata_is_bounded_and_source_health_can_carry_it(tmp_path: Path):
    lock = tmp_path / "lock.json"
    evaluation = tmp_path / "evaluation.json"
    selection = tmp_path / "selection.json"
    strict = tmp_path / "strict.json"
    cycle = tmp_path / "cycle.json"
    lock.write_text(json.dumps({
        "status": "pending_prospective_window",
        "model_version_sha256": "a" * 64,
        "freeze_model_name": "locked-v1",
        "locked_at": "2026-08-17T00:00:00+00:00",
        "evaluation_window_started_at": "2026-08-17T00:00:01+00:00",
    }), encoding="utf-8")
    evaluation.write_text(json.dumps({
        "status": "pending_prospective_window",
        "scored_n": 4,
        "pending_n": 1,
        "result_conflicts": 0,
        "sample_requirements_met": False,
        "all_required_targets_scored": True,
        "prediction_freezes_verified": True,
    }), encoding="utf-8")
    selection.write_text(json.dumps({"gate_status": "blocked_selection_debt", "production_eligible": False}), encoding="utf-8")
    strict.write_text(json.dumps({"overall": {
        "market_gate": "research_only_underperforms_market",
        "freeze_stage_gate": "partial_with_explicit_blocks",
        "model_selection_gate": "blocked_selection_debt",
    }}), encoding="utf-8")
    cycle.write_text(json.dumps({"status": "pending_prospective_window", "sync": {"as_of": "2026-08-17T00:00:02+00:00"}}), encoding="utf-8")

    metadata = build_prospective_gate_metadata(
        lock_path=lock,
        evaluation_path=evaluation,
        selection_path=selection,
        strict_report_path=strict,
        cycle_path=cycle,
    )
    assert metadata is not None
    assert metadata["modelVersionSha256"] == "a" * 64
    assert metadata["scoredN"] == 4
    assert metadata["modelUse"] == "display_only_audit_metadata"
    payload = build_source_health_payloads(_source_registry_snapshot(), prospective_gate_metadata=metadata)[0]
    assert "prospectiveGate" not in payload["sourceRun"]["metadata"]


def test_source_health_error_overrides_idle_registry_status():
    snapshot = {
        "as_of": "2026-08-17T01:00:00+00:00",
        "source_registry": [{
            "id": "official_league_lineups",
            "name": "官方首发",
            "status": "enabled",
            "runtime": {
                "status": "not_requested",
                "record_count": 0,
                "error_count": 1,
                "errors": [{"error": "HTTP Error 403: Forbidden"}],
            },
        }],
    }
    payload = build_source_health_payloads(snapshot)[0]
    assert payload["sourceRun"]["status"] == "degraded"
    assert payload["sourceRun"]["errorCode"] == "http_403"


def test_publish_source_health_diagnostics_isolated_and_idempotent(monkeypatch, tmp_path):
    snapshot = tmp_path / "current.json"
    snapshot.write_text(json.dumps(_source_registry_snapshot()), encoding="utf-8")
    receipts = tmp_path / "source-health-receipts.jsonl"
    calls = []

    def fake_publish(payload, **_kwargs):
        calls.append(payload["sourceRun"]["provider"])
        if payload["sourceRun"]["provider"] == "FBref":
            raise RuntimeError("isolated source-run failure")
        return {"accepted": 0, "skippedDuplicate": 0, "sourceRunId": len(calls)}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    first = publish_source_health_diagnostics(
        snapshot,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    second = publish_source_health_diagnostics(
        snapshot,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    assert first["sources"] == 3
    assert first["published"] == 2
    assert first["failed"] == 1
    assert first["status"] == "partial"
    assert second["published"] == 0
    assert second["receiptsSkipped"] == 2
    assert second["failed"] == 1
    assert second["status"] == "partial"
    assert calls == ["ESPN", "FBref", "SofaScore", "FBref"]


def test_publish_response_keeps_bounded_entity_resolution_in_receipt(monkeypatch, tmp_path):
    ledger_path = tmp_path / "observations.jsonl"
    row = _row() | {
        "source_name": "OpenFootball",
        "source_url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        "source_tier": "reliable_public_provider",
    }
    ledger_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"

    def publish(_payload, **_kwargs):
        return {
            "accepted": 1,
            "skippedDuplicate": 0,
            "sourceRunId": 12,
            "entityResolution": {"alreadyBound": 2, "resolved": 1, "ambiguous": 0, "unresolved": 4, "ignored": 999},
            "projections": {
                "weather": 3,
                "odds": 2,
                "news": 1,
                "availability": 0,
                "outcome": 0,
                "skipped": 4,
                "skippedByReason": {"fixture_foreign_key_missing": 4, "ignored": 99999},
                "unexpected": 999,
            },
        }

    monkeypatch.setattr(publisher, "publish_payload", publish)
    result = publish_ledger(
        ledger_path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    assert result["entityResolution"] == {"alreadyBound": 2, "resolved": 1, "ambiguous": 0, "unresolved": 4}
    assert result["projections"] == {
        "weather": 3,
        "odds": 2,
        "news": 1,
        "availability": 0,
        "outcome": 0,
        "skipped": 4,
        "skippedByReason": {"fixture_foreign_key_missing": 4},
    }
    receipt = json.loads(receipts.read_text(encoding="utf-8"))
    assert receipt["response"]["entityResolution"] == {"alreadyBound": 2, "resolved": 1, "ambiguous": 0, "unresolved": 4}
    assert receipt["response"]["projections"] == result["projections"]


def test_publish_requires_https_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        publish_payload({}, endpoint="http://localhost:3000/api/ingest", token="secret")


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.value).encode("utf-8")


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def open(self, _request, timeout):
        assert timeout == 3
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)


def test_publish_retries_503_then_returns_success(monkeypatch):
    error = urllib.error.HTTPError(
        "https://matchline.example/api/ingest",
        503,
        "busy",
        {"Retry-After": "0"},
        io.BytesIO(b"temporary"),
    )
    opener = _Opener([error, {"accepted": 1, "skippedDuplicate": 0}])
    monkeypatch.setattr(publisher.urllib.request, "build_opener", lambda _handler: opener)
    sleeps = []

    result = publish_payload(
        {"observations": []},
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        timeout=3,
        max_retries=2,
        sleep=sleeps.append,
        producer_attestation=_attestation_context(),
    )

    assert result["accepted"] == 1
    assert sleeps == [0.0]


def test_publish_adds_optional_sites_dispatch_header(monkeypatch):
    captured = []

    class CaptureOpener:
        def open(self, request, timeout):
            captured.append(request)
            assert timeout == 3
            return _Response({"accepted": 1})

    monkeypatch.setenv("MATCHLINE_SITES_BYPASS_TOKEN", "dispatch-token")
    monkeypatch.setattr(publisher.urllib.request, "build_opener", lambda _handler: CaptureOpener())

    result = publish_payload(
        {"observations": []},
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        timeout=3,
        max_retries=0,
        producer_attestation=_attestation_context(),
    )

    assert result == {"accepted": 1}
    assert captured[0].get_header("Oai-sites-authorization") == "Bearer dispatch-token"
    assert captured[0].get_header("User-agent").endswith("MatchlinePublisher/1.0")


def test_publish_rejects_sites_dispatch_header_injection(monkeypatch):
    monkeypatch.setenv("MATCHLINE_SITES_BYPASS_TOKEN", "dispatch\ntoken")
    with pytest.raises(ValueError, match="single-line"):
        publish_payload(
            {"observations": []},
            endpoint="https://matchline.example/api/ingest",
            token="secret",
            max_retries=0,
        )


def test_publish_rejects_user_agent_header_injection(monkeypatch):
    monkeypatch.setenv("MATCHLINE_PUBLISH_USER_AGENT", "Matchline\nPublisher")
    with pytest.raises(ValueError, match="single-line"):
        publish_payload(
            {"observations": []},
            endpoint="https://matchline.example/api/ingest",
            token="secret",
            max_retries=0,
        )


def test_publish_does_not_retry_auth_failure(monkeypatch):
    error = urllib.error.HTTPError(
        "https://matchline.example/api/ingest",
        401,
        "unauthorized",
        {},
        io.BytesIO(b"unauthorized"),
    )
    opener = _Opener([error])
    monkeypatch.setattr(publisher.urllib.request, "build_opener", lambda _handler: opener)
    sleeps = []

    with pytest.raises(RuntimeError, match="HTTP 401"):
        publish_payload(
            {"observations": []},
            endpoint="https://matchline.example/api/ingest",
            token="secret",
            timeout=3,
            max_retries=3,
            sleep=sleeps.append,
            producer_attestation=_attestation_context(),
        )
    assert sleeps == []


def test_publish_signs_exact_body_destination_and_independent_evidence(monkeypatch):
    captured = []

    class CaptureOpener:
        def open(self, request, timeout):
            captured.append(request)
            assert timeout == 3
            return _Response({"accepted": 1})

    monkeypatch.setattr(
        publisher.urllib.request,
        "build_opener",
        lambda _handler: CaptureOpener(),
    )
    context = _attestation_context()
    publish_payload(
        {"observations": [], "sourceRun": {"provider": "OpenFootball"}},
        endpoint="https://MATCHLINE.example:443/api/ingest",
        token="secret",
        timeout=3,
        max_retries=0,
        producer_attestation=context,
    )

    request = captured[0]
    encoded = request.get_header("X-matchline-producer-attestation")
    signature = request.get_header("X-matchline-producer-signature")
    assert encoded is not None
    padding = "=" * (-len(encoded) % 4)
    canonical = base64.urlsafe_b64decode(encoded + padding)
    attestation = json.loads(canonical)
    assert set(attestation) == {
        "schemaVersion",
        "producer",
        "keyId",
        "issuedAt",
        "expiresAt",
        "stream",
        "rightsUseCase",
        "sourcePolicyIds",
        "publicationEpoch",
        "destination",
        "bodySha256",
        "maturityReceiptRawSha256",
        "liveSnapshotRawSha256",
    }
    assert attestation["schemaVersion"] == "matchline.producer_attestation.v1"
    assert attestation["producer"] == {
        "name": "matchline-python-publisher",
        "version": "v260",
    }
    assert attestation["keyId"] == "test-key"
    issued = datetime.fromisoformat(attestation["issuedAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(attestation["expiresAt"].replace("Z", "+00:00"))
    assert (expires - issued).total_seconds() == 300
    assert attestation["stream"] == "intelligence_ingest"
    assert attestation["rightsUseCase"] == "redistribution"
    assert attestation["sourcePolicyIds"] == ["openfootball_current"]
    assert attestation["destination"] == "https://matchline.example/api/ingest"
    assert attestation["bodySha256"] == hashlib.sha256(request.data).hexdigest()
    assert attestation["maturityReceiptRawSha256"] == "d" * 64
    assert attestation["liveSnapshotRawSha256"] == "e" * 64
    assert signature == "v1=" + hmac.new(
        context.signing_secret,
        canonical,
        hashlib.sha256,
    ).hexdigest()


def test_python_attestation_is_accepted_by_sites_typescript_verifier() -> None:
    node = shutil.which("node")
    sites_root = Path(__file__).resolve().parents[1] / "matchline_sites"
    if node is None or not (sites_root / "node_modules" / "tsx").exists():
        pytest.skip("Sites Node/tsx runtime is unavailable")
    body = publisher._canonical_json(
        {
            "fixtureId": 12,
            "featureSnapshotId": 31,
            "provenance": {
                "materialSources": [
                    {
                        "name": "OpenFootball",
                        "url": (
                            "https://raw.githubusercontent.com/openfootball/"
                            "football.json/master/2026-27/en.1.json"
                        ),
                    }
                ]
            },
        }
    ).encode("utf-8")
    endpoint = "https://predict.example/api/forecast"
    issued_at = datetime.fromisoformat("2026-08-25T10:00:00+00:00")
    context = publisher.ProducerAttestationContext(
        key_id="publisher-key-1",
        signing_secret=b"test-producer-signing-secret-at-least-32-bytes",
        stream="forecast_prediction",
        rights_use_case="model_input",
        source_policy_ids=("openfootball_current",),
        publication_epoch="epoch-2026-08-25",
        maturity_receipt_raw_sha256="a" * 64,
        live_snapshot_raw_sha256="b" * 64,
    )
    headers = publisher._producer_attestation_headers(
        body,
        endpoint=endpoint,
        context=context,
        issued_at=issued_at,
    )
    node_program = """
import fs from "node:fs";
import { verifyProducerAttestation } from "./app/api/d1-source-rights-policy.ts";
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const decision = await verifyProducerAttestation({
  body: input.body,
  destination: input.endpoint,
  env: {
    MATCHLINE_PRODUCER_KEY_ID: input.keyId,
    MATCHLINE_PRODUCER_SIGNING_SECRET: input.secret,
  },
  expectedStream: "forecast_prediction",
  expectedRightsUseCase: "model_input",
  expectedSourcePolicyIds: ["openfootball_current"],
  headers: new Headers(input.headers),
  now: Date.parse("2026-08-25T10:01:00.000Z"),
});
process.stdout.write(JSON.stringify(decision));
"""
    completed = subprocess.run(
        [node, "--import", "tsx", "--input-type=module", "-e", node_program],
        cwd=sites_root,
        input=json.dumps(
            {
                "body": body.decode("utf-8"),
                "endpoint": endpoint,
                "keyId": context.key_id,
                "secret": context.signing_secret.decode("utf-8"),
                "headers": headers,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    decision = json.loads(completed.stdout)
    assert decision["allowed"] is True
    assert decision["keyId"] == "publisher-key-1"
    assert decision["producer"] == {
        "name": "matchline-python-publisher",
        "version": "v260",
    }
    assert decision["issuedAt"] == "2026-08-25T10:00:00.000Z"
    assert decision["expiresAt"] == "2026-08-25T10:05:00.000Z"


def _ledger_observation(entity_id: str):
    return observation_from_payload(
        entity_type="fixture",
        entity_id=entity_id,
        kind="fixture_schedule",
        payload={"kickoff_at": "2026-08-12T11:00:00+00:00"},
        source_name="OpenFootball",
        source_url="https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        source_tier="reliable_public_provider",
        observed_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        published_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        confidence=0.9,
        enters_model=True,
    )


def test_iter_ledger_batches_is_bounded(tmp_path):
    path = tmp_path / "intelligence.jsonl"
    ledger = ObservationLedger(path)
    ledger.append([_ledger_observation(f"fixture-{index}") for index in range(201)])

    batches = list(iter_ledger_batches(ObservationLedger(path)))

    assert [len(batch) for batch in batches] == [200, 1]


def test_iter_ledger_batches_keeps_sources_separate(tmp_path):
    path = tmp_path / "intelligence.jsonl"
    ledger = ObservationLedger(path)
    rows = [_ledger_observation("espn-1"), _ledger_observation("understat-1")]
    rows[1] = observation_from_payload(
        entity_type="fixture",
        entity_id="understat-1",
        kind="team_form_xg",
        payload={"xg": 1.2},
        source_name="Understat",
        source_url="https://understat.com/league/EPL",
        source_tier="authorized",
        observed_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        published_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        confidence=0.9,
        enters_model=True,
    )
    ledger.append(rows)
    batches = list(iter_ledger_batches(ObservationLedger(path), batch_size=10))
    assert [row[0]["source_name"] for row in batches] == ["OpenFootball", "Understat"]


def test_publish_ledger_resumes_from_completed_receipt(monkeypatch, tmp_path):
    path = tmp_path / "intelligence.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    ObservationLedger(path).append([_ledger_observation("fixture-1")])
    calls = []

    def fake_publish(payload, **_kwargs):
        calls.append(payload)
        return {"accepted": len(payload["observations"]), "skippedDuplicate": 0, "sourceRunId": 7}

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    first = publish_ledger(
        path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )
    second = publish_ledger(
        path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert first["accepted"] == 1
    assert first["requests"] == 1
    assert second["requests"] == 0
    assert second["receiptsSkipped"] == 1
    assert len(calls) == 1
    assert len(receipts.read_text(encoding="utf-8").splitlines()) == 1


def test_intelligence_receipt_and_cursor_are_scoped_to_destination_and_epoch(monkeypatch, tmp_path):
    path = tmp_path / "intelligence.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    cursor = tmp_path / "cursor.json"
    ObservationLedger(path).append([_ledger_observation("fixture-1")])
    calls: list[str] = []

    def fake_publish(payload, *, endpoint, **_kwargs):
        calls.append(endpoint)
        return {
            "accepted": len(payload["observations"]),
            "skippedDuplicate": 0,
            "sourceRunId": len(calls),
        }

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    for endpoint, epoch in (
        ("https://staging.example/api/ingest", "deploy-1"),
        ("https://production.example/api/ingest", "deploy-1"),
        ("https://production.example/api/ingest", "deploy-2"),
    ):
        result = publish_ledger(
            path,
            endpoint=endpoint,
            token="secret",
            receipts_path=receipts,
            cursor_path=cursor,
            publication_epoch=epoch,
        )
        assert result["requests"] == 1
    assert calls == [
        "https://staging.example/api/ingest",
        "https://production.example/api/ingest",
        "https://production.example/api/ingest",
    ]


def test_empty_intelligence_response_never_writes_completed_receipt(monkeypatch, tmp_path):
    path = tmp_path / "intelligence.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    ObservationLedger(path).append([_ledger_observation("fixture-1")])
    monkeypatch.setattr(publisher, "publish_payload", lambda *_args, **_kwargs: {})

    result = publish_ledger(
        path,
        endpoint="https://production.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert not receipts.exists()


def test_missing_maturity_context_fails_before_intelligence_request(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "intelligence.jsonl"
    ObservationLedger(path).append([_ledger_observation("fixture-1")])
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
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_typed_projection_failure_never_writes_completed_receipt(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "intelligence.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    ObservationLedger(path).append([_ledger_observation("fixture-1")])
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda *_args, **_kwargs: {
            "sourceRunId": 7,
            "accepted": 1,
            "skippedDuplicate": 0,
            "projections": {
                "skipped": 1,
                "skippedByReason": {"typed_projection_write_failed": 1},
            },
        },
    )

    result = publish_ledger(
        path,
        endpoint="https://production.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert not receipts.exists()


def test_mixed_rights_ledger_is_blocked_before_any_request(monkeypatch, tmp_path):
    path = tmp_path / "intelligence.jsonl"
    allowed = _ledger_observation("openfootball-1")
    blocked = observation_from_payload(
        entity_type="fixture",
        entity_id="espn-1",
        kind="fixture_schedule",
        payload={"kickoff_at": "2026-08-12T11:00:00+00:00"},
        source_name="ESPN",
        source_url="https://site.api.espn.com/apis/site/v2/sports/soccer/fixtures",
        source_tier="authorized",
        observed_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        published_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        confidence=0.9,
        enters_model=False,
    )
    ObservationLedger(path).append([allowed, blocked])
    calls: list[dict] = []
    monkeypatch.setattr(publisher, "publish_payload", lambda payload, **_kwargs: calls.append(payload))

    with pytest.raises(ValueError, match="source_rights_blocked:espn_schedule_summary:redistribution"):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_publish_ledger_blocks_relabelled_crawl4ai_page_before_any_request(
    monkeypatch,
    tmp_path,
):
    """A Crawl4AI page cannot be relabelled as an OpenFootball observation."""

    path = tmp_path / "observations.jsonl"
    page = observation_from_payload(
        entity_type="source_page",
        entity_id="https://raw.githubusercontent.com/openfootball/deutschland/master/2024-25/1-bundesliga.txt",
        kind="crawl4ai_page",
        payload={
            "source": "OpenFootball",
            "source_id": "public_news",
            "fact_source_id": "public_news",
            "capture_engine": "Crawl4AI",
            "capture_role": "fetch_runtime",
            "source_role": "fact_source_capture",
            "test_only": True,
        },
        source_name="OpenFootball",
        source_url="https://raw.githubusercontent.com/openfootball/deutschland/master/2024-25/1-bundesliga.txt",
        source_tier="reliable_public_provider",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.35,
        enters_model=False,
        model_exclusion_reason="unstructured_browser_capture_requires_source_parser",
        raw_hash="f" * 64,
    )
    ObservationLedger(path).append([page])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="crawl4ai"):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("operator_name", "source_id"),
    _CRAWL4AI_OPERATOR_SOURCE_ALIASES,
    ids=[source_id.value for _operator_name, source_id in _CRAWL4AI_OPERATOR_SOURCE_ALIASES],
)
def test_crawl4ai_operator_names_resolve_to_exact_source_policy_ids(
    operator_name: str,
    source_id: SourceId,
) -> None:
    """Config names are aliases only; the central policy ID stays exact."""

    assert publisher._SOURCE_NAME_TO_POLICY_ID[operator_name] is source_id


def test_publish_ledger_long_crawl4ai_operator_name_reaches_central_rights_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Alias/provenance fixes must not turn a blocked source into an allow."""

    path = tmp_path / "observations.jsonl"
    row = observation_from_payload(
        entity_type="source_page",
        entity_id="https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
        kind="crawl4ai_page",
        payload={
            "source": "WhoScored public match centre operator source",
            "source_id": "whoscored_public_pages",
            "fact_source_id": "whoscored_public_pages",
            "provider": "Crawl4AI",
            "provider_role": "execution_layer",
            "capture_engine": "Crawl4AI",
            "capture_role": "fetch_runtime",
            "source_role": "fact_source_capture",
            "robots": {"allowed": True},
            "runtime_evidence": True,
            "network_opened": True,
            "test_only": False,
            "model_eligible": False,
            "policy": {
                "runtime_evidence": True,
                "network_opened": True,
                "test_only": False,
            },
        },
        source_name="WhoScored public match centre operator source",
        source_url="https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
        source_tier="reliable_public_provider",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.35,
        enters_model=False,
        raw_hash="f" * 64,
    )
    ObservationLedger(path).append([row])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(
        ValueError,
        match="source_rights_blocked:whoscored_public_pages:redistribution",
    ):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


@pytest.mark.parametrize(
    "execution_marker",
    [
        pytest.param(
            {"provider_role": "execution_layer", "runtime_evidence": True},
            id="top-level-provider-role",
        ),
        pytest.param(
            {
                "metadata": {
                    "provider_role": "execution_layer",
                    "runtime_evidence": True,
                }
            },
            id="nested-metadata-provider-role",
        ),
    ],
)
def test_publish_ledger_blocks_execution_layer_relabel_without_capture_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_marker: dict,
) -> None:
    """An execution-layer marker cannot be hidden by a generic observation kind."""

    path = tmp_path / "observations.jsonl"
    payload = {
        "source": "OpenFootball",
        "source_id": "openfootball_current",
        "fact_source_id": "openfootball_current",
        "robots": {"allowed": True},
        "model_eligible": False,
        **execution_marker,
    }
    row = observation_from_payload(
        entity_type="fixture",
        entity_id="execution-layer-relabel",
        # Deliberately omit the crawl4ai_* kind and all capture_* markers.
        kind="fixture_schedule",
        payload=payload,
        source_name="OpenFootball",
        source_url="https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        source_tier="reliable_public_provider",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.35,
        enters_model=False,
        raw_hash="f" * 64,
    )
    ObservationLedger(path).append([row])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="crawl4ai_openfootball_relabel_blocked"):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


@pytest.mark.parametrize(
    "nested_marker",
    [
        pytest.param(
            {"metadata": {"runtime": {"provider_role": "execution_layer"}}},
            id="metadata-runtime-provider-role",
        ),
        pytest.param(
            {"policy": {"provider_role": "execution_layer"}},
            id="policy-provider-role",
        ),
    ],
)
def test_deep_execution_layer_marker_cannot_hide_from_strict_lane(
    nested_marker: dict,
) -> None:
    """Nested runtime envelopes still identify a Crawl4AI observation."""

    row = observation_from_payload(
        entity_type="fixture",
        entity_id="deep-execution-layer-relabel",
        kind="fixture_schedule",
        payload={
            "source_id": "openfootball_current",
            "fact_source_id": "openfootball_current",
            "robots": {"allowed": True},
            "runtime_evidence": True,
            "model_eligible": False,
            **nested_marker,
        },
        source_name="OpenFootball",
        source_url="https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        source_tier="reliable_public_provider",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.35,
        enters_model=False,
        raw_hash="e" * 64,
    )

    assert publisher._is_crawl4ai_observation(row) is True
    assert publisher._observation_rights_reason(row) == (
        "crawl4ai_openfootball_relabel_blocked"
    )


@pytest.mark.parametrize(
    ("source_name", "source_id", "source_url", "runtime_evidence"),
    [
        pytest.param(
            source_name,
            source_id,
            source_url,
            runtime_evidence,
            id=f"{source_id}-{label}",
        )
        for source_name, source_id, source_url in _RUNTIME_EVIDENCE_SOURCE_CASES
        for label, runtime_evidence in (
            ("missing", _MISSING_RUNTIME_EVIDENCE),
            ("null", None),
            ("string", "true"),
            ("false", False),
        )
    ],
)
def test_publish_ledger_requires_explicit_boolean_runtime_evidence_before_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_name: str,
    source_id: str,
    source_url: str,
    runtime_evidence: object,
) -> None:
    """Execution-layer rows require an actual boolean runtime receipt."""

    path = tmp_path / "observations.jsonl"
    payload: dict[str, object] = {
        "source": source_name,
        "source_id": source_id,
        "fact_source_id": source_id,
        "robots": {"allowed": True},
        "model_eligible": False,
        "provider_role": "execution_layer",
    }
    if runtime_evidence is not _MISSING_RUNTIME_EVIDENCE:
        payload["runtime_evidence"] = runtime_evidence
    row = observation_from_payload(
        entity_type="fixture",
        entity_id=f"execution-layer-{source_id}",
        # The execution marker is intentionally independent of the kind.
        kind="fixture_schedule",
        payload=payload,
        source_name=source_name,
        source_url=source_url,
        source_tier="authorized",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.35,
        enters_model=False,
        raw_hash="e" * 64,
    )
    ObservationLedger(path).append([row])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="crawl4ai_runtime_evidence_missing"):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("source_name", "source_id", "source_url"),
    _RUNTIME_EVIDENCE_SOURCE_CASES,
    ids=[source_id for _source_name, source_id, _source_url in _RUNTIME_EVIDENCE_SOURCE_CASES],
)
def test_publish_ledger_keeps_legal_source_identity_without_execution_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_name: str,
    source_id: str,
    source_url: str,
) -> None:
    """A legal ordinary row does not enter the Crawl4AI runtime lane."""

    path = tmp_path / "observations.jsonl"
    row = observation_from_payload(
        entity_type="fixture",
        entity_id=f"ordinary-{source_id}",
        kind="fixture_schedule",
        payload={
            "source_id": source_id,
            "fact_source_id": source_id,
            # This field is ignored without an execution/capture marker.
            "runtime_evidence": False,
        },
        source_name=source_name,
        source_url=source_url,
        source_tier="authorized",
        observed_at="2026-08-30T10:00:00+00:00",
        published_at="2026-08-30T10:00:00+00:00",
        confidence=0.8,
        enters_model=False,
        raw_hash="d" * 64,
    )
    ObservationLedger(path).append([row])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload)
        or {
            "accepted": len(payload["observations"]),
            "skippedDuplicate": 0,
            "sourceRunId": 1,
        },
    )

    result = publish_ledger(
        path,
        endpoint="https://production.example/api/ingest",
        token="secret",
        publication_epoch="production-v1",
    )

    assert result["status"] == "ok"
    assert result["requests"] == 1
    assert result["accepted"] == 1
    assert len(calls) == 1


def test_publish_ledger_blocks_openfootball_row_not_bound_to_raw_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid global admission cannot authorize an unrelated ledger row."""

    source_id = "openfootball:football.json:2026-27:en.1"
    source_config = OPENFOOTBALL_SOURCES[source_id]
    retrieved_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
    raw_payload = (
        b'{"matches":[{"date":"2026-08-30","time":"16:00",'
        b'"team1":"Arsenal FC","team2":"Coventry City FC"}]}'
    )
    archive_root = tmp_path / "openfootball-raw"
    OpenFootballRawArchive(archive_root).store(
        OpenFootballRawObservation(
            source_id=source_id,
            url=source_config["url"],
            retrieved_at=retrieved_at,
            payload=raw_payload,
            source_format=source_config["format"],
            competition_id=source_config["competition_id"],
            season=source_config["season"],
            timezone_name=source_config["timezone"],
            license="CC0-1.0",
        )
    )
    verified = load_verified_openfootball_archive(
        archive_root,
        source_ids=[source_id],
        observed_before=retrieved_at,
    )
    admission = {
        key: verified[key]
        for key in (
            "schema_version",
            "observed_before",
            "source_ids",
            "selected_records",
            "admission_sha256",
            "source_manifest_sha256",
            "parser_contract_sha256",
            "rows_sha256",
        )
    }
    admission.update(
        {
            "status": "verified_current_raw",
            "policy_version": "v260",
            "row_count": len(verified["rows"]),
        }
    )
    snapshot_path = tmp_path / "current.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "as_of": retrieved_at.isoformat(),
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "raw_archive_admission": admission,
                    "fixtures": verified["rows"],
                },
            }
        ),
        encoding="utf-8",
    )

    forged = observation_from_payload(
        entity_type="fixture",
        entity_id="forged-third-party",
        kind="fixture_schedule",
        payload={
            "kickoff_at": "2026-08-30T16:00:00+00:00",
            "third_party": "forged",
        },
        source_name="OpenFootball",
        source_url=source_config["url"],
        source_tier="reliable_public_provider",
        observed_at=retrieved_at,
        published_at=retrieved_at,
        confidence=0.2,
        enters_model=False,
        raw_hash="f" * 64,
    )
    ledger_path = tmp_path / "intelligence.jsonl"
    ObservationLedger(ledger_path).append([forged])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload)
        or {
            "accepted": len(payload["observations"]),
            "skippedDuplicate": 0,
            "sourceRunId": 1,
        },
    )

    with pytest.raises(ValueError, match="openfootball_ledger_row_unbound"):
        publish_ledger(
            ledger_path,
            snapshot_path=snapshot_path,
            openfootball_raw_archive_dir=archive_root,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_openfootball_ledger_requires_raw_producer_receipt_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.jsonl"
    ObservationLedger(path).append([_ledger_observation("openfootball-1")])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )
    monkeypatch.setattr(
        publisher,
        "require_raw_source_producer_receipt",
        _REAL_REQUIRE_RAW_SOURCE_PRODUCER_RECEIPT,
    )

    with pytest.raises(ValueError, match="raw_producer_receipt_required"):
        publish_ledger(
            path,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_openfootball_ledger_replays_snapshot_admission_before_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.jsonl"
    snapshot = tmp_path / "current.json"
    archive = tmp_path / "openfootball-raw"
    snapshot.write_text(json.dumps({"schema_version": "live.v260"}), encoding="utf-8")
    ObservationLedger(path).append([_ledger_observation("openfootball-1")])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "require_raw_source_producer_receipt",
        _REAL_REQUIRE_RAW_SOURCE_PRODUCER_RECEIPT,
    )
    monkeypatch.setattr(
        publisher,
        "require_openfootball_ledger_raw_admission",
        lambda *_args, **_kwargs: "verified_current_raw:" + "a" * 64,
    )
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload)
        or {
            "accepted": len(payload["observations"]),
            "skippedDuplicate": 0,
            "sourceRunId": 1,
        },
    )

    result = publish_ledger(
        path,
        snapshot_path=snapshot,
        openfootball_raw_archive_dir=archive,
        endpoint="https://production.example/api/ingest",
        token="secret",
        publication_epoch="production-v1",
    )

    assert result["rawProvenanceStatus"] == "verified_current_raw:" + "a" * 64
    assert len(calls) == 1


def test_openfootball_ledger_admission_failure_blocks_before_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.jsonl"
    snapshot = tmp_path / "current.json"
    archive = tmp_path / "openfootball-raw"
    snapshot.write_text(json.dumps({"schema_version": "live.v260"}), encoding="utf-8")
    ObservationLedger(path).append([_ledger_observation("openfootball-1")])
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "require_raw_source_producer_receipt",
        _REAL_REQUIRE_RAW_SOURCE_PRODUCER_RECEIPT,
    )
    monkeypatch.setattr(
        publisher,
        "require_openfootball_ledger_raw_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("raw_producer_receipt_invalid:tampered")
        ),
    )
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="raw_producer_receipt_invalid:tampered"):
        publish_ledger(
            path,
            snapshot_path=snapshot,
            openfootball_raw_archive_dir=archive,
            endpoint="https://production.example/api/ingest",
            token="secret",
            publication_epoch="production-v1",
        )

    assert calls == []


def test_publish_ledger_keeps_legacy_receipt_batching(monkeypatch, tmp_path):
    path = tmp_path / "intelligence.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    first = _ledger_observation("fixture-1")
    second = _ledger_observation("fixture-2")
    ObservationLedger(path).append([first, second])
    calls = []

    def fake_publish(payload, **_kwargs):
        calls.append(payload)
        return {
            "accepted": len(payload["observations"]),
            "skippedDuplicate": 0,
            "sourceRunId": 7,
        }

    monkeypatch.setattr(publisher, "publish_payload", fake_publish)
    legacy = publish_ledger(
        path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        group_by_source=False,
        publication_epoch="production-v1",
    )
    resumed = publish_ledger(
        path,
        endpoint="https://matchline.example/api/ingest",
        token="secret",
        receipts_path=receipts,
        publication_epoch="production-v1",
    )

    assert legacy["batchingMode"] == "legacy"
    assert resumed["batchingMode"] == "legacy"
    assert resumed["requests"] == 0
    assert len(calls) == 1


def test_publish_ledger_reports_malformed_history_without_calling_it_success(tmp_path, monkeypatch):
    path = tmp_path / "intelligence.jsonl"
    item = _ledger_observation("fixture-1")
    path.write_text(
        json.dumps(item.as_dict(), ensure_ascii=False) + "\n{bad-json\n",
        encoding="utf-8",
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        publisher,
        "publish_payload",
        lambda payload, **_kwargs: calls.append(payload),
    )

    with pytest.raises(ValueError, match="ledger_preflight_integrity_failed"):
        publish_ledger(
            path,
            endpoint="https://matchline.example/api/ingest",
            token="secret",
            receipts_path=tmp_path / "receipts.jsonl",
            publication_epoch="production-v1",
        )
    assert calls == []
