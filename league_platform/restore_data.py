"""Restore the pinned MatchHistory inputs into the ignored data directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


MANIFEST = Path(__file__).with_name("data_manifest.json")
ALLOWED_HOSTS = {"www.football-data.co.uk", "raw.githubusercontent.com"}
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"unapproved manifest URL: {url}")


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore(destination: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    for item in manifest["files"]:
        name = item["name"]
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError(f"unsafe manifest filename: {name}")
        _validate_download_url(item["url"])
        target = destination / name
        if target.exists() and _sha256(target) == item["sha256"]:
            print(f"verified {target}")
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
                temporary = Path(stream.name)
                opener = urllib.request.build_opener(_AllowlistedRedirectHandler())
                with opener.open(item["url"], timeout=30) as response:
                    downloaded = 0
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError(f"download too large for {name}")
                        stream.write(chunk)
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        actual = _sha256(temporary)
        if actual != item["sha256"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"hash mismatch for {item['name']}: {actual}")
        temporary.replace(target)
        print(f"restored {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("data/MatchHistory"))
    args = parser.parse_args()
    restore(args.destination)


if __name__ == "__main__":
    main()
