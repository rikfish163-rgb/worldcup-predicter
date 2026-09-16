from datetime import datetime, timezone

import pytest

from league_platform.sources.football_data_china import parse_football_data_china_csv


def test_football_data_china_parser_preserves_odds_and_hash():
    payload = (
        "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA\n"
        "China,Super League,2024,01/03/2024,10:00,Shanghai Port,Wuhan Three Towns,3,1,H,1.5,4,6,1.4,4.2,7\n"
    ).encode()
    row = parse_football_data_china_csv(
        payload,
        retrieved_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )[0]
    assert row.competition_id == "csl"
    assert row.market_opening_probability is not None
    assert row.market_closing_probability is not None
    assert row.source_sha256
    assert row.market_time_basis == "closing_average_pre_kickoff"


def test_football_data_china_parser_rejects_unallowlisted_url():
    with pytest.raises(ValueError, match="allowlisted"):
        parse_football_data_china_csv(
            b"Season,Date,Time,Home,Away,HG,AG\n2024,01/03/2024,10:00,A,B,1,0\n",
            retrieved_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            url="https://example.invalid/new/CHN.csv",
        )
