"""Strict nested pooled calibration research for public pre-match markets.

This is a diagnostic candidate, not a production predictor.  It learns only
from completed seasons before the designated final evaluation period:

* 2018/19--2019/20: fit candidate parameters;
* 2020: select feature set, regularisation and blend weight;
* 2021/22 onward: final evaluation for this one candidate.

That later period has now been inspected by many candidate reports, so the
project-level model-selection audit correctly treats it as development
evidence rather than an untouched production test.

The identity parameterisation exactly reproduces the raw closing market.  All
learned coefficients are regularised toward that identity, and every reported
delta is paired on the same fixtures.  Missing opening/model fields exclude a
row; they are never silently filled with zero.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from league_platform.metrics import bootstrap_mean_ci, evaluate_three_way
from league_platform.sources.match_history import MatchHistorySource
from league_platform.strict_backtest import evaluate_strict_walk_forward


LEAGUES = ("premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1")
TRAIN_SEASONS = {"1819", "1920"}
VALIDATION_SEASONS = {"2020"}
HOLDOUT_SEASONS = {"2122", "2223", "2324", "2425", "2526"}
TARGET_LABELS = {
    "three_way": ("home", "draw", "away"),
    "total_over_under": ("over", "under"),
}
FEATURE_SETS = (
    "closing_only",
    "closing_opening",
    "closing_opening_model",
    "closing_opening_model_league",
)
REGULARISATION_GRID = (0.0001, 0.001, 0.01, 0.1, 1.0)
BLEND_GRID = tuple(step / 10 for step in range(11))


@dataclass(frozen=True)
class Example:
    league: str
    season: str
    fixture_id: str
    outcome: str
    raw_probability: tuple[float, ...]
    opening_probability: tuple[float, ...]
    model_probability: tuple[float, ...]
    expected_goals: tuple[float, float]


@dataclass(frozen=True)
class Calibrator:
    weights: np.ndarray
    bias: np.ndarray
    identity_weights: np.ndarray
    feature_set: str
    target: str


def _probability_tuple(value: object, labels: Sequence[str]) -> tuple[float, ...] | None:
    if not isinstance(value, dict):
        return None
    result: list[float] = []
    for label in labels:
        try:
            number = float(value[label])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0 or number >= 1:
            return None
        result.append(number)
    total = sum(result)
    if abs(total - 1.0) > 1e-5:
        return None
    return tuple(number / total for number in result)


def examples_from_rows(rows_by_league: dict[str, list[dict]], target: str) -> list[Example]:
    """Build a common complete-case sample for every candidate feature set."""

    if target not in TARGET_LABELS:
        raise ValueError(f"unsupported target: {target}")
    labels = TARGET_LABELS[target]
    examples: list[Example] = []
    for league in LEAGUES:
        for row in rows_by_league.get(league, []):
            if target == "three_way":
                raw = _probability_tuple(row.get("market_closing_probability"), labels)
                opening = _probability_tuple(row.get("market_opening_probability"), labels)
                model = _probability_tuple(row.get("scoreline_probability"), labels)
                outcome = row.get("outcome_1x2")
            else:
                raw = _probability_tuple(row.get("total_market_closing_probability"), labels)
                opening = _probability_tuple(row.get("total_market_opening_probability"), labels)
                model = _probability_tuple(row.get("total_over_under_probability"), labels)
                outcome = row.get("actual_total_ou")
            expected = row.get("expected_goals")
            if (
                raw is None
                or opening is None
                or model is None
                or outcome not in labels
                or not isinstance(expected, dict)
            ):
                continue
            try:
                home_xg = float(expected["home"])
                away_xg = float(expected["away"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) and value > 0 for value in (home_xg, away_xg)):
                continue
            examples.append(
                Example(
                    league=league,
                    season=str(row["season"]),
                    fixture_id=str(row["fixture_id"]),
                    outcome=str(outcome),
                    raw_probability=raw,
                    opening_probability=opening,
                    model_probability=model,
                    expected_goals=(home_xg, away_xg),
                )
            )
    return examples


def _feature_vector(example: Example, *, feature_set: str, target: str) -> list[float]:
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"unsupported feature set: {feature_set}")
    values = [math.log(max(1e-12, value)) for value in example.raw_probability]
    if feature_set != "closing_only":
        values.extend(math.log(max(1e-12, value)) for value in example.opening_probability)
    if feature_set in {"closing_opening_model", "closing_opening_model_league"}:
        values.extend(math.log(max(1e-12, value)) for value in example.model_probability)
        home_xg, away_xg = example.expected_goals
        values.extend((home_xg - away_xg, home_xg + away_xg))
    if feature_set == "closing_opening_model_league":
        values.extend(float(example.league == league) for league in LEAGUES)
    if target not in TARGET_LABELS:
        raise ValueError(f"unsupported target: {target}")
    return values


def _matrix(
    examples: Sequence[Example], *, feature_set: str, target: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    labels = TARGET_LABELS[target]
    x = np.asarray(
        [_feature_vector(example, feature_set=feature_set, target=target) for example in examples],
        dtype=np.float64,
    )
    y = np.zeros((len(examples), len(labels)), dtype=np.float64)
    raw = np.asarray([example.raw_probability for example in examples], dtype=np.float64)
    for index, example in enumerate(examples):
        y[index, labels.index(example.outcome)] = 1.0
    return x, y, raw


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=1, keepdims=True)


def fit_calibrator(
    examples: Sequence[Example],
    *,
    target: str,
    feature_set: str,
    regularisation: float,
    iterations: int = 800,
    learning_rate: float = 0.02,
) -> Calibrator:
    """Fit a deterministic Adam-optimised softmax map toward market identity."""

    if len(examples) < 100:
        raise ValueError("at least 100 examples are required")
    if regularisation <= 0 or not math.isfinite(regularisation):
        raise ValueError("regularisation must be positive and finite")
    if iterations < 100 or learning_rate <= 0:
        raise ValueError("invalid optimiser settings")
    x, y, _ = _matrix(examples, feature_set=feature_set, target=target)
    classes = len(TARGET_LABELS[target])
    identity = np.zeros((classes, x.shape[1]), dtype=np.float64)
    identity[:, :classes] = np.eye(classes)
    weights = identity.copy()
    bias = np.zeros(classes, dtype=np.float64)
    mw = np.zeros_like(weights)
    vw = np.zeros_like(weights)
    mb = np.zeros_like(bias)
    vb = np.zeros_like(bias)
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    for step in range(1, iterations + 1):
        probabilities = _softmax(x @ weights.T + bias)
        error = probabilities - y
        grad_w = error.T @ x / len(x) + 2.0 * regularisation * (weights - identity)
        grad_b = np.mean(error, axis=0) + 2.0 * regularisation * bias
        mw = beta1 * mw + (1.0 - beta1) * grad_w
        vw = beta2 * vw + (1.0 - beta2) * (grad_w * grad_w)
        mb = beta1 * mb + (1.0 - beta1) * grad_b
        vb = beta2 * vb + (1.0 - beta2) * (grad_b * grad_b)
        mw_hat = mw / (1.0 - beta1**step)
        vw_hat = vw / (1.0 - beta2**step)
        mb_hat = mb / (1.0 - beta1**step)
        vb_hat = vb / (1.0 - beta2**step)
        weights -= learning_rate * mw_hat / (np.sqrt(vw_hat) + epsilon)
        bias -= learning_rate * mb_hat / (np.sqrt(vb_hat) + epsilon)
    if not np.all(np.isfinite(weights)) or not np.all(np.isfinite(bias)):
        raise ValueError("calibrator produced non-finite parameters")
    return Calibrator(
        weights=weights,
        bias=bias,
        identity_weights=identity,
        feature_set=feature_set,
        target=target,
    )


def predict_calibrator(model: Calibrator, examples: Sequence[Example]) -> np.ndarray:
    x, _, _ = _matrix(examples, feature_set=model.feature_set, target=model.target)
    return _softmax(x @ model.weights.T + model.bias)


def _blend(raw: np.ndarray, candidate: np.ndarray, alpha: float) -> np.ndarray:
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between zero and one")
    result = (1.0 - alpha) * raw + alpha * candidate
    return result / np.sum(result, axis=1, keepdims=True)


def _loss_values(
    probabilities: np.ndarray, examples: Sequence[Example], target: str
) -> tuple[list[float], list[float]]:
    labels = TARGET_LABELS[target]
    brier: list[float] = []
    log_values: list[float] = []
    for probability, example in zip(probabilities, examples):
        actual = labels.index(example.outcome)
        brier.append(
            sum((float(value) - float(index == actual)) ** 2 for index, value in enumerate(probability))
        )
        log_values.append(-math.log(max(1e-12, float(probability[actual]))))
    return brier, log_values


def _metrics(probabilities: np.ndarray, examples: Sequence[Example], target: str) -> dict:
    labels = TARGET_LABELS[target]
    rows = [
        {label: float(value) for label, value in zip(labels, probability)}
        for probability in probabilities
    ]
    outcomes = [example.outcome for example in examples]
    if target == "three_way":
        return evaluate_three_way(rows, outcomes)
    brier, log_values = _loss_values(probabilities, examples, target)
    return {
        "sample_n": len(examples),
        "brier": sum(brier) / len(brier),
        "log_loss": sum(log_values) / len(log_values),
    }


def _select_candidate(
    train: Sequence[Example], validation: Sequence[Example], *, target: str
) -> dict:
    _, _, raw_validation = _matrix(
        validation, feature_set="closing_only", target=target
    )
    candidates: list[dict] = []
    for feature_set in FEATURE_SETS:
        for regularisation in REGULARISATION_GRID:
            model = fit_calibrator(
                train,
                target=target,
                feature_set=feature_set,
                regularisation=regularisation,
            )
            unblended = predict_calibrator(model, validation)
            for alpha in BLEND_GRID:
                predicted = _blend(raw_validation, unblended, alpha)
                metrics = _metrics(predicted, validation, target)
                league_deltas = {}
                for league in LEAGUES:
                    indexes = [
                        index for index, example in enumerate(validation) if example.league == league
                    ]
                    if not indexes:
                        continue
                    league_examples = [validation[index] for index in indexes]
                    league_candidate = predicted[indexes]
                    league_raw = raw_validation[indexes]
                    league_deltas[league] = (
                        _metrics(league_candidate, league_examples, target)["brier"]
                        - _metrics(league_raw, league_examples, target)["brier"]
                    )
                candidates.append(
                    {
                        "feature_set": feature_set,
                        "regularisation": regularisation,
                        "alpha": alpha,
                        "validation_brier": metrics["brier"],
                        "validation_log_loss": metrics["log_loss"],
                        "validation_league_brier_delta": league_deltas,
                    }
                )
    selected = min(
        candidates,
        key=lambda value: (
            value["validation_brier"],
            value["validation_log_loss"],
            FEATURE_SETS.index(value["feature_set"]),
            -value["regularisation"],
            value["alpha"],
        ),
    )
    raw_metrics = _metrics(raw_validation, validation, target)
    selected["raw_validation_brier"] = raw_metrics["brier"]
    selected["raw_validation_log_loss"] = raw_metrics["log_loss"]
    selected["validation_brier_delta"] = selected["validation_brier"] - raw_metrics["brier"]
    selected["validation_log_loss_delta"] = (
        selected["validation_log_loss"] - raw_metrics["log_loss"]
    )
    selected["nontrivial"] = selected["alpha"] > 0
    return selected


def evaluate_candidate(examples: Sequence[Example], *, target: str) -> dict:
    train = [example for example in examples if example.season in TRAIN_SEASONS]
    validation = [example for example in examples if example.season in VALIDATION_SEASONS]
    holdout = [example for example in examples if example.season in HOLDOUT_SEASONS]
    selection = _select_candidate(train, validation, target=target)
    final_fit = train + validation
    model = fit_calibrator(
        final_fit,
        target=target,
        feature_set=selection["feature_set"],
        regularisation=float(selection["regularisation"]),
    )
    _, _, raw = _matrix(holdout, feature_set="closing_only", target=target)
    candidate = _blend(
        raw,
        predict_calibrator(model, holdout),
        float(selection["alpha"]),
    )
    per_league: dict[str, dict] = {}
    all_differences: list[float] = []
    for league in LEAGUES:
        indexes = [index for index, example in enumerate(holdout) if example.league == league]
        league_examples = [holdout[index] for index in indexes]
        league_candidate = candidate[indexes]
        league_raw = raw[indexes]
        candidate_metrics = _metrics(league_candidate, league_examples, target)
        raw_metrics = _metrics(league_raw, league_examples, target)
        candidate_brier, _ = _loss_values(league_candidate, league_examples, target)
        raw_brier, _ = _loss_values(league_raw, league_examples, target)
        differences = [left - right for left, right in zip(candidate_brier, raw_brier)]
        all_differences.extend(differences)
        per_league[league] = {
            "sample_n": len(league_examples),
            "candidate": candidate_metrics,
            "raw_market": raw_metrics,
            "candidate_minus_market_brier": candidate_metrics["brier"] - raw_metrics["brier"],
            "candidate_minus_market_log_loss": (
                candidate_metrics["log_loss"] - raw_metrics["log_loss"]
            ),
            "paired_brier_ci": bootstrap_mean_ci(differences),
        }
    pooled_candidate = _metrics(candidate, holdout, target)
    pooled_raw = _metrics(raw, holdout, target)
    promoted = bool(
        selection["nontrivial"]
        and all(
            value["candidate_minus_market_brier"] < 0
            and value["candidate_minus_market_log_loss"] <= 0
            and value["paired_brier_ci"]["upper"] < 0
            for value in per_league.values()
        )
    )
    return {
        "target": target,
        "split": {
            "fit": sorted(TRAIN_SEASONS),
            "validation": sorted(VALIDATION_SEASONS),
            "holdout": sorted(HOLDOUT_SEASONS),
            "fit_n": len(train),
            "validation_n": len(validation),
            "holdout_n": len(holdout),
        },
        "selection": selection,
        "holdout": {
            "candidate": pooled_candidate,
            "raw_market": pooled_raw,
            "candidate_minus_market_brier": pooled_candidate["brier"] - pooled_raw["brier"],
            "candidate_minus_market_log_loss": (
                pooled_candidate["log_loss"] - pooled_raw["log_loss"]
            ),
            "paired_brier_ci": bootstrap_mean_ci(all_differences),
        },
        "leagues": per_league,
        "promotion_rule": (
            "nonzero validation-selected correction; every league must improve Brier and "
            "Log Loss on the designated final evaluation period; every paired Brier CI upper bound must be below zero"
        ),
        "promoted": promoted,
    }


def build_evidence(data_dir: Path, kickoff_enrichment: Path | None = None) -> dict:
    source = MatchHistorySource(
        data_dir,
        kickoff_enrichment_path=kickoff_enrichment,
    )
    rows_by_league: dict[str, list[dict]] = {}
    for league in LEAGUES:
        result = evaluate_strict_walk_forward(source.load(league).matches)
        rows_by_league[league] = result["rows"]
    targets = {
        target: evaluate_candidate(examples_from_rows(rows_by_league, target), target=target)
        for target in TARGET_LABELS
    }
    return {
        "schema_version": "1.0.0",
        "candidate": "strict_nested_pooled_market_dirichlet_calibration",
        "causal_protocol": (
            "2018/19-2019/20 fit; 2020 selects feature set, regularisation and blend; "
            "2021/22 onward designated final evaluation; all deltas paired on common complete-case rows; "
            "project-level selection audit treats this repeatedly inspected period as development evidence"
        ),
        "missing_policy": "complete-case only; no zero fill and no opening-to-closing substitution",
        "identity_policy": "calibrator is initialised and regularised toward the raw closing market",
        "targets": targets,
        "promoted": all(value["promoted"] for value in targets.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/MatchHistory"))
    parser.add_argument(
        "--kickoff-enrichment",
        type=Path,
        default=Path("docs/evidence/espn-kickoff-enrichment-2026-08-13.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = build_evidence(
        args.data_dir,
        args.kickoff_enrichment if args.kickoff_enrichment.exists() else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({target: value["promoted"] for target, value in evidence["targets"].items()}))


if __name__ == "__main__":
    main()


__all__ = [
    "Example",
    "evaluate_candidate",
    "examples_from_rows",
    "fit_calibrator",
    "predict_calibrator",
]
