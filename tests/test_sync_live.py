from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

import league_platform.sync_live as sync_live
import pytest

from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_SOURCES,
    fetch_openfootball_current as fetch_openfootball_current_from_transport,
)
from league_platform.source_rights import SourceId, UseCase, decide_source_rights

from league_platform.sync_live import (
    _blocked_espn_sections,
    _blocked_current_sections,
    _lineup_poll_diagnostics,
    _fetch_secondary_official_lineups,
    _official_lineup_poll_plan,
    _premier_league_matchweeks_for_horizon,
    _load_recent_fixture_source_snapshot,
    _load_recent_oddstorm_snapshot,
    _prepare_geocoding_requests,
    _reuse_recent_oddstorm_snapshot,
    _reuse_recent_fixture_source,
    _sofascore_window,
    _attach_wikidata_venues,
    _wikidata_unavailable_section,
)


PREMIER_LEAGUE_SOURCE_ID = "openfootball:football.json:2026-27:en.1"
CHAMPIONSHIP_SOURCE_ID = "openfootball:football.json:2026-27:en.2"
HISTORICAL_OPENFOOTBALL_SOURCE_ID = "openfootball:england:2025-26:1-premierleague"


def test_wikidata_venue_join_is_exact_and_keeps_nested_source_identity():
    feed = {
        "fixtures": [
            {"id": "openfootball:premier-league:one", "home_team": "Arsenal"},
            {"id": "openfootball:premier-league:two", "home_team": "Other"},
        ]
    }
    source = {
        "name": "Wikidata structured data",
        "source_id": "wikidata_entities",
        "url": "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q163995",
        "retrieved_at": "2026-08-25T00:00:00+00:00",
        "raw_sha256": "a" * 64,
        "license": "CC0-1.0",
        "license_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "attribution_required": False,
    }
    _attach_wikidata_venues(
        feed,
        {
            "venues": [
                {
                    "fixture_id": "openfootball:premier-league:one",
                    "venue": {
                        "name": "Emirates Stadium",
                        "wikidata_id": "Q163995",
                        "latitude": 51.555,
                        "longitude": -0.1083333333,
                        "source": source,
                    },
                },
                {"fixture_id": "unknown", "venue": {"name": "must not attach"}},
            ]
        },
    )
    assert feed["fixtures"][0]["venue"]["wikidata_id"] == "Q163995"
    assert feed["fixtures"][0]["field_sources"]["venue"]["source_id"] == "wikidata_entities"
    assert "venue" not in feed["fixtures"][1]


def test_wikidata_disabled_lane_is_explicitly_not_configured():
    section = _wikidata_unavailable_section(datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert section["status"] == "not_configured"
    assert section["network_opened"] is False
    assert section["venues"] == []
    assert section["error_count"] == 0


def test_blocked_sections_are_compatible_with_current_readmodel_contract():
    from league_platform.current import _validate_live_snapshot

    reference = datetime(2026, 8, 25, tzinfo=timezone.utc)
    blocked = _blocked_current_sections(reference, crawl4ai_config=None)
    _validate_live_snapshot(
        {
            "fixture_feed": {"provider": "OpenFootball", "fixtures": [], "errors": []},
            "espn_injuries": blocked["espn_injuries"],
            "news": blocked["news"],
            "oddstorm": blocked["oddstorm"],
        },
        reference,
        {"premier-league"},
    )


class _OpenFootballResponse:
    def __init__(self, *, url: str, payload: bytes) -> None:
        self._url = url
        self._payload = payload
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, _size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._payload


class _OpenFootballPayloadOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def open(self, request, timeout: float):
        assert timeout > 0
        url = request.full_url
        if url not in self._payloads:
            raise AssertionError(f"unexpected network URL: {url}")
        return _OpenFootballResponse(url=url, payload=self._payloads[url])


def _openfootball_payload(*, home: str, away: str, exact: bool = True) -> bytes:
    match = {
        "round": "Matchday 1",
        "date": "2026-08-26",
        "team1": home,
        "team2": away,
    }
    if exact:
        match["time"] = "20:00"
    return json.dumps({"matches": [match]}, separators=(",", ":")).encode()


def _patch_openfootball_transport(
    monkeypatch,
    payloads_by_source_id: dict[str, bytes],
    *,
    mutate_result=None,
    mutate_archive=None,
) -> None:
    payloads_by_url = {
        OPENFOOTBALL_SOURCES[source_id]["url"]: payload
        for source_id, payload in payloads_by_source_id.items()
    }

    def fake_fetch(*, now, raw_sink):
        assert raw_sink is not None
        result = fetch_openfootball_current_from_transport(
            now=now,
            source_ids=payloads_by_source_id,
            opener=_OpenFootballPayloadOpener(payloads_by_url),
            raw_sink=raw_sink,
        )
        if mutate_archive is not None:
            mutate_archive(raw_sink, result)
        if mutate_result is not None:
            mutate_result(result)
        return result

    monkeypatch.setattr(sync_live, "fetch_openfootball_current", fake_fetch)


def _patch_openligadb_offline(monkeypatch, reference: datetime) -> None:
    monkeypatch.setattr(
        sync_live,
        "fetch_openligadb_fixtures",
        lambda **_kwargs: {
            "provider": "OpenLigaDB",
            "retrieved_at": reference.isoformat(),
            "matches": [],
            "errors": [],
            "status": "unavailable",
        },
    )


def test_blocked_fixture_source_is_never_carried_after_transient_empty_failure():
    reference = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
    previous = {
        "cfl_official": {
            "provider": "Chinese Professional Football League official",
            "retrieved_at": "2026-08-23T23:30:00+00:00",
            "status": "ok",
            "fixtures": [{"id": "cfl-official:csl:one"}],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [],
            "upcoming_7_days": [{"id": "cfl-official:csl:one"}],
            "errors": [],
            "source_contract": {"fact_source": "CFL official"},
            "diagnostics": {"parsed_fixture_count": 1},
        }
    }
    failed = {
        "provider": "Chinese Professional Football League official",
        "retrieved_at": reference.isoformat(),
        "status": "unavailable",
        "fixtures": [],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [],
        "errors": [{"stage": "discover_or_fetch", "error": "TLS EOF"}],
    }

    result = _reuse_recent_fixture_source(
        failed,
        previous_snapshot=previous,
        source_key="cfl_official",
        reference_time=reference,
    )

    assert result is failed
    assert result["status"] == "unavailable"
    assert result["fixtures"] == []
    assert "fallback" not in result


def test_expired_fixture_source_is_not_carried_forward():
    reference = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
    previous = {
        "cfl_official": {
            "provider": "Chinese Professional Football League official",
            "retrieved_at": "2026-08-23T23:30:00+00:00",
            "status": "ok",
            "fixtures": [{"id": "cfl-official:csl:one"}],
            "errors": [],
        }
    }
    failed = {
        "provider": "Chinese Professional Football League official",
        "retrieved_at": reference.isoformat(),
        "status": "unavailable",
        "fixtures": [],
        "errors": [{"stage": "discover_or_fetch", "error": "TLS EOF"}],
    }

    result = _reuse_recent_fixture_source(
        failed,
        previous_snapshot=previous,
        source_key="cfl_official",
        reference_time=reference,
    )

    assert result is failed


def test_blocked_fixture_source_does_not_recover_from_append_only_archive(
    tmp_path: Path,
):
    archive = tmp_path / "archive"
    raw_dir = archive / "raw"
    raw_dir.mkdir(parents=True)
    archived = {
        "as_of": "2026-08-23T23:30:00+00:00",
        "cfl_official": {
            "provider": "Chinese Professional Football League official",
            "retrieved_at": "2026-08-23T23:29:00+00:00",
            "status": "ok",
            "fixtures": [{"id": "cfl-official:csl:archived"}],
            "errors": [],
            "source_contract": {"fact_source": "CFL official"},
        },
    }
    raw_path = raw_dir / "snapshot.json"
    raw_path.write_text(json.dumps(archived), encoding="utf-8")
    (archive / "snapshots.jsonl").write_text(
        json.dumps(
            {
                "as_of": archived["as_of"],
                "raw_path": "raw/snapshot.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    current_pointer = {
        "as_of": "2026-08-24T00:00:00+00:00",
        "cfl_official": {
            "status": "unavailable",
            "retrieved_at": "2026-08-24T00:00:00+00:00",
            "fixtures": [],
        },
    }

    result = _load_recent_fixture_source_snapshot(
        current_pointer,
        archive,
        source_key="cfl_official",
        reference_time=datetime(2026, 8, 24, 0, tzinfo=timezone.utc),
    )

    assert result is None
    assert json.loads(raw_path.read_text(encoding="utf-8")) == archived


def test_premier_league_matchweek_window_keeps_opening_round_for_current_fixtures():
    reference = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-22T11:30:00+00:00",
        },
        {
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-09-06T15:30:00+00:00",
        },
    ]

    season, matchweeks = _premier_league_matchweeks_for_horizon(
        fixtures,
        reference_time=reference,
        horizon=reference + sync_live.timedelta(days=16),
    )

    assert season == "2026"
    assert matchweeks[0] == 1
    assert matchweeks == tuple(range(1, 9))


def test_unlicensed_espn_sections_are_structurally_blocked_without_fixture_fallback():
    blocked = _blocked_espn_sections(datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc))

    assert set(blocked) == {"espn", "espn_markets", "espn_rosters"}
    assert blocked["espn"]["fixtures"] == []
    assert blocked["espn_markets"]["markets"] == []
    assert blocked["espn_markets"]["fixture_updates"] == []
    assert blocked["espn_rosters"]["rosters"] == []
    assert all(row["status"] == "rights_blocked" for row in blocked.values())
    assert all(
        row["schema_version"] == "matchline.source_rights_result.v1" for row in blocked.values()
    )
    assert all(row["network_opened"] is False for row in blocked.values())
    assert all(
        row["rights"]["terms_url"] == "https://disneytermsofuse.com/english/"
        for row in blocked.values()
    )
    assert blocked["espn"]["rights"]["source_id"] == "espn_schedule_summary"
    assert blocked["espn_markets"]["rights"]["source_id"] == "espn_market_summary"
    assert blocked["espn_rosters"]["rights"]["source_id"] == "espn_team_rosters"


