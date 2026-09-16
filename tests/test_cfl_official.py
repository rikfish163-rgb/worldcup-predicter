from __future__ import annotations

import hashlib
import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from league_platform.live_sources.cfl_official import (
    CFL_API_HOST,
    CFL_OFFICIAL_PAGE_URL,
    SOURCE_NAME,
    SOURCE_RIGHTS_STATUS,
    TOURNAMENTS_PATH,
    _read_source,
    fetch_cfl_current,
    parse_cfl_matches,
)


TOURNAMENT_ID = "e6818x4pwankpph8awr91m1hw"
STAGE_ID = "e6tn384fbw6e38sk3kf0g83ys"
COMPETITION_ID = "82jkgccg7phfjpd0mltdl3pat"
OBSERVED = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _match(
    native_id: str,
    *,
    kickoff: str,
    status: str,
    week: int,
    home_id: str = "home-team-id",
    away_id: str = "away-team-id",
    home: str = "上海申花",
    away: str = "山东泰山",
    ft_home: int = 0,
    ft_away: int = 0,
    ht_home: int = 0,
    ht_away: int = 0,
    venue_name: str = "上海体育场",
    venue_name_en: str = "Shanghai Stadium",
) -> dict:
    return {
        "id": native_id,
        "competition_code": "CSL",
        "competition_id": COMPETITION_ID,
        "competition_name": "中超",
        "tournament_calendar_id": TOURNAMENT_ID,
        "tournament_calendar_name": "2026",
        "stage_id": STAGE_ID,
        "stage_name": "联赛",
        "stage_name_en": "Regular Season",
        "week": week,
        "local_date": kickoff[:10],
        "local_date_time": kickoff,
        "local_time": kickoff[11:],
        "match_status": status,
        "home_contestant_id": home_id,
        "home_contestant_name": home,
        "home_contestant_name_en": "Shanghai Shenhua",
        "home_contestant_official_name": f"{home}足球俱乐部",
        "away_contestant_id": away_id,
        "away_contestant_name": away,
        "away_contestant_name_en": "Shandong Taishan",
        "away_contestant_official_name": f"{away}足球俱乐部",
        "ft_home_score": ft_home,
        "ft_away_score": ft_away,
        "ht_home_score": ht_home,
        "ht_away_score": ht_away,
        "total_home_score": ft_home,
        "total_away_score": ft_away,
        "venue_long_name": venue_name,
        "venue_long_name_en": venue_name_en,
        "updateTime": "2026-08-23 18:00:37",
    }


