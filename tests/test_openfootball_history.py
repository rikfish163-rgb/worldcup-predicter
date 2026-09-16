from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from league_platform.live_sources import openfootball_live
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_CURRENT_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
    OPENFOOTBALL_HISTORY_SOURCES,
    fetch_openfootball_current,
    fetch_openfootball_history,
    parse_openfootball_txt,
)


AS_OF = datetime(2026, 8, 25, tzinfo=timezone.utc)


def test_history_txt_parser_accepts_the_score_between_team_names_format():
    source_id = "openfootball:england:2015-16:1-premierleague"
    config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
    payload = """= English Premier League 2015/16
▪ Matchday 1
Sat Aug 8 2015
  12:45  Manchester United        1-0 (1-0)  Tottenham Hotspur
  15:00  AFC Bournemouth          0-1 (0-0)  Aston Villa
         Everton FC               2-2 (0-1)  Watford FC
""".encode()

    rows = parse_openfootball_txt(
        payload,
        source_id=source_id,
        retrieved_at=AS_OF,
        url=config["url"],
    )

    assert [(row["home_team"], row["away_team"]) for row in rows] == [
        ("Manchester United", "Tottenham Hotspur"),
        ("AFC Bournemouth", "Aston Villa"),
        ("Everton FC", "Watford FC"),
    ]
    assert rows[0]["score"] == {"home": 1, "away": 0}
    assert rows[0]["halftime_score"] == {"home": 1, "away": 0}
    assert rows[2]["kickoff_at"] == rows[1]["kickoff_at"]


def test_history_identity_distinguishes_regular_and_playoff_rematches():
    source_id = "openfootball:england:2021-22:2-championship"
    config = OPENFOOTBALL_HISTORY_SOURCES[source_id]
    payload = """= English Championship 2021/22
▪ Matchday 8
Sat Sep 18 2021
  15:00  Huddersfield Town AFC  0-2 (0-1)  Nottingham Forest FC
▪ Playoffs
Sun May 29 2022
  16:30  Huddersfield Town AFC  0-1 (0-1)  Nottingham Forest FC
""".encode()

    rows = parse_openfootball_txt(
        payload,
        source_id=source_id,
        retrieved_at=AS_OF,
        url=config["url"],
    )

    assert len(rows) == 2
    assert rows[0]["home_team"] == rows[1]["home_team"]
    assert rows[0]["away_team"] == rows[1]["away_team"]
    assert rows[0]["id"] != rows[1]["id"]


def test_history_manifest_is_an_exact_cc0_six_league_eleven_season_allowlist():
    assert len(OPENFOOTBALL_HISTORY_SOURCES) == 66
    assert len(OPENFOOTBALL_HISTORY_SOURCE_IDS) == 66
    assert set(OPENFOOTBALL_CURRENT_SOURCE_IDS).isdisjoint(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    assert len({row["url"] for row in OPENFOOTBALL_HISTORY_SOURCES.values()}) == 66
    assert Counter(row["competition_id"] for row in OPENFOOTBALL_HISTORY_SOURCES.values()) == {
        "premier-league": 11,
        "championship": 11,
        "bundesliga": 11,
        "la-liga": 11,
        "serie-a": 11,
        "ligue-1": 11,
    }
    assert {row["season"] for row in OPENFOOTBALL_HISTORY_SOURCES.values()} == {
        "2015-16",
        "2016-17",
        "2017-18",
        "2018-19",
        "2019-20",
        "2020-21",
        "2021-22",
        "2022-23",
        "2023-24",
        "2024-25",
        "2025-26",
    }
    assert all(
        row["format"] == "txt"
        and row["url"].startswith("https://raw.githubusercontent.com/openfootball/")
        and row["url"].endswith(".txt")
        for row in OPENFOOTBALL_HISTORY_SOURCES.values()
    )


def test_current_default_never_expands_into_the_historical_fanout(monkeypatch):
    seen: list[str] = []

    def fake_fetch_one(config, *, retrieved_at, opener):
        assert retrieved_at == AS_OF
        assert opener is None
        seen.append(config["source_id"])
        return [], 0

    monkeypatch.setattr(openfootball_live, "_fetch_one", fake_fetch_one)

    result = fetch_openfootball_current(now=AS_OF)

    assert set(seen) == set(OPENFOOTBALL_CURRENT_SOURCE_IDS)
    assert len(seen) == 8
    assert result["diagnostics"]["selected_source_count"] == 8
    assert result["source_contract"]["rights"]["source_id"] == "openfootball_current"
    assert result["source_contract"]["rights"]["use_case"] == "network_fetch"
    assert result["training_admitted"] is False
    assert result["raw_archive_receipts"] == []
    assert result["source_contract"]["admission_status"] == "candidate_only_unarchived"


def test_history_fetch_uses_only_the_fixed_history_manifest(monkeypatch):
    seen: list[str] = []

    def fake_fetch_one(config, *, retrieved_at, opener):
        assert retrieved_at == AS_OF
        assert opener is None
        seen.append(config["source_id"])
        fixture_id = config["source_id"].replace(":", "-")
        return [
            {
                "id": f"openfootball:{config['competition_id']}:{fixture_id}",
                "competition_id": config["competition_id"],
                "season": config["season"],
                "kickoff_at": "2025-08-01T12:00:00+00:00",
                "kickoff_date": "2025-08-01",
                "kickoff_time_quality": "exact",
                "status": "finished",
                "source": {
                    "name": "OpenFootball",
                    "source_id": config["source_id"],
                    "url": config["url"],
                    "license": "CC0-1.0",
                },
                "lineage": {"source_id": config["source_id"]},
            }
        ], 0

    monkeypatch.setattr(openfootball_live, "_fetch_one", fake_fetch_one)

    result = fetch_openfootball_history(now=AS_OF)

    assert set(seen) == set(OPENFOOTBALL_HISTORY_SOURCE_IDS)
    assert len(seen) == 66
    assert result["status"] == "ok"
    assert result["history_fixture_count"] == 66
    assert result["unfinished_fixture_count"] == 0
    assert result["source_contract"]["rights"]["source_id"] == ("openfootball_historical")
    assert result["source_contract"]["rights"]["use_case"] == "network_fetch"
    assert result["training_admitted"] is False
    assert result["raw_archive_receipts"] == []
    assert result["source_contract"]["admission_status"] == "candidate_only_unarchived"
    assert Counter(row["competition_id"] for row in result["history"]) == {
        "premier-league": 11,
        "championship": 11,
        "bundesliga": 11,
        "la-liga": 11,
        "serie-a": 11,
        "ligue-1": 11,
    }
