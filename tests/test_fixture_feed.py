from datetime import datetime, timezone
from datetime import timedelta

import pytest

from league_platform.fixture_feed import (
    MULTI_SOURCE_FIXTURE_PROVIDER,
    apply_official_schedule_overlay,
    fixture_feed_key,
    fixture_feed_section,
    fixture_rows,
    merge_current_fixture_feeds,
    upcoming_fixture_rows,
)


def test_current_fixture_feed_is_preferred_without_masquerading_as_espn() -> None:
    snapshot = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [{"id": "openfootball:eng.1:example"}],
        },
        "espn": {
            "provider": "ESPN",
            "fixtures": [{"id": "espn:legacy"}],
        },
    }

    assert fixture_feed_key(snapshot) == "fixture_feed"
    assert fixture_feed_section(snapshot)["provider"] == "OpenFootball"
    assert fixture_rows(snapshot) == [{"id": "openfootball:eng.1:example"}]


def test_legacy_espn_fixture_section_remains_replayable() -> None:
    snapshot = {
        "espn": {
            "provider": "ESPN",
            "fixtures": [{"id": "espn:legacy"}],
        }
    }

    assert fixture_feed_key(snapshot) == "espn"
    assert fixture_rows(snapshot) == [{"id": "espn:legacy"}]


def test_present_but_invalid_current_feed_fails_closed_instead_of_falling_back() -> None:
    snapshot = {
        "fixture_feed": "tampered",
        "espn": {"provider": "ESPN", "fixtures": [{"id": "espn:legacy"}]},
    }

    assert fixture_feed_key(snapshot) == "fixture_feed"
    assert fixture_feed_section(snapshot) == {}
    assert fixture_rows(snapshot) == []


def test_non_list_fixture_rows_fail_closed() -> None:
    snapshot = {"fixture_feed": {"provider": "OpenFootball", "fixtures": {"id": "bad"}}}

    assert fixture_rows(snapshot) == []


def test_upcoming_date_only_rows_respect_bounded_window() -> None:
    snapshot = {
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [
                {
                    "id": "inside",
                    "status": "upcoming",
                    "kickoff_date": "2026-08-31",
                },
                {
                    "id": "outside",
                    "status": "upcoming",
                    "kickoff_date": "2026-10-10",
                },
            ],
        }
    }

    rows = upcoming_fixture_rows(
        snapshot,
        as_of=datetime(2026, 8, 30, 1, tzinfo=timezone.utc),
        horizon=timedelta(days=7),
        include_unparseable_kickoff=True,
    )

    assert [row["id"] for row in rows] == ["inside"]


def test_merge_current_fixture_feeds_preserves_fact_source_identity_and_windows():
    openfootball = {
        "provider": "OpenFootball",
        "retrieved_at": "2026-08-24T12:00:00+00:00",
        "status": "ok",
        "fixtures": [
            {
                "id": "openfootball:premier-league:one",
                "kickoff_at": "2026-08-25T19:00:00+00:00",
                "source": {"name": "OpenFootball"},
            }
        ],
        "date_only_fixtures": [{"id": "openfootball:premier-league:date-only"}],
        "recent_results": [],
        "upcoming_3_days": [{"id": "openfootball:premier-league:one"}],
        "upcoming_7_days": [{"id": "openfootball:premier-league:one"}],
        "errors": [],
        "source_contract": {"fact_source": "OpenFootball", "license": "CC0-1.0"},
    }
    cfl = {
        "provider": "Chinese Professional Football League official",
        "retrieved_at": "2026-08-24T12:00:00+00:00",
        "status": "ok",
        "fixtures": [
            {
                "id": "cfl-official:csl:one",
                "kickoff_at": "2026-08-29T11:35:00+00:00",
                "source": {
                    "name": "Chinese Professional Football League official",
                    "rights_status": "public_first_party_access_reuse_terms_unverified",
                },
            }
        ],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [{"id": "cfl-official:csl:one"}],
        "errors": [],
        "source_contract": {
            "fact_source": "Chinese Professional Football League official",
            "license": "not_published",
        },
    }

    merged = merge_current_fixture_feeds(
        [openfootball, cfl],
        retrieved_at="2026-08-24T12:00:00+00:00",
    )

    assert merged["provider"] == MULTI_SOURCE_FIXTURE_PROVIDER
    assert merged["status"] == "ok"
    assert [row["id"] for row in merged["fixtures"]] == [
        "openfootball:premier-league:one",
        "cfl-official:csl:one",
    ]
    assert [row["source"]["name"] for row in merged["fixtures"]] == [
        "OpenFootball",
        "Chinese Professional Football League official",
    ]
    assert [row["id"] for row in merged["upcoming_7_days"]] == [
        "openfootball:premier-league:one",
        "cfl-official:csl:one",
    ]
    assert merged["date_only_fixtures"] == [
        {"id": "openfootball:premier-league:date-only"}
    ]
    assert merged["source_contract"]["aggregation_role"] == (
        "provider-neutral union; row source remains authoritative"
    )
    assert merged["source_contract"]["fact_sources"] == [
        "OpenFootball",
        "Chinese Professional Football League official",
    ]


