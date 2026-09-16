from __future__ import annotations

import importlib
import json

import pytest


def test_policy_declares_the_closed_use_case_and_source_id_sets() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    assert {item.value for item in source_rights.UseCase} == {
        "network_fetch",
        "serve_current",
        "model_input",
        "training",
        "redistribution",
    }
    assert {item.value for item in source_rights.SourceId} == {
        "openfootball_current",
        "openfootball_historical",
        "openligadb_secondary_results",
        "cfl_official_current",
        "espn_schedule_summary",
        "espn_market_summary",
        "espn_team_rosters",
        "espn_injury_reports",
        "understat_xg",
        "official_league_lineups",
        "official_premier_league_lineups",
        "official_laliga_lineups",
        "official_bundesliga_lineups",
        "official_serie_a_lineups",
        "official_ligue1_lineups",
        "public_rss_news",
        "open_meteo_geocoding",
        "open_meteo_weather",
        "sofascore_prematch",
        "sports_lottery_official",
        "oddstorm_market_comparison",
        "oddstorm_market_history",
        "football_data_historical",
        "fbref_public_stats",
        "whoscored_public_pages",
        "premier_league_public_pages",
        "laliga_public_pages",
        "laliga_official_news",
        "bundesliga_public_pages",
        "seriea_public_pages",
        "ligue1_official_news",
        "clubelo_public_ratings",
        "sofifa_public_reference",
        "fotmob_public_api",
        "wikidata_entities",
        "met_norway_weather",
        "osm_geodata",
        "pappalardo_wyscout_historical",
        "five_hundred_league_public_pages",
        "lazq_public_mirror",
        "football_data_china_public_csv",
        "sevenm_csl_public_fixture_script",
        "checkbestodds_public_pages",
    }


def test_openfootball_sources_are_allowed_for_every_use_case_with_stable_decisions() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    for source_id in ("openfootball_current", "openfootball_historical"):
        for use_case in source_rights.UseCase:
            result = source_rights.decide_source_rights(source_id, use_case)

            assert isinstance(result, source_rights.Decision)
            assert result.policy_version == "v260"
            assert result.source_id == source_id
            assert result.use_case == use_case.value
            assert result.decision == source_rights.DecisionValue.ALLOW
            assert result.rights_status == "verified_cc0"
            assert result.license_url == (
                "https://github.com/openfootball/football.json/blob/master/LICENSE.md"
            )
            assert result.terms_url is None
            assert result.commercial_reuse_verified is True
            assert result.access_allowed is True
            assert result.model_eligible is True
            assert result.network_opened is False
            assert result.attribution_required is False
            assert result.share_alike_required is False
            assert result.reason
            assert result.ignored_inputs == ()
            assert result.allowed is True
            assert result.is_allowed is True

            payload = result.as_dict()
            assert list(payload) == sorted(payload)
            assert payload == {
                "access_allowed": True,
                "attribution_required": False,
                "commercial_reuse_verified": True,
                "decision": "allow",
                "ignored_inputs": [],
                "license_url": (
                    "https://github.com/openfootball/football.json/blob/master/LICENSE.md"
                ),
                "model_eligible": True,
                "network_opened": False,
                "policy_version": "v260",
                "reason": "OpenFootball CC0 is verified for every v260 use case.",
                "rights_status": "verified_cc0",
                "share_alike_required": False,
                "source_id": source_id,
                "terms_url": None,
                "use_case": use_case.value,
            }


