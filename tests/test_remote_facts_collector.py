import importlib.util
from pathlib import Path
from urllib.parse import urlparse


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "remote-facts" / "matchline_facts_collector.py"
SPEC = importlib.util.spec_from_file_location("matchline_facts_collector", MODULE_PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def _row(match_id: int, shortcut: str) -> dict[str, object]:
    return {
        "matchID": match_id,
        "matchDateTimeUTC": "2026-09-04T18:30:00Z",
        "leagueSeason": 2026,
        "leagueShortcut": shortcut,
        "matchIsFinished": False,
        "team1": {"teamId": match_id, "teamName": f"Home {shortcut}"},
        "team2": {"teamId": match_id + 1, "teamName": f"Away {shortcut}"},
        "matchResults": [],
    }


def _finished_row(match_id: int, shortcut: str) -> dict[str, object]:
    value = _row(match_id, shortcut)
    value.update(
        {
            "matchIsFinished": True,
            "matchResults": [
                {"resultName": "Halbzeit", "pointsTeam1": 1, "pointsTeam2": 0},
                {"resultName": "Endergebnis", "pointsTeam1": 2, "pointsTeam2": 1},
            ],
            "goals": [
                {
                    "goalID": 77,
                    "scoreTeam1": 1,
                    "scoreTeam2": 0,
                    "matchMinute": 12,
                    "goalGetterID": 501,
                    "goalGetterName": "Home scorer",
                    "scoringTeamId": match_id,
                    "isPenalty": False,
                    "isOwnGoal": False,
                    "isOvertime": False,
                }
            ],
            "location": {"locationCity": "Test City", "locationStadium": "Test Ground"},
        }
    )
    return value


def test_openligadb_collector_fetches_only_allowlisted_leagues(monkeypatch):
    calls: list[str] = []

    def fake_http_json(url: str, **_kwargs):
        calls.append(url)
        shortcut = urlparse(url).path.split("/")[-2]
        return 200, b"[]", [_row(90000 + len(calls), shortcut)]

    monkeypatch.setattr(collector, "_http_json", fake_http_json)
    sources, rows = collector.collect_openligadb(2026, "2026-09-03T00:00:00Z")

    assert [urlparse(url).path.split("/")[-2] for url in calls] == ["bl1", "bl2", "bl3"]
    assert [source["sourceId"] for source in sources] == [
        "openligadb_secondary_results",
        "openligadb_secondary_results_bl2",
        "openligadb_secondary_results_bl3",
    ]
    assert {row["league"] for row in rows} == {"bl1", "bl2", "bl3"}
    assert {row["competitionId"] for row in rows} == {"bundesliga", "bundesliga-2", "bundesliga-3"}


def test_openligadb_collector_supports_expanded_public_competitions(monkeypatch):
    calls: list[str] = []

    def fake_http_json(url: str, **_kwargs):
        calls.append(url)
        shortcut = urlparse(url).path.split("/")[-2]
        return 200, b"[]", [_row(92000 + len(calls), shortcut)]

    monkeypatch.setattr(collector, "_http_json", fake_http_json)
    sources, rows = collector.collect_openligadb(
        2026,
        "2026-09-03T00:00:00Z",
        league_shortcuts=collector.REMOTE_OPENLIGADB_LEAGUES,
    )

    assert [urlparse(url).path.split("/")[-2] for url in calls] == [
        "bl1", "bl2", "bl3", "pl", "la1", "ucl", "dfb", "uel2026", "nla", "wm26",
    ]
    assert len(sources) == len(collector.REMOTE_OPENLIGADB_LEAGUES)
    assert {row["league"] for row in rows} == set(collector.REMOTE_OPENLIGADB_LEAGUES)
    assert {row["competitionId"] for row in rows} == {
        "bundesliga", "bundesliga-2", "bundesliga-3", "premier-league",
        "la-liga", "champions-league", "dfb-pokal", "europa-league",
        "nations-league-a", "world-cup",
    }


def test_openligadb_collector_quarantines_wrong_shortcut_rows(monkeypatch):
    def fake_http_json(url: str, **_kwargs):
        shortcut = urlparse(url).path.split("/")[-2]
        wrong = "bl2" if shortcut == "bl1" else "bl1"
        return 200, b"[]", [_row(91000, wrong)]

    monkeypatch.setattr(collector, "_http_json", fake_http_json)
    sources, rows = collector.collect_openligadb(2026, "2026-09-03T00:00:00Z")

    assert rows == []
    assert all(source["recordCount"] == 0 for source in sources)
    assert all(source["errorCode"] == "no_valid_matches" for source in sources)


def test_openligadb_collector_keeps_explicit_goal_and_venue_facts(monkeypatch):
    monkeypatch.setattr(
        collector,
        "_http_json",
        lambda _url: (200, b"payload", [_finished_row(93000, "bl1")]),
    )
    _sources, rows = collector.collect_openligadb(2026, "2026-09-03T00:00:00Z", ["bl1"])
    assert len(rows) == 1
    row = rows[0]
    assert row["score"] == {"home": 2, "away": 1}
    assert row["halftimeScore"] == {"home": 1, "away": 0}
    assert row["goals"] == [{
        "id": "77",
        "minute": 12,
        "score": {"home": 1, "away": 0},
        "playerId": "501",
        "playerName": "Home scorer",
        "teamId": "93000",
        "isPenalty": False,
        "isOwnGoal": False,
        "isOvertime": False,
    }]
    assert row["venue"] == {"city": "Test City", "name": "Test Ground"}


def test_openfootball_collector_fetches_all_fixed_current_leagues(monkeypatch):
    json_payload = (
        b'{"matches":[{"round":"Matchday 1","date":"2026-09-04",'
        b'"time":"20:00","team1":"Alpha FC","team2":"Beta FC",'
        b'"score":{"ft":[2,1]}}]}'
    )
    txt_payload = (
        "▪ Matchday 1\n"
        "  Fri Sep 4 2026\n"
        "    20:30  Gamma FC             v Delta FC             1-0\n"
    ).encode("utf-8")
    calls: list[str] = []

    def fake_http_payload(url: str, **_kwargs):
        calls.append(url)
        return 200, json_payload if url.endswith(".json") else txt_payload

    monkeypatch.setattr(collector, "_http_payload", fake_http_payload)
    sources, rows = collector.collect_openfootball("2026-09-03T00:00:00Z")

    assert len(calls) == 8
    assert len(sources) == 8
    assert all(source["status"] == "fresh" for source in sources)
    assert all(source["recordCount"] == 1 for source in sources)
    assert len(rows) == 8
    assert {row["competitionId"] for row in rows} == {
        "premier-league",
        "championship",
        "bundesliga",
        "la-liga",
        "serie-a",
        "ligue-1",
        "eredivisie",
        "primeira-liga",
    }
    assert {row["status"] for row in rows} == {"finished"}
    assert all(row["score"] == {"home": 2, "away": 1} or row["score"] == {"home": 1, "away": 0} for row in rows)


def test_openfootball_ids_match_the_canonical_local_identity_contract():
    config = collector.OPENFOOTBALL_CURRENT_SOURCES[0]
    # This is the same stable identity emitted by
    # league_platform.live_sources.openfootball_live._fixture_id.  Date and
    # kickoff are deliberately absent so a reschedule cannot mint a new id.
    assert collector._openfootball_id(
        config,
        "2026-09-06",
        "Arsenal FC",
        "Chelsea FC",
        "Matchday 3",
    ) == "openfootball:premier-league:fa9e5908fbcace047ac2375e"


def test_openfootball_postseason_ids_keep_the_round_phase():
    config = collector.OPENFOOTBALL_CURRENT_SOURCES[1]
    regular = collector._openfootball_id(config, "2026-05-01", "Alpha FC", "Beta FC", "Matchday 40")
    playoff = collector._openfootball_id(config, "2026-05-01", "Alpha FC", "Beta FC", "Playoff Final")
    assert regular != playoff


def test_openfootball_history_covers_three_seasons_and_eight_leagues():
    sources = collector.OPENFOOTBALL_HISTORY_SOURCES
    assert len(sources) == 24
    assert {source["season"] for source in sources} == {"2025-26", "2024-25", "2023-24"}
    assert {source["competitionId"] for source in sources} == {
        "premier-league",
        "championship",
        "bundesliga",
        "la-liga",
        "serie-a",
        "ligue-1",
        "eredivisie",
        "primeira-liga",
    }
    assert len({source["sourceId"] for source in sources}) == 24
    urls = {source["sourceId"]: source["url"] for source in sources}
    assert all("/openfootball/europe/master/france/" in url for key, url in urls.items() if "ligue_1" in key)
    assert all("/openfootball/europe/master/netherlands/" in url for key, url in urls.items() if "eredivisie" in key)
    assert all("/openfootball/europe/master/portugal/" in url for key, url in urls.items() if "primeira_liga" in key)


def test_openfootball_collector_keeps_date_only_rows_without_inventing_kickoff(monkeypatch):
    payload = (
        "▪ Matchday 1\n"
        "  Fri Sep 4 2026\n"
        "           Date-only FC         v Time-unknown FC\n"
    ).encode("utf-8")

    monkeypatch.setattr(
        collector,
        "_http_payload",
        lambda _url, **_kwargs: (200, payload),
    )
    _sources, rows = collector.collect_openfootball(
        "2026-09-03T00:00:00Z",
        configs=collector.OPENFOOTBALL_CURRENT_SOURCES[2:],
    )

    assert len(rows) == 6
    assert all(row["status"] == "scheduled" for row in rows)
    assert all(row["kickoffAt"] is None for row in rows)
    assert all(row["scheduledDate"] == "2026-09-04" for row in rows)


def test_openfootball_history_txt_parses_score_first_completed_rows(monkeypatch):
    payload = (
        "▪ Regular Season - 1\n"
        "Fri Aug 15 2025\n"
        "  19:00   Liverpool  4-2 (1-0)  Bournemouth\n"
        "Sat Aug 16\n"
        "  15:00   Chelsea FC  1-1  Crystal Palace\n"
    ).encode("utf-8")
    config = collector.OPENFOOTBALL_HISTORY_SOURCES[0]
    source = {
        "sourceId": config["sourceId"],
        "provider": "OpenFootball",
        "url": config["url"],
        "retrievedAt": "2026-09-06T00:00:00Z",
        "license": "CC0-1.0",
        "licenseUrl": collector.OPENFOOTBALL_LICENSE_URL,
        "rawSha256": collector.sha256_hex(payload),
    }
    rows = collector._parse_openfootball_txt(payload, config, source)
    assert [(row["homeTeam"], row["awayTeam"], row["score"], row["status"]) for row in rows] == [
        ("Liverpool", "Bournemouth", {"home": 4, "away": 2}, "finished"),
        ("Chelsea FC", "Crystal Palace", {"home": 1, "away": 1}, "finished"),
    ]
    assert [row["scheduledDate"] for row in rows] == ["2025-08-15", "2025-08-16"]


def test_football_data_sidecar_keeps_only_parseable_fixture_counts(monkeypatch):
    calls: list[str] = []
    csv_body = (
        "Date,HomeTeam,AwayTeam,FTHG,FTAG,PSH\n"
        "04/09/2026,Alpha FC,Beta FC,2,1,1.5\n"
        "not-a-date,Ignored FC,Also ignored,1,0,2.0\n"
        "05/09/2026,Missing away,,0,0,3.0\n"
    ).encode("utf-8")

    def fake_http_payload(url: str, **_kwargs):
        calls.append(url)
        return 200, csv_body

    monkeypatch.setattr(collector, "_http_payload", fake_http_payload)
    sources = [
        collector._collect_football_data_source(config, 2026, "2026-09-03T00:00:00Z", rights_verified=True)
        for config in collector.FOOTBALL_DATA_SOURCES
    ]

    assert len(sources) == len(collector.FOOTBALL_DATA_SOURCES) == 8
    assert len(calls) == 8
    assert all(source["status"] == "fresh" for source in sources)
    assert all(source["recordCount"] == 1 for source in sources)
    assert all(source["modelEligible"] is False for source in sources)
    assert all(source["attributionRequired"] is True for source in sources)
    assert all("FTHG" not in source and "PSH" not in source for source in sources)
    assert all("2627" in url for url in calls)

def test_football_data_sidecar_records_transport_failure_without_zero(monkeypatch):
    monkeypatch.setattr(collector, "_http_payload", lambda _url, **_kwargs: (0, b""))
    source = collector._collect_football_data_source(
        collector.FOOTBALL_DATA_SOURCES[0],
        2026,
        "2026-09-03T00:00:00Z",
        rights_verified=True,
    )

    assert source["status"] == "unavailable"
    assert source["recordCount"] is None
    assert source["errorCode"] == "transport_error"
    assert source["licenseUrl"] == collector.FOOTBALL_DATA_DISCLAIMER_URL


def test_openfootball_txt_rolls_year_forward_after_explicit_year(monkeypatch):
    payload = (
        "▪ Matchday 18\n"
        "  Fri Jan 8 2027\n"
        "    20:00  New-year home       v New-year away\n"
        "  Sat Jan 9\n"
        "    15:00  Same-month home     v Same-month away\n"
    ).encode("utf-8")

    monkeypatch.setattr(collector, "_http_payload", lambda _url, **_kwargs: (200, payload))
    _sources, rows = collector.collect_openfootball(
        "2026-09-03T00:00:00Z",
        configs=collector.OPENFOOTBALL_CURRENT_SOURCES[2:3],
    )

    assert [row["scheduledDate"] for row in rows] == ["2027-01-08", "2027-01-09"]
    assert [row["kickoffAt"] for row in rows] == ["2027-01-08T19:00:00Z", "2027-01-09T14:00:00Z"]

def test_unverified_football_data_sidecar_is_blocked_without_network(monkeypatch):
    calls: list[str] = []

    def fail_http(*args, **kwargs):
        calls.append(str(args[0] if args else kwargs))
        raise AssertionError("unverified Football-Data source must not open network")

    monkeypatch.setattr(collector, "_http_payload", fail_http)
    sources = collector.collect_football_data(2026, "2026-09-03T00:00:00Z")

    assert calls == []
    assert len(sources) == 8
    assert all(source["status"] == "blocked" for source in sources)
    assert all(source["recordCount"] is None for source in sources)
    assert all(source["errorCode"] == "current_collection_rights_unverified" for source in sources)


def test_restricted_provider_diagnostic_is_non_network(monkeypatch):
    def fail_http(*args, **kwargs):
        raise AssertionError("restricted provider diagnostic must not open network")

    monkeypatch.setattr(collector, "_http_json", fail_http)
    result = collector.probe_provider(
        "espn_public_api",
        "ESPN public API (diagnostic only)",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?limit=1",
        "2026-09-03T00:00:00Z",
    )

    assert result["status"] == "blocked"
    assert result["recordCount"] is None
    assert result["httpStatus"] is None
    assert result["rawSha256"] is None
    assert result["errorCode"] == "provider_permission_required"