def test_merge_current_fixture_feeds_rejects_duplicate_canonical_ids():
    feed = {
        "provider": "provider-one",
        "status": "ok",
        "fixtures": [{"id": "duplicate", "kickoff_at": "2026-08-25T00:00:00+00:00"}],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [],
        "errors": [],
    }
    duplicate = {**feed, "provider": "provider-two"}

    try:
        merge_current_fixture_feeds(
            [feed, duplicate], retrieved_at="2026-08-24T12:00:00+00:00"
        )
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("duplicate canonical fixture IDs must fail closed")


def test_upcoming_fixture_rows_deduplicates_compact_and_feed_rows() -> None:
    row = {
        "id": "same-fixture",
        "status": "upcoming",
        "kickoff_at": "2026-08-31T12:00:00Z",
    }
    snapshot = {
        "matches": [dict(row)],
        "fixture_feed": {
            "provider": "OpenFootball",
            "fixtures": [dict(row)],
        },
    }

    rows = upcoming_fixture_rows(
        snapshot,
        as_of=datetime(2026, 8, 30, tzinfo=timezone.utc),
        horizon=timedelta(days=3),
    )

    assert [item["id"] for item in rows] == ["same-fixture"]


def test_upcoming_fixture_rows_rejects_negative_horizon() -> None:
    with pytest.raises(ValueError, match="horizon"):
        upcoming_fixture_rows(
            {"matches": []},
            as_of=datetime(2026, 8, 30, tzinfo=timezone.utc),
            horizon=timedelta(days=-1),
        )


def test_merge_current_fixture_feeds_deduplicates_date_only_and_window_rows():
    row = {"id": "date-only", "status": "upcoming", "kickoff_date": "2026-08-31"}
    exact = {
        "id": "exact",
        "status": "upcoming",
        "kickoff_at": "2026-08-31T12:00:00Z",
    }
    feed = {
        "provider": "OpenFootball",
        "retrieved_at": "2026-08-30T00:00:00Z",
        "status": "ok",
        "fixtures": [exact],
        "date_only_fixtures": [row, dict(row)],
        "recent_results": [],
        "upcoming_3_days": [exact, dict(exact)],
        "upcoming_7_days": [exact, dict(exact)],
        "errors": [],
    }

    merged = merge_current_fixture_feeds(
        [feed], retrieved_at="2026-08-30T00:00:00Z"
    )

    assert [item["id"] for item in merged["date_only_fixtures"]] == ["date-only"]
    assert [item["id"] for item in merged["upcoming_3_days"]] == ["exact"]
    assert [item["id"] for item in merged["upcoming_7_days"]] == ["exact"]