def test_every_declared_source_and_use_case_has_the_v260_fail_closed_decision() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")
    openfootball_sources = {"openfootball_current", "openfootball_historical"}
    openligadb_current_only = {"network_fetch", "serve_current"}
    future_disabled = {
        "osm_geodata",
        "pappalardo_wyscout_historical",
    }
    observed_pairs: set[tuple[str, str]] = set()

    for source_id in source_rights.SourceId:
        for use_case in source_rights.UseCase:
            result = source_rights.decide_source_rights(source_id, use_case)
            observed_pairs.add((source_id.value, use_case.value))

            if source_id.value in openfootball_sources:
                expected = source_rights.DecisionValue.ALLOW
            elif (
                source_id.value == "openligadb_secondary_results"
                and use_case.value in openligadb_current_only
            ):
                expected = source_rights.DecisionValue.ALLOW
            elif (
                source_id.value == "met_norway_weather"
                and use_case.value
                in {"network_fetch", "serve_current", "model_input", "redistribution"}
            ):
                expected = source_rights.DecisionValue.ALLOW
            elif (
                source_id.value == "wikidata_entities"
                and use_case.value in {"network_fetch", "serve_current", "model_input", "redistribution"}
            ):
                expected = source_rights.DecisionValue.ALLOW
            elif source_id.value in future_disabled:
                expected = source_rights.DecisionValue.FUTURE_DISABLED
            else:
                expected = source_rights.DecisionValue.BLOCK

            assert result.policy_version == "v260"
            assert result.source_id == source_id.value
            assert result.use_case == use_case.value
            assert result.decision is expected
            assert result.access_allowed is (expected is source_rights.DecisionValue.ALLOW)
            assert result.allowed is result.access_allowed
            assert result.is_allowed is result.access_allowed
            assert result.model_eligible is (
                source_id.value in openfootball_sources
                or (
                source_id.value == "met_norway_weather"
                    and use_case.value
                    in {"network_fetch", "serve_current", "model_input", "redistribution"}
                )
                or (
                    source_id.value == "wikidata_entities"
                    and use_case.value
                    in {"network_fetch", "serve_current", "model_input", "redistribution"}
                )
            )
            assert result.network_opened is False
            assert isinstance(result.commercial_reuse_verified, bool)
            assert isinstance(result.attribution_required, bool)
            assert isinstance(result.share_alike_required, bool)
            assert result.reason

    assert len(observed_pairs) == len(source_rights.SourceId) * len(source_rights.UseCase)


def test_openligadb_is_isolated_to_fetch_and_current_serving() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    for use_case in source_rights.UseCase:
        result = source_rights.decide_source_rights(
            source_rights.SourceId.OPENLIGADB_SECONDARY_RESULTS,
            use_case,
        )
        expected_allowed = use_case in {
            source_rights.UseCase.NETWORK_FETCH,
            source_rights.UseCase.SERVE_CURRENT,
        }
        assert result.allowed is expected_allowed
        assert result.rights_status == "verified_odbl_isolated"
        assert result.license_url == "https://www.openligadb.de/lizenz"
        assert result.terms_url is None
        assert result.commercial_reuse_verified is True
        assert result.model_eligible is False
        assert result.attribution_required is True
        assert result.share_alike_required is True
        assert "ODbL" in result.reason


def test_future_sources_keep_open_license_facts_but_are_not_currently_enabled() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")
    expected_metadata: dict[str, tuple[str, str | None, bool, bool]] = {
        "osm_geodata": (
            "https://opendatacommons.org/licenses/odbl/1-0/",
            "https://www.openstreetmap.org/copyright",
            True,
            True,
        ),
        "pappalardo_wyscout_historical": (
            "https://creativecommons.org/licenses/by/4.0/",
            "https://figshare.com/collections/Soccer_match_event_dataset/4415000",
            True,
            False,
        ),
    }

    for source_id, metadata in expected_metadata.items():
        license_url, terms_url, attribution_required, share_alike_required = metadata
        for use_case in source_rights.UseCase:
            result = source_rights.decide_source_rights(source_id, use_case)
            assert result.decision is source_rights.DecisionValue.FUTURE_DISABLED
            assert result.rights_status == ("future_disabled_pending_adapter_attribution_contract")
            assert result.commercial_reuse_verified is False
            assert result.access_allowed is False
            assert result.model_eligible is False
            assert result.license_url == license_url
            assert result.terms_url == terms_url
            assert result.attribution_required is attribution_required
            assert result.share_alike_required is share_alike_required


