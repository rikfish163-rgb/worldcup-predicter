import json

from league_platform.ingestion import archive_snapshot_intelligence, snapshot_observations


def _snapshot():
    return {
        "as_of": "2026-08-11T12:00:00+00:00",
        "espn": {
            "fixtures": [{
                "id": "espn:1",
                "competition_id": "premier-league",
                "season": "2026",
                "kickoff_at": "2026-08-12T19:00:00+00:00",
                "home_team": "Home",
                "away_team": "Away",
                "status": "upcoming",
                "score": None,
                "source": {
                    "name": "ESPN",
                    "url": "https://site.api.espn.com/scoreboard",
                    "retrieved_at": "2026-08-11T11:59:00+00:00",
                    "raw_sha256": "a" * 64,
                },
            }]
        },
        "espn_markets": {"markets": [], "errors": []},
        "understat": {"team_features": [], "errors": []},
        "news": {"feeds": [], "errors": []},
        "weather": {"weather": [], "errors": []},
        "sofascore": {"events": [], "errors": []},
    }


def test_snapshot_observations_preserve_provenance():
    observations = snapshot_observations(_snapshot())
    assert len(observations) == 1
    assert observations[0].entity_id == "espn:1"
    assert observations[0].event_time.isoformat() == "2026-08-11T11:59:00+00:00"
    assert observations[0].raw_hash == "a" * 64


def test_espn_injury_team_report_is_archived_without_claiming_complete_health():
    snapshot = _snapshot()
    snapshot["espn_injuries"] = {
        "reports": [
            {
                "competition_id": "premier-league",
                "provider_team_id": "359",
                "team_name": "Arsenal",
                "report_status": "published",
                "retrieved_at": "2026-08-11T11:58:00+00:00",
                "reported_player_count": 1,
                "scheduled_fixture_ids": ["espn:1"],
                "injuries": [
                    {
                        "provider_player_id": "123",
                        "name": "Example Player",
                        "status": "Out",
                    }
                ],
                "model_eligible": False,
                "enters_model": False,
                "source": {
                    "name": "ESPN injury report",
                    "url": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/injuries",
                    "retrieved_at": "2026-08-11T11:58:00+00:00",
                    "raw_sha256": "d" * 64,
                    "policy": {"allow_model": False},
                },
            }
        ]
    }

    rows = snapshot_observations(snapshot)
    report = next(row for row in rows if row.kind == "player_availability_report")

    assert report.entity_type == "team"
    assert report.entity_id == "espn-team:premier-league:359"
    assert report.payload["coverage_semantics"] == "published_bucket_only"
    assert report.enters_model is False
    assert report.model_exclusion_reason == (
        "league_report_not_fixture_complete_and_provider_terms_require_review"
    )


def test_finished_fixture_emits_separate_regulation_result_observation():
    snapshot = _snapshot()
    fixture = snapshot["espn"]["fixtures"][0]
    fixture.update({
        "status": "finished",
        "score": {"home": 2, "away": 1},
        "halftime_score": {"home": 1, "away": 0},
        "result_scope": "regulation_90",
    })
    rows = snapshot_observations(snapshot)
    assert [row.kind for row in rows] == ["fixture_schedule", "match_status", "match_result"]
    status = rows[1]
    assert status.enters_model is False
    assert status.model_exclusion_reason == "live_or_postmatch_status"
    result = rows[2]
    assert result.enters_model is False
    assert result.model_exclusion_reason == "postmatch_result_not_a_pre_match_feature"
    assert result.payload["score"] == {"home": 2, "away": 1}
    assert result.payload["score_scope"] == "regulation_90"


def test_extra_time_result_is_archived_but_kept_out_of_90_minute_projection():
    snapshot = _snapshot()
    fixture = snapshot["espn"]["fixtures"][0]
    fixture.update({"status": "finished", "score": {"home": 2, "away": 1}, "result_scope": "extra_time"})
    rows = snapshot_observations(snapshot)
    result = next(row for row in rows if row.kind == "match_result")
    assert result.payload["score_scope"] == "extra_time"
    assert result.enters_model is False