def test_official_schedule_overlay_corrects_fields_without_relabelling_canonical_source():
    reference = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    source = {
        "name": "OpenFootball",
        "source_id": "openfootball:football.json:2026-27:en.1",
        "url": "https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json",
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "a" * 64,
        "license": "CC0-1.0",
    }
    fixture = {
        "id": "openfootball:premier-league:palace-city",
        "provider_fixture_ids": {
            "OpenFootball": "openfootball:premier-league:palace-city",
        },
        "competition_id": "premier-league",
        "season": "2026-27",
        "kickoff_at": "2026-08-29T14:00:00+00:00",
        "kickoff_date": "2026-08-29",
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "OpenFootball",
        "home_team": "Crystal Palace FC",
        "away_team": "Manchester City FC",
        "status": "upcoming",
        "score": None,
        "source": source,
        "lineage": {**source, "source_row_id": "row-1"},
    }
    feed = {
        "provider": MULTI_SOURCE_FIXTURE_PROVIDER,
        "retrieved_at": reference.isoformat(),
        "status": "ok",
        "fixtures": [fixture],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [fixture],
        "errors": [],
        "provider_status": [],
        "source_contract": {
            "aggregation_role": "provider-neutral union; row source remains authoritative",
            "fact_sources": ["OpenFootball"],
        },
    }
    official_source = {
        "name": "Premier League official",
        "url": (
            "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1/"
            "competitions/8/seasons/2026/matchweeks/2/matches"
        ),
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "b" * 64,
        "native_fixture_id": "2645209",
        "source_kind": "official_fixture",
    }
    official = {
        "id": "premierleague:2645209",
        "competition_id": "premier-league",
        "season": "2026",
        "kickoff_at": "2026-08-28T19:00:00+00:00",
        "home_team": "Crystal Palace",
        "away_team": "Manchester City",
        "status": "upcoming",
        "venue": "Selhurst Park, London",
        "source": official_source,
    }

    result = apply_official_schedule_overlay(
        feed,
        [official],
        competition_id="premier-league",
        provider="Premier League official",
        provider_id_key="premier_league_official",
        country="England",
        country_code="GB",
        reference_time=reference,
    )

    row = result["fixtures"][0]
    assert feed["fixtures"][0]["kickoff_at"] == "2026-08-29T14:00:00+00:00"
    assert row["id"] == fixture["id"]
    assert row["source"] == source
    assert row["lineage"] == fixture["lineage"]
    assert row["kickoff_at"] == official["kickoff_at"]
    assert row["kickoff_date"] == "2026-08-28"
    assert row["kickoff_time_source"] == "Premier League official"
    assert row["kickoff_time_observed_at"] == reference.isoformat()
    assert row["provider_fixture_ids"] == {
        "OpenFootball": "openfootball:premier-league:palace-city",
        "premier_league_official": "2645209"
    }
    assert row["venue"]["name"] == "Selhurst Park"
    assert row["venue"]["city"] == "London"
    assert row["venue"]["country"] == "England"
    assert row["venue"]["country_code"] == "GB"
    assert row["field_sources"]["kickoff_at"] == official_source
    assert row["field_sources"]["venue"] == official_source
    assert row["schedule_overlay"] == {
        "provider": "Premier League official",
        "provider_fixture_id": "2645209",
        "join_basis": "exact_competition_and_explicit_canonical_team_pair",
        "observed_at": reference.isoformat(),
        "prior_kickoff_at": "2026-08-29T14:00:00+00:00",
        "kickoff_delta_seconds": -68400,
        "fields": ["kickoff_at", "venue"],
    }
    assert result["upcoming_7_days"][0]["kickoff_at"] == official["kickoff_at"]
    assert result["schedule_overlay_diagnostics"]["matched_count"] == 1
    assert result["schedule_overlay_diagnostics"]["kickoff_changed_count"] == 1
    assert result["schedule_overlay_diagnostics"]["venue_attached_count"] == 1
    assert result["schedule_overlay_diagnostics_by_competition"]["premier-league"] == result[
        "schedule_overlay_diagnostics"
    ]