def _matches_payload(matches: list[dict]) -> bytes:
    return json.dumps(
        {
            "status": 200,
            "msg": "操作成功",
            "data": {
                # The official pagination envelope uses 0 for success even
                # though the top-level API status is 200.
                "status": 0,
                "count": len(matches),
                "curPage": 1,
                "pageSize": 400,
                "dataList": matches,
                "entity": {},
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def test_parser_preserves_official_identity_lineage_and_ignores_fixture_score_placeholders():
    payload = _matches_payload(
        [
            _match(
                "played-1",
                kickoff="2026-08-23 19:35:00",
                status="Played",
                week=24,
                ft_home=2,
                ft_away=1,
                ht_home=1,
                ht_away=0,
            ),
            _match(
                "fixture-1",
                kickoff="2026-08-28 19:35:00",
                status="Fixture",
                week=25,
                home_id="future-home-id",
                away_id="future-away-id",
                home="大连英博",
                away="北京国安",
            ),
        ]
    )
    url = (
        "https://api.cfl-china.cn/frontweb/api/matches/page"
        f"?tournament_calendar_id={TOURNAMENT_ID}&competition_code=CSL"
        f"&stage_id={STAGE_ID}&curPage=1&pageSize=400"
    )

    rows = parse_cfl_matches(
        payload,
        retrieved_at=OBSERVED,
        url=url,
        tournament_calendar_id=TOURNAMENT_ID,
        stage_id=STAGE_ID,
        season="2026",
    )

    assert len(rows) == 2
    played, fixture = rows
    assert played["id"] == "cfl-official:csl:played-1"
    assert played["native_fixture_id"] == "played-1"
    assert played["provider_fixture_ids"] == {"cfl_official": "played-1"}
    assert played["home_provider_team_id"] == "home-team-id"
    assert played["away_provider_team_id"] == "away-team-id"
    assert played["kickoff_at"] == "2026-08-23T11:35:00+00:00"
    assert played["kickoff_timezone"] == "Asia/Shanghai"
    assert played["kickoff_time_quality"] == "exact"
    assert played["observed_at"] == OBSERVED.isoformat()
    assert played["effective_at"] == "2026-08-23T10:00:37+00:00"
    assert played["time_semantics"] == {
        "effective_at_basis": "provider_update_time",
        "observed_at_basis": "capture_retrieved_at",
        "fallback_reason": None,
    }
    assert played["round"] == "24"
    assert played["status"] == "finished"
    assert played["score"] == {"home": 2, "away": 1}
    assert played["halftime_score"] == {"home": 1, "away": 0}
    assert played["result_scope"] == "regulation_90"
    digest = hashlib.sha256(payload).hexdigest()
    assert played["venue"] == {
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
                "retrieved_at": OBSERVED.isoformat(),
                "raw_sha256": digest,
                "rights_status": SOURCE_RIGHTS_STATUS,
                "commercial_reuse_verified": False,
            },
        },
    }

    assert fixture["id"] == "cfl-official:csl:fixture-1"
    assert fixture["status"] == "upcoming"
    assert fixture["score"] is None
    assert fixture["halftime_score"] is None
    assert fixture["result_scope"] is None

    assert played["source"]["name"] == SOURCE_NAME
    assert played["source"]["source_id"] == "cfl-official:csl:2026"
    assert played["source"]["raw_sha256"] == digest
    assert played["source"]["hash"] == f"sha256:{digest}"
    assert played["source"]["license"] == "not_published"
    assert played["source"]["rights_status"] == SOURCE_RIGHTS_STATUS
    assert played["source"]["commercial_reuse_verified"] is False
    assert played["source"]["official_page_url"] == CFL_OFFICIAL_PAGE_URL
    assert played["source"]["retrieved_at"] == OBSERVED.isoformat()
    assert played["lineage"]["record_path"] == "$.data.dataList[0]"
    assert played["lineage"]["native_fixture_id"] == "played-1"
    assert played["lineage"]["raw_sha256"] == digest

    amended = json.loads(payload)
    amended["data"]["dataList"][0]["local_date"] = "2026-08-24"
    amended["data"]["dataList"][0]["local_date_time"] = "2026-08-24 20:00:00"
    amended["data"]["dataList"][0]["local_time"] = "20:00:00"
    amended_rows = parse_cfl_matches(
        json.dumps(amended, ensure_ascii=False).encode("utf-8"),
        retrieved_at=OBSERVED,
        url=url,
        tournament_calendar_id=TOURNAMENT_ID,
        stage_id=STAGE_ID,
        season="2026",
    )
    assert amended_rows[0]["id"] == played["id"]


@pytest.mark.parametrize(
    ("venue_name", "venue_name_en", "city", "source_name", "source_url", "raw_sha256"),
    [
        (
            "黄龙体育中心体育场",
            "Huanglong Sports Centre",
            "Hangzhou",
            "General Administration of Sport of China",
            "https://www.sport.gov.cn/n14471/n14482/n14519/c930951/content.html",
            "5ee5616474038ff61db3bdd13605f21a4b59cb11a15166a70914da2b312f7971",
        ),
        (
            "泰达足球场",
            "TEDA Football Stadium",
            "Tianjin",
            "Tianjin Economic-Technological Development Area official",
            "https://www.teda.gov.cn/contents/21/68913.html",
            "36a6fd2f83fb77f19328248cc560a4537c0f8024c99a75491dff04dcb5d114f2",
        ),
        (
            "五粮液文化体育中心专业足球场",
            "Wuliangye Sports Center Stadium",
            "Chengdu",
            "General Administration of Sport of China",
            "https://www.sport.gov.cn/n14471/n14494/n14544/c29628458/content.html",
            "f07afd381ed8468c7e3a9e01792b9b68fef9ef01afdfdc27208e8bf75ae69f77",
        ),
    ],
)
def test_parser_uses_only_reviewed_first_party_registry_for_non_explicit_venue_city(
    venue_name: str,
    venue_name_en: str,
    city: str,
    source_name: str,
    source_url: str,
    raw_sha256: str,
) -> None:
    payload = _matches_payload(
        [
            _match(
                "fixture-venue",
                kickoff="2026-08-28 19:35:00",
                status="Fixture",
                week=25,
                venue_name=venue_name,
                venue_name_en=venue_name_en,
            )
        ]
    )
    url = (
        "https://api.cfl-china.cn/frontweb/api/matches/page"
        f"?tournament_calendar_id={TOURNAMENT_ID}&competition_code=CSL"
        f"&stage_id={STAGE_ID}&curPage=1&pageSize=400"
    )

    [row] = parse_cfl_matches(
        payload,
        retrieved_at=OBSERVED,
        url=url,
        tournament_calendar_id=TOURNAMENT_ID,
        stage_id=STAGE_ID,
        season="2026",
    )

    assert row["venue"]["city"] == city
    assert row["venue"]["country"] == "China"
    assert row["venue"]["country_code"] == "CN"
    resolution = row["venue"]["city_resolution"]
    assert resolution["basis"] == "first_party_static_venue_city_registry_v1"
    assert resolution["matched_on"] == {
        "name": venue_name,
        "name_en": venue_name_en,
    }
    assert resolution["source"] == {
        "name": source_name,
        "url": source_url,
        "raw_sha256": raw_sha256,
        "reviewed_on": "2026-08-24",
        "rights_status": "public_first_party_access_reuse_terms_unverified",
        "commercial_reuse_verified": False,
    }


def test_parser_does_not_guess_city_from_unknown_venue_or_home_team() -> None:
    payload = _matches_payload(
        [
            _match(
                "fixture-unknown-venue",
                kickoff="2026-08-28 19:35:00",
                status="Fixture",
                week=25,
                home="上海申花",
                venue_name="新建专业足球场",
                venue_name_en="New Professional Football Stadium",
            )
        ]
    )
    url = (
        "https://api.cfl-china.cn/frontweb/api/matches/page"
        f"?tournament_calendar_id={TOURNAMENT_ID}&competition_code=CSL"
        f"&stage_id={STAGE_ID}&curPage=1&pageSize=400"
    )

    [row] = parse_cfl_matches(
        payload,
        retrieved_at=OBSERVED,
        url=url,
        tournament_calendar_id=TOURNAMENT_ID,
        stage_id=STAGE_ID,
        season="2026",
    )

    assert row["venue"] == {
        "name": "新建专业足球场",
        "name_en": "New Professional Football Stadium",
    }


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda rows: rows.append(dict(rows[0])),
            "duplicate official fixture ID",
        ),
        (
            lambda rows: rows[0].__setitem__("competition_code", "CFA"),
            "competition",
        ),
        (
            lambda rows: rows[0].__setitem__("tournament_calendar_name", "2025"),
            "season",
        ),
        (
            lambda rows: rows[0].__setitem__("ht_home_score", 3),
            "halftime",
        ),
        (
            lambda rows: rows[0].__setitem__("match_status", "Unknown"),
            "status",
        ),
    ],
)
def test_parser_fails_closed_on_identity_scope_score_and_status_errors(mutator, message):
    matches = [
        _match(
            "played-1",
            kickoff="2026-08-23 19:35:00",
            status="Played",
            week=24,
            ft_home=2,
            ft_away=1,
            ht_home=1,
            ht_away=0,
        )
    ]
    mutator(matches)
    with pytest.raises(ValueError, match=message):
        parse_cfl_matches(
            _matches_payload(matches),
            retrieved_at=OBSERVED,
            url=(
                "https://api.cfl-china.cn/frontweb/api/matches/page"
                f"?tournament_calendar_id={TOURNAMENT_ID}&competition_code=CSL"
                f"&stage_id={STAGE_ID}&curPage=1&pageSize=400"
            ),
            tournament_calendar_id=TOURNAMENT_ID,
            stage_id=STAGE_ID,
            season="2026",
        )


