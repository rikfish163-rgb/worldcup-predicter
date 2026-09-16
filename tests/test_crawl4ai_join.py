from __future__ import annotations

from league_platform.crawl4ai_join import (
    attach_crawl4ai_fixture_joins,
    attach_whoscored_fixture_joins,
    canonical_team_key,
    classify_public_page_admission,
    join_public_page_records,
    join_whoscored_records,
    parse_whoscored_kickoff,
)
from league_platform.ingestion import snapshot_observations
from league_platform.current import _crawl4ai_whoscored_features
from datetime import datetime, timezone


def _record(**overrides):
    value = {
        "source_match_id": "1983546",
        "match_url": "https://www.whoscored.com/matches/1983546/show/arsenal-coventry",
        "date_label": "Friday, Aug 21 2026",
        "kickoff_time_label": "20:00",
        "time_semantics": "source_display_label_no_timezone",
        "home_team": "Arsenal",
        "away_team": "Coventry",
        "status": "scheduled",
        "one_x_two_odds": {"home": 1.17, "draw": 7.5, "away": 15.0},
    }
    value.update(overrides)
    return value


def _fixture(**overrides):
    value = {
        "id": "espn:401879301",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-21T19:00:00+00:00",
        "home_team": "Arsenal",
        "away_team": "Coventry City",
        "status": "upcoming",
    }
    value.update(overrides)
    return value


