from league_platform.news_evidence import resolve_news_rows


FIXTURES = [
    {
        "id": "espn:1",
        "kickoff_at": "2026-08-20T19:00:00+00:00",
        "home_team": "Marseille",
        "away_team": "Strasbourg",
    },
]


def _news(*, title: str, published: str = "2026-08-20T10:00:00+00:00", observed: str = "2026-08-20T11:00:00+00:00") -> dict:
    return {
        "entity_type": "news",
        "entity_id": "https://news.example/story",
        "kind": "news_fact_candidate",
        "payload": {
            "title": title,
            "summary": "Team news and expected lineups.",
            "link": "https://news.example/story",
            "published_at": published,
        },
        "source_name": "Google News RSS",
        "source_url": "https://news.google.com/rss/search?q=football",
        "source_tier": "reliable_media",
        "effective_at": published,
        "observed_at": observed,
        "confidence": 0.45,
        "raw_hash": "a" * 64,
        "enters_model": False,
    }


def test_resolves_unique_exact_team_pair_and_keeps_display_only_boundary():
    rows, diagnostics = resolve_news_rows(
        [_news(title="Marseille predicted XI v Strasbourg")],
        FIXTURES,
    )

    assert diagnostics["linked_news_rows"] == 1
    assert diagnostics["unlinked_news_rows"] == 0
    assert rows[0]["entity_type"] == "fixture"
    assert rows[0]["entity_id"] == "espn:1"
    assert rows[0]["enters_model"] is False
    assert rows[0]["payload"]["match_resolution"]["model_use"] == "display_only"


def test_rejects_single_team_noise_without_mutating_raw_row():
    original = _news(title="Marseille training update")
    rows, diagnostics = resolve_news_rows([original], FIXTURES)

    assert diagnostics["linked_news_rows"] == 0
    assert diagnostics["unlinked_reasons"] == {"team_provider_id_unavailable": 1}
    assert rows[0] == original


def test_rejects_post_kickoff_or_missing_time():
    late = _news(
        title="Marseille v Strasbourg post-match report",
        published="2026-08-20T20:01:00+00:00",
        observed="2026-08-20T20:02:00+00:00",
    )
    missing = _news(title="Marseille v Strasbourg preview")
    missing["effective_at"] = None
    missing["payload"].pop("published_at")

    rows, diagnostics = resolve_news_rows([late, missing], FIXTURES)

    assert diagnostics["linked_news_rows"] == 0
    assert diagnostics["unlinked_reasons"] == {
        "missing_causal_timestamp": 1,
        "no_exact_team_pair_pre_kickoff": 1,
    }
    assert all(row["entity_type"] == "news" for row in rows)


def test_rejects_ambiguous_repeated_fixture_pair():
    fixtures = FIXTURES + [{**FIXTURES[0], "id": "espn:2", "kickoff_at": "2026-08-27T19:00:00+00:00"}]
    rows, diagnostics = resolve_news_rows(
        [_news(title="Marseille v Strasbourg team news", published="2026-08-19T10:00:00+00:00")],
        fixtures,
    )

    assert diagnostics["linked_news_rows"] == 0
    assert diagnostics["unlinked_reasons"] == {"ambiguous_team_pair_fixture": 1}
    assert rows[0]["entity_type"] == "news"


def test_rejects_old_pair_article_even_when_the_same_pair_is_scheduled_later():
    rows, diagnostics = resolve_news_rows(
        [_news(
            title="Marseille v Strasbourg preview",
            published="2026-07-01T10:00:00+00:00",
            observed="2026-07-01T11:00:00+00:00",
        )],
        FIXTURES,
    )

    assert diagnostics["unlinked_reasons"] == {"news_too_early_for_fixture": 1}
    assert rows[0]["entity_type"] == "news"