def test_blocked_provider_terms_are_retained_without_granting_access() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")
    terms = {
        "espn_schedule_summary": "https://disneytermsofuse.com/english/",
        "espn_market_summary": "https://disneytermsofuse.com/english/",
        "espn_team_rosters": "https://disneytermsofuse.com/english/",
        "espn_injury_reports": "https://disneytermsofuse.com/english/",
        "oddstorm_market_comparison": "https://www.oddstorm.com/terms",
        "oddstorm_market_history": "https://www.oddstorm.com/terms",
    }

    for source_id, terms_url in terms.items():
        result = source_rights.decide_source_rights(
            source_id,
            source_rights.UseCase.NETWORK_FETCH,
        )
        assert result.decision is source_rights.DecisionValue.BLOCK
        assert result.rights_status == "blocked_pending_express_written_permission"
        assert result.terms_url == terms_url
        assert result.access_allowed is False
        assert result.network_opened is False


def test_unknown_source_and_unknown_use_case_fail_closed_instead_of_raising() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    for use_case in source_rights.UseCase:
        unknown_source = source_rights.decide_source_rights(
            "operator_claimed_new_source",
            use_case,
        )
        assert unknown_source.source_id == "operator_claimed_new_source"
        assert unknown_source.use_case == use_case.value
        assert unknown_source.decision is source_rights.DecisionValue.BLOCK
        assert unknown_source.rights_status == "unknown_source_fail_closed"
        assert unknown_source.access_allowed is False
        assert unknown_source.model_eligible is False
        assert unknown_source.network_opened is False

    unknown_use_case = source_rights.decide_source_rights(
        source_rights.SourceId.OPENFOOTBALL_CURRENT,
        "operator_defined_use_case",
    )
    assert unknown_use_case.decision is source_rights.DecisionValue.BLOCK
    assert unknown_use_case.rights_status == "unknown_use_case_fail_closed"
    assert unknown_use_case.access_allowed is False
    assert unknown_use_case.model_eligible is False
    assert unknown_use_case.network_opened is False


def test_untrusted_authorization_registry_and_config_are_recorded_but_never_uplift() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")
    external_inputs = {
        "authorization_reference": "EXPRESS-WRITTEN-PERMISSION-CLAIM",
        "operator_registry": {
            "default": "allow",
            "commercial_reuse_verified": True,
        },
        "config": {
            "decision": "allow",
            "model_eligible": True,
            "network_opened": True,
        },
    }
    blocked_pairs = 0

    for source_id in source_rights.SourceId:
        for use_case in source_rights.UseCase:
            baseline = source_rights.decide_source_rights(source_id, use_case)
            if baseline.allowed:
                continue
            blocked_pairs += 1
            attempted_uplift = source_rights.decide_source_rights(
                source_id,
                use_case,
                **external_inputs,
            )
            assert attempted_uplift.decision is baseline.decision
            assert attempted_uplift.rights_status == baseline.rights_status
            assert attempted_uplift.access_allowed is False
            assert attempted_uplift.model_eligible is baseline.model_eligible
            assert attempted_uplift.network_opened is False
            assert attempted_uplift.ignored_inputs == (
                "authorization_reference",
                "config",
                "operator_registry",
            )

    assert blocked_pairs > 0


def test_require_source_rights_returns_allow_and_raises_a_fixed_typed_block() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    allowed = source_rights.require_source_rights(
        source_rights.SourceId.OPENFOOTBALL_CURRENT,
        source_rights.UseCase.TRAINING,
    )
    assert allowed.is_allowed is True

    with pytest.raises(source_rights.SourceRightsBlocked) as caught:
        source_rights.require_source_rights(
            source_rights.SourceId.ESPN_SCHEDULE_SUMMARY,
            source_rights.UseCase.NETWORK_FETCH,
            authorization_reference="untrusted-string",
        )

    assert str(caught.value) == (
        "source rights blocked by v260: source_id=espn_schedule_summary "
        "use_case=network_fetch decision=block"
    )
    assert caught.value.decision.source_id == "espn_schedule_summary"
    assert caught.value.decision.network_opened is False
    assert caught.value.decision.ignored_inputs == ("authorization_reference",)


