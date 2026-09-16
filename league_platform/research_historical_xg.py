"""Evaluate the historical Understat xG lane as a research-only candidate.

This command deliberately compares the *pre-market* independent probability
to the same-match market baseline.  The regular strict report keeps its
market-informed display matrix for compatibility, while this report exposes
the independent lane explicitly so a market blend cannot be mistaken for a
standalone model edge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from league_platform.historical_xg import load_historical_xg_index
from league_platform.metrics import (
    assert_no_future_leakage,
    assert_probability_contract,
    brier_score,
    bootstrap_mean_ci,
    evaluate_three_way,
    log_loss,
)
from league_platform.sources.match_history import MatchHistorySource
from league_platform.sources.openfootball import OpenFootballSource
from league_platform.strict_backtest import evaluate_strict_walk_forward


EUROPEAN_LEAGUES = ("premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _brier(probability: dict[str, float], outcome: str, labels: tuple[str, ...]) -> float:
    return sum((float(probability[label]) - (label == outcome)) ** 2 for label in labels)


def _paired_market_metrics(
    rows: list[dict[str, Any]],
    *,
    prediction_key: str,
    market_key: str,
    outcome_key: str,
    labels: tuple[str, ...],
) -> dict[str, Any] | None:
    complete = [
        row
        for row in rows
        if isinstance(row.get(prediction_key), dict)
        and isinstance(row.get(market_key), dict)
        and row.get(outcome_key) in labels
    ]
    if not complete:
        return None
    candidate_probabilities = [row[prediction_key] for row in complete]
    market_probabilities = [row[market_key] for row in complete]
    outcomes = [str(row[outcome_key]) for row in complete]
    candidate_brier = brier_score(candidate_probabilities, outcomes)
    market_brier = brier_score(market_probabilities, outcomes)
    candidate_log_loss = log_loss(candidate_probabilities, outcomes)
    market_log_loss = log_loss(market_probabilities, outcomes)
    differences = [
        _brier(row[prediction_key], outcome, labels)
        - _brier(row[market_key], outcome, labels)
        for row, outcome in zip(complete, outcomes)
    ]
    return {
        "sample_n": len(complete),
        "candidate": {
            "brier": candidate_brier,
            "log_loss": candidate_log_loss,
        },
        "market": {
            "brier": market_brier,
            "log_loss": market_log_loss,
        },
        "candidate_minus_market_brier": candidate_brier - market_brier,
        "candidate_minus_market_log_loss": candidate_log_loss - market_log_loss,
        "candidate_minus_market_brier_ci": bootstrap_mean_ci(differences),
    }


def _three_way_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    independent = _paired_market_metrics(
        rows,
        prediction_key="independent_scoreline_probability",
        market_key="market_probability",
        outcome_key="outcome_1x2",
        labels=("home", "draw", "away"),
    )
    base = _paired_market_metrics(
        rows,
        prediction_key="elo_probability",
        market_key="market_probability",
        outcome_key="outcome_1x2",
        labels=("home", "draw", "away"),
    )
    all_independent = [
        row
        for row in rows
        if isinstance(row.get("independent_scoreline_probability"), dict)
    ]
    return {
        "independent_vs_market": independent,
        "base_elo_vs_market": base,
        "independent_all_rows": evaluate_three_way(
            [row["independent_scoreline_probability"] for row in all_independent],
            [row["outcome_1x2"] for row in all_independent],
        )
        if all_independent
        else None,
        "historical_xg_rows": sum("recent_xg" in row.get("live_feature_fields", []) for row in rows),
    }


def _total_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return _paired_market_metrics(
        rows,
        prediction_key="independent_total_over_under_probability",
        market_key="total_market_probability",
        outcome_key="actual_total_ou",
        labels=("over", "under"),
    )


def _evaluate_league(
    league_id: str,
    *,
    match_history_dir: Path,
    understat_dir: Path,
    warmup_seasons: int,
    window: int,
    min_sample: int,
    blend_weight: float,
) -> dict[str, Any]:
    if league_id == "csl":
        matches = OpenFootballSource(match_history_dir).load("csl").matches
    else:
        matches = MatchHistorySource(match_history_dir).load(league_id).matches
    index = load_historical_xg_index(understat_dir, league_ids=(league_id,))
    base = evaluate_strict_walk_forward(
        matches,
        warmup_seasons=warmup_seasons,
        compute_metrics=False,
    )
    candidate = evaluate_strict_walk_forward(
        matches,
        warmup_seasons=warmup_seasons,
        historical_xg_index=index,
        historical_xg_window=window,
        historical_xg_min_sample=min_sample,
        historical_xg_blend_weight=blend_weight,
        compute_metrics=False,
    )
    assert_no_future_leakage(candidate["rows"])
    assert_probability_contract(candidate["rows"])
    rows = candidate["rows"]
    return {
        "model": candidate["model"],
        "sample_n": candidate["sample_n"],
        "held_out_seasons": candidate["held_out_seasons"],
        "historical_xg_index": candidate["historical_xg_index"],
        "three_way": _three_way_summary(rows),
        "total_over_under": _total_summary(rows),
        "base_result_model": base["model"],
        "base_independent_three_way": _three_way_summary(base["rows"]),
        "time_audit": {
            "future_leakage_status": "pass",
            "feature_time_max": max(
                (max(row.get("feature_times", []), default="") for row in rows),
                default="",
            ),
        },
    }


def build_report(
    *,
    match_history_dir: Path,
    understat_dir: Path,
    leagues: tuple[str, ...] = EUROPEAN_LEAGUES,
    warmup_seasons: int = 2,
    window: int = 5,
    min_sample: int = 3,
    blend_weight: float = 0.20,
) -> dict[str, Any]:
    reports = {
        league_id: _evaluate_league(
            league_id,
            match_history_dir=match_history_dir,
            understat_dir=understat_dir,
            warmup_seasons=warmup_seasons,
            window=window,
            min_sample=min_sample,
            blend_weight=blend_weight,
        )
        for league_id in leagues
    }
    all_three_way = [
        value["three_way"]["independent_vs_market"]
        for value in reports.values()
        if value["three_way"]["independent_vs_market"] is not None
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "candidate": "strict_dynamic_elo_scoreline_v2_causal_rho+historical_xg_candidate",
            "feature_source": "Understat historical match xG archive",
            "source_role": "fact_source",
            "execution_layer": "Crawl4AI or another fetch runtime may acquire the archive; it is not a fact source",
            "availability": "effective_at_plus_1d_conservative",
            "rolling_window": window,
            "minimum_team_sample": min_sample,
            "blend_weight": blend_weight,
            "market_comparison": "same fixtures with closing/opening pre-kickoff market fields",
            "selection": "research-only; no parameter promotion or production lock mutation",
        },
        "code_sha256": {
            "historical_xg.py": _sha256(Path(__file__).with_name("historical_xg.py")),
            "strict_backtest.py": _sha256(Path(__file__).with_name("strict_backtest.py")),
            "research_historical_xg.py": _sha256(Path(__file__)),
        },
        "leagues": reports,
        "pooled": {
            "market_sample_n": sum(value["sample_n"] for value in all_three_way),
            "candidate_minus_market_brier": (
                sum(value["candidate_minus_market_brier"] * value["sample_n"] for value in all_three_way)
                / sum(value["sample_n"] for value in all_three_way)
                if all_three_way
                else None
            ),
        },
        "decision": {
            "promoted": False,
            "production_allowed": False,
            "reason": "历史 xG 仍是研究候选；即使时间账本合格，也必须通过独立前瞻窗口和跨联赛市场门禁后才能考虑锁定。",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-history-dir", type=Path, default=Path("data/MatchHistory"))
    parser.add_argument("--understat-dir", type=Path, default=Path("data/understat_enriched"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--min-sample", type=int, default=3)
    parser.add_argument("--blend-weight", type=float, default=0.20)
    args = parser.parse_args()
    report = build_report(
        match_history_dir=args.match_history_dir,
        understat_dir=args.understat_dir,
        window=args.window,
        min_sample=args.min_sample,
        blend_weight=args.blend_weight,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "generated",
                "output": str(args.output),
                "pooled": report["pooled"],
                "production_allowed": report["decision"]["production_allowed"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
