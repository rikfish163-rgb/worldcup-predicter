"""Immutable pre-match intelligence observations and conflict resolution.

The module is deliberately provider-agnostic.  Fetchers create observations;
the ledger stores them without overwriting prior evidence; the resolver picks
the best usable observation for a cutoff while keeping conflicts visible.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:  # pragma: no cover - the deployed runtime is Linux, but keep imports portable.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SOURCE_TIERS = {
    "official": 4,
    "authorized": 3,
    # Public provider endpoint with reproducible provenance, but not a
    # first-party club or league announcement.
    "reliable_public_provider": 2,
    "reliable_media": 2,
    "social": 1,
}
# These observations can change the pre-match decision materially.  A source
# priority winner is not enough when two payloads disagree: until the conflict
# is resolved, the field must remain unavailable to the final model.
CRITICAL_KINDS = {
    "lineup",
    "official_lineup",
    "fotmob_lineup",
    "injury",
    "suspension",
    "player_availability",
}
MAX_OBSERVATION_BYTES = 512 * 1024


def utc(value: datetime | str) -> datetime:
    """Parse a timezone-aware timestamp and normalize it to UTC."""

    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    entity_type: str
    entity_id: str
    kind: str
    payload: dict
    source_name: str
    source_url: str
    source_tier: str
    observed_at: str
    effective_at: str | None
    confidence: float
    raw_hash: str
    enters_model: bool = False
    model_exclusion_reason: str | None = None
    time_fallback: str | None = None
    conflict_group: str | None = None
    # Optional canonical foreign keys.  Provider observations may arrive
    # before entity registration, so the immutable ledger keeps them null
    # rather than copying a provider id into a D1 integer column.
    fixture_id: int | None = None
    team_id: int | None = None
    player_id: int | None = None

    def __post_init__(self) -> None:
        if self.source_tier not in SOURCE_TIERS:
            raise ValueError(f"unknown source tier: {self.source_tier}")
        if not self.entity_type or not self.entity_id or not self.kind:
            raise ValueError("entity_type, entity_id and kind are required")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.model_exclusion_reason is not None and not isinstance(self.model_exclusion_reason, str):
            raise ValueError("model_exclusion_reason must be a string or null")
        for name, value in (("fixture_id", self.fixture_id), ("team_id", self.team_id), ("player_id", self.player_id)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer or null")
        observed = utc(self.observed_at)
        effective = utc(self.effective_at) if self.effective_at else None
        if effective and effective > observed:
            raise ValueError("effective_at cannot be later than observed_at")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("source_url must be an HTTPS URL")
        raw = self.raw_hash.lower()
        if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
            raise ValueError("raw_hash must be a SHA-256 hex digest")
        if len(_canonical_json(self.payload).encode("utf-8")) > MAX_OBSERVATION_BYTES:
            raise ValueError("observation payload exceeds size limit")

    @property
    def event_time(self) -> datetime:
        return utc(self.effective_at or self.observed_at)

    @property
    def source_rank(self) -> int:
        return SOURCE_TIERS[self.source_tier]

    def as_dict(self) -> dict:
        value = asdict(self)
        # Keep the append-only wire shape compatible with rows written before
        # policy explanations were added.  Reasons are emitted only when a
        # source was explicitly quarantined from model use.
        if value.get("model_exclusion_reason") is None:
            value.pop("model_exclusion_reason", None)
        # Keep the pre-canonical wire shape stable.  Optional foreign keys are
        # emitted only after a trusted registrar has bound the provider row.
        for key in ("fixture_id", "team_id", "player_id"):
            if value.get(key) is None:
                value.pop(key, None)
        return value


class ObservationLedger:
    """Append-only JSONL ledger with hash-based idempotency."""

    # The source ledger is intentionally human-auditable JSONL, but scanning a
    # several-hundred-megabyte history on every five/ten-minute poll is both
    # wasteful and a threat to the polling deadline.  This sidecar is only an
    # idempotency cache: the JSONL remains the source of truth and the index can
    # always be rebuilt from it after a crash or manual removal.
    INDEX_SUFFIX = ".index"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_read_errors: list[dict[str, object]] = []

    @staticmethod
    def _record_key(row: Observation | dict) -> str:
        """Build an exact-record key instead of deduplicating by content hash.

        A single raw response can legitimately produce many observations (for
        example one RSS response can contain several news items).  The raw
        hash is provenance, not an entity-level primary key.  Idempotency
        therefore means the complete immutable record is repeated, while a
        different entity, timestamp, or payload remains appendable.
        """

        value = row.as_dict() if isinstance(row, Observation) else row
        return _canonical_json(value)

    @classmethod
    def _record_digest(cls, row: Observation | dict) -> str:
        """Return a bounded-size idempotency key for one immutable record."""

        return hashlib.sha256(cls._record_key(row).encode("utf-8")).hexdigest()

    @property
    def index_path(self) -> Path:
        """Return the derived digest index beside the append-only ledger."""

        return Path(f"{self.path}{self.INDEX_SUFFIX}")

    @staticmethod
    def _valid_digest(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

    def _rebuild_index(self) -> set[str]:
        """Build the digest cache once, preserving malformed history lines.

        A malformed historical line is deliberately skipped here just as it
        is in ``_existing_keys``.  ``read()`` remains responsible for exposing
        the diagnostic; the index must never make an invalid row look valid.
        """

        keys: set[str] = set()
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        keys.add(self._record_digest(row))
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(
            f".{self.index_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("w", encoding="ascii") as stream:
                for digest in sorted(keys):
                    stream.write(digest + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.index_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return keys

    def _load_index(self) -> set[str]:
        """Load the cache, rebuilding it when absent or malformed."""

        if not self.index_path.exists():
            return self._rebuild_index()
        keys: set[str] = set()
        try:
            with self.index_path.open(encoding="ascii") as stream:
                for line in stream:
                    digest = line.strip()
                    if not self._valid_digest(digest):
                        return self._rebuild_index()
                    keys.add(digest)
        except (OSError, UnicodeDecodeError):
            return self._rebuild_index()
        return keys

    def _existing_keys(self) -> set[str]:
        return self._load_index()

    def _exclusive_lock(self):
        """Serialize read-before-append across independent capture processes.

        ``ObservationLedger.append`` performs a read of the existing idempotency
        keys followed by multiple buffered writes.  Without an inter-process
        lock, two scheduled sources can interleave those writes at arbitrary
        buffer boundaries and produce a line that is not valid JSON.  The lock
        lives beside the ledger so it is stable across process restarts and does
        not mutate the append-only data file itself.
        """

        class _Lock:
            def __init__(self, path: Path):
                self.path = path
                self.handle = None

            def __enter__(self):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.path.open("a+")
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(self, exc_type, exc, traceback):
                if self.handle is None:
                    return False
                try:
                    if fcntl is not None:
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    self.handle.close()
                return False

        return _Lock(Path(f"{self.path}.lock"))

    def append(self, observations: list[Observation]) -> dict[str, int]:
        with self._exclusive_lock():
            existing = self._existing_keys()
            accepted = 0
            skipped = 0
            with self.path.open("a", encoding="utf-8") as handle, self.index_path.open(
                "a", encoding="ascii"
            ) as index:
                for observation in observations:
                    key = self._record_digest(observation)
                    if key in existing:
                        skipped += 1
                        continue
                    handle.write(_canonical_json(observation.as_dict()) + "\n")
                    index.write(key + "\n")
                    existing.add(key)
                    accepted += 1
                # Flush while the inter-process lock is still held.  This
                # prevents a second writer from observing an incomplete tail.
                handle.flush()
                os.fsync(handle.fileno())
                index.flush()
                os.fsync(index.fileno())
            return {"accepted": accepted, "skipped_duplicate": skipped}

    def read(self, *, strict: bool = False) -> list[Observation]:
        """Read valid observations and retain any malformed-line diagnostics.

        The ledger is append-only, so a historical crash or pre-lock writer
        must not be repaired by rewriting or deleting the original file.  The
        default read path therefore returns valid records while exposing
        ``last_read_errors`` for the audit surface.  Callers that require a
        fail-closed integrity check can pass ``strict=True``.
        """

        self.last_read_errors = []
        if not self.path.exists():
            return []
        rows: list[Observation] = []
        with self.path.open(encoding="utf-8") as stream:
            lines = enumerate(stream, start=1)
            for line_number, line in lines:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("JSON root must be an object")
                    rows.append(Observation(**value))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    diagnostic = {
                        "line": line_number,
                        "bytes": len(line.encode("utf-8")),
                        "error": str(exc),
                    }
                    self.last_read_errors.append(diagnostic)
                    if strict:
                        raise ValueError(
                            f"invalid observation ledger line {line_number}: {exc}"
                        ) from exc
        return rows


def recover_observation_fragments(line: str) -> list[dict[str, object]]:
    """Find complete observation objects embedded in a malformed JSONL line.

    A pre-lock writer can leave a truncated observation followed by a complete
    observation on the same physical line.  ``json.loads`` correctly rejects
    that line, and the normal ledger reader must continue to do so.  This
    helper is deliberately audit-only: it scans candidate object boundaries,
    validates each candidate against the immutable ``Observation`` contract,
    and returns the complete fragments with their offsets.  Callers must keep
    the original line quarantined and must not feed recovered fragments into
    model features or the publish stream.
    """

    if not isinstance(line, str) or not line:
        return []
    decoder = json.JSONDecoder()
    recovered: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    for offset, character in enumerate(line):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(line, offset)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or end <= offset:
            continue
        try:
            Observation(**value)
        except (KeyError, TypeError, ValueError):
            continue
        digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
        key = (offset, end, digest)
        if key in seen:
            continue
        seen.add(key)
        recovered.append(
            {
                "offset": offset,
                "end": end,
                "record_digest": digest,
                "observation": value,
            }
        )
    return recovered


def resolve_observations(
    observations: list[Observation],
    *,
    cutoff_at: datetime | str,
) -> dict[tuple[str, str, str], dict]:
    """Resolve observations visible at a cutoff without hiding conflicts."""

    cutoff = utc(cutoff_at)
    groups: dict[tuple[str, str, str], list[Observation]] = {}
    for observation in observations:
        if observation.event_time > cutoff or utc(observation.observed_at) > cutoff:
            continue
        key = (observation.entity_type, observation.entity_id, observation.kind)
        groups.setdefault(key, []).append(observation)

    resolved: dict[tuple[str, str, str], dict] = {}
    for key, candidates in groups.items():
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.source_rank,
                item.event_time,
                utc(item.observed_at),
                sum(_canonical_json(peer.payload) == _canonical_json(item.payload) for peer in candidates),
                item.confidence,
            ),
            reverse=True,
        )
        winner = ordered[0]
        payload_counts: dict[str, int] = {}
        for item in ordered:
            canonical = _canonical_json(item.payload)
            payload_counts[canonical] = payload_counts.get(canonical, 0) + 1
        distinct_payloads = set(payload_counts)
        conflict = len(distinct_payloads) > 1
        critical_conflict = conflict and key[2] in CRITICAL_KINDS
        resolved[key] = {
            "observation": winner.as_dict(),
            "candidates": [item.as_dict() for item in ordered],
            "conflict": conflict,
            # Source priority identifies the preferred record for display,
            # but it cannot turn two incompatible facts into one fact.  Keep
            # every unresolved conflict out of the final model; callers may
            # still use the winner for an explicitly labelled audit view.
            "unresolved_conflict": conflict,
            "critical_conflict": critical_conflict,
            "confirmation_count": payload_counts[_canonical_json(winner.payload)],
            "usable_for_model": bool(winner.enters_model and not conflict),
        }
    return resolved


def coverage_level(
    *,
    expected_features: set[str],
    available_features: set[str],
    critical_conflict: bool = False,
) -> dict[str, object]:
    """Return a conservative coverage grade used by UI and model gates."""

    expected = expected_features or set()
    ratio = len(expected & available_features) / len(expected) if expected else 0.0
    missing = expected - available_features
    if critical_conflict:
        level = "low"
    # ``high`` is a completeness claim in the professional workbench, not a
    # rounded score.  A report missing even one declared feature can remain
    # useful at medium coverage, but must not be marketed as complete.
    elif ratio >= 0.8 and not missing:
        level = "high"
    elif ratio >= 0.5:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "ratio": round(ratio, 4),
        "missing": sorted(missing),
        "critical_conflict": critical_conflict,
    }


def observation_from_payload(
    *,
    entity_type: str,
    entity_id: str,
    kind: str,
    payload: dict,
    source_name: str,
    source_url: str,
    source_tier: str,
    observed_at: datetime | str,
    published_at: datetime | str | None = None,
    confidence: float = 0.5,
    enters_model: bool = False,
    model_exclusion_reason: str | None = None,
    raw_hash: str | None = None,
    fixture_id: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
) -> Observation:
    """Build an observation with publication-time-first semantics."""

    observed = utc(observed_at)
    if published_at is not None:
        effective = utc(published_at)
        fallback = None
    else:
        effective = observed
        fallback = "missing_published_at"
    return Observation(
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        payload=payload,
        source_name=source_name,
        source_url=source_url,
        source_tier=source_tier,
        observed_at=observed.isoformat(),
        effective_at=effective.isoformat(),
        confidence=confidence,
        raw_hash=raw_hash or payload_hash(payload),
        enters_model=enters_model,
        model_exclusion_reason=model_exclusion_reason,
        time_fallback=fallback,
        fixture_id=fixture_id,
        team_id=team_id,
        player_id=player_id,
    )


__all__ = [
    "CRITICAL_KINDS",
    "Observation",
    "ObservationLedger",
    "SOURCE_TIERS",
    "coverage_level",
    "observation_from_payload",
    "payload_hash",
    "recover_observation_fragments",
    "resolve_observations",
    "utc",
]
