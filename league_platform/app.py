"""Small read-only HTTP application for the Matchline platform."""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from league_platform.store import PlatformStore


ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
DEFAULT_DATA_DIR = ROOT.parent / "data" / "MatchHistory"
DEFAULT_LIVE_PATH = ROOT.parent / "data" / "live" / "current.json"


class PlatformHandler(SimpleHTTPRequestHandler):
    """Serve the static UI and versioned JSON endpoints from one origin."""

    store: PlatformStore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/snapshot":
            self._send_json(self.store.snapshot())
            return
        if parsed.path == "/api/v1/competitions":
            self._send_json({"competitions": self.store.competitions()})
            return
        if parsed.path == "/api/v1/matches":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["100"])[0])
                offset = int(params.get("offset", ["0"])[0])
            except ValueError:
                self._send_json({"error": "limit and offset must be integers"}, status=400)
                return
            try:
                payload = self.store.matches(
                    competition_id=_first(params, "competition"),
                    season=_first(params, "season"),
                    status=_first(params, "status"),
                    match_date=_first(params, "date"),
                    query=_first(params, "q"),
                    limit=limit,
                    offset=offset,
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(payload)
            return
        if parsed.path == "/api/v1/health":
            self._send_json(self.store.health())
            return
        if parsed.path == "/api/v1/model-evaluations":
            self._send_json({"evaluations": self.store.model_evaluations()})
            return
        if parsed.path == "/api/v1/predictions":
            self._send_json(self.store.predictions())
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _send_json(self, payload: object, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[matchline] {self.address_string()} {fmt % args}")


def _first(params: dict[str, list[str]], key: str) -> str | None:
    value = params.get(key, [None])[0]
    return value or None


def create_server(
    host: str,
    port: int,
    data_dir: Path | str,
    live_path: Path | str | None = DEFAULT_LIVE_PATH,
) -> ThreadingHTTPServer:
    PlatformHandler.store = PlatformStore(
        Path(data_dir), live_path=Path(live_path) if live_path is not None else None
    )
    return ThreadingHTTPServer((host, port), PlatformHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Matchline multi-league platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--live-data", type=Path, default=DEFAULT_LIVE_PATH)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.data_dir, args.live_data)
    print(f"Matchline is running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
