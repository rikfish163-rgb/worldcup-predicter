"""Restore the pinned MatchHistory inputs into the ignored data directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path


MANIFEST = Path(__file__).with_name("data_manifest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore(destination: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        target = destination / item["name"]
        if target.exists() and _sha256(target) == item["sha256"]:
            print(f"verified {target}")
            continue
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
            temporary = Path(stream.name)
            with urllib.request.urlopen(item["url"], timeout=30) as response:  # noqa: S310
                stream.write(response.read())
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
