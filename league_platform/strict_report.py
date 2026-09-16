"""Reproducible evidence report for the causal walk-forward backtest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, cast

from league_platform.domain import Match
from league_platform.gates import release_gate
from league_platform.metrics import (
    assert_no_future_leakage,
    assert_probability_contract,
    bootstrap_mean_ci,
    evaluate_three_way,
)
from league_platform.model_selection_audit import build_model_selection_audit
from league_platform.live_sources.openfootball_live import (
    OPENFOOTBALL_HISTORY_SOURCE_IDS,
)
from league_platform.source_rights import POLICY_VERSION
from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource
from league_platform.sources.openfootball_verified import (
    VerifiedOpenFootballHistorySource,
)
from league_platform.strict_backtest import (
    evaluate_freeze_stage_walk_forward,
    evaluate_strict_walk_forward,
)


EUROPEAN_LEAGUES = (
    "premier-league",
    "championship",
    "la-liga",
    "bundesliga",
    "serie-a",
    "ligue-1",
)

FORMAL_REPORT_SCHEMA = "matchline.strict_report.v260"
LEGACY_REPORT_SCHEMA = "matchline.strict_report.legacy_research_only.v1"

_CACHE_SCHEMA_VERSION = 2
_LEGACY_CODE_INPUTS = (
    "league_platform/strict_report.py",
    "league_platform/strict_backtest.py",
    "league_platform/metrics.py",
    "league_platform/gates.py",
    "league_platform/model_selection_audit.py",
    "league_platform/sources/match_history.py",
    "league_platform/sources/openfootball.py",
    "league_platform/domain.py",
    "league_platform/identity.py",
    "league_platform/historical_xg.py",
    "league_platform/research_historical_xg.py",
)
_FORMAL_CODE_INPUTS = (
    "league_platform/strict_report.py",
    "league_platform/strict_backtest.py",
    "league_platform/metrics.py",
    "league_platform/gates.py",
    "league_platform/model_selection_audit.py",
    "league_platform/sources/openfootball_verified.py",
    "league_platform/openfootball_raw_archive.py",
    "league_platform/live_sources/openfootball_live.py",
    "league_platform/source_rights.py",
    "league_platform/domain.py",
    "league_platform/identity.py",
    "league_platform/targets.py",
)


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _file_input(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path.resolve()), "exists": False}
    return {
        "path": str(path.resolve()),
        "exists": True,
        "size": stat.st_size,
        "sha256": _sha256_file(path),
    }


def build_legacy_input_fingerprint(
    data_dir: Path,
    *,
    kickoff_enrichment_path: Path | None = None,
    candidate_evidence_dir: Path = Path("docs/evidence"),
    prospective_lock_path: Path | None = Path("docs/evidence/prospective-model-lock-current.json"),
) -> dict[str, Any]:
    """Return a content fingerprint for every input that affects the report.

    The strict report is expensive by design.  This inventory is deliberately
    limited to files read by the report and its model-selection audit, so a
    newly captured live snapshot does not invalidate historical backtest work.
    Source code is included to prevent a code change from reusing an old report.
    """

    repository_root = Path(__file__).resolve().parent.parent
    data_files = sorted(
        [*data_dir.glob("*.csv"), *data_dir.glob("*.txt")],
        key=lambda path: path.name,
    )
    candidate_files = sorted(
        candidate_evidence_dir.glob("model-candidate-*.json"),
        key=lambda path: path.name,
    )
    inputs: dict[str, Any] = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "data_files": [_file_input(path) for path in data_files],
        "kickoff_enrichment": (
            _file_input(kickoff_enrichment_path) if kickoff_enrichment_path is not None else None
        ),
        "candidate_files": [_file_input(path) for path in candidate_files],
        "prospective_lock": (
            _file_input(prospective_lock_path) if prospective_lock_path is not None else None
        ),
        "code_files": [
            _file_input(repository_root / relative_path) for relative_path in _LEGACY_CODE_INPUTS
        ],
    }
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "report_lane": "legacy_research_only",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "inputs": inputs,
    }


def build_input_fingerprint(
    *,
    openfootball_raw_archive: Path | str,
    observed_before: datetime,
    source_ids: Iterable[str] = OPENFOOTBALL_HISTORY_SOURCE_IDS,
    candidate_evidence_dir: Path = Path("docs/evidence"),
    prospective_lock_path: Path | None = Path("docs/evidence/prospective-model-lock-current.json"),
) -> dict[str, Any]:
    """Fingerprint only the verified raw admission and formal report inputs."""

    source = VerifiedOpenFootballHistorySource(
        openfootball_raw_archive,
        observed_before=observed_before,
        source_ids=source_ids,
    )
    repository_root = Path(__file__).resolve().parent.parent
    candidate_files = sorted(
        candidate_evidence_dir.glob("model-candidate-*.json"),
        key=lambda path: path.name,
    )
    inputs: dict[str, Any] = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "report_lane": "formal_v260",
        "policy_version": POLICY_VERSION,
        "training_admission": source.admission_manifest(),
        "candidate_files": [_file_input(path) for path in candidate_files],
        "prospective_lock": (
            _file_input(prospective_lock_path) if prospective_lock_path is not None else None
        ),
        "code_files": [
            _file_input(repository_root / relative_path) for relative_path in _FORMAL_CODE_INPUTS
        ],
    }
    encoded = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "report_lane": "formal_v260",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "inputs": inputs,
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_report_cache(
    cache_path: Path,
    output_path: Path,
    fingerprint: dict[str, Any],
) -> None:
    """Persist the output identity only after a complete report write."""

    output_sha256 = _sha256_file(output_path)
    if output_sha256 is None:
        raise OSError(f"strict report output is not readable: {output_path}")
    _write_json_atomic(
        cache_path,
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "report_lane": fingerprint.get("report_lane"),
            "input_fingerprint": fingerprint.get("sha256"),
            "output_path": str(output_path.resolve()),
            "output_sha256": output_sha256,
            "written_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _cached_report_matches_fingerprint(
    report: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
) -> bool:
    lane = fingerprint.get("report_lane")
    inputs = fingerprint.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    if lane == "formal_v260":
        admission = inputs.get("training_admission")
        contract = report.get("training_source_contract")
        leagues = report.get("leagues")
        combined = report.get("combined")
        selection_audit = report.get("model_selection_audit")
        overall = report.get("overall")
        if not isinstance(admission, Mapping):
            return False
        if not isinstance(contract, Mapping):
            return False
        if not isinstance(leagues, Mapping):
            return False
        if not isinstance(combined, Mapping):
            return False
        if not isinstance(selection_audit, Mapping):
            return False
        if not isinstance(overall, Mapping):
            return False
        if not bool(
            report.get("schema_version") == FORMAL_REPORT_SCHEMA
            and report.get("report_lane") == "formal_v260"
            and report.get("training_admission") == admission
            and contract.get("provider") == "OpenFootball"
            and contract.get("raw_archive_required") is True
            and contract.get("training_admission_sha256") == admission.get("admission_sha256")
            and contract.get("policy_version") == inputs.get("policy_version")
            and contract.get("observed_before") == admission.get("observed_before")
            and contract.get("source_ids") == admission.get("source_ids")
            and contract.get("prohibited_formal_inputs")
            == [
                "football_data_csv",
                "legacy_csl_files",
                "snapshot_self_report",
            ]
        ):
            return False
        if set(leagues) != {*EUROPEAN_LEAGUES, "csl"}:
            return False
        required_gates = {
            "three_way",
            "totals",
            "half_full",
            "handicap",
            "total_over_under",
        }
        for league_value in leagues.values():
            if not isinstance(league_value, Mapping):
                return False
            gates = league_value.get("gates")
            if not isinstance(gates, Mapping) or set(gates) != required_gates:
                return False
            if any(
                not isinstance(gate, Mapping)
                or gate.get("status")
                not in {
                    "blocked",
                    "research_only_underperforms_market",
                    "research_ready",
                }
                for gate in gates.values()
            ):
                return False
        csl = leagues.get("csl")
        if not isinstance(csl, Mapping):
            return False
        csl_gates = csl.get("gates")
        if not isinstance(csl_gates, Mapping):
            return False
        if not bool(
            csl.get("status") == "unavailable"
            and csl.get("reason") == "no_declared_verified_raw_history_source"
            and csl.get("sample_n") == 0
            and all(
                isinstance(gate, Mapping) and gate.get("status") == "blocked"
                for gate in csl_gates.values()
            )
        ):
            return False
        try:
            recalculated_combined = _scoreline_summary(dict(leagues))
            recalculated_overall = _formal_overall(
                dict(leagues),
                dict(combined),
                dict(selection_audit),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return combined == recalculated_combined and overall == recalculated_overall
    if lane == "legacy_research_only":
        overall = report.get("overall")
        return bool(
            report.get("schema_version") == LEGACY_REPORT_SCHEMA
            and report.get("report_lane") == "legacy_research_only"
            and report.get("formal_maturity_eligible") is False
            and isinstance(overall, Mapping)
            and overall.get("production_allowed") is False
            and overall.get("legacy_source_gate") == "blocked"
        )
    return False


def load_cached_report(
    cache_path: Path,
    output_path: Path,
    fingerprint: dict[str, Any],
) -> dict[str, Any] | None:
    """Load a cache hit only when both input and output identities match."""

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            return None
        if cache.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        if cache.get("report_lane") != fingerprint.get("report_lane"):
            return None
        if cache.get("input_fingerprint") != fingerprint.get("sha256"):
            return None
        if cache.get("output_path") != str(output_path.resolve()):
            return None
        if cache.get("output_sha256") != _sha256_file(output_path):
            return None
        report = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("combined"), dict):
            return None
        if not _cached_report_matches_fingerprint(report, fingerprint):
            return None
        return report
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def _market_failure_targets(leagues: dict[str, dict[str, Any]]) -> list[str]:
    return [
        f"{league}:{target}"
        for league, value in leagues.items()
        for target, gate in value["gates"].items()
        if gate.get("market_status") == "underperforms_market"
    ]


def _gates(result: dict[str, Any]) -> dict[str, Any]:
    targets = result["targets"]
    rows = result["rows"]
    gates: dict[str, Any] = {
        "three_way": release_gate(
            "three_way",
            sample_n=result["sample_n"],
            model_metrics=(
                result["three_way"]["model_on_market_sample"] or result["three_way"]["model"]
            ),
            frequency_metrics=(
                result["three_way"]["historical_frequency_on_market_sample"]
                or result["three_way"]["historical_frequency"]
            ),
            market_metrics=result["three_way"]["market"],
            prediction_rows=rows,
            comparison_sample_n=(
                result["three_way"]["market"]["sample_n"]
                if result["three_way"]["market"]
                else None
            ),
        ),
        "totals": release_gate(
            "totals",
            sample_n=targets["totals"]["sample_n"],
            model_metrics={"log_loss": targets["totals"]["log_loss"]},
            frequency_metrics={"log_loss": targets["totals"]["frequency_log_loss"]},
            prediction_rows=rows,
            primary_metric="log_loss",
        ),
        "half_full": release_gate(
            "half_full",
            sample_n=targets["half_full"]["sample_n"],
            model_metrics={"log_loss": targets["half_full"]["log_loss"]},
            frequency_metrics={"log_loss": targets["half_full"]["frequency_log_loss"]},
            prediction_rows=rows,
            primary_metric="log_loss",
        ),
        "handicap": release_gate(
            "handicap",
            sample_n=targets.get("handicap", {}).get("sample_n", 0),
            model_metrics=targets.get("handicap", {}).get("model", {}),
            frequency_metrics=targets.get("handicap", {}).get("frequency"),
            prediction_rows=rows,
            primary_metric="log_loss",
        ),
    }
    total_ou = targets.get("total_over_under")
    if total_ou:
        gates["total_over_under"] = release_gate(
            "totals",
            sample_n=total_ou["sample_n"],
            model_metrics=total_ou["model"],
            frequency_metrics=total_ou["frequency"],
            market_metrics=total_ou["market"],
            prediction_rows=rows,
        )
    return gates


def _time_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize timestamp semantics without persisting every backtest row."""

    basis_counts = Counter(
        str(row.get("market_time_basis", "unavailable"))
        for row in rows
        if row.get("market_probability") is not None
    )
    market_rows = [row for row in rows if row.get("market_probability") is not None]
    violations: list[dict[str, str]] = []
    for row in rows:
        try:
            assert_no_future_leakage([row])
        except ValueError as exc:
            violations.append({"fixture_id": str(row.get("fixture_id", "")), "reason": str(exc)})
    market_bound_violations = 0
    for row in market_rows:
        retrieved_at = row.get("market_retrieved_at")
        bound_at = row.get("market_time_bound_at")
        cutoff = datetime.fromisoformat(row["cutoff_at"]).astimezone(timezone.utc)
        if retrieved_at and datetime.fromisoformat(retrieved_at).astimezone(timezone.utc) > cutoff:
            market_bound_violations += 1
        if bound_at and datetime.fromisoformat(bound_at).astimezone(timezone.utc) > cutoff:
            market_bound_violations += 1
    feature_time_violations = sum("feature timestamp" in item["reason"] for item in violations)
    probability_contract_error = None
    try:
        assert_probability_contract(rows)
    except ValueError as exc:
        probability_contract_error = str(exc)
    return {
        "market_rows": len(market_rows),
        "kickoff_time_quality_counts": dict(
            sorted(Counter(str(row.get("kickoff_time_quality", "exact")) for row in rows).items())
        ),
        "kickoff_time_source_counts": dict(
            sorted(
                Counter(str(row.get("kickoff_time_source", "unavailable")) for row in rows).items()
            )
        ),
        "market_time_basis_counts": dict(sorted(basis_counts.items())),
        "market_time_quality_counts": dict(
            sorted(
                Counter(
                    str(row.get("market_time_quality", "unavailable")) for row in market_rows
                ).items()
            )
        ),
        "market_opening_rows": sum(
            row.get("market_opening_probability") is not None for row in rows
        ),
        "market_closing_rows": sum(
            row.get("market_closing_probability") is not None for row in rows
        ),
        "market_bound_violations": market_bound_violations,
        "feature_time_violations": feature_time_violations,
        "future_leakage_violations": len(violations),
        "probability_contract_error": probability_contract_error,
        "probability_contract_status": "pass" if probability_contract_error is None else "blocked",
        "status": "pass" if not violations and probability_contract_error is None else "blocked",
    }


