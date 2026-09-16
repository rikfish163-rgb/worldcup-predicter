from league_platform.source_registry import (
    build_runtime_source_registry,
    get_source_registry,
    validate_source_registry,
)


def test_source_registry_has_enabled_and_fail_closed_lanes():
    rows = get_source_registry()
    validate_source_registry(rows)


    by_id = {row["id"]: row for row in rows}
    assert by_id["sports_lottery_official"]["status"] == "enabled"
    assert by_id["openfootball_current"]["status"] == "enabled"
    assert by_id["openfootball_current"]["license"] == "CC0-1.0"
    assert by_id["openfootball_current"]["license_url"].startswith("https://github.com/openfootball/")
    assert by_id["openfootball_current"]["hosts"] == ["raw.githubusercontent.com"]
    assert by_id["cfl_official_current"]["status"] == "enabled"
    assert by_id["cfl_official_current"]["hosts"] == ["api.cfl-china.cn"]
    assert by_id["cfl_official_current"]["license"] == "not_published"
    assert by_id["cfl_official_current"]["commercial_reuse_verified"] is False
    assert by_id["cfl_official_current"]["rights_status"] == (
        "public_first_party_access_reuse_terms_unverified"
    )
    assert by_id["espn_schedule_summary"]["status"] == "forbidden"
    assert by_id["espn_team_rosters"]["status"] == "forbidden"
    assert by_id["espn_schedule_summary"]["polling"] == {
        "normal_minutes": 0,
        "near_kickoff_minutes": 0,
    }
    assert by_id["espn_schedule_summary"]["terms_url"] == (
        "https://disneytermsofuse.com/english/"
    )
    assert by_id["espn_injury_reports"]["status"] == "forbidden"
    assert by_id["espn_injury_reports"]["access_policy"] == (
        "provider_terms_block_unlicensed_automated_access_and_commercial_use"
    )
    assert by_id["espn_injury_reports"]["model_policy"] == (
        "never_fetch_or_publish_without_express_written_permission"
    )
    assert by_id["espn_injury_reports"]["terms_url"] == (
        "https://disneytermsofuse.com/english/"
    )
    assert by_id["oddstorm_market_comparison"]["status"] == "forbidden"
    assert by_id["oddstorm_market_comparison"]["polling"] == {
        "normal_minutes": 0,
        "near_kickoff_minutes": 0,
    }
    assert by_id["oddstorm_market_comparison"]["access_policy"] == (
        "provider_terms_forbid_automated_scraping_mirroring_redistribution_and_resale"
    )
    assert by_id["oddstorm_market_comparison"]["model_policy"] == (
        "never_fetch_or_publish_without_express_written_permission"
    )
    assert by_id["oddstorm_market_comparison"]["terms_url"] == (
        "https://www.oddstorm.com/terms"
    )
    assert by_id["crawl4ai_allowlisted_pages"]["role"] == "fetch_runtime"
    assert by_id["crawl4ai_allowlisted_pages"]["fact_source"] is False
    assert by_id["crawl4ai_allowlisted_pages"]["model_policy"].startswith("runtime_only")
    assert by_id["crawl4ai_allowlisted_pages"]["access_policy"] == (
        "explicit_allowlist_robots_fail_closed_same_host_redirect"
    )
    assert by_id["fotmob_public_api"]["status"] == "blocked_by_robots"
    assert by_id["legacy_anti_waf_relay"]["status"] == "forbidden"
    assert by_id["bundesliga_public_pages"].get("role", "fact_source") == "fact_source"
    assert by_id["bundesliga_public_pages"]["status"] == "opt_in"
    assert by_id["seriea_public_pages"]["status"] == "opt_in"
    assert by_id["ligue1_official_news"]["status"] == "opt_in"
    assert by_id["laliga_official_news"]["status"] == "opt_in"
    assert by_id["openligadb_secondary_results"]["hosts"] == ["api.openligadb.de"]
    assert by_id["openligadb_secondary_results"]["license"] == "ODbL-1.0"
    assert by_id["openligadb_secondary_results"]["attribution_required"] is True


def test_met_norway_weather_is_registered_as_attributed_model_source():
    rows = {row["id"]: row for row in get_source_registry()}
    row = rows["met_norway_weather"]
    assert row["adapter"] == "league_platform.live_sources.met_norway"
    assert row["hosts"] == ["api.met.no"]
    assert row["license"] == "CC-BY-4.0"
    assert row["attribution_required"] is True
    assert row["model_policy"] == "eligible_only_with_exact_coordinates_and_time_bound"
    validate_source_registry(rows.values())