def test_all_decisions_are_immutable_pure_json_and_canonicalizable() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    def assert_json_only(value: object) -> None:
        if isinstance(value, dict):
            assert all(isinstance(key, str) for key in value)
            for child in value.values():
                assert_json_only(child)
        elif isinstance(value, list):
            for child in value:
                assert_json_only(child)
        else:
            assert value is None or isinstance(value, (str, bool))

    first_pass: list[str] = []
    second_pass: list[str] = []
    for source_id in source_rights.SourceId:
        for use_case in source_rights.UseCase:
            result = source_rights.decide_source_rights(source_id, use_case)
            payload = result.as_dict()
            assert list(payload) == sorted(payload)
            assert_json_only(payload)
            first_pass.append(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            second_pass.append(
                json.dumps(
                    source_rights.decide_source_rights(source_id, use_case).as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    assert first_pass == second_pass
    decision = source_rights.decide_source_rights(
        source_rights.SourceId.OPENFOOTBALL_CURRENT,
        source_rights.UseCase.NETWORK_FETCH,
    )
    with pytest.raises((AttributeError, TypeError)):
        decision.reason = "mutated"


def test_rights_blocked_envelope_is_typed_empty_and_never_claims_network_failure() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    envelope = source_rights.rights_blocked_envelope(
        source_rights.SourceId.ESPN_MARKET_SUMMARY,
        provider="ESPN market summary",
        checked_at="2026-08-25T00:30:00+00:00",
        empty_fields=("fixtures", "markets", "lines"),
        authorization_reference="self-issued",
    )

    assert envelope == {
        "access_allowed": False,
        "checked_at": "2026-08-25T00:30:00+00:00",
        "commercial_reuse_verified": False,
        "errors": [],
        "fixtures": [],
        "lines": [],
        "markets": [],
        "model_eligible": False,
        "network_opened": False,
        "provider": "ESPN market summary",
        "retrieved_at": None,
        "rights": source_rights.decide_source_rights(
            source_rights.SourceId.ESPN_MARKET_SUMMARY,
            source_rights.UseCase.NETWORK_FETCH,
            authorization_reference="self-issued",
        ).as_dict(),
        "rights_status": "blocked_pending_express_written_permission",
        "schema_version": "matchline.source_rights_result.v1",
        "status": "rights_blocked",
    }
    assert envelope["rights"]["ignored_inputs"] == ["authorization_reference"]
    assert json.loads(json.dumps(envelope, allow_nan=False)) == envelope


def test_rights_blocked_envelope_rejects_allowed_sources_and_malformed_contracts() -> None:
    source_rights = importlib.import_module("league_platform.source_rights")

    with pytest.raises(ValueError, match="allowed decision"):
        source_rights.rights_blocked_envelope(
            source_rights.SourceId.OPENFOOTBALL_CURRENT,
            provider="OpenFootball",
            checked_at="2026-08-25T00:30:00+00:00",
            empty_fields=("fixtures",),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        source_rights.rights_blocked_envelope(
            source_rights.SourceId.ESPN_SCHEDULE_SUMMARY,
            provider="ESPN",
            checked_at="2026-08-25T00:30:00",
            empty_fields=("fixtures",),
        )
    with pytest.raises(ValueError, match="empty field"):
        source_rights.rights_blocked_envelope(
            source_rights.SourceId.ESPN_SCHEDULE_SUMMARY,
            provider="ESPN",
            checked_at="2026-08-25T00:30:00+00:00",
            empty_fields=("operator_invented_rows",),
        )
