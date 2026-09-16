"""Publish explicit pre-kickoff stage evaluation rows to the Sites D1 ledger.

This stream is separate from the ordinary prediction evaluation table.  Each
row carries the freeze stage, stage lock time, result observation time and the
model file digest, so a later result cannot be silently attributed to another
freeze.  Publication is opt-in and uses the same bounded HTTPS/retry policy as
the intelligence publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from league_platform.publish_intelligence import (
    PublicationReceiptStore,
    PublicationMaturityContext,
    build_publication_maturity_context,
    publication_receipt_identity,
    require_publication_maturity,
    resolve_publication_epoch,
    publish_payload,
)
from league_platform.publish_forecast import publication_model_version
from league_platform.prospective_evaluation import (
    evaluate_archive,
    require_durable_openfootball_raw_archive,
    resolve_openfootball_raw_archive_dir,
)
from league_platform.prospective_archive import validate_lock_integrity


MAX_ROWS_PER_REQUEST = 200
MAX_PAYLOAD_BYTES = 512 * 1024
TARGETS = ("three_way", "total_goals", "scoreline", "half_full")


def stage_evaluation_publication_status() -> dict[str, str]:
    """Describe the deliberately closed v260 stage-evaluation write lane."""

    return {
        "status": "blocked",
        "reason": "stage_evaluation_publication_unavailable",
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


def _provider_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    provider = str(row.get("provider") or "ESPN").strip()
    if not provider:
        raise ValueError("provider is missing")
    raw = str(row.get("provider_fixture_id") or row.get("fixture_id") or "").strip()
    if not raw:
        raise ValueError("provider fixture identity is missing")
    if ":" in raw and raw.split(":", 1)[0].lower() == provider.lower():
        raw = raw.split(":", 1)[1]
    return provider, raw


def _metric_row(target: str, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    def number(key: str) -> float:
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            raise ValueError(f"{target}.{key} is missing")
        parsed = float(value)
        if parsed < 0 or parsed != parsed or parsed in (float("inf"), float("-inf")):
            raise ValueError(f"{target}.{key} is invalid")
        return parsed

    if target == "three_way":
        metrics: dict[str, Any] = {"brier": number("brier"), "logLoss": number("log_loss")}
        market = None
        if values.get("market_brier") is not None and values.get("market_log_loss") is not None:
            market = {"brier": number("market_brier"), "logLoss": number("market_log_loss")}
        return metrics, market
    if target == "scoreline":
        return {"logLoss": number("log_loss"), "top5Hit": bool(values.get("top5_hit", False))}, None
    if target in {"total_goals", "half_full"}:
        return {"logLoss": number("log_loss")}, None
    raise ValueError(f"unsupported target {target}")


def build_stage_evaluation_rows(
    scored_rows: Iterable[Mapping[str, Any]],
    *,
    model_version_sha256: str | None = None,
    evaluated_at: str | None = None,
) -> list[dict[str, Any]]:
    """Convert evaluator rows into strict D1 payload rows.

    Missing lock digests, stage timestamps or result fingerprints are hard
    skips.  It is safer to publish fewer auditable rows than to infer a model
    identity from a mutable filename or a post-result timestamp.
    """

    generated_at = _timestamp(evaluated_at or datetime.now(timezone.utc).isoformat(), field="evaluated_at")
    rows: list[dict[str, Any]] = []
    for scored in scored_rows:
        if not isinstance(scored, Mapping):
            continue
        model_version = scored.get("model_version")
        stage = scored.get("freeze_stage")
        if not isinstance(model_version, str) or not model_version.strip() or not isinstance(stage, str) or not stage.strip():
            continue
        digest = scored.get("model_version_sha256") or model_version_sha256
        if digest is None:
            continue
        digest = _digest(digest, field="model_version_sha256")
        stage_locked_at = scored.get("stage_locked_at") or scored.get("prediction_observed_at")
        result_observed_at = scored.get("result_observed_at")
        result_fingerprint = scored.get("result_fingerprint")
        if not isinstance(stage_locked_at, str) or not isinstance(result_observed_at, str):
            continue
        stage_locked_at = _timestamp(stage_locked_at, field="stage_locked_at")
        result_observed_at = _timestamp(result_observed_at, field="result_observed_at")
        result_fingerprint = _digest(result_fingerprint, field="result_fingerprint")
        provider, provider_fixture_id = _provider_identity(scored)
        targets = scored.get("targets")
        if not isinstance(targets, Mapping):
            continue
        for target in TARGETS:
            values = targets.get(target)
            if not isinstance(values, Mapping):
                continue
            metrics, market_metrics = _metric_row(target, values)
            rows.append({
                "provider": provider,
                "providerFixtureId": provider_fixture_id,
                "modelVersion": publication_model_version(model_version, digest),
                "modelVersionSha256": digest,
                "stage": stage,
                "stageLockedAt": stage_locked_at,
                "target": target,
                "metrics": metrics,
                "marketMetrics": market_metrics,
                "evaluatedAt": generated_at,
                "resultObservedAt": result_observed_at,
                "resultFingerprint": result_fingerprint,
            })
    return rows


def _batches(rows: list[dict[str, Any]], size: int = MAX_ROWS_PER_REQUEST) -> list[list[dict[str, Any]]]:
    if not 1 <= size <= MAX_ROWS_PER_REQUEST:
        raise ValueError("batch size is out of bounds")
    return [rows[index:index + size] for index in range(0, len(rows), size)]


def _validate_stage_evaluation_response(
    response: Mapping[str, Any],
    *,
    row_count: int,
) -> None:
    created = response.get("created")
    skipped = response.get("skipped")
    received = response.get("received")
    if (
        response.get("status") != "ok"
        or not isinstance(created, int)
        or isinstance(created, bool)
        or created < 0
        or not isinstance(skipped, int)
        or isinstance(skipped, bool)
        or skipped < 0
        or not isinstance(received, int)
        or isinstance(received, bool)
        or received != row_count
        or created + skipped != row_count
    ):
        raise RuntimeError(
            "stage evaluation endpoint returned an unexpected response"
        )


def _completed_stage_receipt_is_valid(
    receipt: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    row_count: int,
) -> bool:
    response = receipt.get("response")
    if not isinstance(response, Mapping) or receipt.get("rowCount") != row_count:
        return False
    if receipt.get("payloadSha256") != _sha256(payload):
        return False
    try:
        _validate_stage_evaluation_response(response, row_count=row_count)
    except RuntimeError:
        return False
    return True


def publish_stage_evaluations(
    prediction_archive: Path,
    snapshot_archive: Path,
    *,
    openfootball_raw_archive_dir: Path,
    endpoint: str,
    token: str,
    lock: dict[str, Any] | None = None,
    model_version_sha256: str | None = None,
    receipts_path: Path | None = None,
    publication_epoch: str | None = None,
    maturity_context: PublicationMaturityContext | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    openfootball_raw_archive_dir = require_durable_openfootball_raw_archive(
        openfootball_raw_archive_dir
    )
    epoch = resolve_publication_epoch(publication_epoch, dry_run=dry_run)
    publication_gate = stage_evaluation_publication_status()
    if not dry_run and publication_gate.get("status") != "ok":
        raise RuntimeError(str(publication_gate["reason"]))
    maturity = require_publication_maturity(maturity_context, dry_run=dry_run)
    if lock is None:
        raise ValueError("prospective model lock is required for stage evaluation publication")
    validate_lock_integrity(lock)
    evidence = evaluate_archive(
        prediction_archive,
        snapshot_archive,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        lock=lock,
        include_scored_rows=True,
    )
    scored_rows = evidence.get("scored_rows", [])
    rows = build_stage_evaluation_rows(
        scored_rows if isinstance(scored_rows, list) else [],
        model_version_sha256=model_version_sha256 or (lock or {}).get("model_version_sha256"),
    )
    batches = _batches(rows)
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "scoredRows": len(scored_rows) if isinstance(scored_rows, list) else 0,
        "stageRows": len(rows),
        "batches": len(batches),
        "published": 0,
        "requests": 0,
        "receiptsSkipped": 0,
        "evaluationStatus": evidence.get("status"),
        "publicationGate": publication_gate,
    }
    if not batches:
        return result
    receipt_store = None if dry_run else PublicationReceiptStore(receipts_path or prediction_archive.with_name(f"{prediction_archive.name}.stage-evaluation-receipts.jsonl"))
    completed = receipt_store.completed_records() if receipt_store else {}
    for index, batch in enumerate(batches):
        payload = {"evaluations": batch}
        if len(_canonical(payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"stage evaluation batch {index} exceeds 512 KiB")
        digest = _sha256(batch)
        payload_digest = _sha256(payload)
        receipt_identity = publication_receipt_identity(
            stream="stage_evaluations",
            destinations={
                "stage_evaluations": endpoint
                or "https://invalid.example/api/stage-evaluations"
            },
            publication_epoch_value=epoch,
            publication_gate_sha256=str(maturity["receiptSha256"]),
            outbound_payload=payload,
        )
        receipt_key = (digest, str(receipt_identity["receiptIdentitySha256"]))
        completed_receipt = completed.get(receipt_key)
        if completed_receipt is not None:
            if not _completed_stage_receipt_is_valid(
                completed_receipt,
                payload=payload,
                row_count=len(batch),
            ):
                raise ValueError("stage_evaluation_completed_receipt_invalid")
            result["receiptsSkipped"] += 1
            continue
        if dry_run:
            result["published"] += len(batch)
            continue
        result["requests"] += 1
        response = publish_payload(
            payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            sleep=sleep or __import__("time").sleep,
            error_label="stage evaluation",
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("stage evaluation endpoint returned an unexpected response")
        _validate_stage_evaluation_response(response, row_count=len(batch))
        response_summary = {
            key: response.get(key)
            for key in ("created", "skipped", "received", "status")
            if key in response
        }
        receipt = {
            **receipt_identity,
            "batchIndex": index,
            "rowCount": len(batch),
            "batchDigest": digest,
            "payloadSha256": payload_digest,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
            "response": response_summary,
            "responseSha256": hashlib.sha256(
                _canonical(response_summary)
            ).hexdigest(),
        }
        if receipt_store is None:
            raise AssertionError("receipt store is unavailable outside dry-run")
        receipt_store.append_completed(receipt)
        completed[receipt_key] = receipt
        result["published"] += len(batch)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime_root_default = (
        Path(os.environ["MATCHLINE_RUNTIME_DIR"])
        if os.environ.get("MATCHLINE_RUNTIME_DIR")
        else None
    )
    parser.add_argument(
        "--prediction-archive", type=Path, default=Path("data/live/prospective_predictions.jsonl")
    )
    parser.add_argument("--snapshot-archive", type=Path, default=Path("data/live/archive"))
    parser.add_argument("--openfootball-raw-archive-dir", type=Path)
    parser.add_argument(
        "--lock", type=Path, default=Path("docs/evidence/prospective-model-lock-current.json")
    )
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publication-epoch",
        default=os.environ.get("MATCHLINE_PUBLICATION_EPOCH"),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--maturity-receipt", type=Path)
    parser.add_argument("--maturity-runtime-root", type=Path, default=runtime_root_default)
    parser.add_argument("--maturity-platform-verification", type=Path)
    parser.add_argument("--maturity-strict-report", type=Path)
    parser.add_argument("--maturity-evaluation", type=Path)
    parser.add_argument("--maturity-offline-snapshot", type=Path)
    parser.add_argument("--maturity-sites-build-evidence", type=Path)
    parser.add_argument("--maturity-test-evidence", type=Path)
    parser.add_argument("--maturity-publication-audit", type=Path)
    parser.add_argument("--maturity-live-snapshot", type=Path)
    parser.add_argument("--maturity-cycle", type=Path)
    parser.add_argument("--maturity-publication-diagnostic", type=Path)
    parser.add_argument("--maturity-repository-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    openfootball_raw_archive_dir = resolve_openfootball_raw_archive_dir(
        args.openfootball_raw_archive_dir,
        runtime_default=args.snapshot_archive.parent / "openfootball-raw",
    )
    endpoint = os.environ.get("MATCHLINE_STAGE_EVALUATIONS_URL", "")
    token = os.environ.get("MATCHLINE_INGEST_TOKEN", "")
    maturity_context = None
    maturity_values = (
        args.maturity_receipt,
        args.maturity_runtime_root,
        args.maturity_platform_verification,
        args.maturity_strict_report,
        args.maturity_evaluation,
        args.maturity_offline_snapshot,
        args.maturity_sites_build_evidence,
        args.maturity_test_evidence,
        args.maturity_publication_audit,
        args.maturity_live_snapshot,
        args.maturity_cycle,
        args.maturity_publication_diagnostic,
    )
    if all(value is not None for value in maturity_values):
        maturity_context = build_publication_maturity_context(
            receipt_path=args.maturity_receipt,
            platform_verification_path=args.maturity_platform_verification,
            strict_report_path=args.maturity_strict_report,
            prospective_lock_path=args.lock,
            cycle_path=args.maturity_cycle,
            evaluation_path=args.maturity_evaluation,
            live_snapshot_path=args.maturity_live_snapshot,
            offline_snapshot_path=args.maturity_offline_snapshot,
            sites_build_evidence_path=args.maturity_sites_build_evidence,
            test_evidence_path=args.maturity_test_evidence,
            publication_audit_path=args.maturity_publication_audit,
            prediction_archive_path=args.prediction_archive,
            publication_diagnostic_path=args.maturity_publication_diagnostic,
            runtime_root=args.maturity_runtime_root,
            repository_root=args.maturity_repository_root,
        )
    elif not args.dry_run:
        parser.error("complete maturity artifact paths are required")
    result = publish_stage_evaluations(
        args.prediction_archive,
        args.snapshot_archive,
        openfootball_raw_archive_dir=openfootball_raw_archive_dir,
        endpoint=endpoint,
        token=token,
        lock=lock,
        receipts_path=args.receipts,
        publication_epoch=args.publication_epoch,
        maturity_context=maturity_context,
        dry_run=args.dry_run,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_stage_evaluation_rows", "publish_stage_evaluations"]