def test_wikidata_is_registered_as_a_cc0_entity_coordinate_source():
    rows = {row["id"]: row for row in get_source_registry()}
    row = rows["wikidata_entities"]
    assert row["adapter"] == "league_platform.live_sources.wikidata"
    assert row["hosts"] == ["www.wikidata.org"]
    assert row["license"] == "CC0-1.0"
    assert row["license_url"] == "https://www.wikidata.org/wiki/Wikidata:Licensing"
    assert row["model_policy"].startswith("eligible_only_exact_team_preferred_venue")
    validate_source_registry(rows.values())


def test_met_norway_runtime_preserves_total_error_count_when_errors_are_summarized():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "met_norway_weather": {
                    "provider": "MET Norway Locationforecast",
                    "source_id": "met_norway_weather",
                    "status": "unavailable",
                    "retrieved_at": "2026-08-25T16:13:34.393401+00:00",
                    "weather": [],
                    "errors": [
                        {"reason": "venue_coordinates_missing", "count": 63},
                        {"reason": "additional_errors_omitted", "count": 57},
                    ],
                    "error_count": 120,
                    "network_opened": False,
                }
            }
        )
    }

    runtime = rows["met_norway_weather"]["runtime"]
    assert runtime["error_count"] == 120
    assert runtime["errors"] == [
        {"reason": "venue_coordinates_missing", "count": 63},
        {"reason": "additional_errors_omitted", "count": 57},
    ]


def test_openfootball_runtime_binding_uses_the_canonical_fixture_feed():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "fixture_feed": {
                    "provider": "OpenFootball",
                    "retrieved_at": "2026-08-24T00:00:00+00:00",
                    "fixtures": [{"id": "openfootball:premier-league:1"}],
                    "errors": [],
                    "status": "ok",
                },
                "espn": {
                    "provider": "ESPN",
                    "fixtures": [],
                    "errors": [],
                    "status": "rights_blocked",
                },
            }
        )
    }
    runtime = rows["openfootball_current"]["runtime"]
    assert runtime["status"] == "fresh"
    assert runtime["record_count"] == 1
    assert rows["espn_schedule_summary"]["runtime"]["status"] == "rights_blocked"


def test_cfl_runtime_binding_counts_only_the_first_party_csl_section():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "openfootball_current": {
                    "provider": "OpenFootball",
                    "retrieved_at": "2026-08-24T00:00:00+00:00",
                    "fixtures": [{"id": "openfootball:premier-league:1"}],
                    "errors": [],
                    "status": "ok",
                },
                "cfl_official": {
                    "provider": "Chinese Professional Football League official",
                    "retrieved_at": "2026-08-24T00:00:00+00:00",
                    "fixtures": [
                        {"id": "cfl-official:csl:1"},
                        {"id": "cfl-official:csl:2"},
                    ],
                    "errors": [],
                    "status": "ok",
                },
            }
        )
    }

    assert rows["openfootball_current"]["runtime"]["record_count"] == 1
    assert rows["cfl_official_current"]["runtime"]["status"] == "fresh"
    assert rows["cfl_official_current"]["runtime"]["record_count"] == 2


def test_espn_injury_runtime_keeps_observed_empty_distinct_from_healthy():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "espn_injuries": {
                    "status": "fresh",
                    "retrieved_at": "2026-08-24T00:00:00+00:00",
                    "reports": [],
                    "requested_competitions": 1,
                    "observed_competitions": 1,
                    "observed_empty_competitions": 1,
                    "reported_player_count": 0,
                    "healthy_team_count": None,
                    "errors": [],
                }
            }
        )
    }

    runtime = rows["espn_injury_reports"]["runtime"]
    assert runtime["status"] == "observed_empty"
    assert runtime["record_count"] == 0
    assert runtime["observed_competition_count"] == 1
    assert runtime["observed_empty_competition_count"] == 1
    assert runtime["healthy_team_count"] is None


def test_espn_injury_runtime_preserves_rights_block_without_network_error():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "espn_injuries": {
                    "status": "rights_blocked",
                    "retrieved_at": None,
                    "checked_at": "2026-08-24T00:00:00+00:00",
                    "reports": [],
                    "requested_competitions": 0,
                    "observed_competitions": 0,
                    "observed_empty_competitions": 0,
                    "reported_player_count": 0,
                    "healthy_team_count": None,
                    "rights_status": "blocked_pending_express_written_permission",
                    "terms_url": "https://disneytermsofuse.com/english/",
                    "network_opened": False,
                    "errors": [],
                }
            }
        )
    }

    runtime = rows["espn_injury_reports"]["runtime"]
    assert runtime["status"] == "rights_blocked"
    assert runtime["retrieved_at"] is None
    assert runtime["checked_at"] == "2026-08-24T00:00:00+00:00"
    assert runtime["record_count"] == 0
    assert runtime["error_count"] == 0
    assert runtime["rights_status"] == "blocked_pending_express_written_permission"
    assert runtime["network_opened"] is False