def test_live_status_preserves_score_clock_and_period_as_display_only():
    snapshot = _snapshot()
    fixture = snapshot["espn"]["fixtures"][0]
    fixture.update({
        "status": "live",
        "score": {"home": 2, "away": 1},
        "clock": "67:14",
        "period": 2,
        "status_text": "2nd Half",
        "status_observed_at": "2026-08-11T12:00:00+00:00",
    })
    status = next(row for row in snapshot_observations(snapshot) if row.kind == "match_status")
    assert status.payload["score"] == {"home": 2, "away": 1}
    assert status.payload["clock"] == "67:14"
    assert status.payload["period"] == 2
    assert status.payload["status_text"] == "2nd Half"
    assert status.observed_at == "2026-08-11T12:00:00+00:00"
    assert status.enters_model is False
    assert status.model_exclusion_reason == "live_or_postmatch_status"


def test_espn_incidents_are_archived_as_display_only_observations():
    snapshot = _snapshot()
    snapshot["espn_markets"] = {
        "markets": [],
        "incidents": [{
            "fixture_id": "espn:1",
            "retrieved_at": "2026-08-11T11:59:00+00:00",
            "events": [{"type": "Goal", "minute": "45+2", "score": {"home": 1, "away": 0}}],
            "source": {
                "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
                "raw_sha256": "b" * 64,
            },
        }],
    }
    event = next(row for row in snapshot_observations(snapshot) if row.kind == "live_event")
    assert event.entity_id == "espn:1"
    assert event.enters_model is False
    assert event.model_exclusion_reason == "live_or_postmatch_event"
    assert event.payload["events"][0]["minute"] == "45+2"


def test_espn_match_stats_are_archived_as_display_only_observations():
    snapshot = _snapshot()
    snapshot["espn_markets"] = {
        "markets": [],
        "match_stats": [{
            "fixture_id": "espn:1",
            "retrieved_at": "2026-08-11T11:59:00+00:00",
            "teams": {"home": {"statistics": [{"name": "totalShots", "value": 8}]}},
            "source": {
                "url": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=1",
                "raw_sha256": "c" * 64,
            },
        }],
    }
    stats = next(row for row in snapshot_observations(snapshot) if row.kind == "match_stat")
    assert stats.entity_id == "espn:1"
    assert stats.enters_model is False
    assert stats.model_exclusion_reason == "live_or_postmatch_event"
    assert stats.payload["teams"]["home"]["statistics"][0]["name"] == "totalShots"
    assert stats.payload["players"] == []


def test_source_policy_denial_overrides_requested_model_entry():
    snapshot = _snapshot()
    snapshot["espn"]["fixtures"][0]["source"].update(
        {
            "content_signal": "search=yes,ai-train=no,use=reference",
            "robots_allowed": False,
        }
    )

    fixture = snapshot_observations(snapshot)[0]

    assert fixture.enters_model is False
    assert fixture.model_exclusion_reason == "ai_training_denied;robots_denied"


def test_nested_source_policy_denial_is_applied_to_market():
    snapshot = _snapshot()
    snapshot["espn_markets"] = {
        "markets": [
            {
                "fixture_id": "espn:1",
                "provider": "Restricted provider",
                "american_odds": {"home": -110, "draw": 250, "away": 300},
                "probability": {"home": 0.5, "draw": 0.25, "away": 0.25},
                "retrieved_at": "2026-08-11T11:59:00+00:00",
                "source": {
                    "url": "https://example.com/market",
                    "retrieved_at": "2026-08-11T11:59:00+00:00",
                    "raw_sha256": "e" * 64,
                    "policy": {"allow_model": False},
                },
            }
        ]
    }

    market = next(row for row in snapshot_observations(snapshot) if row.kind == "market_1x2")

    assert market.enters_model is False
    assert market.model_exclusion_reason == "model_use_denied"


def test_unmatched_sports_lottery_market_is_display_only():
    snapshot = _snapshot()
    snapshot["sports_lottery"] = {
        "matches": [{
            "match_id": "lottery-1",
            "match_num": "周一001",
            "league": "英超",
            "home_team": "Home",
            "away_team": "Away",
            "kickoff_at": "2026-08-12T19:00:00+00:00",
            "had_odds": {"h": 1.8, "d": 3.2, "a": 4.0},
            "source": {
                "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
                "retrieved_at": "2026-08-11T11:59:00+00:00",
                "raw_sha256": "b" * 64,
            },
        }],
        "errors": [],
    }
    rows = snapshot_observations(snapshot)
    lottery = next(row for row in rows if row.kind == "sports_lottery_markets")
    assert lottery.enters_model is False
    assert lottery.model_exclusion_reason == "official_lottery_fixture_join_unproven"


