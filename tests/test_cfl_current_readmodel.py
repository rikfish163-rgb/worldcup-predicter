from datetime import datetime, timezone
import json
from pathlib import Path

from league_platform.current import CURRENT_SOURCE_ROLES, attach_current_data
from league_platform.fixture_feed import MULTI_SOURCE_FIXTURE_PROVIDER
from league_platform.live_sources.cfl_official import (
    SOURCE_NAME,
    SOURCE_RIGHTS_STATUS,
)
from league_platform.snapshot import build_platform_snapshot


SHA256 = "b" * 64


def test_mixed_current_readmodel_quarantines_cfl_without_relabelling_source(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    url = (
        "https://api.cfl-china.cn/frontweb/api/matches/page"
        "?tournament_calendar_id=e6818x4pwankpph8awr91m1hw"
        "&competition_code=CSL&contestant_id=&week="
        "&stage_id=e6tn384fbw6e38sk3kf0g83ys&curPage=1&pageSize=400"
    )
    source = {
        "name": SOURCE_NAME,
        "source_id": "cfl-official:csl:2026",
        "url": url,
        "official_page_url": (
            "https://www.cfl-china.cn/zh/fixtures/list.html?competition_code=CSL"
        ),
        "retrieved_at": as_of.isoformat(),
        "raw_sha256": SHA256,
        "hash": f"sha256:{SHA256}",
        "license": "not_published",
        "rights_status": SOURCE_RIGHTS_STATUS,
        "commercial_reuse_verified": False,
        "access_basis": "official_public_frontend_api",
    }
    fixture = {
        "id": "cfl-official:csl:official-fixture-id",
        "native_fixture_id": "official-fixture-id",
        "provider_fixture_ids": {"cfl_official": "official-fixture-id"},
        "competition_id": "csl",
        "provider_competition_id": "82jkgccg7phfjpd0mltdl3pat",
        "season": "2026",
        "round": "25",
        "kickoff_date": "2026-08-28",
        "kickoff_at": "2026-08-28T11:35:00+00:00",
        "kickoff_time_quality": "exact",
        "kickoff_time_source": "official_local_date_time",
        "kickoff_timezone": "Asia/Shanghai",
        "effective_at": "2026-08-23T10:00:37+00:00",
        "observed_at": as_of.isoformat(),
        "time_semantics": {
            "effective_at_basis": "provider_update_time",
            "observed_at_basis": "capture_retrieved_at",
            "fallback_reason": None,
        },
        "home_team": "上海申花",
        "away_team": "山东泰山",
        "home_team_en": "Shanghai Shenhua",
        "away_team_en": "Shandong Taishan",
        "home_provider_team_id": "home-official-id",
        "away_provider_team_id": "away-official-id",
        "provider_team_ids": {
            "home": {"cfl_official": "home-official-id"},
            "away": {"cfl_official": "away-official-id"},
        },
        "status": "upcoming",
        "score": None,
        "halftime_score": None,
        "result_scope": None,
        "venue": {
            "name": "上海体育场",
            "name_en": "Shanghai Stadium",
            "city": "Shanghai",
            "country": "China",
            "country_code": "CN",
            "city_resolution": {
                "basis": "official_venue_label_explicit_city",
                "matched_on": {
                    "name": "上海体育场",
                    "name_en": "Shanghai Stadium",
                },
                "source": {
                    "name": SOURCE_NAME,
                    "source_id": "cfl-official:csl:2026",
                    "url": url,
                    "retrieved_at": as_of.isoformat(),
                    "raw_sha256": SHA256,
                    "rights_status": SOURCE_RIGHTS_STATUS,
                    "commercial_reuse_verified": False,
                },
            },
        },
        "source": source,
        "lineage": {
            "source_id": "cfl-official:csl:2026",
            "url": url,
            "retrieved_at": as_of.isoformat(),
            "raw_sha256": SHA256,
            "hash": f"sha256:{SHA256}",
            "license": "not_published",
            "rights_status": SOURCE_RIGHTS_STATUS,
            "record_path": "$.data.dataList[0]",
            "source_row_id": "cfl-official:csl:2026#$.data.dataList[0]",
            "native_fixture_id": "official-fixture-id",
            "effective_at": "2026-08-23T10:00:37+00:00",
            "observed_at": as_of.isoformat(),
        },
    }
    live = {
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "expected_competitions": [
            "bundesliga",
            "championship",
            "csl",
            "la-liga",
            "ligue-1",
            "premier-league",
            "serie-a",
        ],
        "roles": CURRENT_SOURCE_ROLES,
        "fixture_feed": {
            "provider": MULTI_SOURCE_FIXTURE_PROVIDER,
            "retrieved_at": as_of.isoformat(),
            "status": "ok",
            "errors": [],
            "fixtures": [fixture],
            "date_only_fixtures": [],
            "recent_results": [],
            "upcoming_3_days": [],
            "upcoming_7_days": [fixture],
            "source_contract": {
                "aggregation_role": (
                    "provider-neutral union; row source remains authoritative"
                ),
                "fact_sources": ["OpenFootball", SOURCE_NAME],
            },
            "provider_status": [
                {
                    "provider": "OpenFootball",
                    "status": "unavailable",
                    "retrieved_at": as_of.isoformat(),
                    "fixture_count": 0,
                    "error_count": 1,
                },
                {
                    "provider": SOURCE_NAME,
                    "status": "ok",
                    "retrieved_at": as_of.isoformat(),
                    "fixture_count": 1,
                    "error_count": 0,
                },
            ],
        },
        "understat": {
            "provider": "Understat",
            "team_features": [],
            "errors": [],
        },
    }
    live_path = tmp_path / "current.json"
    live_path.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")

    result = attach_current_data(
        build_platform_snapshot(Path("data/MatchHistory")),
        live_path,
        now=as_of,
    )

    current = result["current_data"]
    assert current["status"] == "unavailable"
    assert current["canonical_fixture_provider"] == "OpenFootball"
    assert current["canonical_fixture_count"] == 0
    assert current["model_admission"]["eligible"] is False
    assert not any(
        item.get("id") == "cfl-official:csl:official-fixture-id"
        for item in result["matches"]
    )
    assert any(
        item["section"] == "fixture_feed"
        and item["source_id"] == "cfl_official_current"
        and item["fact_count"] == 1
        and item["decision"] == "block"
        for item in current["rights_quarantine"]
    )