def test_oddstorm_runtime_preserves_rights_block_without_serving_legacy_lines():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "oddstorm": {
                    "provider": "OddStorm public bookmaker comparison",
                    "status": "rights_blocked",
                    "retrieved_at": None,
                    "checked_at": "2026-08-24T00:00:00+00:00",
                    "lines": [],
                    "rights_status": "blocked_pending_express_written_permission",
                    "terms_url": "https://www.oddstorm.com/terms",
                    "network_opened": False,
                    "errors": [],
                }
            }
        )
    }

    runtime = rows["oddstorm_market_comparison"]["runtime"]
    assert runtime["status"] == "rights_blocked"
    assert runtime["checked_at"] == "2026-08-24T00:00:00+00:00"
    assert runtime["record_count"] == 0
    assert runtime["error_count"] == 0
    assert runtime["rights_status"] == "blocked_pending_express_written_permission"
    assert runtime["terms_url"] == "https://www.oddstorm.com/terms"
    assert runtime["network_opened"] is False


def test_crawl4ai_adapter_rows_keep_provider_identity_above_execution_layer():
    """A browser adapter may serve many providers, but it is not a provider."""

    rows = get_source_registry()
    crawl_rows = [
        row
        for row in rows
        if row.get("adapter") == "league_platform.live_sources.crawl4ai"
    ]
    assert crawl_rows
    runtime_rows = [
        row
        for row in crawl_rows
        if row.get("role") == "fetch_runtime" or row.get("fact_source") is False
    ]
    assert [row["id"] for row in runtime_rows] == ["crawl4ai_allowlisted_pages"]
    fact_rows = [row for row in crawl_rows if row not in runtime_rows]
    assert fact_rows
    assert all(row.get("role", "fact_source") == "fact_source" for row in fact_rows)
    assert all(row.get("fact_source", True) is True for row in fact_rows)
    assert all("runtime_only" not in str(row.get("model_policy", "")) for row in fact_rows)


def test_runtime_registry_projects_crawl_source_rights_block_by_identity():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "rights_blocked",
                    "retrieved_at": "2026-08-25T00:00:00+00:00",
                    "checked_at": "2026-08-25T00:00:00+00:00",
                    "network_opened": False,
                    "rights_status": "source_rights_not_verified",
                    "pages": [],
                    "errors": [
                        {
                            "stage": "rights_gate",
                            "status": "rights_blocked",
                            "error_code": "source_rights_not_verified",
                            "source_id": "whoscored_public_pages",
                            "license_status": "unknown",
                            "network_opened": False,
                        }
                    ],
                }
            }
        )
    }

    shared = rows["crawl4ai_allowlisted_pages"]["runtime"]
    source = rows["whoscored_public_pages"]["runtime"]
    assert shared["status"] == "rights_blocked"
    assert shared["network_opened"] is False
    assert source["status"] == "rights_blocked"
    assert source["network_opened"] is False
    assert source["errors"][0]["error_code"] == "source_rights_not_verified"


def test_crawl4ai_unidentified_pages_do_not_become_fanout_sources():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-21T14:00:00+00:00",
                    "pages": [
                        {
                            "capture_engine": "Crawl4AI",
                            "capture_role": "fetch_runtime",
                            "url": "https://public.example/anonymous",
                            "extracted_records": [{"headline": "unidentified"}],
                            "enters_model": False,
                        }
                    ],
                    "errors": [],
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["fanout"]["declared_source_count"] == 0
    assert runtime["fanout"]["unidentified_page_count"] == 1
    assert runtime["fact_source_count"] == 0
    assert runtime["model_eligible_count"] == 0


def test_source_registry_rejects_unsafe_enabled_source():
    rows = get_source_registry()
    rows.append(
        {
            "id": "unsafe",
            "status": "enabled",
            "access_policy": "would_bypass_access_controls",
            "model_policy": "never_execute",
        }
    )
    try:
        validate_source_registry(rows)
    except ValueError as exc:
        assert "unsafe access policy" in str(exc)
    else:
        raise AssertionError("unsafe enabled source must be rejected")