def test_second_official_overlay_preserves_each_competition_diagnostics():
    reference = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    canonical_source = {
        "name": "OpenFootball",
        "source_id": "openfootball:italy:2026-27:1-seriea",
        "url": "https://raw.githubusercontent.com/openfootball/italy/master/2026-27/1-seriea.txt",
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "a" * 64,
        "license": "CC0-1.0",
    }
    fixture = {
        "id": "openfootball:serie-a:inter-monza",
        "competition_id": "serie-a",
        "season": "2026-27",
        "kickoff_at": "2026-08-25T18:00:00+00:00",
        "kickoff_date": "2026-08-25",
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "OpenFootball",
        "home_team": "FC Internazionale Milano",
        "away_team": "AC Monza",
        "status": "upcoming",
        "score": None,
        "source": canonical_source,
        "lineage": {**canonical_source, "source_row_id": "row-1"},
    }
    feed = {
        "provider": MULTI_SOURCE_FIXTURE_PROVIDER,
        "retrieved_at": reference.isoformat(),
        "status": "ok",
        "fixtures": [fixture],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [fixture],
        "upcoming_7_days": [fixture],
        "errors": [],
        "schedule_overlay_diagnostics": {"competition_id": "premier-league"},
        "schedule_overlay_diagnostics_by_competition": {
            "premier-league": {"competition_id": "premier-league"}
        },
    }
    official_source = {
        "name": "Serie A official",
        "url": (
            "https://api-sdp.legaseriea.it/v1/serie-a/football/seasons/"
            "serie-a%3A%3AFootball_Season%3A%3A" + "b" * 32 + "/matches/"
            "serie-a%3A%3AFootball_Match%3A%3A" + "c" * 32 + "/header"
        ),
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "b" * 64,
        "native_fixture_id": "c" * 32,
    }
    official = {
        "id": fixture["id"],
        "competition_id": "serie-a",
        "kickoff_at": fixture["kickoff_at"],
        "home_team": "Inter",
        "away_team": "Monza",
        "status": "upcoming",
        "venue": {"name": "Stadio Giuseppe Meazza", "city": "Milan"},
        "source": official_source,
    }

    result = apply_official_schedule_overlay(
        feed,
        [official],
        competition_id="serie-a",
        provider="Serie A official",
        provider_id_key="serie_a_official",
        country="Italy",
        country_code="IT",
        reference_time=reference,
    )

    assert result["schedule_overlay_diagnostics"]["competition_id"] == "premier-league"
    assert result["schedule_overlay_diagnostics_by_competition"]["serie-a"][
        "matched_count"
    ] == 1
    assert result["fixtures"][0]["venue"]["city"] == "Milan"


def test_official_schedule_overlay_quarantines_ambiguous_team_pairs():
    reference = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    fixture = {
        "id": "openfootball:premier-league:palace-city",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-29T14:00:00+00:00",
        "home_team": "Crystal Palace FC",
        "away_team": "Manchester City FC",
        "status": "upcoming",
        "source": {"name": "OpenFootball"},
    }
    feed = {
        "provider": MULTI_SOURCE_FIXTURE_PROVIDER,
        "retrieved_at": reference.isoformat(),
        "status": "ok",
        "fixtures": [fixture],
        "date_only_fixtures": [],
        "recent_results": [],
        "upcoming_3_days": [],
        "upcoming_7_days": [fixture],
        "errors": [],
        "source_contract": {},
    }
    source = {
        "name": "Premier League official",
        "url": "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v1/competitions/8/seasons/2026/matchweeks/2/matches",
        "retrieved_at": reference.isoformat(),
        "raw_sha256": "b" * 64,
        "native_fixture_id": "2645209",
    }
    official = {
        "id": "premierleague:2645209",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-28T19:00:00+00:00",
        "home_team": "Crystal Palace",
        "away_team": "Manchester City",
        "status": "upcoming",
        "source": source,
    }
    duplicate = {
        **official,
        "id": "premierleague:duplicate",
        "source": {**source, "native_fixture_id": "duplicate"},
    }

    result = apply_official_schedule_overlay(
        feed,
        [official, duplicate],
        competition_id="premier-league",
        provider="Premier League official",
        provider_id_key="premier_league_official",
        country="England",
        country_code="GB",
        reference_time=reference,
    )

    assert result["fixtures"][0]["kickoff_at"] == fixture["kickoff_at"]
    assert "field_sources" not in result["fixtures"][0]
    diagnostics = result["schedule_overlay_diagnostics"]
    assert diagnostics["matched_count"] == 0
    assert diagnostics["ambiguous_count"] == 1
    assert diagnostics["samples"][0]["reason"] == "ambiguous_team_pair"