def test_sofascore_window_keeps_only_causal_near_term_fixtures():
    reference = datetime(2026, 8, 14, 7, 0, tzinfo=timezone.utc)
    fixtures = [
        {"id": "before", "kickoff_at": "2026-08-14T06:59:00+00:00"},
        {"id": "near", "kickoff_at": "2026-08-15T06:59:00+00:00"},
        {"id": "boundary", "kickoff_at": "2026-08-16T07:00:00+00:00"},
        {"id": "far", "kickoff_at": "2026-08-16T07:01:00+00:00"},
        {"id": "invalid", "kickoff_at": "not-a-time"},
    ]

    selected = _sofascore_window(fixtures, reference_time=reference)

    assert [fixture["id"] for fixture in selected] == ["near", "boundary"]


def test_lineup_poll_diagnostics_distinguishes_not_due_source_failure_and_missing_adapter():
    reference = datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "id": "pl-near",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-18T06:00:00+00:00",
        },
        {
            "id": "la-far",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-18T06:00:00+00:00",
        },
        {
            "id": "csl-near",
            "competition_id": "csl",
            "status": "upcoming",
            "kickoff_at": "2026-08-18T08:00:00+00:00",
        },
        {
            "id": "bundesliga-far",
            "competition_id": "bundesliga",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T06:00:00+00:00",
        },
    ]
    result = _lineup_poll_diagnostics(
        fixtures,
        reference_time=reference,
        source_results={
            "premier_league_official": {
                "status": "ok",
                "lineups": [],
                "errors": [],
            },
            "laliga_official": {
                "status": "unavailable",
                "lineups": [],
                "errors": [{"stage": "home", "error": "403"}],
            },
        },
    )

    assert result["premier-league"]["state"] == "awaiting_publication"
    assert result["premier-league"]["candidate_count"] == 1
    assert result["premier-league"]["requested"] is True
    assert result["premier-league"]["poll_due"] is True
    assert result["premier-league"]["next_stage"] == "t_minus_24h"
    assert result["premier-league"]["next_fixture"]["fixture_id"] == "pl-near"
    assert result["premier-league"]["stage_plan"][0]["state"] == "due"
    assert result["la-liga"]["state"] == "source_unavailable"
    assert result["la-liga"]["candidate_count"] == 1
    assert result["bundesliga"]["state"] == "not_due"
    assert result["bundesliga"]["candidate_count"] == 0
    assert result["bundesliga"]["next_fixture"] is None
    assert result["bundesliga"]["poll_due"] is False
    assert result["csl"]["state"] == "not_configured"
    assert result["csl"]["reason_code"] == "no_permitted_public_official_adapter"
    assert result["csl"]["next_poll_at"] is None


def test_lineup_poll_diagnostics_does_not_call_empty_lineup_envelope_observed():
    reference = datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "id": "pl-near",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
        },
    ]
    result = _lineup_poll_diagnostics(
        fixtures,
        reference_time=reference,
        source_results={
            "premier_league_official": {
                "status": "ok",
                "lineups": [
                    {
                        "fixture_id": "pl-near",
                        "lineups": {
                            "available": False,
                            "confirmed": False,
                            "model_eligible": False,
                        },
                    },
                ],
                "errors": [],
            },
        },
    )

    row = result["premier-league"]
    assert row["state"] == "awaiting_publication"
    assert row["reason_code"] == "source_polled_without_confirmed_xi"
    assert row["source_lineup_count"] == 1
    assert row["available_lineup_count"] == 0


def test_lineup_poll_diagnostics_marks_carried_rows_not_due_when_schedule_skipped():
    reference = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "id": "la-1",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
        }
    ]
    result = _lineup_poll_diagnostics(
        fixtures,
        reference_time=reference,
        source_results={
            "laliga_official": {
                "provider": "LaLiga official",
                "status": "ok",
                "lineups": [
                    {
                        "fixture_id": "la-1",
                        "lineups": {
                            "available": False,
                            "confirmed": False,
                            "model_eligible": False,
                        },
                        "source": {"retrieved_at": "2026-08-20T18:05:00+00:00"},
                    }
                ],
                "errors": [],
                "poll_schedule": {
                    "requested": False,
                    "poll_due": False,
                    "reason_code": "poll_schedule_not_due",
                    "next_stage": "t_minus_6h",
                    "next_poll_at": "2026-08-21T13:00:00+00:00",
                },
            }
        },
    )
    row = result["la-liga"]
    assert row["state"] == "not_due"
    assert row["requested"] is False
    assert row["poll_due"] is False
    assert row["source_lineup_count"] == 1
    assert row["next_stage"] == "t_minus_6h"


def test_secondary_official_lineup_hosts_are_blocked_before_adapter_calls(monkeypatch):
    calls: list[str] = []

    def fake_adapter(name):
        def fetch(fixtures, *, now, horizon_hours):
            del fixtures, now, horizon_hours
            calls.append(name)
            return {
                "provider": name,
                "fixtures": [],
                "lineups": [],
                "errors": [],
                "status": "ok",
            }

        return fetch

    monkeypatch.setattr(sync_live, "fetch_laliga_lineups", fake_adapter("laliga"))
    monkeypatch.setattr(sync_live, "fetch_bundesliga_lineups", fake_adapter("bundesliga"))
    monkeypatch.setattr(sync_live, "fetch_serie_a_lineups", fake_adapter("serie-a"))
    monkeypatch.setattr(sync_live, "fetch_ligue1_lineups", fake_adapter("ligue-1"))

    result = _fetch_secondary_official_lineups(
        [
            {
                "id": "la-1",
                "competition_id": "la-liga",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
            },
            {
                "id": "de-1",
                "competition_id": "bundesliga",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
            },
            {
                "id": "it-1",
                "competition_id": "serie-a",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
            },
            {
                "id": "fr-1",
                "competition_id": "ligue-1",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
            },
        ],
        source_now=datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc),
    )

    assert set(result) == {
        "laliga_official",
        "bundesliga_official",
        "serie_a_official",
        "ligue1_official",
    }
    assert calls == []
    assert all(value["status"] == "rights_blocked" for value in result.values())
    assert all(value["fixtures"] == [] for value in result.values())
    assert all(value["lineups"] == [] for value in result.values())


def test_official_lineup_poll_plan_waits_for_next_stage_after_successful_poll():
    fixture = {
        "id": "la-1",
        "competition_id": "la-liga",
        "status": "upcoming",
        "kickoff_at": "2026-08-22T18:00:00+00:00",
    }
    first_reference = datetime(2026, 8, 21, 18, 5, tzinfo=timezone.utc)
    first = _official_lineup_poll_plan(
        [fixture],
        competition_id="la-liga",
        source_key="laliga_official",
        previous_snapshot=None,
        source_now=first_reference,
    )
    assert first["requested_fixture_ids"] == ["la-1"]
    assert first["fixture_plans"][0]["next_stage"] == "t_minus_24h"

    previous = {
        "lineup_poll_diagnostics": {
            "la-liga": {
                "state": "awaiting_publication",
                "fixture_plans": [{"fixture_id": "la-1"}],
            }
        },
        "laliga_official": {
            "status": "ok",
            "retrieved_at": first_reference.isoformat(),
            "fixtures": [],
            "lineups": [],
            "errors": [],
            "poll_schedule": first,
        },
    }
    same_stage = _official_lineup_poll_plan(
        [fixture],
        competition_id="la-liga",
        source_key="laliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc),
    )
    assert same_stage["requested_fixture_ids"] == []
    assert same_stage["reason_code"] == "poll_schedule_not_due"
    assert same_stage["next_stage"] == "t_minus_6h"
    assert same_stage["next_poll_at"] == "2026-08-22T12:00:00+00:00"

    next_stage = _official_lineup_poll_plan(
        [fixture],
        competition_id="la-liga",
        source_key="laliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 22, 12, 5, tzinfo=timezone.utc),
    )
    assert next_stage["requested_fixture_ids"] == ["la-1"]
    assert next_stage["fixture_plans"][0]["next_stage"] == "t_minus_6h"


