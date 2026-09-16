import pytest

from league_platform.current import (
    _fixture_name_variants,
    _lottery_name_variants,
    _normalise_join_name,
    _validate_lottery_probability_map,
)
from league_platform.future import _canonical_three_way_probability


def test_lottery_aliases_preserve_cjk_and_map_to_canonical_fixture_names():
    row = {
        "home_team": "阿拉维斯",
        "away_team": "赫塔费",
        "home_team_en": "ALA",
        "away_team_en": "GET",
    }

    assert _normalise_join_name("Alavés") == "alaves"
    assert "阿拉维斯" in _lottery_name_variants(row, "home", "la-liga")
    assert "alaves" in _lottery_name_variants(row, "home", "la-liga")
    assert "赫塔费" in _lottery_name_variants(row, "away", "la-liga")
    assert "getafe" in _lottery_name_variants(row, "away", "la-liga")


def test_lottery_aliases_are_competition_scoped():
    row = {"home_team": "ALA", "away_team": "GET"}

    assert "alaves" in _lottery_name_variants(row, "home", "la-liga")
    assert "alaves" not in _lottery_name_variants(row, "home", "premier-league")


def test_lottery_atletico_malaga_codes_join_canonical_fixture_names():
    row = {
        "home_team": "马德里竞技",
        "away_team": "马拉加",
        "home_team_en": "ATM",
        "away_team_en": "MAL",
    }

    assert "atleticomadrid" in _lottery_name_variants(row, "home", "la-liga")
    assert "malaga" in _lottery_name_variants(row, "away", "la-liga")


def test_lottery_current_round_codes_cover_canonical_six_league_fixture_names():
    rows = [
        ("premier-league", "COV", "考文垂", "coventrycity"),
        ("premier-league", "EVE", "埃弗顿", "everton"),
        ("ligue-1", "STB", "斯特拉斯堡", "strasbourg"),
        ("serie-a", "INM", "国际米兰", "internazionale"),
        ("serie-a", "ACM", "AC米兰", "acmilan"),
        ("la-liga", "CVO", "维戈塞尔塔", "celtavigo"),
        ("la-liga", "VCA", "巴伦西亚", "valencia"),
    ]

    for competition, code, chinese, expected in rows:
        variants = _lottery_name_variants(
            {"home_team": chinese, "home_team_en": code},
            "home",
            competition,
        )
        assert expected in variants, (competition, code, chinese, variants)


@pytest.mark.parametrize(
    ("competition", "chinese", "canonical"),
    [
        ("ligue-1", "勒阿弗尔", "Le Havre AC"),
        ("serie-a", "亚特兰大", "Atalanta"),
        ("serie-a", "萨索洛", "Sassuolo"),
        ("serie-a", "博洛尼亚", "Bologna"),
        ("serie-a", "佛罗伦萨", "Fiorentina"),
        ("premier-league", "富勒姆", "Fulham"),
        ("la-liga", "拉科鲁尼亚", "La Coruna"),
    ],
)
def test_lottery_sales_names_join_current_canonical_fixtures(
    competition: str,
    chinese: str,
    canonical: str,
):
    variants = _lottery_name_variants(
        {"home_team": chinese, "home_team_en": None},
        "home",
        competition,
    )

    assert _normalise_join_name(canonical) in variants


def test_current_fixture_names_expose_explicit_historical_canonical_variants():
    cases = (
        ("premier-league", "Fulham FC", "fulham"),
        ("la-liga", "RC Deportivo La Coruña", "lacoruna"),
        ("serie-a", "Bologna FC 1909", "bologna"),
    )

    for competition, provider_name, expected in cases:
        variants = _fixture_name_variants(
            {"home_team": provider_name, "home_team_en": None},
            "home",
            competition,
        )
        assert expected in variants


def test_lottery_probability_contract_rejects_unknown_keys_and_non_normalized_values():
    _validate_lottery_probability_map({"h": 0.5, "d": 0.25, "a": 0.25}, "had_probability")
    _validate_lottery_probability_map({}, "crs_probability")

    with pytest.raises(ValueError, match="unknown outcome key"):
        _validate_lottery_probability_map({"h": 0.5, "d": 0.25, "x": 0.25}, "had_probability")
    with pytest.raises(ValueError, match="sum to one"):
        _validate_lottery_probability_map({"h": 0.6, "d": 0.2, "a": 0.1}, "hhad_probability")


def test_lottery_three_way_output_uses_platform_probability_keys():
    result = _canonical_three_way_probability({"h": 0.5, "d": 0.25, "a": 0.25})
    assert result == {"home": 0.5, "draw": 0.25, "away": 0.25}
    assert _canonical_three_way_probability({"h": 0.5, "x": 0.5}) is None