def test_fetch_discovers_current_official_season_and_builds_causal_windows():
    tournament_payload = json.dumps(
        {
            "status": 200,
            "data": {
                "count": 2,
                "dataList": [
                    {
                        "id": TOURNAMENT_ID,
                        "name": "2026",
                        "active": "yes",
                        "valid": "yes",
                        "competition_code": "CSL",
                        "competition_id": COMPETITION_ID,
                        "start_date": "2026-03-06Z",
                        "end_date": "2026-11-08Z",
                    },
                    {
                        "id": "old-season",
                        "name": "2025",
                        "active": "no",
                        "valid": "yes",
                        "competition_code": "CSL",
                        "competition_id": COMPETITION_ID,
                    },
                ],
            },
        }
    ).encode("utf-8")
    stage_payload = json.dumps(
        {
            "status": 200,
            "data": [
                {
                    "stage_id": STAGE_ID,
                    "stage_name": "联赛",
                    "stage_name_en": "Regular Season",
                }
            ],
        }
    ).encode("utf-8")
    match_payload = _matches_payload(
        [
            _match(
                "recent",
                kickoff="2026-08-23 19:35:00",
                status="Played",
                week=24,
                ft_home=2,
                ft_away=1,
                ht_home=1,
                ht_away=0,
            ),
            _match(
                "near",
                kickoff="2026-08-25 19:35:00",
                status="Fixture",
                week=25,
            ),
            _match(
                "week",
                kickoff="2026-08-29 19:35:00",
                status="Fixture",
                week=25,
                home_id="week-home",
                away_id="week-away",
            ),
        ]
    )

    class Response:
        def __init__(self, url: str, payload: bytes):
            self.url = url
            self.payload = payload
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, size: int):
            chunk = self.payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class Opener:
        def __init__(self):
            self.requests: list[str] = []

        def open(self, request, timeout):
            assert timeout > 0
            url = request.full_url
            self.requests.append(url)
            parsed = urlparse(url)
            assert parsed.scheme == "https"
            assert parsed.hostname == CFL_API_HOST
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path.endswith("/tournaments"):
                assert query == {"competition_code": ["CSL"]}
                payload = tournament_payload
            elif parsed.path.endswith("/select/stage"):
                assert query["tournament_calendar_id"] == [TOURNAMENT_ID]
                assert query["competition_code"] == ["CSL"]
                payload = stage_payload
            elif parsed.path.endswith("/matches/page"):
                assert query["tournament_calendar_id"] == [TOURNAMENT_ID]
                assert query["competition_code"] == ["CSL"]
                assert query["stage_id"] == [STAGE_ID]
                assert query["curPage"] == ["1"]
                assert query["pageSize"] == ["400"]
                payload = match_payload
            else:  # pragma: no cover - makes an unexpected request explicit
                raise AssertionError(url)
            return Response(url, payload)

    opener = Opener()
    result = fetch_cfl_current(now=OBSERVED, opener=opener)

    assert result["provider"] == SOURCE_NAME
    assert result["status"] == "rights_blocked"
    assert result["errors"] == []
    assert result["checked_at"] == OBSERVED.isoformat()
    assert result["retrieved_at"] is None
    assert result["fixtures"] == []
    assert result["recent_results"] == []
    assert result["upcoming_3_days"] == []
    assert result["upcoming_7_days"] == []
    assert result["rights"]["source_id"] == "cfl_official_current"
    assert opener.requests == []


