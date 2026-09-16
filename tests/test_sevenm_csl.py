from __future__ import annotations

from datetime import datetime, timezone

import pytest

from league_platform.sources.sevenm_csl import (
    fetch_sevenm_csl_season,
    parse_sevenm_csl_fixture_script,
    season_url,
)


URL = season_url("2025")


def _script(*, unfinished: bool = False) -> bytes:
    score = "VS" if unfinished else "2-1(1-0)"
    return (
        "var Tmp_bh_Arr = [4834281];\n"
        'var Time_Arr = ["2025,02,22,19,35,00"];\n'
        f'var Scores_Arr = ["{score}"];\n'
        'var TeamA_Arr = ["北京国安"];\n'
        'var TeamB_Arr = ["上海海港"];\n'
    ).encode()


def test_parser_normalizes_finished_score_and_keeps_audit_only_semantics():
    payload = _script()
    rows = parse_sevenm_csl_fixture_script(payload, season="2025", url=URL)

    assert len(rows) == 1
    row = rows[0]
    assert row.provider_fixture_id == "7m:4834281"
    assert row.kickoff_at.isoformat() == "2025-02-22T19:35:00+08:00"
    assert row.score is not None
    assert row.score.home == 2
    assert row.score.away == 1
    assert row.score.halftime_home == 1
    assert row.score.halftime_away == 0
    assert row.market_probability is None
    assert row.source_name == "7M Sports"
    assert row.source_license_status == "provider_terms_require_review"


def test_parser_skips_unfinished_rows():
    assert parse_sevenm_csl_fixture_script(_script(unfinished=True), season="2025", url=URL) == []


def test_parser_rejects_inconsistent_arrays_and_non_allowlisted_urls():
    payload = _script().replace(b"[4834281]", b"[4834281, 4834282]")
    with pytest.raises(ValueError, match="inconsistent lengths"):
        parse_sevenm_csl_fixture_script(payload, season="2025", url=URL)
    with pytest.raises(ValueError, match="allowlisted"):
        parse_sevenm_csl_fixture_script(_script(), season="2025", url=f"{URL}?round=1")
    with pytest.raises(ValueError, match="allowlisted"):
        parse_sevenm_csl_fixture_script(
            _script(), season="2025", url="https://evil.example/fixture.js"
        )


def test_season_url_is_strictly_allowlisted():
    assert season_url("2025-2026").endswith("/2025-2026/152/en/fixture.js")
    with pytest.raises(ValueError):
        season_url("2025?x=1")


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _limit: int):
        return self.payload


class _Opener:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def open(self, _request, timeout: int):
        assert timeout == 30
        return _Response(self.payload, self.url)


def test_fetch_records_raw_hash_and_never_marks_rows_model_eligible():
    payload = _script()
    result = fetch_sevenm_csl_season(
        "2025",
        now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
        opener=_Opener(payload, URL),
    )

    assert result["status"] == "rights_blocked"
    assert result["matches"] == []
    assert result["model_eligible"] is False
    assert result["rights"]["source_id"] == "sevenm_csl_public_fixture_script"
