from __future__ import annotations

from datetime import datetime, timezone

from league_platform.live_sources.espn import fetch_espn_fixtures


def test_espn_current_window_includes_recent_results_and_future_fixtures():
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"events": []}'

    def opener(request, timeout):
        assert timeout == 30
        urls.append(request.full_url)
        return Response()

    result = fetch_espn_fixtures(
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        horizon_days=45,
        lookback_days=45,
        opener=opener,
    )

    assert result["lookback_days"] == 45
    assert "dates=20260627-20260925" in urls[0]
    assert len(urls) == 6