def test_fetch_fails_closed_on_redirect_without_publishing_partial_rows():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.com/redirected"

        def read(self, _size: int):
            return b"{}"

    class Opener:
        def open(self, _request, timeout):
            assert timeout > 0
            return Response()

    result = fetch_cfl_current(now=OBSERVED, opener=Opener())

    assert result["status"] == "rights_blocked"
    assert result["fixtures"] == []
    assert result["recent_results"] == []
    assert result["upcoming_7_days"] == []
    assert result["errors"] == []


def test_bounded_reader_retries_one_transient_network_failure_without_tls_bypass():
    url = "https://api.cfl-china.cn/frontweb/api/tournaments?competition_code=CSL"

    class Response:
        def __init__(self):
            self.done = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return url

        def read(self, _size: int):
            if self.done:
                return b""
            self.done = True
            return b'{"status":200}'

    class Opener:
        def __init__(self):
            self.calls = 0

        def open(self, _request, timeout):
            assert timeout > 0
            self.calls += 1
            if self.calls == 1:
                raise urllib.error.URLError("transient TLS EOF")
            return Response()

    opener = Opener()
    payload = _read_source(
        url,
        expected_path=TOURNAMENTS_PATH,
        opener=opener,
    )

    assert payload == b'{"status":200}'
    assert opener.calls == 2