def test_official_lineup_poll_plan_retries_after_t90_until_confirmed():
    fixture = {
        "id": "de-1",
        "competition_id": "bundesliga",
        "status": "upcoming",
        "kickoff_at": "2026-08-28T18:00:00+00:00",
    }
    first_reference = datetime(2026, 8, 28, 16, 31, tzinfo=timezone.utc)
    first = _official_lineup_poll_plan(
        [fixture],
        competition_id="bundesliga",
        source_key="bundesliga_official",
        previous_snapshot=None,
        source_now=first_reference,
    )
    assert first["requested_fixture_ids"] == ["de-1"]

    previous = {
        "lineup_poll_diagnostics": {
            "bundesliga": {
                "state": "awaiting_publication",
                "fixture_plans": [{"fixture_id": "de-1"}],
            }
        },
        "bundesliga_official": {
            "status": "ok",
            "retrieved_at": first_reference.isoformat(),
            "fixtures": [],
            "lineups": [
                {
                    "fixture_id": "de-1",
                    "lineups": {
                        "available": False,
                        "confirmed": False,
                        "model_eligible": False,
                        "diagnostic": {"status": "not_published"},
                    },
                }
            ],
            "errors": [],
            "poll_schedule": first,
        },
    }

    early = _official_lineup_poll_plan(
        [fixture],
        competition_id="bundesliga",
        source_key="bundesliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 28, 16, 40, tzinfo=timezone.utc),
    )
    assert early["requested_fixture_ids"] == []
    assert early["fixture_plans"][0]["next_stage"] == "lineup_confirmation"
    assert early["fixture_plans"][0]["next_poll_at"] == "2026-08-28T16:46:00+00:00"

    retry = _official_lineup_poll_plan(
        [fixture],
        competition_id="bundesliga",
        source_key="bundesliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 28, 16, 46, tzinfo=timezone.utc),
    )
    assert retry["requested_fixture_ids"] == ["de-1"]
    assert retry["reason_code"] == "lineup_confirmation_retry_due"

    previous["bundesliga_official"]["lineups"][0]["lineups"] = {
        "available": True,
        "confirmed": True,
        "model_eligible": True,
    }
    confirmed = _official_lineup_poll_plan(
        [fixture],
        competition_id="bundesliga",
        source_key="bundesliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 28, 16, 46, tzinfo=timezone.utc),
    )
    assert confirmed["requested_fixture_ids"] == []
    assert confirmed["fixture_plans"][0]["next_stage"] == "lineup_confirmation"
    assert confirmed["fixture_plans"][0]["next_poll_at"] is None
    assert confirmed["fixture_plans"][0]["poll_reason"] == "confirmed_lineup_observed"


def test_official_lineup_poll_plan_does_not_reuse_legacy_time_for_unrequested_fixture():
    fixtures = [
        {
            "id": "first",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
        },
        {
            "id": "second",
            "competition_id": "la-liga",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T20:00:00+00:00",
        },
    ]
    previous = {
        "lineup_poll_diagnostics": {
            "la-liga": {
                "state": "awaiting_publication",
                "fixture_plans": [
                    {"fixture_id": "first", "last_requested_at": "2026-08-20T22:00:00+00:00"},
                    {"fixture_id": "second", "last_requested_at": None},
                ],
            }
        },
        "laliga_official": {
            "poll_schedule": {
                "last_requested_at": "2026-08-20T22:00:00+00:00",
                "last_requested_by_fixture": {"first": "2026-08-20T22:00:00+00:00"},
            },
            "lineups": [],
            "fixtures": [],
        },
    }

    result = _official_lineup_poll_plan(
        fixtures,
        competition_id="la-liga",
        source_key="laliga_official",
        previous_snapshot=previous,
        source_now=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result["requested_fixture_ids"] == ["second"]
    assert result["fixture_plans"][0]["last_requested_at"] == "2026-08-20T22:00:00+00:00"
    assert result["fixture_plans"][1]["last_requested_at"] is None


def test_lineup_diagnostics_resolves_official_provider_id_to_current_fixture():
    result = _lineup_poll_diagnostics(
        [
            {
                "id": "espn:current-1",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
                "home_team": "Arsenal",
                "away_team": "Coventry City",
            }
        ],
        reference_time=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        source_results={
            "premier_league_official": {
                "status": "ok",
                "fixtures": [
                    {
                        "id": "premierleague:2645195",
                        "competition_id": "premier-league",
                        "kickoff_at": "2026-08-21T19:00:00+00:00",
                        "home_team": "Arsenal",
                        "away_team": "Coventry City",
                    }
                ],
                "lineups": [
                    {
                        "fixture_id": "premierleague:2645195",
                        "lineups": {
                            "available": False,
                            "confirmed": False,
                            "model_eligible": False,
                        },
                        "source": {"retrieved_at": "2026-08-21T09:00:00+00:00"},
                    }
                ],
                "errors": [],
            }
        },
    )

    row = result["premier-league"]
    assert row["source_lineup_count"] == 1
    assert row["fixture_plans"][0]["source_lineup_count"] == 1
    assert row["fixture_plans"][0]["latest_lineup_observed_at"] == "2026-08-21T09:00:00+00:00"


def test_lineup_diagnostics_resolves_provider_poll_plan_to_current_fixture():
    result = _lineup_poll_diagnostics(
        [
            {
                "id": "espn:current-1",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-21T19:00:00+00:00",
                "home_team": "Arsenal",
                "away_team": "Coventry City",
            }
        ],
        reference_time=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        source_results={
            "premier_league_official": {
                "status": "ok",
                "fixtures": [
                    {
                        "id": "premierleague:2645195",
                        "kickoff_at": "2026-08-21T19:00:00+00:00",
                        "home_team": "Arsenal",
                        "away_team": "Coventry City",
                    }
                ],
                "lineups": [],
                "errors": [],
                "poll_schedule": {
                    "requested": False,
                    "poll_due": False,
                    "reason_code": "poll_schedule_not_due",
                    "next_stage": "t_minus_6h",
                    "next_poll_at": "2026-08-21T13:00:00+00:00",
                    "fixture_plans": [
                        {
                            "fixture_id": "premierleague:2645195",
                            "kickoff_at": "2026-08-21T19:00:00+00:00",
                            "home_team": "Arsenal",
                            "away_team": "Coventry City",
                            "next_stage": "t_minus_6h",
                            "next_poll_at": "2026-08-21T13:00:00+00:00",
                            "poll_due": False,
                        }
                    ],
                },
            }
        },
    )

    plan = result["premier-league"]["fixture_plans"][0]
    assert plan["fixture_id"] == "espn:current-1"
    assert plan["poll_due"] is False
    assert plan["next_stage"] == "t_minus_6h"
    assert plan["next_poll_at"] == "2026-08-21T13:00:00+00:00"


def test_lineup_diagnostics_resolves_explicit_official_team_aliases():
    result = _lineup_poll_diagnostics(
        [
            {
                "id": "espn:brighton-bournemouth",
                "competition_id": "premier-league",
                "status": "upcoming",
                "kickoff_at": "2026-08-23T13:00:00+00:00",
                "home_team": "Brighton & Hove Albion",
                "away_team": "AFC Bournemouth",
            }
        ],
        reference_time=datetime(2026, 8, 23, 7, 40, tzinfo=timezone.utc),
        source_results={
            "premier_league_official": {
                "status": "ok",
                "fixtures": [
                    {
                        "id": "premierleague:2645201",
                        "kickoff_at": "2026-08-23T13:00:00+00:00",
                        "home_team": "Brighton and Hove Albion",
                        "away_team": "Bournemouth",
                    }
                ],
                "lineups": [],
                "errors": [],
                "poll_schedule": {
                    "requested": False,
                    "poll_due": False,
                    "reason_code": "poll_schedule_not_due",
                    "next_stage": "t_minus_90m",
                    "next_poll_at": "2026-08-23T11:30:00+00:00",
                    "fixture_plans": [
                        {
                            "fixture_id": "premierleague:2645201",
                            "kickoff_at": "2026-08-23T13:00:00+00:00",
                            "home_team": "Brighton and Hove Albion",
                            "away_team": "Bournemouth",
                            "next_stage": "t_minus_90m",
                            "next_poll_at": "2026-08-23T11:30:00+00:00",
                            "poll_due": False,
                        }
                    ],
                },
            }
        },
    )

    plan = result["premier-league"]["fixture_plans"][0]
    assert plan["poll_due"] is False
    assert plan["next_stage"] == "t_minus_90m"
    assert plan["next_poll_at"] == "2026-08-23T11:30:00+00:00"


def test_secondary_official_not_due_does_not_carry_forward_rows_or_call_adapter(monkeypatch):
    called = False

    def fail_adapter(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("adapter must not be called before the next lineup stage")

    monkeypatch.setattr(sync_live, "fetch_laliga_lineups", fail_adapter)
    fixture = {
        "id": "la-1",
        "competition_id": "la-liga",
        "status": "upcoming",
        "kickoff_at": "2026-08-22T18:00:00+00:00",
    }
    first_reference = datetime(2026, 8, 21, 18, 5, tzinfo=timezone.utc)
    first_plan = _official_lineup_poll_plan(
        [fixture],
        competition_id="la-liga",
        source_key="laliga_official",
        previous_snapshot=None,
        source_now=first_reference,
    )
    previous = {
        "lineup_poll_diagnostics": {
            "la-liga": {"state": "awaiting_publication", "fixture_plans": []}
        },
        "laliga_official": {
            "status": "ok",
            "retrieved_at": first_reference.isoformat(),
            "fixtures": [{"id": "la-1", "competition_id": "la-liga"}],
            "lineups": [{"fixture_id": "la-1", "lineups": {"confirmed": False}}],
            "errors": [],
            "poll_schedule": first_plan,
        },
    }
    result = _fetch_secondary_official_lineups(
        [fixture],
        source_now=datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc),
        previous_snapshot=previous,
    )
    assert called is False
    assert result["laliga_official"]["status"] == "rights_blocked"
    assert result["laliga_official"]["fixtures"] == []
    assert result["laliga_official"]["lineups"] == []
    assert "poll_schedule" not in result["laliga_official"]


def test_lineup_poll_diagnostics_keeps_a_causal_plan_for_each_candidate_fixture():
    reference = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)
    fixtures = [
        {
            "id": "pl-near",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
            "home_team": "Home Near",
            "away_team": "Away Near",
        },
        {
            "id": "pl-later",
            "competition_id": "premier-league",
            "status": "upcoming",
            "kickoff_at": "2026-08-22T19:00:00+00:00",
            "home_team": "Home Later",
            "away_team": "Away Later",
        },
    ]
    result = _lineup_poll_diagnostics(
        fixtures,
        reference_time=reference,
        source_results={
            "premier_league_official": {
                "status": "ok",
                "lineups": [
                    {
                        "fixture_id": "pl-later",
                        "lineups": {"available": True, "confirmed": True, "model_eligible": False},
                        "observed_at": "2026-08-20T07:00:00+00:00",
                    }
                ],
                "errors": [],
            },
        },
    )

    plans = result["premier-league"]["fixture_plans"]
    assert [plan["fixture_id"] for plan in plans] == ["pl-near", "pl-later"]
    assert plans[0]["poll_due"] is True
    assert plans[0]["confirmed_lineup_count"] == 0
    assert plans[1]["poll_due"] is False
    assert plans[1]["confirmed_lineup_count"] == 1
    assert plans[1]["stage_plan"][0]["state"] == "upcoming"