def test_runtime_registry_projects_snapshot_health_without_authorizing_sources():
    snapshot = {
        "sports_lottery": {
            "retrieved_at": "2026-08-16T14:00:00+00:00",
            "matches": [{"match_id": "1"}],
            "errors": [],
        },
        "sofascore": {"retrieved_at": "2026-08-16T14:00:00+00:00", "events": [], "errors": [{"error": "403"}]},
    }
    rows = {row["id"]: row for row in build_runtime_source_registry(snapshot)}
    assert rows["sports_lottery_official"]["runtime"]["status"] == "fresh"
    assert rows["sports_lottery_official"]["runtime"]["record_count"] == 1
    assert rows["sofascore_prematch"]["runtime"]["status"] == "unavailable"
    assert rows["fotmob_public_api"]["runtime"]["status"] == "blocked_by_robots"
    assert rows["legacy_anti_waf_relay"]["runtime"]["status"] == "forbidden"


def test_runtime_registry_projects_laliga_public_match_directory_separately_from_lineups():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "laliga_official": {
                    "status": "ok",
                    "retrieved_at": "2026-08-20T02:00:00+00:00",
                    "fixtures": [{"id": "espn:1"}],
                    "lineups": [],
                    "match_discovery": {
                        "status": "ok",
                        "endpoint": "https://apim.laliga.com/public-service/api/v1/matches",
                        "pages": 4,
                        "records_seen": 380,
                        "matched_count": 1,
                        "truncated": False,
                        "raw_sha256": "a" * 64,
                        "errors": [],
                    },
                    "errors": [],
                }
            }
        )
    }
    runtime = rows["laliga_official_match_directory"]["runtime"]
    assert runtime["status"] == "fresh"
    assert runtime["record_count"] == 380
    assert runtime["match_discovery"]["page_count"] == 4
    assert runtime["match_discovery"]["matched_count"] == 1
    assert runtime["match_discovery"]["truncated"] is False
    assert rows["official_league_lineups"]["runtime"]["record_count"] == 0


def test_runtime_registry_projects_bundesliga_page_from_shared_crawl_engine():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-19T02:00:00+00:00",
                    "execution": {
                        "strategy": "bounded_cross_host_parallel_per_host_serial",
                        "host_group_count": 3,
                        "max_host_workers": 3,
                        "configured_source_count": 4,
                        "index_page_count": 3,
                        "detail_page_count": 2,
                        "follow_up_candidate_count": 6,
                        "follow_up_requested_count": 2,
                        "follow_up_rejected_count": 1,
                        "follow_up_capped_count": 3,
                        "follow_up_rejections": [{"source_id": "bundesliga_public_pages", "enters_model": False}],
                        "duration_ms": 123.4567,
                    },
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [{
                        "source_id": "bundesliga_public_pages",
                        "fact_source_id": "bundesliga_public_pages",
                        "capture_engine": "Crawl4AI",
                        "parser_contract": "sports_event_jsonld_v1",
                        "crawl_stage": "detail",
                        "parent_url": "https://www.bundesliga.com/en/bundesliga/matchday",
                        "parent_content_sha256": "a" * 64,
                        "follow_reason": "match_url",
                        "url": "https://www.bundesliga.com/en/bundesliga/matchday",
                        "extracted_records": [{"home_team": "Borussia Dortmund"}],
                    }],
                    "errors": [],
                    "security_policy": {
                        "ignore_https_errors": False,
                        "robots_check": "required",
                        "redirects": "same_host_and_allowlisted_path_only",
                        "login_captcha_bypass": False,
                        "access_control_bypass": False,
                    },
                }
            }
        )
    }
    runtime = rows["bundesliga_public_pages"]["runtime"]
    assert runtime["status"] == "fresh"
    assert runtime["record_count"] == 1
    assert runtime["extracted_record_count"] == 1
    assert runtime["page_summaries"][0]["parser_contract"] == "sports_event_jsonld_v1"
    assert runtime["page_summaries"][0]["crawl_stage"] == "detail"
    assert runtime["page_summaries"][0]["follow_reason"] == "match_url"
    shared_runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert shared_runtime["execution"] == {
        "strategy": "bounded_cross_host_parallel_per_host_serial",
        "host_group_count": 3,
        "max_host_workers": 3,
        "configured_source_count": 4,
        "index_page_count": 3,
        "detail_page_count": 2,
        "follow_up_candidate_count": 6,
        "follow_up_requested_count": 2,
        "follow_up_rejected_count": 1,
        "follow_up_capped_count": 3,
        "follow_up_rejections": [{"source_id": "bundesliga_public_pages", "enters_model": False}],
        "duration_ms": 123.457,
    }