def test_unmatched_oddstorm_market_keeps_dual_time_and_is_display_only():
    snapshot = _snapshot()
    snapshot["oddstorm"] = {
        "lines": [{
            "match_id": "13410978",
            "home_team": "Shandong Taishan",
            "away_team": "Qingdao Hainiu",
            "kickoff_at": "2026-08-14T10:35:00+00:00",
            "handicap": {
                "line": -1.0,
                "home_odds": 1.84,
                "away_odds": 2.04,
                "devig_probability": {"home": 0.5257732, "away": 0.4742268},
            },
            "total": None,
            "effective_at": "2026-08-11T11:58:00+00:00",
            "observed_at": "2026-08-11T11:59:00+00:00",
            "available_at": "2026-08-11T11:59:00+00:00",
            "enters_model": True,
            "source": {
                "name": "OddStorm public bookmaker comparison",
                "url": "https://www.oddstorm.com/asianodds/league/2182861-china-chinese-super-league",
                "retrieved_at": "2026-08-11T11:59:00+00:00",
                "raw_sha256": "c" * 64,
            },
        }],
        "errors": [],
    }
    rows = snapshot_observations(snapshot)
    market = next(row for row in rows if row.kind == "independent_asian_market")
    assert market.entity_id == "oddstorm:13410978"
    assert market.effective_at == "2026-08-11T11:58:00+00:00"
    assert market.observed_at == "2026-08-11T11:59:00+00:00"
    assert market.payload["source_model_eligible"] is True
    assert market.enters_model is False
    assert market.model_exclusion_reason == "independent_market_fixture_join_unproven"
    assert market.payload["canonical_join_status"] == "unmatched"


def test_display_only_roster_and_news_records_keep_rejection_reasons():
    snapshot = _snapshot()
    snapshot["espn_markets"] = {
        "markets": [],
        "team_status": [{
            "fixture_id": "espn:1",
            "retrieved_at": "2026-08-11T11:59:00+00:00",
            "provider": "ESPN event summary",
            "confirmed": False,
            "model_eligible": False,
            "status": "roster_evidence_only",
            "teams": {},
            "source": {
                "url": "https://site.api.espn.com/summary",
                "raw_sha256": "e" * 64,
            },
        }],
    }
    snapshot["news"] = {
        "feeds": [{
            "name": "Example RSS",
            "url": "https://example.com/rss",
            "retrieved_at": "2026-08-11T12:00:00+00:00",
            "raw_sha256": "f" * 64,
            "items": [{
                "title": "Team update",
                "summary": "A fact",
                "link": "https://example.com/news/1",
                "published_at": "2026-08-11T11:00:00+00:00",
            }],
        }],
        "errors": [],
    }
    rows = snapshot_observations(snapshot)
    reasons = {row.kind: row.model_exclusion_reason for row in rows}
    assert reasons["roster_evidence"] == "provider_roster_not_confirmed_lineup"
    assert reasons["news_fact_candidate"] == "news_fact_not_structured_model_feature"


def test_official_lineup_is_archived_with_pre_kickoff_eligibility():
    snapshot = _snapshot()
    snapshot["premier_league_official"] = {
        "provider": "Premier League official",
        "fixtures": [],
        "lineups": [{
            "match_id": "2645195",
            "fixture_id": "premierleague:2645195",
            "lineups": {"confirmed": True, "model_eligible": True, "home": {}, "away": {}},
            "source": {
                "name": "Premier League official",
                "url": "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v3/matches/2645195/lineups",
                "retrieved_at": "2026-08-11T11:59:00+00:00",
                "raw_sha256": "d" * 64,
                "time_basis": "observed_at_no_published_at",
                "effective_at": None,
            },
        }],
        "errors": [],
    }
    rows = snapshot_observations(snapshot)
    lineup = next(row for row in rows if row.kind == "official_lineup")
    assert lineup.entity_id == "premierleague:2645195"
    assert lineup.enters_model is True
    assert lineup.source_tier == "official"
    assert lineup.raw_hash == "d" * 64


def test_archive_snapshot_is_idempotent(tmp_path):
    path = tmp_path / "intelligence.jsonl"
    snapshot = _snapshot()
    assert archive_snapshot_intelligence(snapshot, path) == {"accepted": 1, "skipped_duplicate": 0}
    assert archive_snapshot_intelligence(snapshot, path) == {"accepted": 0, "skipped_duplicate": 1}