def test_blocked_geocoding_does_not_reuse_previous_coordinates():
    fixtures = [
        {
            "id": "cached",
            "venue": {"city": "London", "country": "England"},
        },
        {
            "id": "new",
            "venue": {"city": "Madrid", "country": "Spain"},
        },
        {
            "id": "cached-again",
            "venue": {"city": "London", "country": "England"},
        },
        {"id": "no-city", "venue": {"name": "Unknown ground"}},
    ]
    cached, missing = _prepare_geocoding_requests(
        fixtures,
        {
            "geocoding": {
                "geocodes": [
                    {
                        "fixture_id": "old-fixture",
                        "city": "London",
                        "requested_country": "England",
                        "latitude": 51.5074,
                        "longitude": -0.1278,
                        "source": {"retrieved_at": "2026-08-19T00:00:00+00:00"},
                    },
                    {
                        "fixture_id": "bad-fixture",
                        "city": "Rome",
                        "requested_country": "Italy",
                        "latitude": "not-a-coordinate",
                        "longitude": 12.5,
                    },
                ],
            }
        },
    )

    assert cached == []
    assert [row["id"] for row in missing] == ["cached", "new", "no-city"]


def test_sync_forwards_explicit_crawl4ai_config_path(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_sync(output: Path, **kwargs):
        seen["output"] = output
        seen.update(kwargs)
        return {"as_of": "2026-08-17T00:00:00+00:00"}

    monkeypatch.setattr(sync_live, "_sync_unlocked", fake_sync)
    output = tmp_path / "current.json"
    result = sync_live.sync(
        output,
        crawl4ai_config_path=tmp_path / "operator.json",
        openfootball_raw_archive_dir=tmp_path / "openfootball-raw",
    )

    assert result["as_of"] == "2026-08-17T00:00:00+00:00"
    assert seen["output"] == output
    assert seen["crawl4ai_config_path"] == tmp_path / "operator.json"
    assert seen["openfootball_raw_archive_dir"] == tmp_path / "openfootball-raw"


def test_cli_env_raw_archive_overrides_output_parent_when_flag_is_omitted(
    tmp_path: Path,
    monkeypatch,
):
    output = tmp_path / "current.json"
    durable_archive = Path("/tmp/matchline-cli-env-openfootball-raw")
    resolved: dict[str, Path] = {}

    class StopAfterResolution(Exception):
        pass

    def stop_after_resolution(output_path: Path, **kwargs):
        resolved["archive"] = sync_live._openfootball_raw_archive_root(
            output_path,
            kwargs["openfootball_raw_archive_dir"],
        )
        raise StopAfterResolution

    monkeypatch.setenv(sync_live.OPENFOOTBALL_RAW_ARCHIVE_ENV, str(durable_archive))
    monkeypatch.setattr(sync_live, "_sync_unlocked", stop_after_resolution)
    monkeypatch.setattr(sys, "argv", ["sync_live", "--output", str(output)])

    with pytest.raises(StopAfterResolution):
        sync_live.main()

    assert resolved["archive"] == durable_archive


def test_sync_writes_then_reloads_raw_openfootball_before_fixture_publication(monkeypatch):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    config = OPENFOOTBALL_SOURCES[PREMIER_LEAGUE_SOURCE_ID]
    payload = json.dumps(
        {
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-08-26",
                    "time": "20:00",
                    "team1": "Verified Home",
                    "team2": "Verified Away",
                },
                {
                    "round": "Matchday 1",
                    "date": "2026-08-27",
                    "team1": "Date Only Home",
                    "team2": "Date Only Away",
                },
            ]
        },
        separators=(",", ":"),
    ).encode()
    _patch_openfootball_transport(
        monkeypatch,
        {PREMIER_LEAGUE_SOURCE_ID: payload},
    )
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-sync-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        snapshot = sync_live._sync_unlocked(
            runtime_root / "current.json",
            now=reference,
            archive_dir=runtime_root / "archive",
            intelligence_ledger=None,
        )

        assert (runtime_root / "openfootball-raw/manifest.jsonl").is_file()
        assert snapshot["fixture_feed"]["fixtures"][0]["home_team"] == "Verified Home"
        assert snapshot["fixture_feed"]["date_only_fixtures"][0]["home_team"] == ("Date Only Home")
        admission = snapshot["fixture_feed"]["raw_archive_admission"]
        assert snapshot["openfootball_current"]["raw_archive_admission"] == admission
        assert set(admission) == {
            "schema_version",
            "status",
            "policy_version",
            "observed_before",
            "source_ids",
            "row_count",
            "selected_records",
            "admission_sha256",
            "source_manifest_sha256",
            "parser_contract_sha256",
            "rows_sha256",
        }
        assert admission["schema_version"] == "matchline.openfootball_raw_admission.v1"
        assert admission["status"] == "verified_current_raw"
        assert admission["policy_version"] == "v260"
        assert admission["observed_before"] == reference.isoformat()
        assert admission["source_ids"] == [PREMIER_LEAGUE_SOURCE_ID]
        assert admission["row_count"] == 2
        assert admission["selected_records"][0]["source_id"] == PREMIER_LEAGUE_SOURCE_ID
        assert (
            admission["selected_records"][0]["raw_sha256"]
            == (snapshot["fixture_feed"]["fixtures"][0]["source"]["raw_sha256"])
        )
        assert "manifest_sha256" not in admission
        assert "raw_archive_receipts" not in snapshot["fixture_feed"]
        assert "raw_archive_receipts" not in snapshot["openfootball_current"]
        assert "raw_archive_producer_receipt" not in json.dumps(snapshot)
        assert snapshot["fixture_feed"]["fixtures"][0]["source"]["url"] == config["url"]
        assert snapshot["roles"]["historical_training"] == (
            "OpenFootball durable raw archive verified consumer required"
        )
        assert "football-data.co.uk" not in json.dumps(snapshot["roles"])