def test_runtime_registry_projects_official_news_pages_separately_from_engine():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-19T02:00:00+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "source_id": "seriea_public_pages",
                            "fact_source_id": "seriea_public_pages",
                            "capture_engine": "Crawl4AI",
                            "parser_contract": "seriea_public_fixtures_v1",
                            "url": "https://en.legaseriea.it/serie-a",
                            "extracted_records": [{"home_team": "A", "away_team": "B"}],
                        },
                        {
                            "source_id": "ligue1_official_news",
                            "fact_source_id": "ligue1_official_news",
                            "capture_engine": "Crawl4AI",
                            "parser_contract": "official_news_cards_v1",
                            "url": "https://ligue1.com/",
                            "extracted_records": [{"headline": "B"}],
                        },
                        {
                            "source_id": "laliga_official_news",
                            "fact_source_id": "laliga_official_news",
                            "capture_engine": "Crawl4AI",
                            "parser_contract": "official_news_jsonld_v1",
                            "url": "https://www.laliga.com/en-GB",
                            "extracted_records": [{"headline": "C"}],
                        },
                    ],
                    "errors": [],
                }
            }
        )
    }
    assert rows["seriea_public_pages"]["runtime"]["status"] == "fresh"
    assert rows["seriea_public_pages"]["runtime"]["page_summaries"][0]["fact_source_id"] == "seriea_public_pages"
    assert rows["ligue1_official_news"]["runtime"]["status"] == "fresh"
    assert rows["ligue1_official_news"]["runtime"]["page_summaries"][0]["fact_source_id"] == "ligue1_official_news"
    assert rows["laliga_official_news"]["runtime"]["status"] == "fresh"
    assert rows["laliga_official_news"]["runtime"]["extracted_record_count"] == 1
    assert rows["laliga_official_news"]["runtime"]["page_summaries"][0]["parser_contract"] == "official_news_jsonld_v1"


def test_runtime_registry_preserves_optional_source_not_configured_state():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {"crawl4ai": {"status": "not_configured", "pages": [], "errors": []}}
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["status"] == "not_configured"
    assert runtime["error_count"] == 0


def test_runtime_registry_preserves_persistent_throttle_as_not_due():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "not_due",
                    "retrieved_at": "2026-08-17T10:47:00+00:00",
                    "pages": [],
                    "errors": [
                        {
                            "stage": "throttle",
                            "error_code": "rate_limited_wait",
                            "retry_after_seconds": 600,
                        }
                    ],
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["status"] == "not_due"
    assert runtime["error_count"] == 1
    assert runtime["errors"][0]["error_code"] == "rate_limited_wait"


def test_runtime_registry_marks_last_verified_page_stale_when_poll_is_deferred():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "not_due",
                    "retrieved_at": "2026-08-17T10:47:00+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "url": "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
                            "stale": True,
                            "source_state": "stale",
                            "stale_reason": "host_throttled",
                        }
                    ],
                    "errors": [
                        {
                            "stage": "throttle",
                            "error_code": "rate_limited_wait",
                            "retry_after_seconds": 600,
                        }
                    ],
                }
            }
        )
    }
    runtime = rows["whoscored_public_pages"]["runtime"]
    assert runtime["status"] == "stale"
    assert runtime["record_count"] == 1
    assert runtime["fresh_record_count"] == 0
    assert runtime["stale_record_count"] == 1
    assert runtime["page_summaries"][0]["stale_reason"] == "host_throttled"


def test_runtime_registry_keeps_successful_page_fresh_when_peer_is_throttled():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-17T11:18:31+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "source": "WhoScored public match centre",
                            "source_id": "whoscored_public_pages",
                            "fact_source_id": "whoscored_public_pages",
                            "capture_engine": "Crawl4AI",
                            "capture_role": "fetch_runtime",
                            "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                        }
                    ],
                    "errors": [
                        {
                            "stage": "throttle",
                            "error_code": "rate_limited_wait",
                            "retry_after_seconds": 2695,
                        }
                    ],
                }
            }
        )
    }
    generic = rows["crawl4ai_allowlisted_pages"]["runtime"]
    whoscored = rows["whoscored_public_pages"]["runtime"]
    assert generic["status"] == "fresh"
    assert generic["record_count"] == 1
    assert generic["error_count"] == 1
    assert generic["errors"][0]["error_code"] == "rate_limited_wait"
    assert whoscored["status"] == "fresh"


