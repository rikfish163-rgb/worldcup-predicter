from league_platform.current import (
    CURRENT_SOURCE_ROLES,
    EXPECTED_SOURCE_ROLES,
    _compatible_source_roles,
)


def test_historical_optional_role_combinations_are_replayable():
    roles = {
        key: value
        for key, value in EXPECTED_SOURCE_ROLES.items()
        if key in {
            "fixtures_and_results",
            "recent_xg_and_form",
            "historical_training",
            "current_market",
            "official_premier_league_lineups",
            "official_laliga_lineups",
        }
    }
    assert _compatible_source_roles(roles)


def test_unknown_role_value_is_rejected():
    roles = dict(EXPECTED_SOURCE_ROLES)
    roles["official_laliga_lineups"] = "unverified-feed"
    assert not _compatible_source_roles(roles)


def test_legacy_crawl4ai_role_label_remains_replayable():
    roles = dict(EXPECTED_SOURCE_ROLES)
    roles["browser_rendered_public_pages"] = (
        "Crawl4AI; explicit allowlist and append-only display evidence"
    )
    assert _compatible_source_roles(roles)


def test_current_openfootball_role_set_is_accepted_without_relabeling_as_espn():
    assert CURRENT_SOURCE_ROLES["fixtures_and_results"].startswith("OpenFootball")
    assert "CFL official" in CURRENT_SOURCE_ROLES["fixtures_and_results"]
    assert "ESPN" not in CURRENT_SOURCE_ROLES["fixtures_and_results"]
    assert CURRENT_SOURCE_ROLES["independent_asian_market"] == (
        "OddStorm authorization blocked; independent Asian market baseline unavailable"
    )
    assert _compatible_source_roles(CURRENT_SOURCE_ROLES)


def test_current_and_legacy_base_roles_cannot_be_mixed_silently():
    roles = dict(CURRENT_SOURCE_ROLES)
    roles["fixtures_and_results"] = "ESPN"

    assert not _compatible_source_roles(roles)