def test_sync_bridges_crawl4ai_after_verified_raw_only_for_explicit_allow_seam(
    tmp_path: Path,
    monkeypatch,
):
    """A typed central ALLOW seam may invoke Crawl4AI after raw admission.

    The fixture and Crawl4AI runner are both local test doubles: this test must
    never open a real browser or network connection.  The production policy is
    currently BLOCK for this source; replacing only the sync module's policy
    decision gives the bridge an explicit, auditable ALLOW seam.
    """

    from dataclasses import replace

    from league_platform.source_rights import DecisionValue

    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    _patch_openfootball_transport(
        monkeypatch,
        {
            PREMIER_LEAGUE_SOURCE_ID: _openfootball_payload(
                home="Bridge Home",
                away="Bridge Away",
            )
        },
    )
    _patch_openligadb_offline(monkeypatch, reference)
    monkeypatch.setattr(
        sync_live,
        "fetch_met_norway_weather",
        lambda *_args, **_kwargs: {"weather": [], "errors": [], "status": "not_configured"},
        raising=False,
    )

    config_path = tmp_path / "crawl4ai-allow.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "FBRef explicit test seam",
                        "source_id": SourceId.FBREF_PUBLIC_STATS.value,
                        "url": "https://fbref.com/en/",
                        "parser": "generic_public_page_v1",
                        "enabled": True,
                        "license_status": "open",
                        "rights_reference": "https://fbref.com/robots.txt",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    import league_platform.live_sources.crawl4ai as crawl4ai_module

    real_require = crawl4ai_module.source_rights.require_source_rights
    allow = replace(
        decide_source_rights(SourceId.OPENFOOTBALL_CURRENT, UseCase.NETWORK_FETCH),
        source_id=SourceId.FBREF_PUBLIC_STATS.value,
        use_case=UseCase.NETWORK_FETCH.value,
        decision=DecisionValue.ALLOW,
        access_allowed=True,
    )

    def explicit_allow(source_id, use_case, **kwargs):
        source_value = source_id.value if isinstance(source_id, SourceId) else source_id
        use_case_value = use_case.value if isinstance(use_case, UseCase) else use_case
        if (
            source_value == SourceId.FBREF_PUBLIC_STATS.value
            and use_case_value == UseCase.NETWORK_FETCH.value
        ):
            return allow
        return real_require(source_id, use_case, **kwargs)

    monkeypatch.setattr(crawl4ai_module.source_rights, "require_source_rights", explicit_allow)
    calls: list[dict[str, object]] = []

    def fake_crawl4ai(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "Crawl4AI",
            "provider_role": "execution_layer",
            "status": "ok",
            "pages": [
                {
                    "source_id": SourceId.FBREF_PUBLIC_STATS.value,
                    "url": "https://fbref.com/en/",
                    "model_eligible": False,
                    "enters_model": False,
                    "network_opened": False,
                }
            ],
            "errors": [],
            "network_opened": False,
            "model_eligible": False,
        }

    monkeypatch.setattr(sync_live, "fetch_crawl4ai", fake_crawl4ai, raising=False)

    snapshot = sync_live._sync_unlocked(
        tmp_path / "current.json",
        now=reference,
        archive_dir=tmp_path / "archive",
        intelligence_ledger=None,
        crawl4ai_config_path=config_path,
        enable_wikidata=False,
    )

    assert snapshot["openfootball_current"]["raw_archive_admission"]["status"] == (
        "verified_current_raw"
    )
    assert len(calls) == 1
    assert calls[0]["config_path"] == config_path
    assert calls[0]["now"] == reference
    assert snapshot["crawl4ai"]["status"] == "ok"
    assert snapshot["crawl4ai"]["model_eligible"] is False
    assert snapshot["crawl4ai"]["network_opened"] is False


def test_sync_blocks_crawl4ai_when_its_evidence_archive_is_volatile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    config_path = tmp_path / "crawl4ai-allow.json"
    config_path.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        sync_live,
        "load_crawl4ai_configs",
        lambda **_kwargs: ([object()], []),
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_crawl4ai",
        lambda **kwargs: calls.append(kwargs),
    )
    blocked = sync_live._blocked_crawl4ai_section(reference, config=config_path)
    result = sync_live._crawl4ai_bridge_section(
        {
            "raw_archive_admission": {
                "schema_version": sync_live.OPENFOOTBALL_RAW_ADMISSION_SCHEMA,
                "policy_version": sync_live.POLICY_VERSION,
                "status": "verified_current_raw",
            }
        },
        reference_time=reference,
        config_path=config_path,
        archive_root=Path("/dev/shm"),
        blocked_section=blocked,
    )

    assert calls == []
    assert result["status"] == "blocked_storage"
    assert result["network_opened"] is False
    assert result["model_eligible"] is False


def test_sync_reloads_raw_archive_and_blocks_rows_after_raw_object_tamper(monkeypatch):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def tamper_archive(raw_sink, result):
        receipt = result["raw_archive_receipts"][0]
        raw_path = raw_sink.root / receipt["raw_path"]
        raw_path.write_bytes(b"tampered-after-fetch")

    _patch_openfootball_transport(
        monkeypatch,
        {
            PREMIER_LEAGUE_SOURCE_ID: _openfootball_payload(
                home="Tamper Home",
                away="Tamper Away",
            )
        },
        mutate_archive=tamper_archive,
    )
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-sync-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        snapshot = sync_live._sync_unlocked(
            runtime_root / "current.json",
            now=reference,
            intelligence_ledger=None,
        )

    assert snapshot["fixture_feed"]["fixtures"] == []
    assert snapshot["fixture_feed"]["date_only_fixtures"] == []
    admission = snapshot["openfootball_current"]["raw_archive_admission"]
    assert admission["status"] == "blocked"
    assert not any(key.endswith("_sha256") for key in admission)


def test_sync_isolates_only_source_whose_fetch_rows_disagree_with_raw_reparse(monkeypatch):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def mutate_result(result):
        for row in result["fixtures"]:
            if row["source"]["source_id"] == CHAMPIONSHIP_SOURCE_ID:
                row["home_team"] = "Self Attested Replacement"

    _patch_openfootball_transport(
        monkeypatch,
        {
            PREMIER_LEAGUE_SOURCE_ID: _openfootball_payload(
                home="Premier Verified",
                away="Premier Away",
            ),
            CHAMPIONSHIP_SOURCE_ID: _openfootball_payload(
                home="Championship Raw",
                away="Championship Away",
            ),
        },
        mutate_result=mutate_result,
    )
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-sync-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        snapshot = sync_live._sync_unlocked(
            runtime_root / "current.json",
            now=reference,
            intelligence_ledger=None,
        )

    assert [row["home_team"] for row in snapshot["fixture_feed"]["fixtures"]] == [
        "Premier Verified"
    ]
    admission = snapshot["fixture_feed"]["raw_archive_admission"]
    assert admission["source_ids"] == [PREMIER_LEAGUE_SOURCE_ID]
    assert admission["row_count"] == 1
    assert any(
        error.get("source_id") == CHAMPIONSHIP_SOURCE_ID
        and error.get("stage") == "raw_archive_admission"
        for error in snapshot["fixture_feed"]["errors"]
    )
    assert "Self Attested Replacement" not in json.dumps(snapshot)


def test_sync_partial_transport_failure_admits_only_source_with_this_cycle_receipt(
    monkeypatch,
):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    premier_config = OPENFOOTBALL_SOURCES[PREMIER_LEAGUE_SOURCE_ID]
    opener = _OpenFootballPayloadOpener(
        {
            premier_config["url"]: _openfootball_payload(
                home="Only Successful Home",
                away="Only Successful Away",
            )
        }
    )

    def partial_fetch(*, now, raw_sink):
        return fetch_openfootball_current_from_transport(
            now=now,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID, CHAMPIONSHIP_SOURCE_ID],
            opener=opener,
            raw_sink=raw_sink,
        )

    monkeypatch.setattr(sync_live, "fetch_openfootball_current", partial_fetch)
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-sync-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        snapshot = sync_live._sync_unlocked(
            runtime_root / "current.json",
            now=reference,
            intelligence_ledger=None,
        )

    assert [row["home_team"] for row in snapshot["fixture_feed"]["fixtures"]] == [
        "Only Successful Home"
    ]
    admission = snapshot["fixture_feed"]["raw_archive_admission"]
    assert admission["source_ids"] == [PREMIER_LEAGUE_SOURCE_ID]
    assert admission["row_count"] == 1
    assert any(
        error.get("source_id") == CHAMPIONSHIP_SOURCE_ID
        for error in snapshot["fixture_feed"]["errors"]
    )