def test_runtime_registry_preserves_crawl4ai_security_policy():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-17T09:31:05+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [{"url": "https://public.example/news/1"}],
                    "errors": [],
                    "security_policy": {
                        "ignore_https_errors": False,
                        "robots_check": "required",
                        "redirects": "same_host_and_allowlisted_path_only",
                        "login_captcha_bypass": False,
                        "access_control_bypass": False,
                        "untrusted_extra": "discarded",
                    },
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["security_policy"] == {
        "ignore_https_errors": False,
        "robots_check": "required",
        "redirects": "same_host_and_allowlisted_path_only",
        "login_captcha_bypass": False,
        "access_control_bypass": False,
    }


def test_runtime_registry_projects_crawl4ai_pages_to_their_declared_source():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-17T09:31:05+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "source": "WhoScored public match centre",
                            "source_id": "whoscored_public_pages",
                            "fact_source_id": "whoscored_public_pages",
                            "capture_engine": "Crawl4AI",
                            "capture_role": "fetch_runtime",
                            "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                            "final_url": "https://www.whoscored.com/regions/252/tournaments/2/england-premier-league",
                        },
                        {
                            "source": "Football-Data historical archive",
                            "url": "https://www.football-data.co.uk/englandm.php",
                        },
                    ],
                    "errors": [],
                    "security_policy": {
                        "ignore_https_errors": False,
                        "robots_check": "required",
                        "redirects": "same_host_and_allowlisted_path_only",
                        "login_captcha_bypass": False,
                        "access_control_bypass": False,
                    },
                }
            }
        )
    }
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["record_count"] == 2
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["fanout"]["declared_source_count"] == 1
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["fanout"]["declared_source_ids"] == ["whoscored_public_pages"]
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["fanout"]["unidentified_page_count"] == 1
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["execution_layer_count"] == 1
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["fact_source_count"] == 1
    assert rows["crawl4ai_allowlisted_pages"]["runtime"]["fanout_source_count"] == 1
    assert rows["whoscored_public_pages"]["runtime"]["status"] == "fresh"
    assert rows["whoscored_public_pages"]["runtime"]["record_count"] == 1
    assert rows["whoscored_public_pages"]["runtime"]["page_summaries"][0]["final_url"].endswith("england-premier-league")
    assert rows["whoscored_public_pages"]["runtime"]["page_summaries"][0]["fact_source_id"] == "whoscored_public_pages"
    assert rows["whoscored_public_pages"]["runtime"]["page_summaries"][0]["capture_role"] == "fetch_runtime"
    assert rows["whoscored_public_pages"]["runtime"]["security_policy"]["robots_check"] == "required"


def test_crawl4ai_fanout_counts_sources_after_the_page_summary_cap():
    pages = [
        {
            "source_id": "whoscored_public_pages",
            "fact_source_id": "whoscored_public_pages",
            "url": f"https://www.whoscored.com/regions/page-{index}",
            "enters_model": False,
        }
        for index in range(20)
    ]
    pages.append(
        {
            "source_id": "laliga_public_pages",
            "fact_source_id": "laliga_public_pages",
            "url": "https://www.laliga.com/en-GB/late-page",
            "enters_model": False,
        }
    )
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-21T09:00:00+00:00",
                    "pages": pages,
                    "errors": [],
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert len(runtime["page_summaries"]) == 20
    assert runtime["record_count"] == 21
    assert runtime["fanout"]["declared_source_count"] == 2
    assert runtime["fanout"]["declared_source_ids"] == [
        "laliga_public_pages",
        "whoscored_public_pages",
    ]
    assert runtime["fanout"]["pages_by_source"] == {
        "laliga_public_pages": 1,
        "whoscored_public_pages": 20,
    }
    assert runtime["fanout_source_count"] == 2
    assert runtime["fact_source_count"] == 2


def test_runtime_registry_keeps_each_new_crawl4ai_fact_source_isolated():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-18T07:00:00+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "source_id": "premier_league_public_pages",
                            "fact_source_id": "premier_league_public_pages",
                            "url": "https://www.premierleague.com/en/matches",
                            "status_code": 200,
                        },
                        {
                            "source_id": "laliga_public_pages",
                            "fact_source_id": "laliga_public_pages",
                            "url": "https://www.laliga.com/en-GB",
                            "status_code": 200,
                        },
                    ],
                    "errors": [],
                }
            }
        )
    }
    premier = rows["premier_league_public_pages"]["runtime"]
    laliga = rows["laliga_public_pages"]["runtime"]
    assert premier["status"] == "fresh"
    assert premier["record_count"] == 1
    assert laliga["status"] == "fresh"
    assert laliga["record_count"] == 1
    assert premier["page_summaries"][0]["source_id"] != laliga["page_summaries"][0]["source_id"]