def test_whoscored_time_uses_competition_timezone_and_returns_utc():
    parsed = parse_whoscored_kickoff("Friday, Aug 21 2026", "20:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-08-21T19:00:00+00:00"


def test_team_aliases_are_explicit_and_do_not_collapse_manchester_clubs():
    assert canonical_team_key("AFC Bournemouth") == canonical_team_key("Bournemouth")
    assert canonical_team_key("Coventry") == canonical_team_key("Coventry City")
    assert canonical_team_key("Manchester City") != canonical_team_key("Manchester United")


def test_exact_join_carries_canonical_id_and_keeps_model_quarantined():
    result = join_whoscored_records([_record()], [_fixture()])
    assert result["summary"]["status"] == "exact"
    assert result["summary"]["joined_count"] == 1
    joined = result["joined"][0]
    assert joined["fixture_id"] == "espn:401879301"
    assert joined["kickoff_delta_seconds"] == 0
    assert joined["source_kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert joined["canonical_kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert joined["pre_match_eligible"] is True
    assert joined["enters_model"] is False
    assert "license" in joined["model_exclusion_reason"]
    assert joined["admission"]["status"] == "blocked"
    assert joined["admission"]["reason_code"] == "observed_at_missing_or_invalid"


def test_public_page_admission_requires_license_review_before_model_entry():
    admission = classify_public_page_admission(
        parser_contract="whoscored_public_fixtures_v1",
        join_status="exact",
        pre_match_eligible=True,
        observed_at="2026-08-20T12:00:00+00:00",
        canonical_kickoff_at="2026-08-21T19:00:00+00:00",
    )
    assert admission["status"] == "review_required"
    assert admission["reason_code"] == "source_license_review_required"
    assert admission["enters_model"] is False


def test_public_page_admission_marks_authorized_source_as_candidate_only():
    admission = classify_public_page_admission(
        parser_contract="whoscored_public_fixtures_v1",
        join_status="exact",
        pre_match_eligible=True,
        observed_at="2026-08-20T12:00:00+00:00",
        canonical_kickoff_at="2026-08-21T19:00:00+00:00",
        source_license_status="authorized",
    )
    assert admission["status"] == "candidate_ready"
    assert admission["reason_code"] == "dedicated_source_model_adapter_required"
    assert admission["enters_model"] is False


def test_premier_league_short_display_label_resolves_year_only_from_unique_exact_pair():
    record = _record(
        date_label="Fri 21 Aug",
        kickoff_time_label="20:00",
        home_team="Arsenal",
        away_team="Coventry City",
    )
    result = join_public_page_records(
        [record],
        [_fixture()],
        parser_contract="premier_league_public_fixtures_v1",
    )
    assert result["summary"]["status"] == "exact"
    joined = result["joined"][0]
    assert joined["source_kickoff_at"] == "2026-08-21T19:00:00+00:00"
    assert joined["source_time_resolution"] == "year_resolved_from_unique_canonical_pair"
    assert joined["enters_model"] is False


def test_missing_source_kickoff_is_quarantined_without_aborting_join():
    record = _record(
        kickoff_at=None,
        date_label="Friday, Aug 21 2026",
        kickoff_time_label="20:00",
    )
    result = join_public_page_records(
        [record],
        [_fixture()],
        parser_contract="laliga_public_sports_events_v1",
    )
    assert result["joined"] == []
    assert result["unmatched"][0]["reason"] == "source_kickoff_unparseable"


def test_team_or_time_mismatch_is_retained_as_a_join_diagnostic():
    result = join_whoscored_records(
        [_record(away_team="Liverpool"), _record(kickoff_time_label="21:00")],
        [_fixture()],
    )
    assert result["joined"] == []
    assert len(result["unmatched"]) == 2
    assert {row["reason"] for row in result["unmatched"]} == {"no_exact_team_and_kickoff_match"}


def test_laliga_sports_event_jsonld_joins_by_embedded_iso8601_and_pair():
    record = {
        "source_event_id": "https://www.laliga.com/partido/atletico-malaga",
        "match_url": "https://www.laliga.com/partido/atletico-malaga",
        "kickoff_at": "2026-08-19T19:00:00+00:00",
        "home_team": "Atlético de Madrid",
        "away_team": "Málaga CF",
        "status": "scheduled",
    }
    fixture = {
        "id": "espn:401882925",
        "competition_id": "la-liga",
        "kickoff_at": "2026-08-19T19:00:00+00:00",
        "home_team": "Atlético de Madrid",
        "away_team": "Málaga CF",
        "status": "upcoming",
    }
    result = join_public_page_records(
        [record],
        [fixture],
        parser_contract="laliga_public_sports_events_v1",
    )
    assert result["summary"]["status"] == "exact"
    joined = result["joined"][0]
    assert joined["fixture_id"] == "espn:401882925"
    assert joined["source_timezone"] == "embedded_iso8601"
    assert joined["fact_type"] == "fixture_context"
    assert joined["enters_model"] is False


def test_laliga_jsonld_uses_explicit_provider_aliases_without_fuzzy_join():
    records = [
        {
            "source_event_id": "laliga:atletico-malaga",
            "kickoff_at": "2026-08-19T19:00:00+00:00",
            "home_team": "Atlético de Madrid",
            "away_team": "Málaga CF",
        },
        {
            "source_event_id": "laliga:deportivo-getafe",
            "kickoff_at": "2026-08-15T17:30:00+00:00",
            "home_team": "Deportivo Alavés",
            "away_team": "Getafe CF",
        },
    ]
    fixtures = [
        {
            "id": "espn:laliga-atletico-malaga",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-19T19:00:00+00:00",
            "home_team": "Atlético Madrid",
            "away_team": "Málaga",
            "status": "upcoming",
        },
        {
            "id": "espn:laliga-alaves-getafe",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-15T17:30:00+00:00",
            "home_team": "Alavés",
            "away_team": "Getafe",
            "status": "finished",
        },
    ]
    result = join_public_page_records(
        records,
        fixtures,
        parser_contract="laliga_public_sports_events_v1",
        observed_at="2026-08-14T12:00:00+00:00",
    )
    assert result["summary"]["joined_count"] == 2
    assert result["unmatched"] == []
    assert [row["fixture_id"] for row in result["joined"]] == [
        "espn:laliga-atletico-malaga",
        "espn:laliga-alaves-getafe",
    ]


def test_seriea_match_card_joins_by_source_declared_utc_kickoff():
    record = {
        "source_match_id": "1cc7b922e8d44f93a52fb4bf8858e454",
        "match_url": "https://en.legaseriea.it/serie-a/match/1cc7b922e8d44f93a52fb4bf8858e454/inter-vs-monza",
        "kickoff_at": "2026-08-22T16:30:00+00:00",
        "time_semantics": "source_declared_matchDateUtc",
        "source_timezone": "UTC",
        "home_team": "Inter",
        "away_team": "Monza",
        "status": "scheduled",
        "enters_model": False,
    }
    fixture = {
        "id": "espn:401900001",
        "competition_id": "serie-a",
        "kickoff_at": "2026-08-22T16:30:00+00:00",
        "home_team": "Internazionale",
        "away_team": "Monza",
        "status": "upcoming",
    }
    result = join_public_page_records(
        [record],
        [fixture],
        parser_contract="seriea_public_fixtures_v1",
        source_license_status="authorized",
        observed_at="2026-08-20T12:00:00+00:00",
    )
    assert result["summary"]["status"] == "exact"
    joined = result["joined"][0]
    assert joined["fixture_id"] == "espn:401900001"
    assert joined["source_timezone"] == "embedded_iso8601"
    assert joined["kickoff_delta_seconds"] == 0
    assert joined["enters_model"] is False
    assert joined["admission"]["status"] == "candidate_ready"


def test_bundesliga_sports_event_jsonld_joins_by_iso8601_and_explicit_aliases():
    record = {
        "source_event_id": "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/fc-bayern-muenchen-vs-vfb-stuttgart/liveticker",
        "match_url": "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1/fc-bayern-muenchen-vs-vfb-stuttgart/liveticker",
        "kickoff_at": "2026-08-28T18:30:00+0000",
        "home_team": "FC Bayern München",
        "away_team": "VfB Stuttgart",
        "status": "https://schema.org/EventScheduled",
    }
    fixture = {
        "id": "espn:401884817",
        "competition_id": "bundesliga",
        "kickoff_at": "2026-08-28T18:30:00+00:00",
        "home_team": "Bayern Munich",
        "away_team": "VfB Stuttgart",
        "status": "upcoming",
    }
    result = join_public_page_records(
        [record],
        [fixture],
        parser_contract="sports_event_jsonld_v1",
    )
    assert result["summary"]["status"] == "exact"
    assert result["summary"]["timezone"] == "embedded_iso8601"
    joined = result["joined"][0]
    assert joined["fixture_id"] == "espn:401884817"
    assert joined["kickoff_delta_seconds"] == 0
    assert joined["source_timezone"] == "embedded_iso8601"
    assert joined["enters_model"] is False


def test_bundesliga_jsonld_does_not_join_a_different_competition():
    record = {
        "kickoff_at": "2026-08-28T18:30:00+0000",
        "home_team": "FC Bayern München",
        "away_team": "VfB Stuttgart",
    }
    fixture = {
        "id": "espn:premier-league-lookalike",
        "competition_id": "premier-league",
        "kickoff_at": "2026-08-28T18:30:00+00:00",
        "home_team": "Bayern Munich",
        "away_team": "VfB Stuttgart",
        "status": "upcoming",
    }
    result = join_public_page_records(
        [record],
        [fixture],
        parser_contract="sports_event_jsonld_v1",
    )
    assert result["joined"] == []
    assert result["unmatched"][0]["reason"] == "no_exact_team_and_kickoff_match"


def test_crawl4ai_attachment_routes_laliga_context_without_promoting_it():
    snapshot = {
        "pages": [{
            "source": "LaLiga official public match pages operator source",
            "source_id": "laliga_public_pages",
            "source_tier": "official",
            "url": "https://www.laliga.com/en-GB",
            "observed_at": "2026-08-18T11:55:46+00:00",
            "policy": {"license_status": "authorized"},
            "raw_sha256": "c" * 64,
            "extraction": {"parser": "laliga_public_sports_events_v1", "record_count": 1},
            "extracted_records": [{
                "source_event_id": "https://www.laliga.com/partido/atletico-malaga",
                "match_url": "https://www.laliga.com/partido/atletico-malaga",
                "kickoff_at": "2026-08-19T19:00:00+00:00",
                "home_team": "Atlético de Madrid",
                "away_team": "Málaga CF",
                "status": "scheduled",
            }],
        }],
    }
    annotated = attach_crawl4ai_fixture_joins(snapshot, [{
        "id": "espn:401882925",
        "competition_id": "la-liga",
        "kickoff_at": "2026-08-19T19:00:00+00:00",
        "home_team": "Atlético de Madrid",
        "away_team": "Málaga CF",
        "status": "upcoming",
    }])
    page = annotated["pages"][0]
    assert annotated["fixture_join"]["joined_count"] == 1
    assert page["joined_fixture_records"][0]["fact_type"] == "fixture_context"
    assert page["joined_fixture_records"][0]["enters_model"] is False
    assert page["joined_fixture_records"][0]["admission"]["status"] == "candidate_ready"
    assert annotated["fixture_join"]["admission_counts"] == {"candidate_ready": 1}


def test_crawl4ai_attachment_routes_bundesliga_jsonld_without_promoting_it():
    snapshot = {
        "pages": [{
            "source": "Bundesliga official public match pages operator source",
            "source_id": "bundesliga_public_pages",
            "url": "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1",
            "observed_at": "2026-08-18T11:55:46+00:00",
            "raw_sha256": "e" * 64,
            "extraction": {"parser": "sports_event_jsonld_v1", "record_count": 1},
            "extracted_records": [{
                "source_event_id": "https://www.bundesliga.com/match/bayern-stuttgart",
                "match_url": "https://www.bundesliga.com/match/bayern-stuttgart",
                "kickoff_at": "2026-08-28T18:30:00+0000",
                "home_team": "FC Bayern München",
                "away_team": "VfB Stuttgart",
                "status": "scheduled",
            }],
        }],
    }
    annotated = attach_crawl4ai_fixture_joins(snapshot, [{
        "id": "espn:401884817",
        "competition_id": "bundesliga",
        "kickoff_at": "2026-08-28T18:30:00+00:00",
        "home_team": "Bayern Munich",
        "away_team": "VfB Stuttgart",
        "status": "upcoming",
    }])
    page = annotated["pages"][0]
    assert annotated["fixture_join"]["joined_count"] == 1
    assert page["fixture_join"]["parser"] == "sports_event_jsonld_v1"
    assert page["joined_fixture_records"][0]["fixture_id"] == "espn:401884817"
    assert page["joined_fixture_records"][0]["enters_model"] is False


def test_crawl4ai_attachment_rejects_parser_source_identity_mismatch():
    """A page's declared fact source must own the parser used for joining."""

    snapshot = {
        "pages": [{
            "source": "WhoScored page carrying a LaLiga parser",
            "source_id": "whoscored_public_pages",
            "fact_source_id": "whoscored_public_pages",
            "parser_contract": "whoscored_public_fixtures_v1",
            "extraction": {
                "parser": "laliga_public_sports_events_v1",
                "record_count": 1,
            },
            "extracted_records": [{
                "source_event_id": "https://www.laliga.com/partido/atletico-malaga",
                "kickoff_at": "2026-08-19T19:00:00+00:00",
                "home_team": "Atlético de Madrid",
                "away_team": "Málaga CF",
            }],
        }],
    }
    annotated = attach_crawl4ai_fixture_joins(
        snapshot,
        [{
            "id": "espn:401882925",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-19T19:00:00+00:00",
            "home_team": "Atlético de Madrid",
            "away_team": "Málaga CF",
            "status": "upcoming",
        }],
    )

    page = annotated["pages"][0]
    assert page["joined_fixture_records"] == []
    assert page["fixture_join"]["status"] == "unavailable"
    assert page["fixture_join"]["reason"] == "parser_contract_mismatch"
    assert annotated["fixture_join"]["joined_count"] == 0


def test_crawl4ai_attachment_rejects_conflicting_page_source_id_fields():
    snapshot = {
        "pages": [{
            "source_id": "whoscored_public_pages",
            "fact_source_id": "laliga_public_pages",
            "parser_contract": "laliga_public_sports_events_v1",
            "extraction": {"parser": "laliga_public_sports_events_v1"},
            "extracted_records": [],
        }],
    }
    annotated = attach_crawl4ai_fixture_joins(snapshot, [])
    page = annotated["pages"][0]
    assert page["fixture_join"]["reason"] == "source_identity_mismatch"
    assert page["joined_fixture_records"] == []


def test_crawl4ai_attachment_rejects_source_id_with_unowned_parser_contract():
    snapshot = {
        "pages": [{
            "source_id": "whoscored_public_pages",
            "fact_source_id": "whoscored_public_pages",
            "parser_contract": "laliga_public_sports_events_v1",
            "extraction": {"parser": "laliga_public_sports_events_v1"},
            "extracted_records": [],
        }],
    }
    annotated = attach_crawl4ai_fixture_joins(snapshot, [])
    page = annotated["pages"][0]
    assert page["fixture_join"]["reason"] == "parser_source_mismatch"
    assert page["joined_fixture_records"] == []


def test_fixture_context_join_is_separate_from_market_observation():
    snapshot = {
        "as_of": "2026-08-18T11:55:46+00:00",
        "espn": {"fixtures": [{
            "id": "espn:401882925",
            "competition_id": "la-liga",
            "kickoff_at": "2026-08-19T19:00:00+00:00",
            "home_team": "Atlético de Madrid",
            "away_team": "Málaga CF",
            "status": "upcoming",
        }]},
        "crawl4ai": {
            "pages": [{
                "source": "LaLiga official public match pages operator source",
                "source_id": "laliga_public_pages",
                "fact_source_id": "laliga_public_pages",
                "capture_engine": "Crawl4AI",
                "crawl_stage": "detail",
                "parent_url": "https://www.laliga.com/en-GB",
                "parent_content_sha256": "p" * 64,
                "content_sha256": "q" * 64,
                "parser_contract": "laliga_public_sports_events_v1",
                "source_tier": "official",
                "url": "https://www.laliga.com/en-GB",
                "observed_at": "2026-08-18T11:55:46+00:00",
                "raw_sha256": "d" * 64,
                "extraction": {"parser": "laliga_public_sports_events_v1", "record_count": 1},
                "extracted_records": [{
                    "source_event_id": "https://www.laliga.com/partido/atletico-malaga",
                    "match_url": "https://www.laliga.com/partido/atletico-malaga",
                    "kickoff_at": "2026-08-19T19:00:00+00:00",
                    "home_team": "Atlético de Madrid",
                    "away_team": "Málaga CF",
                    "status": "scheduled",
                }],
                "robots": {"allowed": True},
            }],
            "errors": [],
        },
    }
    snapshot["crawl4ai"] = attach_crawl4ai_fixture_joins(
        snapshot["crawl4ai"], snapshot["espn"]["fixtures"]
    )
    rows = snapshot_observations(snapshot)
    context = next(row for row in rows if row.kind == "crawl4ai_fixture_context")
    assert context.entity_id == "espn:401882925"
    assert context.enters_model is False
    assert context.payload["fact_type"] == "fixture_context"
    assert context.payload["source_id"] == "laliga_public_pages"
    assert context.payload["capture_engine"] == "Crawl4AI"
    assert context.payload["crawl_stage"] == "detail"
    assert context.payload["parent_content_sha256"] == "p" * 64
    assert context.payload["page_content_sha256"] == "q" * 64


def test_snapshot_annotation_and_ingestion_emit_joined_observation_only():
    snapshot = {
        "as_of": "2026-08-17T15:57:52+00:00",
        "espn": {"fixtures": [_fixture()]},
        "crawl4ai": {
            "provider": "Crawl4AI",
            "status": "ok",
            "retrieved_at": "2026-08-17T15:56:08+00:00",
            "pages": [{
                "source": "WhoScored public match centre operator source",
                "source_tier": "reliable_public_provider",
                "url": "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
                "final_url": "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
                "observed_at": "2026-08-17T15:56:08+00:00",
                "raw_sha256": "a" * 64,
                "extraction": {"parser": "whoscored_public_fixtures_v1", "record_count": 1},
                "extracted_records": [_record()],
                "robots": {"allowed": True},
            }],
            "errors": [],
        },
    }
    annotated = attach_whoscored_fixture_joins(snapshot["crawl4ai"], snapshot["espn"]["fixtures"])
    assert annotated["fixture_join"]["status"] == "exact"
    assert annotated["fixture_join"]["joined_count"] == 1
    snapshot["crawl4ai"] = annotated
    rows = snapshot_observations(snapshot)
    page = next(row for row in rows if row.kind == "crawl4ai_page")
    joined = next(row for row in rows if row.kind == "crawl4ai_fixture_market")
    assert page.payload["fixture_join"]["joined_count"] == 1
    assert joined.entity_id == "espn:401879301"
    assert joined.enters_model is False
    assert joined.payload["one_x_two_odds"]["home"] == 1.17
    assert joined.payload["admission"]["status"] == "review_required"
    assert joined.payload["admission"]["enters_model"] is False


def test_current_projection_exposes_joined_source_as_display_only_feature():
    crawl = {
        "pages": [{
            "source": "WhoScored public match centre operator source",
            "url": "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
            "observed_at": "2026-08-17T15:56:08+00:00",
            "raw_sha256": "b" * 64,
            "joined_fixture_records": [{
                **_record(),
                "fixture_id": "espn:401879301",
                "canonical_home_team": "Arsenal",
                "canonical_away_team": "Coventry City",
                "source_kickoff_at": "2026-08-21T19:00:00+00:00",
                "canonical_kickoff_at": "2026-08-21T19:00:00+00:00",
                "kickoff_delta_seconds": 0,
                "source_timezone": "Europe/London",
                "join_status": "exact",
                "join_confidence": 0.99,
            }],
        }],
    }
    result = _crawl4ai_whoscored_features(
        {"crawl4ai": crawl},
        as_of=datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc),
    )
    feature = result["espn:401879301"]
    assert feature["provider"] == "WhoScored"
    assert feature["one_x_two_odds"]["away"] == 15.0
    assert feature["enters_model"] is False
    assert feature["conflict"] is False