def test_sync_does_not_admit_forged_receipt_when_fetch_never_writes_archive(monkeypatch):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    config = OPENFOOTBALL_SOURCES[PREMIER_LEAGUE_SOURCE_ID]
    payload = _openfootball_payload(home="Forged Home", away="Forged Away")
    opener = _OpenFootballPayloadOpener({config["url"]: payload})

    def fake_fetch(*, now, raw_sink):
        assert raw_sink is not None
        result = fetch_openfootball_current_from_transport(
            now=now,
            source_ids=[PREMIER_LEAGUE_SOURCE_ID],
            opener=opener,
        )
        result["raw_archive_receipts"] = [
            {
                "status": "raw_observation_archived",
                "source_id": PREMIER_LEAGUE_SOURCE_ID,
                "retrieved_at": now.isoformat(),
                "raw_sha256": result["fixtures"][0]["source"]["raw_sha256"],
                "record_sha256": "f" * 64,
                "observation_id": "e" * 64,
            }
        ]
        return result

    monkeypatch.setattr(sync_live, "fetch_openfootball_current", fake_fetch)
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-sync-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        snapshot = sync_live._sync_unlocked(
            runtime_root / "current.json",
            now=reference,
            intelligence_ledger=None,
        )

    assert snapshot["fixture_feed"]["fixtures"] == []
    assert snapshot["openfootball_current"]["raw_archive_admission"]["status"] == "blocked"


def test_current_sync_rejects_history_source_receipt_even_when_shape_is_valid():
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    receipt = {
        "status": "raw_observation_archived",
        "observation_id": "a" * 64,
        "source_id": HISTORICAL_OPENFOOTBALL_SOURCE_ID,
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "b" * 64,
        "size_bytes": 10,
        "raw_path": "raw/sha256/bb/" + "b" * 64 + ".raw",
        "record_sha256": "c" * 64,
        "manifest_sha256": "d" * 64,
        "duplicate": False,
    }

    accepted, errors = sync_live._raw_archive_receipts_by_source(
        {"raw_archive_receipts": [receipt]},
        reference_time=reference,
    )

    assert accepted == {}
    assert errors == [
        {
            "source_id": HISTORICAL_OPENFOOTBALL_SOURCE_ID,
            "stage": "raw_archive_admission",
            "error": "OpenFootball raw archive receipt is invalid or duplicated",
        }
    ]


def test_sync_rejects_openfootball_archive_under_dev_shm_before_fetch(monkeypatch, tmp_path):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    calls = 0

    def forbidden_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("OpenFootball fetch must not start with a volatile raw archive")

    monkeypatch.setattr(sync_live, "fetch_openfootball_current", forbidden_fetch)
    _patch_openligadb_offline(monkeypatch, reference)
    volatile_root = Path("/dev/shm") / f"matchline-sync-{tmp_path.name}"

    snapshot = sync_live._sync_unlocked(
        tmp_path / "current.json",
        now=reference,
        openfootball_raw_archive_dir=volatile_root,
        intelligence_ledger=None,
    )

    assert calls == 0
    assert snapshot["fixture_feed"]["fixtures"] == []
    assert snapshot["openfootball_current"]["raw_archive_admission"] == {
        "schema_version": "matchline.openfootball_raw_admission.v1",
        "status": "blocked",
        "policy_version": "v260",
        "observed_before": reference.isoformat(),
        "source_ids": [],
        "row_count": 0,
        "reason": "volatile_raw_archive_path",
    }


def test_sync_uses_explicit_durable_env_archive_for_volatile_runtime_output(
    monkeypatch,
    tmp_path,
):
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    _patch_openfootball_transport(
        monkeypatch,
        {
            PREMIER_LEAGUE_SOURCE_ID: _openfootball_payload(
                home="Durable Evidence Home",
                away="Durable Evidence Away",
            )
        },
    )
    _patch_openligadb_offline(monkeypatch, reference)

    with tempfile.TemporaryDirectory(prefix="matchline-raw-", dir="/tmp") as directory:
        durable_raw_root = Path(directory) / "openfootball-raw"
        monkeypatch.setenv(
            sync_live.OPENFOOTBALL_RAW_ARCHIVE_ENV,
            str(durable_raw_root),
        )
        snapshot = sync_live._sync_unlocked(
            tmp_path / "current.json",
            now=reference,
            intelligence_ledger=None,
        )

        assert (durable_raw_root / "manifest.jsonl").is_file()
        assert [row["home_team"] for row in snapshot["fixture_feed"]["fixtures"]] == [
            "Durable Evidence Home"
        ]
        assert snapshot["fixture_feed"]["raw_archive_admission"]["status"] == (
            "verified_current_raw"
        )


def test_openfootball_current_cycle_remains_candidate_until_raw_archive_verification():
    reference = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    fixture = {"id": "openfootball:current"}
    result = sync_live._admit_openfootball_current(
        {
            "provider": "OpenFootball",
            "retrieved_at": reference.isoformat(),
            "status": "ok",
            "fixtures": [fixture],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [fixture],
            "upcoming_7_days": [fixture],
            "errors": [],
            "source_contract": {
                "rights": decide_source_rights(
                    SourceId.OPENFOOTBALL_CURRENT,
                    UseCase.NETWORK_FETCH,
                ).as_dict()
            },
        },
        reference_time=reference,
    )

    assert result["fixtures"] == [fixture]
    assert result["serve_current_rights"]["decision"] == "allow"
    assert result["raw_archive_receipts"] == []
    assert "raw_archive_admission" not in result
    assert "raw_archive_producer_receipt" not in result


def test_openfootball_self_asserted_old_cycle_is_rejected():
    reference = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    result = sync_live._admit_openfootball_current(
        {
            "provider": "OpenFootball",
            "retrieved_at": "2026-08-24T23:30:00+00:00",
            "status": "ok",
            "authorization_reference": "self-issued",
            "raw_archive_producer_receipt": {"status": "trusted"},
            "fixtures": [{"id": "openfootball:self-asserted-old"}],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [],
            "upcoming_7_days": [],
            "errors": [],
            "source_contract": {
                "rights": decide_source_rights(
                    SourceId.OPENFOOTBALL_CURRENT,
                    UseCase.NETWORK_FETCH,
                ).as_dict()
            },
        },
        reference_time=reference,
    )

    assert result["status"] == "unavailable"
    assert result["fixtures"] == []
    assert "authorization_reference" not in result
    assert "raw_archive_producer_receipt" not in result