def test_runtime_registry_scopes_fixture_join_summary_to_declared_fact_source():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-19T02:00:00+00:00",
                    "tls_runtime": {"status": "supported", "reason": "test"},
                    "pages": [
                        {
                            "source_id": "premier_league_public_pages",
                            "fact_source_id": "premier_league_public_pages",
                            "url": "https://www.premierleague.com/en/matches",
                            "fixture_join": {
                                "status": "exact",
                                "joined_count": 10,
                                "unmatched_count": 0,
                                "admission_counts": {"review_required": 8, "blocked": 2},
                            },
                        },
                        {
                            "source_id": "bundesliga_public_pages",
                            "fact_source_id": "bundesliga_public_pages",
                            "url": "https://www.bundesliga.com/en/bundesliga/matchday/2026-2027/1",
                            "fixture_join": {
                                "status": "exact",
                                "joined_count": 9,
                                "unmatched_count": 0,
                                "admission_counts": {"candidate_ready": 4, "review_required": 5},
                            },
                        },
                    ],
                    "fixture_join": {
                        "status": "exact",
                        "joined_count": 19,
                        "unmatched_count": 0,
                    },
                    "errors": [],
                }
            }
        )
    }
    aggregate = rows["crawl4ai_allowlisted_pages"]["runtime"]["fixture_join"]
    fanout = rows["crawl4ai_allowlisted_pages"]["runtime"]["fanout"]
    assert fanout["declared_source_count"] == 2
    assert fanout["declared_source_ids"] == ["bundesliga_public_pages", "premier_league_public_pages"]
    assert fanout["pages_by_source"] == {"bundesliga_public_pages": 1, "premier_league_public_pages": 1}
    premier = rows["premier_league_public_pages"]["runtime"]["fixture_join"]
    bundesliga = rows["bundesliga_public_pages"]["runtime"]["fixture_join"]
    assert aggregate["joined_count"] == 19
    assert premier["joined_count"] == 10
    assert bundesliga["joined_count"] == 9
    assert premier["admission_counts"] == {"review_required": 8, "blocked": 2}
    assert bundesliga["admission_counts"] == {"candidate_ready": 4, "review_required": 5}
    assert premier["joined_count"] != aggregate["joined_count"]


def test_runtime_registry_quarantines_pages_without_tls_attestation():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-17T11:33:55+00:00",
                    "pages": [{"url": "https://public.example/news/1"}],
                    "errors": [],
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["status"] == "blocked_tls_policy"
    assert runtime["tls_runtime"]["status"] == "unverified"
    assert runtime["record_count"] == 1


def test_runtime_registry_preserves_tls_block_for_whoscored_binding():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "blocked_tls_policy",
                    "retrieved_at": "2026-08-17T11:48:40+00:00",
                    "pages": [],
                    "errors": [
                        {
                            "source": "WhoScored public match centre operator source",
                            "url": "https://www.whoscored.com/Regions/252/Tournaments/2/England-Premier-League",
                            "stage": "tls_policy",
                            "error_code": "tls_policy_unsupported",
                        }
                    ],
                    "tls_runtime": {"status": "unsupported", "unsafe_flags": ["--ignore-certificate-errors"]},
                }
            }
        )
    }
    runtime = rows["whoscored_public_pages"]["runtime"]
    assert runtime["status"] == "blocked_tls_policy"
    assert runtime["error_count"] == 1
    assert runtime["tls_runtime"]["status"] == "unsupported"


def test_runtime_registry_distinguishes_missing_crawl4ai_dependency():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "unavailable",
                    "pages": [],
                    "errors": [{"stage": "crawl", "error": "No module named 'crawl4ai'"}],
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["status"] == "dependency_missing"
    assert runtime["dependency"] == "crawl4ai"


def test_runtime_registry_counts_explicit_crawl4ai_model_admission_without_upgrading_runtime():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "crawl4ai": {
                    "status": "ok",
                    "retrieved_at": "2026-08-20T14:30:00+00:00",
                    "pages": [
                        {
                            "source_id": "whoscored_public_pages",
                            "enters_model": False,
                            "extracted_records": [{"fixture_id": "f-1"}],
                        },
                        {
                            "source_id": "premier_league_public_pages",
                            "enters_model": False,
                            "extracted_records": [{"fixture_id": "f-2"}],
                        },
                    ],
                    "errors": [],
                    "security_policy": {
                        "ignore_https_errors": False,
                        "robots_check": "required",
                    },
                    "tls_runtime": {"status": "supported"},
                }
            }
        )
    }
    runtime = rows["crawl4ai_allowlisted_pages"]["runtime"]
    assert runtime["model_eligible_count"] == 0
    assert runtime["fanout"]["declared_source_count"] == 2


