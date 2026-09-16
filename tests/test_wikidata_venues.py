from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from league_platform.source_rights import DecisionValue, SourceId, UseCase, decide_source_rights


def _responses(*, ambiguous: bool = False, generic_duplicate: bool = False) -> dict[str, bytes]:
    search_rows = [
        {
            "id": "Q9617",
            "label": "Arsenal F.C.",
            "description": "association football club in London, England",
        }
    ]
    if ambiguous:
        search_rows.append(
            {
                "id": "Q123456",
                "label": "Arsenal F.C.",
                "description": "association football club in another country",
            }
        )
    if generic_duplicate:
        search_rows.append(
            {
                "id": "Q123457",
                "label": "Arsenal F.C.",
                "description": "football club in another country",
            }
        )
    return {
        "search": json.dumps({"search": search_rows}).encode(),
        "club": json.dumps(
            {
                "entities": {
                    "Q9617": {
                        "id": "Q9617",
                        "labels": {"en": {"language": "en", "value": "Arsenal F.C."}},
                        "descriptions": {
                            "en": {
                                "language": "en",
                                "value": "association football club in London, England",
                            }
                        },
                        "claims": {
                            "P115": [
                                {
                                    "rank": "preferred",
                                    "mainsnak": {
                                        "datavalue": {
                                            "value": {"entity-type": "item", "id": "Q163995"}
                                        }
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        ).encode(),
        "venue": json.dumps(
            {
                "entities": {
                    "Q163995": {
                        "id": "Q163995",
                        "labels": {"en": {"language": "en", "value": "Emirates Stadium"}},
                        "descriptions": {
                            "en": {
                                "language": "en",
                                "value": "association football stadium in London",
                            }
                        },
                        "claims": {
                            "P625": [
                                {
                                    "rank": "normal",
                                    "mainsnak": {
                                        "datavalue": {
                                            "value": {
                                                "latitude": 51.555,
                                                "longitude": -0.1083333333,
                                            }
                                        }
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        ).encode(),
    }


def test_wikidata_structured_data_policy_allows_current_coordinate_use_only() -> None:
    allowed = {
        UseCase.NETWORK_FETCH,
        UseCase.SERVE_CURRENT,
        UseCase.MODEL_INPUT,
        UseCase.REDISTRIBUTION,
    }
    for use_case in UseCase:
        result = decide_source_rights(SourceId.WIKIDATA_ENTITIES, use_case)
        if use_case in allowed:
            assert result.decision is DecisionValue.ALLOW
            assert result.commercial_reuse_verified is True
            assert result.model_eligible is True
        else:
            assert result.decision is DecisionValue.BLOCK
            assert result.model_eligible is False
        assert result.license_url == "https://www.wikidata.org/wiki/Wikidata:Licensing"


def test_wikidata_fetch_requires_unique_club_and_preferred_home_venue() -> None:
    from league_platform.live_sources.wikidata import fetch_wikidata_venues

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def geturl(self) -> str:
            return "https://www.wikidata.org/w/api.php"

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def open(self, request: object, timeout: float) -> Response:
            assert timeout == 10.0
            url = str(getattr(request, "full_url"))
            self.requests.append(url)
            action = parse_qs(urlparse(url).query)["action"][0]
            if action == "wbsearchentities":
                return Response(_responses()["search"])
            if action == "wbgetentities":
                ids = parse_qs(urlparse(url).query)["ids"][0]
                return Response(_responses()["club" if ids == "Q9617" else "venue"])
            raise AssertionError(action)

    opener = Opener()
    result = fetch_wikidata_venues(
        [
            {
                "id": "openfootball:premier-league:1001",
                "home_team": "Arsenal",
                "kickoff_at": "2026-08-28T18:00:00Z",
            }
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        opener=opener,
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert result["status"] == "ok"
    assert result["error_count"] == 0
    assert result["venues"][0]["fixture_id"] == "openfootball:premier-league:1001"
    venue = result["venues"][0]["venue"]
    assert venue["name"] == "Emirates Stadium"
    assert venue["wikidata_id"] == "Q163995"
    assert venue["latitude"] == 51.555
    assert venue["longitude"] == -0.1083333333
    assert venue["source"]["source_id"] == "wikidata_entities"
    assert venue["source"]["license"] == "CC0-1.0"
    assert len(opener.requests) == 3


def test_wikidata_ambiguous_search_is_quarantined_without_venue() -> None:
    from league_platform.live_sources.wikidata import fetch_wikidata_venues

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def geturl(self) -> str:
            return "https://www.wikidata.org/w/api.php"

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            return Response(_responses(ambiguous=True)["search"])

    result = fetch_wikidata_venues(
        [
            {
                "id": "openfootball:premier-league:1001",
                "home_team": "Arsenal",
                "kickoff_at": "2026-08-28T18:00:00Z",
            }
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        opener=Opener(),
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert result["status"] == "unavailable"
    assert result["venues"] == []
    assert result["error_count"] == 1
    assert result["errors"] == [{"reason": "team_entity_ambiguous", "count": 1}]


def test_wikidata_prefers_a_unique_primary_club_over_a_generic_duplicate() -> None:
    from league_platform.live_sources.wikidata import fetch_wikidata_venues

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def geturl(self) -> str:
            return "https://www.wikidata.org/w/api.php"

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def open(self, request: object, timeout: float) -> Response:
            url = str(getattr(request, "full_url"))
            self.requests.append(url)
            action = parse_qs(urlparse(url).query)["action"][0]
            if action == "wbsearchentities":
                return Response(_responses(generic_duplicate=True)["search"])
            ids = parse_qs(urlparse(url).query)["ids"][0]
            return Response(_responses()["club" if ids == "Q9617" else "venue"])

    opener = Opener()
    result = fetch_wikidata_venues(
        [
            {
                "id": "openfootball:premier-league:1001",
                "home_team": "Arsenal",
                "kickoff_at": "2026-08-28T18:00:00Z",
            }
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        opener=opener,
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert result["status"] == "ok"
    assert result["venues"][0]["venue"]["wikidata_id"] == "Q163995"
    assert len(opener.requests) == 3


def test_wikidata_missing_team_labels_do_not_open_network() -> None:
    from league_platform.live_sources.wikidata import fetch_wikidata_venues

    class ExplodingOpener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("network must not open for a missing team label")

    result = fetch_wikidata_venues(
        [{"id": "openfootball:premier-league:1001", "kickoff_at": "2026-08-28T18:00:00Z"}],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        opener=ExplodingOpener(),
        user_agent="Matchline/1.0 support@example.invalid",
    )
    assert result["status"] == "unavailable"
    assert result["network_opened"] is False
    assert result["errors"] == [{"reason": "home_team_missing", "count": 1}]


def test_wikidata_request_budget_stops_before_unbounded_fixture_fanout() -> None:
    from league_platform.live_sources.wikidata import fetch_wikidata_venues

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            value, self.payload = self.payload, b""
            return value

        def geturl(self) -> str:
            return "https://www.wikidata.org/w/api.php"

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def open(self, request: object, timeout: float) -> Response:
            assert timeout == 10.0
            url = str(getattr(request, "full_url"))
            self.requests.append(url)
            action = parse_qs(urlparse(url).query)["action"][0]
            if action == "wbsearchentities":
                return Response(_responses()["search"])
            return Response(_responses()["club"])

    opener = Opener()
    result = fetch_wikidata_venues(
        [
            {
                "id": "openfootball:premier-league:1001",
                "home_team": "Arsenal",
                "kickoff_at": "2026-08-28T18:00:00Z",
            }
        ],
        now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        max_requests=2,
        opener=opener,
        user_agent="Matchline/1.0 support@example.invalid",
    )

    assert len(opener.requests) == 2
    assert result["request_count"] == 2
    assert result["request_budget"] == 2
    assert result["venues"] == []
    assert result["errors"] == [{"reason": "wikidata_request_budget_exhausted", "count": 1}]