def test_sync_cycle_drops_blocked_stale_facts_before_fetch_and_publication(
    tmp_path: Path,
    monkeypatch,
):
    reference = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    stale_fixture = {
        "id": "cfl-official:csl:stale",
        "competition_id": "csl",
        "status": "upcoming",
        "kickoff_at": "2026-08-25T12:00:00+00:00",
        "home_team": "Old Home",
        "away_team": "Old Away",
        "venue": {"city": "Madrid", "country": "Spain"},
        "source": {"name": "Chinese Professional Football League official"},
    }
    previous = {
        "schema_version": "1.0.0",
        "as_of": "2026-08-24T23:30:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball + CFL official",
            "fixtures": [
                {
                    "id": "openfootball:old-self-reported",
                    "status": "upcoming",
                    "kickoff_at": "2026-08-25T10:00:00+00:00",
                    "source": {"name": "OpenFootball"},
                },
                stale_fixture,
            ],
        },
        "openfootball_current": {
            "provider": "OpenFootball",
            "status": "ok",
            "authorization_reference": "self-issued-old-row",
            "fixtures": [{"id": "openfootball:old-self-reported"}],
        },
        "cfl_official": {
            "provider": "Chinese Professional Football League official",
            "retrieved_at": "2026-08-24T23:29:00+00:00",
            "status": "ok",
            "authorization_reference": "operator-says-ok",
            "fixtures": [stale_fixture],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [stale_fixture],
            "upcoming_7_days": [stale_fixture],
            "errors": [],
            "source_contract": {"fact_source": "CFL official"},
        },
        "espn": {"status": "ok", "fixtures": [{"id": "espn:stale"}]},
        "espn_markets": {"status": "ok", "markets": [{"fixture_id": "espn:stale"}]},
        "espn_rosters": {"status": "ok", "rosters": [{"team_id": "old"}]},
        "espn_injuries": {"status": "ok", "reports": [{"team_id": "old"}]},
        "understat": {"status": "ok", "team_features": [{"team_id": "old"}]},
        "news": {"status": "ok", "news": [{"title": "old"}]},
        "geocoding": {
            "status": "ok",
            "geocodes": [
                {
                    "fixture_id": "old",
                    "city": "London",
                    "requested_country": "England",
                    "latitude": 51.5,
                    "longitude": -0.1,
                }
            ],
        },
        "weather": {"status": "ok", "weather": [{"fixture_id": "old"}]},
        "sofascore": {"status": "ok", "events": [{"fixture_id": "old"}]},
        "premier_league_official": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
        "laliga_official": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
        "bundesliga_official": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
        "serie_a_official": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
        "ligue1_official": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
        "sports_lottery": {"status": "ok", "matches": [{"id": "old"}]},
        "oddstorm": {
            "status": "ok",
            "raw_sha256": "a" * 64,
            "lines": [{"match_id": "old"}],
        },
        "crawl4ai": {"status": "ok", "pages": [{"url": "https://old.invalid"}]},
        "fotmob": {"status": "ok", "lineups": [{"fixture_id": "old"}]},
    }
    output = tmp_path / "current.json"
    output.write_text(json.dumps(previous), encoding="utf-8")
    archive = tmp_path / "archive"
    raw_dir = archive / "raw"
    raw_dir.mkdir(parents=True)
    archived_path = raw_dir / "previous.json"
    archived_bytes = json.dumps(previous, sort_keys=True).encode()
    archived_path.write_bytes(archived_bytes)
    (archive / "snapshots.jsonl").write_text(
        json.dumps(
            {
                "as_of": previous["as_of"],
                "raw_path": "raw/previous.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    blocked_calls: list[str] = []

    def recording_fetch(name: str, payload: dict):
        def fetch(*_args, **_kwargs):
            blocked_calls.append(name)
            return deepcopy(payload)

        return fetch

    def unavailable_openfootball(**_kwargs):
        raise RuntimeError("current cycle unavailable")

    monkeypatch.setattr(
        sync_live,
        "fetch_openfootball_current",
        unavailable_openfootball,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_openligadb_fixtures",
        lambda **_kwargs: {
            "provider": "OpenLigaDB",
            "retrieved_at": reference.isoformat(),
            "matches": [{"id": "openligadb:42", "provider_match_id": "42"}],
            "errors": [],
            "status": "ok",
        },
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_cfl_current",
        recording_fetch(
            "cfl",
            {
                "provider": "Chinese Professional Football League official",
                "retrieved_at": reference.isoformat(),
                "status": "unavailable",
                "fixtures": [],
                "date_only_fixtures": [],
                "recent_results": [],
                "upcoming_3_days": [],
                "upcoming_7_days": [],
                "errors": [],
            },
        ),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_understat_features",
        recording_fetch(
            "understat", {"team_features": [{"team_id": "new-blocked"}], "errors": []}
        ),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_espn_injuries",
        recording_fetch("espn_injuries", {"reports": [{"team_id": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_premier_league_fixtures",
        recording_fetch("premier_league", {"fixtures": [], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_news",
        recording_fetch("rss", {"news": [{"title": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "_fetch_secondary_official_lineups",
        recording_fetch(
            "secondary_official",
            {
                key: {
                    "fixtures": [],
                    "lineups": [{"fixture_id": f"{key}:new-blocked"}],
                    "errors": [],
                    "status": "ok",
                }
                for key in (
                    "laliga_official",
                    "bundesliga_official",
                    "serie_a_official",
                    "ligue1_official",
                )
            },
        ),
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_open_meteo_geocoding",
        recording_fetch("geocoding", {"geocodes": [{"fixture_id": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_open_meteo_weather",
        recording_fetch("weather", {"weather": [{"fixture_id": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_sofascore_prematch",
        recording_fetch("sofascore", {"events": [{"fixture_id": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_sports_lottery",
        recording_fetch("sports_lottery", {"matches": [{"id": "new-blocked"}], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_oddstorm_asian_lines",
        recording_fetch("oddstorm", {"status": "empty", "lines": [], "errors": []}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_crawl4ai",
        recording_fetch("crawl4ai", {"pages": [], "errors": [], "status": "ok"}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_live,
        "archive_source_snapshot",
        lambda *_args, **_kwargs: {},
    )

    snapshot = sync_live._sync_unlocked(
        output,
        now=reference,
        archive_dir=archive,
        intelligence_ledger=None,
        crawl4ai_config_path=tmp_path / "self-issued-authorization.json",
    )

    assert blocked_calls == []
    blocked_facts = {
        "cfl_official": ("fixtures",),
        "espn": ("fixtures",),
        "espn_markets": ("markets", "team_status", "fixture_updates"),
        "espn_rosters": ("rosters",),
        "espn_injuries": ("reports",),
        "understat": ("observations", "team_features"),
        "news": ("news",),
        "geocoding": ("geocodes",),
        "weather": ("weather",),
        "sofascore": ("events",),
        "premier_league_official": ("fixtures", "lineups"),
        "laliga_official": ("fixtures", "lineups"),
        "bundesliga_official": ("fixtures", "lineups"),
        "serie_a_official": ("fixtures", "lineups"),
        "ligue1_official": ("fixtures", "lineups"),
        "sports_lottery": ("matches",),
        "oddstorm": ("lines",),
        "fotmob": ("lineups",),
    }
    for section, fields in blocked_facts.items():
        assert snapshot[section]["status"] == "rights_blocked", section
        assert snapshot[section]["schema_version"] == "matchline.source_rights_result.v1"
        assert snapshot[section]["network_opened"] is False
        for field in fields:
            assert snapshot[section][field] == [], (section, field)
    assert snapshot["crawl4ai"]["status"] == "rights_blocked"
    assert snapshot["crawl4ai"]["pages"] == []
    assert snapshot["crawl4ai"]["network_opened"] is False
    assert all(
        block["schema_version"] == "matchline.source_rights_result.v1"
        for block in snapshot["crawl4ai"]["source_blocks"].values()
    )
    assert all(
        block["rights"]["ignored_inputs"] == ["config"]
        for block in snapshot["crawl4ai"]["source_blocks"].values()
    )
    assert snapshot["fixture_feed"]["provider"] == "OpenFootball"
    assert snapshot["fixture_feed"]["fixtures"] == []
    assert snapshot["openfootball_current"]["fixtures"] == []
    assert snapshot["openfootball_current"]["raw_archive_admission"]["status"] == "blocked"
    assert "raw_archive_producer_receipt" not in snapshot["openfootball_current"]
    assert "old-self-reported" not in json.dumps(snapshot)
    assert snapshot["openligadb"]["matches"][0]["id"] == "openligadb:42"
    assert snapshot["openligadb"]["matches"][0]["model_eligible"] is False
    assert snapshot["openligadb"]["model_eligible"] is False
    assert snapshot["openligadb"]["training_eligible"] is False
    assert snapshot["openligadb"]["redistribution_allowed"] is False
    assert snapshot["openligadb"]["rights"]["use_case"] == "serve_current"
    assert snapshot["openligadb"]["model_rights"]["decision"] == "block"
    assert archived_path.read_bytes() == archived_bytes
    assert json.loads(output.read_text(encoding="utf-8")) == snapshot


def test_empty_fixture_result_never_replays_the_whole_previous_pointer(tmp_path: Path):
    output = tmp_path / "current.json"
    previous = {
        "as_of": "2026-08-17T00:00:00+00:00",
        "espn": {"fixtures": [{"id": "espn:previous"}]},
    }
    attempted = {
        "as_of": "2026-08-17T01:00:00+00:00",
        "espn": {"fixtures": [], "status": "unavailable", "errors": [{"error": "timeout"}]},
    }
    output.write_text(json.dumps(previous), encoding="utf-8")

    handoff = sync_live._guard_empty_snapshot(
        output,
        attempted,
        reference_time=datetime(2026, 8, 17, 1, tzinfo=timezone.utc),
        allow_empty_snapshot=False,
    )

    assert handoff is None
    assert attempted["publication"]["status"] == "allowed_empty"
    assert (
        attempted["publication"]["reason"]
        == "empty_fixture_set_fail_closed_no_current_raw_admission"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == previous
    diagnostic = json.loads((tmp_path / "current.publication.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "allowed"
    assert diagnostic["attempted"]["fixture_count"] == 0
    assert diagnostic["previous"]["fixture_count"] == 1


def test_empty_current_fixture_feed_guard_does_not_depend_on_espn_schema(tmp_path: Path):
    output = tmp_path / "current.json"
    previous = {
        "as_of": "2026-08-17T00:00:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [{"id": "openfootball:previous"}],
        },
    }
    attempted = {
        "as_of": "2026-08-17T01:00:00+00:00",
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [],
            "status": "unavailable",
            "errors": [{"error": "timeout"}],
        },
    }
    output.write_text(json.dumps(previous), encoding="utf-8")

    handoff = sync_live._guard_empty_snapshot(
        output,
        attempted,
        reference_time=datetime(2026, 8, 17, 1, tzinfo=timezone.utc),
        allow_empty_snapshot=False,
    )

    assert handoff is None
    assert attempted["fixture_feed"]["fixtures"] == []
    assert "openfootball:previous" not in json.dumps(attempted)
    diagnostic = json.loads((tmp_path / "current.publication.json").read_text(encoding="utf-8"))
    assert diagnostic["attempted"]["provider"] == "OpenFootball"
    assert diagnostic["attempted"]["fixture_count"] == 0
    assert diagnostic["previous"]["fixture_count"] == 1


def test_sync_polls_met_norway_weather_as_a_separate_attributed_section(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reference = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    calls: list[dict[str, object]] = []

    def fake_openfootball(**_kwargs: object) -> dict[str, object]:
        return {
            "provider": "OpenFootball",
            "retrieved_at": reference.isoformat(),
            "status": "unavailable",
            "fixtures": [],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [],
            "upcoming_7_days": [],
            "errors": [],
            "source_contract": {
                "fact_source": "OpenFootball",
                "rights": sync_live.decide_source_rights(
                    SourceId.OPENFOOTBALL_CURRENT,
                    UseCase.NETWORK_FETCH,
                ).as_dict(),
            },
        }

    def fake_met(fixtures: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
        calls.append({"fixtures": fixtures, **kwargs})
        return {
            "provider": "MET Norway Locationforecast",
            "source_id": "met_norway_weather",
            "status": "unavailable",
            "retrieved_at": reference.isoformat(),
            "network_opened": False,
            "weather": [],
            "errors": [{"fixture_id": "", "reason": "venue_coordinates_missing"}],
            "license": "CC-BY-4.0",
            "attribution_required": True,
        }

    monkeypatch.setattr(sync_live, "fetch_openfootball_current", fake_openfootball)
    monkeypatch.setattr(
        sync_live,
        "_verify_openfootball_current_from_raw_archive",
        lambda source, **_kwargs: {
            **source,
            "raw_archive_admission": {"status": "blocked"},
        },
    )
    monkeypatch.setattr(
        sync_live,
        "fetch_openligadb_fixtures",
        lambda **_kwargs: {
            "provider": "OpenLigaDB",
            "retrieved_at": reference.isoformat(),
            "status": "unavailable",
            "matches": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(sync_live, "fetch_met_norway_weather", fake_met)

    snapshot = sync_live._sync_unlocked(
        tmp_path / "current.json",
        now=reference,
        archive_dir=tmp_path / "archive",
        openfootball_raw_archive_dir=tmp_path / "openfootball-raw",
        intelligence_ledger=None,
        allow_empty_snapshot=True,
    )

    assert len(calls) == 1
    assert calls[0]["fixtures"] == []
    assert calls[0]["user_agent"] == sync_live.MET_NORWAY_USER_AGENT
    assert calls[0]["allow_display_only_coordinates"] is True
    assert snapshot["met_norway_weather"]["provider"] == "MET Norway Locationforecast"
    assert snapshot["met_norway_weather"]["weather"] == []
    assert snapshot["met_norway_weather"]["errors"] == [
        {"reason": "venue_coordinates_missing", "count": 1}
    ]


def test_met_weather_diagnostics_are_publicly_bounded_and_do_not_leak_fixture_ids() -> None:
    compact = sync_live._compact_met_weather_diagnostics(
        {
            "provider": "MET Norway Locationforecast",
            "status": "unavailable",
            "weather": [],
            "network_opened": False,
            "error_count": 120,
            "errors": [
                {"fixture_id": f"fixture:{index}", "reason": "venue_coordinates_missing"}
                for index in range(63)
            ]
            + [{"fixture_id": "", "reason": "errors_truncated:57_additional"}],
        }
    )

    assert compact["errors"] == [
        {"reason": "venue_coordinates_missing", "count": 63},
        {"reason": "additional_errors_omitted", "count": 57},
    ]
    assert "fixture_id" not in json.dumps(compact["errors"])


def test_empty_fixture_result_requires_explicit_override(tmp_path: Path):
    output = tmp_path / "current.json"
    output.write_text(
        json.dumps(
            {
                "as_of": "2026-08-17T00:00:00+00:00",
                "espn": {"fixtures": [{"id": "espn:previous"}]},
            }
        ),
        encoding="utf-8",
    )
    attempted = {
        "as_of": "2026-08-17T01:00:00+00:00",
        "espn": {"fixtures": [], "status": "season_complete", "errors": []},
    }

    assert (
        sync_live._guard_empty_snapshot(
            output,
            attempted,
            reference_time=datetime(2026, 8, 17, 1, tzinfo=timezone.utc),
            allow_empty_snapshot=True,
        )
        is None
    )
    diagnostic = json.loads((tmp_path / "current.publication.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "allowed"
    assert diagnostic["reason"] == "empty_fixture_set_explicitly_allowed"


def test_empty_fixture_bootstrap_is_allowed_without_a_previous_pointer(tmp_path: Path):
    output = tmp_path / "current.json"
    attempted = {"as_of": "2026-08-17T01:00:00+00:00", "espn": {"fixtures": []}}

    assert (
        sync_live._guard_empty_snapshot(
            output,
            attempted,
            reference_time=datetime(2026, 8, 17, 1, tzinfo=timezone.utc),
            allow_empty_snapshot=False,
        )
        is None
    )
    assert not (tmp_path / "current.publication.json").exists()


def test_successful_snapshot_clears_stale_publication_block(tmp_path: Path):
    output = tmp_path / "current.json"
    sync_live._write_json_atomic(
        tmp_path / "current.publication.json",
        {"status": "blocked", "reason": "old_failure"},
    )

    sync_live._write_published_diagnostic(
        output,
        {
            "as_of": "2026-08-17T02:00:00+00:00",
            "espn": {"fixtures": [{"id": "espn:current"}]},
        },
        reference_time=datetime(2026, 8, 17, 2, tzinfo=timezone.utc),
        allow_empty_snapshot=False,
    )

    diagnostic = json.loads((tmp_path / "current.publication.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "published"
    assert diagnostic["reason"] == "snapshot_published"
    assert diagnostic["published"]["fixture_count"] == 1


def test_oddstorm_empty_response_never_reuses_blocked_recent_lines():
    previous = {
        "as_of": "2026-08-20T02:00:00+00:00",
        "oddstorm": {
            "status": "available",
            "raw_sha256": "p" * 64,
            "lines": [{"match_id": "m1", "source": {"retrieved_at": "2026-08-20T02:00:00+00:00"}}],
        },
    }
    attempted = {
        "status": "empty",
        "raw_sha256": "c" * 64,
        "lines": [],
        "errors": [{"stage": "parse", "error": "no_complete_market_rows"}],
    }

    result = _reuse_recent_oddstorm_snapshot(
        attempted,
        previous,
        reference_time=datetime(2026, 8, 20, 2, 15, tzinfo=timezone.utc),
    )

    assert result is attempted
    assert result["status"] == "empty"
    assert result["lines"] == []
    assert "stale_fallback" not in result


def test_oddstorm_stale_fallback_rejects_old_or_empty_previous_snapshot():
    attempted = {"status": "empty", "lines": [], "errors": []}
    old = {
        "as_of": "2026-08-19T00:00:00+00:00",
        "oddstorm": {"lines": [{"match_id": "old"}]},
    }
    empty = {
        "as_of": "2026-08-20T02:00:00+00:00",
        "oddstorm": {"lines": []},
    }

    assert (
        _reuse_recent_oddstorm_snapshot(
            attempted,
            old,
            reference_time=datetime(2026, 8, 20, 2, 15, tzinfo=timezone.utc),
        )
        is attempted
    )
    assert (
        _reuse_recent_oddstorm_snapshot(
            attempted,
            empty,
            reference_time=datetime(2026, 8, 20, 2, 15, tzinfo=timezone.utc),
        )
        is attempted
    )


def test_oddstorm_block_does_not_recover_from_append_only_archive_when_pointer_is_empty(
    tmp_path: Path,
):
    archive = tmp_path / "archive"
    raw_dir = archive / "raw"
    raw_dir.mkdir(parents=True)
    archived = {
        "as_of": "2026-08-20T01:30:00+00:00",
        "oddstorm": {
            "lines": [{"match_id": "archived"}],
            "raw_sha256": "a" * 64,
        },
    }
    raw_path = raw_dir / "snapshot.json"
    raw_path.write_text(json.dumps(archived), encoding="utf-8")
    (archive / "snapshots.jsonl").write_text(
        json.dumps({"as_of": archived["as_of"], "raw_path": "raw/snapshot.json"}) + "\n",
        encoding="utf-8",
    )

    result = _load_recent_oddstorm_snapshot(
        {"as_of": "2026-08-20T02:00:00+00:00", "oddstorm": {"lines": []}},
        archive,
        reference_time=datetime(2026, 8, 20, 2, 15, tzinfo=timezone.utc),
    )

    assert result is None
    assert json.loads(raw_path.read_text(encoding="utf-8")) == archived