def test_supports_unicode_team_pair_names():
    rows, diagnostics = resolve_news_rows(
        [_news(title="上海申花 vs 北京国安 赛前阵容")],
        [{
            "id": "espn:cn-1",
            "kickoff_at": "2026-08-20T11:00:00+00:00",
            "home_team": "上海申花",
            "away_team": "北京国安",
        }],
    )

    assert diagnostics["linked_news_rows"] == 1
    assert rows[0]["entity_id"] == "espn:cn-1"


def test_name_index_keeps_repeated_pair_ambiguous_without_scanning_unrelated_fixtures():
    fixtures = [
        {
            "id": "espn:target",
            "kickoff_at": "2026-08-20T19:00:00+00:00",
            "home_team": "Marseille",
            "away_team": "Strasbourg",
        },
        {
            "id": "espn:other",
            "kickoff_at": "2026-08-20T20:00:00+00:00",
            "home_team": "Lyon",
            "away_team": "Nice",
        },
    ]
    rows, diagnostics = resolve_news_rows(
        [_news(title="Marseille v Strasbourg preview")],
        fixtures,
    )

    assert diagnostics["linked_news_rows"] == 1
    assert rows[0]["entity_id"] == "espn:target"


def test_resolves_exact_single_team_with_provider_identity_for_display_only():
    fixtures = [
        {
            "id": "espn:target-1",
            "kickoff_at": "2026-08-20T19:00:00+00:00",
            "home_team": "Marseille",
            "away_team": "Strasbourg",
            "home_provider_team_id": "1001",
            "away_provider_team_id": "1002",
        },
        {
            "id": "espn:target-2",
            "kickoff_at": "2026-08-27T19:00:00+00:00",
            "home_team": "Marseille",
            "away_team": "Lyon",
            "home_provider_team_id": "1001",
            "away_provider_team_id": "1003",
        },
    ]
    rows, diagnostics = resolve_news_rows(
        [_news(title="Marseille training update")],
        fixtures,
    )

    assert diagnostics["linked_news_rows"] == 1
    assert diagnostics["fixture_linked_news_rows"] == 0
    assert diagnostics["team_linked_news_rows"] == 1
    assert rows[0]["entity_type"] == "news"
    assert rows[0]["payload"]["team_resolution"] == {
        "status": "resolved",
        "method": "exact_team_pre_kickoff_recent_v1",
        "team_provider_id": "1001",
        "canonical_team": "Marseille",
        "provider": "ESPN",
        "confidence": 0.94,
        "model_use": "display_only",
    }
    assert rows[0]["enters_model"] is False


def test_rejects_ambiguous_single_team_name_across_provider_ids():
    fixtures = [
        {
            "id": "espn:one",
            "kickoff_at": "2026-08-20T19:00:00+00:00",
            "home_team": "United",
            "away_team": "Alpha",
            "home_provider_team_id": "2001",
            "away_provider_team_id": "2002",
        },
        {
            "id": "espn:two",
            "kickoff_at": "2026-08-21T19:00:00+00:00",
            "home_team": "United",
            "away_team": "Beta",
            "home_provider_team_id": "3001",
            "away_provider_team_id": "3002",
        },
    ]
    rows, diagnostics = resolve_news_rows([_news(title="United training update")], fixtures)

    assert diagnostics["linked_news_rows"] == 0
    assert diagnostics["unlinked_reasons"] == {"ambiguous_team_entity": 1}
    assert rows[0]["entity_type"] == "news"


def test_single_team_resolution_never_accepts_post_kickoff_news():
    fixture = {
        "id": "espn:late",
        "kickoff_at": "2026-08-20T19:00:00+00:00",
        "home_team": "Marseille",
        "away_team": "Strasbourg",
        "home_provider_team_id": "1001",
        "away_provider_team_id": "1002",
    }
    rows, diagnostics = resolve_news_rows(
        [_news(
            title="Marseille training update",
            published="2026-08-20T20:01:00+00:00",
            observed="2026-08-20T20:02:00+00:00",
        )],
        [fixture],
    )

    assert diagnostics["linked_news_rows"] == 0
    assert diagnostics["unlinked_reasons"] == {"no_exact_team_pre_kickoff": 1}
    assert rows[0]["entity_type"] == "news"
