from datetime import datetime, timezone

from league_platform.live_sources.espn import fetch_espn_fixtures
from league_platform.live_sources.espn_injuries import fetch_espn_injuries
from league_platform.live_sources.espn_market import fetch_espn_markets
from league_platform.live_sources.espn_roster import fetch_espn_team_rosters


AS_OF = datetime(2026, 8, 24, tzinfo=timezone.utc)


class ExplodingOpener:
    def open(self, *_args, **_kwargs):
        raise AssertionError("network must not open before written authorization")


def test_all_espn_current_adapters_fail_closed_before_network_without_rights():
    opener = ExplodingOpener()
    fixtures = fetch_espn_fixtures(now=AS_OF, opener=opener)
    markets = fetch_espn_markets([], now=AS_OF, opener=opener)
    rosters = fetch_espn_team_rosters([], now=AS_OF, opener=opener)

    for payload in (fixtures, markets, rosters):
        assert payload["status"] == "rights_blocked"
        assert payload["retrieved_at"] is None
        assert payload["checked_at"] == AS_OF.isoformat()
        assert payload["rights_status"] == "blocked_pending_express_written_permission"
        assert payload["terms_url"] == "https://disneytermsofuse.com/english/"
        assert payload["authorization_required"] == "express_written_permission"
        assert payload["network_opened"] is False
        assert payload["errors"] == []

    assert fixtures["fixtures"] == []
    assert markets["markets"] == []
    assert markets["fixture_updates"] == []
    assert rosters["rosters"] == []


def test_payload_authorization_reference_cannot_uplift_any_espn_adapter():
    opener = ExplodingOpener()
    payloads = (
        fetch_espn_fixtures(
            now=AS_OF,
            opener=opener,
            authorization_reference="attacker-self-issued",
        ),
        fetch_espn_markets(
            [],
            now=AS_OF,
            opener=opener,
            authorization_reference="attacker-self-issued",
        ),
        fetch_espn_team_rosters(
            [],
            now=AS_OF,
            opener=opener,
            authorization_reference="attacker-self-issued",
        ),
        fetch_espn_injuries(
            [],
            now=AS_OF,
            opener=opener,
            authorization_reference="attacker-self-issued",
        ),
    )

    for payload in payloads:
        assert payload["status"] == "rights_blocked"
        assert payload["network_opened"] is False
        assert payload["errors"] == []
        assert payload["rights"]["source_id"].startswith("espn_")
        assert payload["rights"]["use_case"] == "network_fetch"
        assert payload["rights"]["ignored_inputs"] == ["authorization_reference"]