def _independent_three_way_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose the pre-market scoreline probability beside the market blend.

    The strict walk-forward renderer keeps both matrices: the independent
    scoreline result and the display matrix after a causally observed market
    blend.  The historical report previously exposed only the latter, which
    made it too easy to read a market-informed probability as an independent
    model result.  This additive diagnostic never changes the selected model
    or any release gate.
    """

    independent_rows = [
        row for row in rows if isinstance(row.get("independent_scoreline_probability"), dict)
    ]
    if not independent_rows:
        return {
            "status": "unavailable",
            "sample_n": 0,
            "market_sample_n": 0,
        }
    outcomes = [row["outcome_1x2"] for row in independent_rows]
    independent_probability = [
        row["independent_scoreline_probability"] for row in independent_rows
    ]
    market_rows = [
        row for row in independent_rows if isinstance(row.get("market_probability"), dict)
    ]
    output: dict[str, Any] = {
        "status": "evaluated",
        "sample_n": len(independent_rows),
        "model": evaluate_three_way(independent_probability, outcomes),
        "market_sample_n": len(market_rows),
    }
    if not market_rows:
        output["market"] = None
        output["model_on_market_sample"] = None
        output["model_minus_market_brier_ci"] = None
        return output
    market_outcomes = [row["outcome_1x2"] for row in market_rows]
    market_probability = [row["market_probability"] for row in market_rows]
    output["market"] = evaluate_three_way(market_probability, market_outcomes)
    output["model_on_market_sample"] = evaluate_three_way(
        [row["independent_scoreline_probability"] for row in market_rows],
        market_outcomes,
    )
    differences = [
        sum(
            (row["independent_scoreline_probability"][label] - (label == row["outcome_1x2"])) ** 2
            for label in ("home", "draw", "away")
        )
        - sum(
            (row["market_probability"][label] - (label == row["outcome_1x2"])) ** 2
            for label in ("home", "draw", "away")
        )
        for row in market_rows
    ]
    output["model_minus_market_brier_ci"] = bootstrap_mean_ci(differences)
    return output


def _blocked_gates(reason: str) -> dict[str, dict[str, Any]]:
    gates: dict[str, dict[str, Any]] = {}
    for output_target, gate_target in (
        ("three_way", "three_way"),
        ("totals", "totals"),
        ("half_full", "half_full"),
        ("handicap", "handicap"),
        ("total_over_under", "totals"),
    ):
        gate = cast(
            dict[str, Any],
            release_gate(
                gate_target,
                sample_n=0,
                model_metrics={},
                frequency_metrics=None,
            ),
        )
        gate["failures"] = [*cast(list[str], gate["failures"]), reason]
        gate["status"] = "blocked"
        gate["can_display"] = False
        gates[output_target] = gate
    return gates


def _unavailable_league(*, status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "sample_n": 0,
        "warmup_seasons": [],
        "held_out_seasons": [],
        "data_cutoff": None,
        "walk_forward_folds": [],
        "three_way": {"status": "not_evaluated", "sample_n": 0},
        "targets": {},
        "gates": _blocked_gates(reason),
        "time_audit": {
            "status": "unavailable",
            "probability_contract_status": "unavailable",
            "probability_contract_error": None,
        },
        "freeze_stages": {},
    }


def _evaluated_league(matches: list[Match]) -> dict[str, Any]:
    result = cast(dict[str, Any], evaluate_strict_walk_forward(matches))
    if result.get("status") != "evaluated":
        return _unavailable_league(
            status="not_evaluated",
            reason=str(result.get("message") or "strict_walk_forward_not_evaluated"),
        )
    freeze_stages = cast(dict[str, Any], evaluate_freeze_stage_walk_forward(matches))
    three_way = dict(result["three_way"])
    three_way["independent_scoreline"] = _independent_three_way_metrics(result["rows"])
    return {
        "status": "evaluated",
        "model": result["model"],
        "hyperparameters": result["hyperparameters"],
        "sample_n": result["sample_n"],
        "warmup_seasons": result["warmup_seasons"],
        "held_out_seasons": result["held_out_seasons"],
        "data_cutoff": result["data_cutoff"],
        "walk_forward_folds": result["walk_forward_folds"],
        "three_way": three_way,
        "targets": result["targets"],
        "gates": _gates(result),
        "time_audit": _time_audit(result["rows"]),
        "freeze_stages": freeze_stages["stages"],
    }


def _scoreline_summary(leagues: dict[str, dict[str, Any]]) -> dict[str, Any]:
    league_values = list(leagues.values())
    scoreline_rows = [
        value["targets"]["scoreline"]
        for value in league_values
        if isinstance(value.get("targets"), dict)
        and isinstance(value["targets"].get("scoreline"), dict)
    ]
    scoreline_n = sum(value["sample_n"] for value in scoreline_rows)
    scoreline_model_log_loss = (
        sum(value["sample_n"] * value["log_loss"] for value in scoreline_rows) / scoreline_n
        if scoreline_n
        else None
    )
    scoreline_frequency_log_loss = (
        sum(value["sample_n"] * value["frequency_log_loss"] for value in scoreline_rows)
        / scoreline_n
        if scoreline_n
        else None
    )
    if scoreline_model_log_loss is None or scoreline_frequency_log_loss is None:
        frequency_status = "unavailable"
        frequency_delta = None
    else:
        frequency_delta = scoreline_model_log_loss - scoreline_frequency_log_loss
        frequency_status = (
            "beats_frequency"
            if frequency_delta < 0
            else "underperforms_frequency"
            if frequency_delta > 0
            else "ties_frequency"
        )
    market_sample_n = sum(
        int(
            value.get("three_way", {})
            .get("independent_scoreline", {})
            .get("market_sample_n", 0)
        )
        for value in league_values
        if isinstance(value.get("three_way"), dict)
    )
    return {
        "sample_n": sum(int(value.get("sample_n", 0)) for value in league_values),
        "scoreline_sample_n": scoreline_n,
        "scoreline_model_log_loss": scoreline_model_log_loss,
        "scoreline_frequency_log_loss": scoreline_frequency_log_loss,
        "frequency_comparison": {
            "status": frequency_status,
            "model_log_loss": scoreline_model_log_loss,
            "frequency_log_loss": scoreline_frequency_log_loss,
            "model_minus_frequency_log_loss": frequency_delta,
        },
        "market_baseline": {
            "status": (
                "available"
                if market_sample_n
                else "unavailable_no_independent_market_baseline"
            ),
            "comparison_sample_n": market_sample_n,
        },
        "scoreline_gate": release_gate(
            "scoreline",
            sample_n=scoreline_n,
            model_metrics=(
                {"log_loss": scoreline_model_log_loss}
                if scoreline_model_log_loss is not None
                else {}
            ),
            frequency_metrics=(
                {"log_loss": scoreline_frequency_log_loss}
                if scoreline_frequency_log_loss is not None
                else None
            ),
            primary_metric="log_loss",
        ),
    }


def _load_selection_audit(
    candidate_evidence_dir: Path,
    prospective_lock_path: Path | None,
) -> dict[str, Any]:
    prospective_lock = None
    prospective_lock_error = None
    if prospective_lock_path is not None and prospective_lock_path.exists():
        try:
            value = json.loads(prospective_lock_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("prospective model lock root must be an object")
            prospective_lock = value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            prospective_lock_error = str(exc)
    selection_audit = build_model_selection_audit(
        candidate_evidence_dir,
        prospective_lock=prospective_lock,
    )
    if prospective_lock_error is not None:
        selection_audit["prospective_failures"] = [
            *selection_audit["prospective_failures"],
            f"prospective_lock_unreadable:{prospective_lock_error}",
        ]
        selection_audit["gate_status"] = "blocked_selection_debt"
        selection_audit["production_eligible"] = False
    return selection_audit


def _formal_overall(
    leagues: dict[str, dict[str, Any]],
    combined: dict[str, Any],
    selection_audit: dict[str, Any],
) -> dict[str, Any]:
    source_availability_blocks = [
        f"{league}:{value['reason']}"
        for league, value in leagues.items()
        if value.get("status") != "evaluated"
    ]
    frequency_failures = [
        f"{league}:{target}"
        for league, value in leagues.items()
        for target, gate in value["gates"].items()
        if "worse_than_frequency_baseline" in gate["failures"]
    ]
    blocked_targets = [
        f"{league}:{target}"
        for league, value in leagues.items()
        for target, gate in value["gates"].items()
        if gate["status"] == "blocked"
    ]
    if combined["scoreline_gate"]["status"] == "blocked":
        blocked_targets.append("combined:scoreline")
    market_failures = _market_failure_targets(leagues)
    market_comparison_targets: list[str] = []
    market_unavailable_targets: list[str] = []
    for league, value in leagues.items():
        if value.get("status") != "evaluated":
            continue
        for target in ("three_way", "totals", "handicap"):
            gate = value.get("gates", {}).get(target)
            if not isinstance(gate, dict) or gate.get("status") == "blocked":
                continue
            label = f"{league}:{target}"
            if gate.get("market_status") in {"beats_market", "underperforms_market"}:
                market_comparison_targets.append(label)
            else:
                market_unavailable_targets.append(label)
    if market_failures:
        market_gate = "research_only_underperforms_market"
    elif market_unavailable_targets or not market_comparison_targets:
        market_gate = (
            "partial_market_comparison"
            if market_comparison_targets
            else "unavailable_no_independent_market_baseline"
        )
    else:
        market_gate = "pass"
    probability_contract_failures = [
        f"{league}:{value['time_audit']['probability_contract_error']}"
        for league, value in leagues.items()
        if value.get("status") == "evaluated"
        and value["time_audit"].get("probability_contract_error") is not None
    ]
    freeze_stage_failures = [
        f"{league}:{stage}:{stage_value.get('reason', stage_value.get('time_audit', {}).get('error', 'unavailable'))}"
        for league, league_value in leagues.items()
        if league_value.get("status") == "evaluated"
        for stage, stage_value in league_value.get("freeze_stages", {}).items()
        if stage_value.get("status") != "evaluated"
        or stage_value.get("time_audit", {}).get("status") != "pass"
    ]
    availability_blocks = [*source_availability_blocks, *blocked_targets]
    production_allowed = not any(
        (
            availability_blocks,
            market_failures,
            market_gate != "pass",
            probability_contract_failures,
            freeze_stage_failures,
            frequency_failures,
        )
    ) and bool(selection_audit["production_eligible"])
    blocked_reasons = []
    if source_availability_blocks:
        blocked_reasons.append("正式 raw 历史来源不可用：" + ", ".join(source_availability_blocks))
    if blocked_targets:
        blocked_reasons.append("数据或样本门槛未满足的目标：" + ", ".join(blocked_targets))
    if market_failures:
        blocked_reasons.append("公开收盘市场优于模型的目标：" + ", ".join(market_failures))
    if market_unavailable_targets:
        blocked_reasons.append("独立市场基线不可用：" + ", ".join(market_unavailable_targets))
    elif market_gate == "unavailable_no_independent_market_baseline":
        blocked_reasons.append("独立市场基线不可用：没有可比较的市场样本。")
    if probability_contract_failures:
        blocked_reasons.append("概率契约违规：" + ", ".join(probability_contract_failures))
    if freeze_stage_failures:
        blocked_reasons.append("冻结阶段不可完整回放：" + ", ".join(freeze_stage_failures))
    if not selection_audit["production_eligible"]:
        blocked_reasons.append("模型选择独立性未满足；需要锁定后的独立前瞻窗口。")
    return {
        "chronological_cutoff_gate": "pass",
        "probability_contract_gate": ("pass" if not probability_contract_failures else "blocked"),
        "sample_threshold_gate": (
            "pass_for_available_targets" if not blocked_targets else "partial_with_explicit_blocks"
        ),
        "frequency_baseline_gate": (
            "pass_within_tolerance" if not frequency_failures else "blocked"
        ),
        "frequency_comparison": combined.get("frequency_comparison", {
            "status": "unavailable",
            "model_log_loss": None,
            "frequency_log_loss": None,
            "model_minus_frequency_log_loss": None,
        }),
        "frequency_baseline_failures": frequency_failures,
        "availability_blocks": availability_blocks,
        "market_gate": market_gate,
        "market_baseline": combined.get("market_baseline", {
            "status": market_gate,
            "comparison_sample_n": len(market_comparison_targets),
        }),
        "production_allowed": production_allowed,
        "market_failures": market_failures,
        "market_comparison_targets": market_comparison_targets,
        "market_unavailable_targets": market_unavailable_targets,
        "probability_contract_failures": probability_contract_failures,
        "freeze_stage_gate": (
            "pass" if not freeze_stage_failures else "partial_with_explicit_blocks"
        ),
        "freeze_stage_failures": freeze_stage_failures,
        "model_selection_gate": selection_audit["gate_status"],
        "model_selection_failures": selection_audit["prospective_failures"],
        "blocked_reasons": blocked_reasons,
    }


def build_report(
    *,
    openfootball_raw_archive: Path | str,
    observed_before: datetime,
    source_ids: Iterable[str] = OPENFOOTBALL_HISTORY_SOURCE_IDS,
    candidate_evidence_dir: Path = Path("docs/evidence"),
    prospective_lock_path: Path | None = Path("docs/evidence/prospective-model-lock-current.json"),
) -> dict[str, Any]:
    """Build the formal v260 report from verified raw history only."""

    source = VerifiedOpenFootballHistorySource(
        openfootball_raw_archive,
        observed_before=observed_before,
        source_ids=source_ids,
    )
    admission = source.admission_manifest()
    competitions = set(admission["competition_ids"])
    leagues: dict[str, dict[str, Any]] = {}
    for league in EUROPEAN_LEAGUES:
        leagues[league] = (
            _evaluated_league(source.load(league).matches)
            if league in competitions
            else _unavailable_league(
                status="unavailable",
                reason="no_admitted_openfootball_history_source_at_cutoff",
            )
        )
    leagues["csl"] = _unavailable_league(
        status="unavailable",
        reason="no_declared_verified_raw_history_source",
    )
    combined = _scoreline_summary(leagues)
    selection_audit = _load_selection_audit(
        candidate_evidence_dir,
        prospective_lock_path,
    )
    report: dict[str, Any] = {
        "schema_version": FORMAL_REPORT_SCHEMA,
        "report_lane": "formal_v260",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "training_source_contract": {
            "provider": "OpenFootball",
            "raw_archive_required": True,
            "training_admission_sha256": admission["admission_sha256"],
            "policy_version": POLICY_VERSION,
            "observed_before": admission["observed_before"],
            "source_ids": admission["source_ids"],
            "prohibited_formal_inputs": [
                "football_data_csv",
                "legacy_csl_files",
                "snapshot_self_report",
            ],
        },
        "training_admission": admission,
        "protocol": {
            "walk_forward": (
                "chronological; same-kickoff batch updates only after all predictions"
            ),
            "cutoff": "one second before kickoff",
            "warmup_seasons": 2,
            "market_policy": "no market data is fabricated for verified OpenFootball history",
            "freeze_stage_policy": (
                "causal event replay at 24h, 6h and 90m; lineup confirmation requires historical observed_at"
            ),
        },
        "leagues": leagues,
        "combined": combined,
        "model_selection_audit": selection_audit,
    }
    report["overall"] = _formal_overall(leagues, combined, selection_audit)
    return report


def _build_legacy_report_body(
    data_dir: Path,
    *,
    kickoff_enrichment_path: Path | None = None,
    candidate_evidence_dir: Path = Path("docs/evidence"),
    prospective_lock_path: Path | None = Path("docs/evidence/prospective-model-lock-current.json"),
) -> dict[str, Any]:
    source = MatchHistorySource(
        data_dir,
        kickoff_enrichment_path=kickoff_enrichment_path,
    )
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "walk_forward": "chronological; same-kickoff batch updates only after all predictions",
            "cutoff": "one second before kickoff",
            "warmup_seasons": 2,
            "market_policy": "opening and closing public odds are retained separately; compatibility market prefers closing and falls back to opening",
            "freeze_stage_policy": "causal event replay at 24h, 6h and 90m; lineup confirmation requires historical observed_at",
        },
        "leagues": {},
    }
    for league in EUROPEAN_LEAGUES:
        matches = source.load(league).matches
        result = cast(dict[str, Any], evaluate_strict_walk_forward(matches))
        freeze_stages = cast(dict[str, Any], evaluate_freeze_stage_walk_forward(matches))
        three_way = dict(result["three_way"])
        three_way["independent_scoreline"] = _independent_three_way_metrics(result["rows"])
        report["leagues"][league] = {
            "model": result["model"],
            "hyperparameters": result["hyperparameters"],
            "sample_n": result["sample_n"],
            "warmup_seasons": result["warmup_seasons"],
            "held_out_seasons": result["held_out_seasons"],
            "data_cutoff": result["data_cutoff"],
            "walk_forward_folds": result["walk_forward_folds"],
            "three_way": three_way,
            "targets": result["targets"],
            "gates": _gates(result),
            "time_audit": _time_audit(result["rows"]),
            "freeze_stages": freeze_stages["stages"],
        }
    csl_matches = OpenFootballSource(data_dir).load("csl").matches
    csl = cast(dict[str, Any], evaluate_strict_walk_forward(csl_matches))
    csl_freeze_stages = cast(dict[str, Any], evaluate_freeze_stage_walk_forward(csl_matches))
    csl_three_way = dict(csl["three_way"])
    csl_three_way["independent_scoreline"] = _independent_three_way_metrics(csl["rows"])
    report["leagues"]["csl"] = {
        "model": csl["model"],
        "hyperparameters": csl["hyperparameters"],
        "sample_n": csl["sample_n"],
        "warmup_seasons": csl["warmup_seasons"],
        "held_out_seasons": csl["held_out_seasons"],
        "data_cutoff": csl["data_cutoff"],
        "walk_forward_folds": csl["walk_forward_folds"],
        "three_way": csl_three_way,
        "targets": csl["targets"],
        "gates": _gates(csl),
        "time_audit": _time_audit(csl["rows"]),
        "freeze_stages": csl_freeze_stages["stages"],
    }
    report["combined"] = _scoreline_summary(report["leagues"])
    prospective_lock = None
    prospective_lock_error = None
    if prospective_lock_path is not None and prospective_lock_path.exists():
        try:
            value = json.loads(prospective_lock_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("prospective model lock root must be an object")
            prospective_lock = value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            prospective_lock_error = str(exc)
    selection_audit = build_model_selection_audit(
        candidate_evidence_dir,
        prospective_lock=prospective_lock,
    )
    if prospective_lock_error is not None:
        selection_audit["prospective_failures"] = [
            *selection_audit["prospective_failures"],
            f"prospective_lock_unreadable:{prospective_lock_error}",
        ]
        selection_audit["gate_status"] = "blocked_selection_debt"
        selection_audit["production_eligible"] = False
    report["model_selection_audit"] = selection_audit
    frequency_failures = [
        f"{league}:{target}"
        for league, value in report["leagues"].items()
        for target, gate in value["gates"].items()
        if "worse_than_frequency_baseline" in gate["failures"]
    ]
    blocked_targets = [
        f"{league}:{target}"
        for league, value in report["leagues"].items()
        for target, gate in value["gates"].items()
        if gate["status"] == "blocked"
    ]
    market_failures = _market_failure_targets(report["leagues"])
    market_comparison_targets: list[str] = []
    market_unavailable_targets: list[str] = []
    for league, value in report["leagues"].items():
        if value.get("status") != "evaluated":
            continue
        for target in ("three_way", "totals", "handicap"):
            gate = value.get("gates", {}).get(target)
            if not isinstance(gate, dict) or gate.get("status") == "blocked":
                continue
            label = f"{league}:{target}"
            if gate.get("market_status") in {"beats_market", "underperforms_market"}:
                market_comparison_targets.append(label)
            else:
                market_unavailable_targets.append(label)
    if market_failures:
        market_gate = "research_only_underperforms_market"
    elif market_unavailable_targets or not market_comparison_targets:
        market_gate = (
            "partial_market_comparison"
            if market_comparison_targets
            else "unavailable_no_independent_market_baseline"
        )
    else:
        market_gate = "pass"
    probability_contract_failures = [
        f"{league}:{value['time_audit']['probability_contract_error']}"
        for league, value in report["leagues"].items()
        if value["time_audit"]["probability_contract_error"] is not None
    ]
    freeze_stage_failures = [
        f"{league}:{stage}:{value.get('reason', value.get('time_audit', {}).get('error', 'unavailable'))}"
        for league, league_value in report["leagues"].items()
        for stage, value in league_value.get("freeze_stages", {}).items()
        if value.get("status") != "evaluated"
        or value.get("time_audit", {}).get("status") != "pass"
    ]
    if report["combined"]["scoreline_gate"]["status"] == "blocked":
        blocked_targets.append("combined:scoreline")
    blocked_reasons = []
    if market_failures:
        blocked_reasons.append("公开收盘市场优于模型的目标：" + ", ".join(market_failures))
    if market_unavailable_targets:
        blocked_reasons.append("独立市场基线不可用：" + ", ".join(market_unavailable_targets))
    elif market_gate == "unavailable_no_independent_market_baseline":
        blocked_reasons.append("独立市场基线不可用：没有可比较的市场样本。")
    if blocked_targets:
        blocked_reasons.append("数据或样本门槛未满足的目标：" + ", ".join(blocked_targets))
    if probability_contract_failures:
        blocked_reasons.append("概率契约违规：" + ", ".join(probability_contract_failures))
    if freeze_stage_failures:
        blocked_reasons.append("冻结阶段不可完整回放：" + ", ".join(freeze_stage_failures))
    if not selection_audit["production_eligible"]:
        blocked_reasons.append(
            "模型选择独立性未满足：现有 2021/22–2025/26 结果已被反复用于候选判断；"
            "需要先锁定模型，再用锁定之后发生的前瞻比赛完成独立评估。"
        )
    production_allowed = not any(
        (
            market_failures,
            market_gate != "pass",
            blocked_targets,
            probability_contract_failures,
            freeze_stage_failures,
            frequency_failures,
        )
    ) and bool(selection_audit["production_eligible"])
    report["overall"] = {
        "chronological_cutoff_gate": "pass",
        "probability_contract_gate": "pass" if not probability_contract_failures else "blocked",
        "sample_threshold_gate": "pass_for_available_targets"
        if not blocked_targets
        else "partial_with_explicit_blocks",
        "frequency_baseline_gate": "pass_within_tolerance"
        if not frequency_failures
        else "blocked",
        "frequency_comparison": report["combined"].get("frequency_comparison", {
            "status": "unavailable",
            "model_log_loss": None,
            "frequency_log_loss": None,
            "model_minus_frequency_log_loss": None,
        }),
        "frequency_baseline_failures": frequency_failures,
        "availability_blocks": blocked_targets,
        "market_gate": market_gate,
        "market_baseline": report["combined"].get("market_baseline", {
            "status": market_gate,
            "comparison_sample_n": len(market_comparison_targets),
        }),
        "production_allowed": production_allowed,
        "market_failures": market_failures,
        "market_comparison_targets": market_comparison_targets,
        "market_unavailable_targets": market_unavailable_targets,
        "probability_contract_failures": probability_contract_failures,
        "freeze_stage_gate": "pass"
        if not freeze_stage_failures
        else "partial_with_explicit_blocks",
        "freeze_stage_failures": freeze_stage_failures,
        "model_selection_gate": selection_audit["gate_status"],
        "model_selection_failures": selection_audit["prospective_failures"],
        "blocked_reasons": blocked_reasons,
    }
    return report


def build_legacy_research_report(
    data_dir: Path,
    *,
    kickoff_enrichment_path: Path | None = None,
    candidate_evidence_dir: Path = Path("docs/evidence"),
    prospective_lock_path: Path | None = Path("docs/evidence/prospective-model-lock-current.json"),
) -> dict[str, Any]:
    """Build the old file-backed report with an irreversible research-only label."""

    report = _build_legacy_report_body(
        data_dir,
        kickoff_enrichment_path=kickoff_enrichment_path,
        candidate_evidence_dir=candidate_evidence_dir,
        prospective_lock_path=prospective_lock_path,
    )
    report["schema_version"] = LEGACY_REPORT_SCHEMA
    report["report_lane"] = "legacy_research_only"
    report["formal_maturity_eligible"] = False
    report["legacy_sources"] = [
        "football_data_csv",
        "legacy_csl_files",
    ]
    overall = report.setdefault("overall", {})
    if not isinstance(overall, dict):
        overall = {}
        report["overall"] = overall
    overall["production_allowed"] = False
    overall["legacy_source_gate"] = "blocked"
    blocked_reasons = overall.get("blocked_reasons")
    if not isinstance(blocked_reasons, list):
        blocked_reasons = []
    if "legacy_research_only" not in blocked_reasons:
        blocked_reasons.append("legacy_research_only")
    overall["blocked_reasons"] = blocked_reasons
    return report


def _parse_cli_timestamp(value: str, *, parser: argparse.ArgumentParser) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--observed-before must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parser.error("--observed-before must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-research-only", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--kickoff-enrichment",
        type=Path,
        default=None,
        help=(
            "optional, explicitly authorized exact-time mapping for date-only rows; "
            "disabled by default"
        ),
    )
    parser.add_argument("--openfootball-raw-archive", type=Path, default=None)
    parser.add_argument("--observed-before", default=None)
    parser.add_argument(
        "--openfootball-source-id",
        action="append",
        dest="openfootball_source_ids",
        default=None,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--candidate-evidence-dir",
        type=Path,
        default=Path("docs/evidence"),
    )
    parser.add_argument(
        "--prospective-lock",
        type=Path,
        default=Path("docs/evidence/prospective-model-lock-current.json"),
    )
    parser.add_argument(
        "--cache-state",
        type=Path,
        default=None,
        help="optional input/output identity cache; unchanged reports are not recomputed",
    )
    args = parser.parse_args()
    report_builder: Callable[[], dict[str, Any]]
    if not args.legacy_research_only:
        if args.openfootball_raw_archive is None:
            parser.error("formal v260 mode requires --openfootball-raw-archive")
        if args.observed_before is None:
            parser.error("formal v260 mode requires --observed-before")
        if args.data_dir is not None or args.kickoff_enrichment is not None:
            parser.error("formal v260 mode rejects --data-dir and --kickoff-enrichment")
        observed_before = _parse_cli_timestamp(args.observed_before, parser=parser)
        selected_source_ids = (
            args.openfootball_source_ids
            if args.openfootball_source_ids is not None
            else OPENFOOTBALL_HISTORY_SOURCE_IDS
        )
        fingerprint = build_input_fingerprint(
            openfootball_raw_archive=args.openfootball_raw_archive,
            observed_before=observed_before,
            source_ids=selected_source_ids,
            candidate_evidence_dir=args.candidate_evidence_dir,
            prospective_lock_path=args.prospective_lock,
        )
        report_builder = partial(
            build_report,
            openfootball_raw_archive=args.openfootball_raw_archive,
            observed_before=observed_before,
            source_ids=selected_source_ids,
            candidate_evidence_dir=args.candidate_evidence_dir,
            prospective_lock_path=args.prospective_lock,
        )
    else:
        if args.openfootball_raw_archive is not None or args.observed_before is not None:
            parser.error("legacy research-only mode rejects formal raw archive arguments")
        data_dir = args.data_dir or Path("data/MatchHistory")
        kickoff_enrichment_path = (
            args.kickoff_enrichment
            if args.kickoff_enrichment is not None and args.kickoff_enrichment.exists()
            else None
        )
        fingerprint = build_legacy_input_fingerprint(
            data_dir,
            kickoff_enrichment_path=kickoff_enrichment_path,
            candidate_evidence_dir=args.candidate_evidence_dir,
            prospective_lock_path=args.prospective_lock,
        )
        report_builder = partial(
            build_legacy_research_report,
            data_dir,
            kickoff_enrichment_path=kickoff_enrichment_path,
            candidate_evidence_dir=args.candidate_evidence_dir,
            prospective_lock_path=args.prospective_lock,
        )
    if args.cache_state is not None:
        cached = load_cached_report(args.cache_state, args.output, fingerprint)
        if cached is not None:
            print(
                json.dumps(
                    {
                        "status": "skipped_unchanged",
                        "report_lane": cached.get("report_lane"),
                        "input_fingerprint": fingerprint["sha256"],
                        "combined": cached["combined"],
                    },
                    ensure_ascii=False,
                )
            )
            return
    report = report_builder()
    _write_json_atomic(args.output, report)
    if args.cache_state is not None:
        write_report_cache(args.cache_state, args.output, fingerprint)
    print(
        json.dumps(
            {
                "status": "generated",
                "report_lane": report.get("report_lane"),
                "input_fingerprint": fingerprint["sha256"],
                "combined": report["combined"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "_market_failure_targets",
    "build_input_fingerprint",
    "build_legacy_input_fingerprint",
    "build_legacy_research_report",
    "build_report",
    "load_cached_report",
    "write_report_cache",
]
