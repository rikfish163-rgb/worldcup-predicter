"""Small read-only HTTP application for the Matchline platform."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from league_platform.runtime_paths import resolve_future_openfootball_raw_archive_dir
from league_platform.store import PlatformStore


ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
DEFAULT_DATA_DIR = ROOT.parent / "data" / "MatchHistory"
DEFAULT_LIVE_PATH = ROOT.parent / "data" / "live" / "current.json"
DEFAULT_EVIDENCE_DIR = ROOT.parent / "docs" / "evidence"


class PlatformHandler(SimpleHTTPRequestHandler):
    """Serve the static UI and versioned JSON endpoints from one origin."""

    store: PlatformStore

    def __init__(self, *args: Any, **kwargs: Any) -> None:
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
            self._send_json(self.store.public_predictions())
            return
        if parsed.path == "/api/v1/research-predictions":
            self._send_json(_research_predictions_payload(self.store.predictions()))
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
        accepts_gzip = _accepts_gzip(self.headers.get("Accept-Encoding", ""))
        use_gzip = accepts_gzip and len(body) >= 1024
        encoded = gzip.compress(body, compresslevel=6, mtime=0) if use_gzip else body
        etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
        if _etag_matches(self.headers.get("If-None-Match", ""), etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[matchline] {self.address_string()} {fmt % args}")


def _first(params: dict[str, list[str]], key: str) -> str | None:
    value = params.get(key, [None])[0]
    return value or None


def _accepts_gzip(value: str) -> bool:
    wildcard_quality: float | None = None
    for item in value.lower().split(","):
        token, *parameters = item.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, raw_value = parameter.strip().partition("=")
            if name == "q" and separator:
                try:
                    quality = float(raw_value)
                except ValueError:
                    quality = 0.0
        if token == "gzip":
            return quality > 0.0
        if token == "*":
            wildcard_quality = quality
    return wildcard_quality is not None and wildcard_quality > 0.0


def _etag_matches(value: str, etag: str) -> bool:
    return any(candidate.strip() in {etag, "*"} for candidate in value.split(","))


def _research_predictions_payload(value: object) -> dict[str, object]:
    """Keep verified future rows in an explicitly non-production API lane."""

    model_input = value.get("model_input") if isinstance(value, dict) else None
    is_verified = (
        isinstance(value, dict)
        and value.get("status") == "research_only"
        and isinstance(model_input, dict)
        and model_input.get("status") == "verified"
        and model_input.get("trust_anchor")
        == "verified_openfootball_raw_archive_replay"
        and model_input.get("snapshot_model_facts_accepted") is False
    )
    if is_verified:
        research: dict[str, object] = {**value, "production_allowed": False}
    else:
        raw_value = value if isinstance(value, dict) else {}
        reason = raw_value.get("reason")
        message = raw_value.get("message")
        research = {
            "status": "unavailable",
            "as_of": raw_value.get("as_of"),
            "predictions": [],
            "blocked": [],
            "reason": (
                reason
                if isinstance(reason, str)
                else "verified_openfootball_future_unavailable"
            ),
            "model_input": {
                "status": "unavailable",
                "trust_anchor": "verified_openfootball_raw_archive_replay",
                "snapshot_model_facts_accepted": False,
            },
            "model_health": {},
            "message": (
                message
                if isinstance(message, str)
                else "verified OpenFootball raw archive replay is unavailable"
            ),
        }
        current_data_status = raw_value.get("current_data_status")
        if current_data_status is not None:
            research["current_data_status"] = current_data_status
    return {
        "status": research["status"],
        "as_of": research.get("as_of"),
        "predictions": [],
        "blocked": [],
        "research_predictions": research,
        "production_allowed": False,
        "message": research.get("message"),
    }


def create_server(
    host: str,
    port: int,
    data_dir: Path | str,
    live_path: Path | str | None = DEFAULT_LIVE_PATH,
    evidence_dir: Path | str | None = DEFAULT_EVIDENCE_DIR,
    runtime_evidence_dir: Path | str | None = None,
    strict_report_path: Path | str | None = None,
    openfootball_raw_archive_dir: Path | str | None = None,
) -> ThreadingHTTPServer:
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ValueError("remote binding requires a secured reverse proxy to the loopback server")
    # Freeze one aware cutoff for the store's snapshot, current attachment and
    # verified raw replay.  Separate endpoint calls cannot drift across clocks.
    observed_before = datetime.now(timezone.utc)
    PlatformHandler.store = PlatformStore(
        Path(data_dir),
        now=observed_before,
        live_path=Path(live_path) if live_path is not None else None,
        evidence_dir=Path(evidence_dir) if evidence_dir is not None else None,
        runtime_evidence_dir=(
            Path(runtime_evidence_dir) if runtime_evidence_dir is not None else None
        ),
        strict_report_path=(
            Path(strict_report_path) if strict_report_path is not None else None
        ),
        openfootball_raw_archive_dir=resolve_future_openfootball_raw_archive_dir(
            openfootball_raw_archive_dir
        ),
    )
    return ThreadingHTTPServer((host, port), PlatformHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Matchline multi-league platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--live-data", type=Path, default=DEFAULT_LIVE_PATH)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--runtime-evidence-dir",
        type=Path,
        default=None,
        help=(
            "optional live cycle/evaluation pointer root; model lock and "
            "selection evidence remain under --evidence-dir"
        ),
    )
    parser.add_argument(
        "--strict-report",
        type=Path,
        default=None,
        help="optional strict backtest artifact; omitted uses the store default",
    )
    parser.add_argument(
        "--openfootball-raw-archive-dir",
        type=Path,
        default=None,
        help=(
            "durable OpenFootball raw archive; when omitted, use only the "
            "operator-managed MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR"
        ),
    )
    args = parser.parse_args()
    raw_archive_dir = resolve_future_openfootball_raw_archive_dir(
        args.openfootball_raw_archive_dir
    )
    server = create_server(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        live_path=args.live_data,
        evidence_dir=args.evidence_dir,
        runtime_evidence_dir=args.runtime_evidence_dir,
        strict_report_path=args.strict_report,
        openfootball_raw_archive_dir=raw_archive_dir,
    )
    print(f"Matchline is running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
