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