def test_legacy_ledger_rows_without_policy_reason_remain_readable(tmp_path):
    path = tmp_path / "legacy.jsonl"
    snapshot = _snapshot()
    row = snapshot_observations(snapshot)[0].as_dict()
    row.pop("model_exclusion_reason", None)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    from league_platform.intelligence import ObservationLedger

    stored = ObservationLedger(path).read()

    assert stored[0].model_exclusion_reason is None


def test_snapshot_observations_deduplicate_repeated_feed_items():
    snapshot = _snapshot()
    snapshot["news"] = {
        "feeds": [{
            "name": "Example RSS",
            "url": "https://example.com/rss",
            "retrieved_at": "2026-08-11T12:00:00+00:00",
            "raw_sha256": "b" * 64,
            "items": [{
                "title": "Team update",
                "summary": "A fact",
                "link": "https://example.com/news/1",
                "published_at": "2026-08-11T11:00:00+00:00",
            }] * 2,
        }],
        "errors": [],
    }
    rows = snapshot_observations(snapshot)
    assert len(rows) == 2
    assert sum(row.kind == "news_fact_candidate" for row in rows) == 1
    news = next(row for row in rows if row.kind == "news_fact_candidate")
    assert news.raw_hash == "b" * 64


def test_crawl4ai_ingestion_preserves_adapter_provenance_and_runtime_fields():
    """The immutable ledger must retain the adapter envelope verbatim.

    Crawl4AI is only an execution layer.  Keeping its provider/runtime fields
    on both the page and any joined fact lets the publisher re-check the
    capture boundary instead of treating a lossy ingestion projection as
    evidence of an ordinary source row.
    """

    page = {
        "source": "WhoScored public match centre operator source",
        "source_id": "whoscored_public_pages",
        "fact_source_id": "whoscored_public_pages",
        "provider": "Crawl4AI",
        "provider_role": "execution_layer",
        "capture_engine": "Crawl4AI",
        "capture_role": "fetch_runtime",
        "source_role": "fact_source_capture",
        "source_tier": "reliable_public_provider",
        "parser_contract": "whoscored_public_fixtures_v1",
        "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
        "final_url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
        "observed_at": "2026-08-30T00:00:00+00:00",
        "status_code": 200,
        "content_type": "text/html",
        "raw_kind": "html",
        "content_size": 7,
        "markdown": "fixture",
        "markdown_truncated": False,
        "extraction": {"parser": "whoscored_public_fixtures_v1"},
        "extracted_records": [],
        "robots": {"allowed": True, "status": "allowed"},
        "policy": {
            "robots_allowed": True,
            "license_status": "unknown",
            "rights_reference": "https://example.invalid/rights",
            "runtime_evidence": True,
            "network_opened": True,
            "test_only": False,
        },
        "runtime_evidence": True,
        "network_opened": True,
        "test_only": False,
        "model_eligible": False,
        "enters_model": False,
        "joined_fixture_records": [
            {
                "fixture_id": "fixture-1",
                "fact_type": "fixture_context",
                "home_team": "Arsenal",
                "away_team": "Coventry City",
                "join_status": "exact",
                "join_confidence": 0.9,
            }
        ],
        "content_sha256": "a" * 64,
    }

    rows = snapshot_observations(
        {
            "as_of": "2026-08-30T00:00:00+00:00",
            "crawl4ai": {"pages": [page]},
        }
    )

    page_row = next(row for row in rows if row.kind == "crawl4ai_page")
    joined_row = next(row for row in rows if row.kind == "crawl4ai_fixture_context")
    for row in (page_row, joined_row):
        assert row.payload["source_id"] == "whoscored_public_pages"
        assert row.payload["fact_source_id"] == "whoscored_public_pages"
        assert row.payload["provider"] == "Crawl4AI"
        assert row.payload["provider_role"] == "execution_layer"
        assert row.payload["capture_engine"] == "Crawl4AI"
        assert row.payload["capture_role"] == "fetch_runtime"
        assert row.payload["source_role"] == "fact_source_capture"
        assert row.payload["parser_contract"] == "whoscored_public_fixtures_v1"
        assert row.payload["runtime_evidence"] is True
        assert row.payload["network_opened"] is True
        assert row.payload["test_only"] is False
        assert row.payload["policy"]["runtime_evidence"] is True
        assert row.payload["policy"]["network_opened"] is True
        assert row.payload["policy"]["test_only"] is False