def test_runtime_registry_does_not_call_empty_official_lineups_fresh():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "premier_league_official": {
                    "retrieved_at": "2026-08-17T06:00:00+00:00",
                    "fixtures": [{"id": "f-1"}, {"id": "f-2"}],
                    "lineups": [],
                    "errors": [],
                    "status": "ok",
                },
                "laliga_official": {
                    "retrieved_at": "2026-08-17T06:00:00+00:00",
                    "fixtures": [],
                    "lineups": [{
                        "fixture_id": "f-3",
                        "lineups": {
                            "available": False,
                            "confirmed": False,
                            "model_eligible": False,
                            "diagnostic": {
                                "status": "not_published",
                                "reason": "official_feed_returned_no_lineup_rows",
                            },
                        },
                    }],
                    "errors": [],
                    "status": "ok",
                },
                "bundesliga_official": {"status": "not_requested", "fixtures": [], "lineups": [], "errors": []},
                "serie_a_official": {"status": "not_requested", "fixtures": [], "lineups": [], "errors": []},
            }
        )
    }
    runtime = rows["official_league_lineups"]["runtime"]
    assert runtime["status"] == "not_published"
    assert runtime["fixture_count"] == 2
    assert runtime["record_count"] == 1
    assert runtime["available_count"] == 0
    assert runtime["model_eligible_count"] == 0
    assert runtime["not_published_count"] == 1


def test_runtime_registry_marks_available_official_lineup_fresh():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "premier_league_official": {
                    "retrieved_at": "2026-08-17T06:00:00+00:00",
                    "fixtures": [],
                    "lineups": [{
                        "fixture_id": "f-1",
                        "lineups": {
                            "available": True,
                            "confirmed": True,
                            "model_eligible": True,
                            "home": {"players": [{"name": "A"}]},
                            "away": {"players": [{"name": "B"}]},
                        },
                    }],
                    "errors": [],
                    "status": "ok",
                },
                "laliga_official": {"status": "not_requested", "fixtures": [], "lineups": [], "errors": []},
                "bundesliga_official": {"status": "not_requested", "fixtures": [], "lineups": [], "errors": []},
                "serie_a_official": {"status": "not_requested", "fixtures": [], "lineups": [], "errors": []},
            }
        )
    }
    runtime = rows["official_league_lineups"]["runtime"]
    assert runtime["status"] == "fresh"
    assert runtime["available_count"] == 1
    assert runtime["model_eligible_count"] == 1


def test_runtime_registry_does_not_call_roster_only_official_feed_fresh():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "ligue1_official": {
                    "retrieved_at": "2026-08-17T06:00:00+00:00",
                    "fixtures": [{"id": "f-1"}],
                    "lineups": [
                        {
                            "fixture_id": "f-1",
                            "lineups": {
                                "available": True,
                                "confirmed": False,
                                "model_eligible": False,
                                "home": {"players": [{"name": "Roster A", "starter": False}]},
                                "away": {"players": [{"name": "Roster B", "starter": False}]},
                                "diagnostic": {
                                    "status": "not_published",
                                    "reason": "official_feed_returned_roster_only",
                                },
                            },
                        }
                    ],
                    "errors": [],
                    "status": "ok",
                }
            }
        )
    }

    runtime = rows["official_league_lineups"]["runtime"]
    assert runtime["status"] == "not_published"
    assert runtime["available_count"] == 1
    assert runtime["model_eligible_count"] == 0
    assert runtime["not_published_count"] == 1


def test_runtime_registry_does_not_call_future_weather_horizon_a_source_failure():
    rows = {
        row["id"]: row
        for row in build_runtime_source_registry(
            {
                "weather": {
                    "retrieved_at": "2026-08-16T14:00:00+00:00",
                    "weather": [],
                    "errors": [
                        {"fixture_id": "future-1", "stage": "horizon", "error": "outside horizon"},
                        {"fixture_id": "future-2", "stage": "horizon", "error": "outside horizon"},
                    ],
                }
            }
        )
    }
    runtime = rows["open_meteo"]["runtime"]
    assert runtime["status"] == "outside_horizon"
    assert runtime["error_count"] == 0
    assert runtime["skipped_count"] == 2
