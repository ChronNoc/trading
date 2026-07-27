"""Honest ML lifecycle and decision/outcome evidence reporting.

The report deliberately distinguishes observed zero counts from metrics that are
undefined because no observations exist. Prediction journal metadata is never
interpreted as decision impact: impact requires an authoritative paper decision
record joined to the exact prediction and outcome identities.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.machine_learning.feature_contract import FEATURE_CONTRACT_VERSION

REPORT_SCHEMA_VERSION = 1
_DECISION_SOURCES = ("HEURISTIC", "ML", "BLENDED", "FALLBACK")


@dataclass(frozen=True, slots=True)
class ModelEvaluationReport:
    """Immutable, JSON-friendly summary of available ML evidence."""

    schema_version: int
    status: str
    evidence_scope: str
    completion_claim: bool
    datasets: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    approval: dict[str, Any]
    evaluation: dict[str, Any]
    decisions: dict[str, Any]
    linkage: dict[str, Any]
    limitations: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable copy."""
        return asdict(self)


def build_model_evaluation_report(
    *,
    dataset_manifests: Sequence[object] = (),
    attempts: Sequence[object] = (),
    registry_records: Sequence[object] = (),
    approval: object | None = None,
    prediction_records: Sequence[object] = (),
    decision_records: Sequence[object] = (),
    outcome_records: Sequence[object] = (),
    evidence_scope: str = "real-market",
    prediction_journal_damaged: bool = False,
    outcome_journal_damaged: bool = False,
    registry_integrity_error: str = "",
) -> ModelEvaluationReport:
    """Build a report without inventing unavailable metrics or relationships.

    ``decision_records`` must come from the authoritative paper decision path
    (currently ``DelayedPaperEngine.recent_evaluations()`` or an explicitly
    supplied record export). The prediction and outcome journals alone cannot
    prove that ML affected a decision.
    """
    datasets = tuple(
        sorted(
            (_dataset_summary(_record(item)) for item in dataset_manifests),
            key=lambda item: str(item["dataset_id"]),
        )
    )
    attempt_summaries = tuple(
        sorted(
            (_attempt_summary(_record(item)) for item in attempts),
            key=lambda item: str(item["attempt_id"]),
        )
    )
    artifacts = tuple(
        sorted(
            (_artifact_summary(_record(item)) for item in registry_records),
            key=lambda item: str(item["artifact_id"]),
        )
    )
    approval_summary = _approval_summary(_record(approval) if approval is not None else {}, artifacts)

    predictions = tuple(_record(item) for item in prediction_records)
    decisions = tuple(_record(item) for item in decision_records)
    outcomes = tuple(_record(item) for item in outcome_records)

    evaluation = _evaluation_summary(attempt_summaries)
    decision_summary = _decision_summary(decisions)
    linkage = _linkage_summary(predictions, decisions, outcomes)
    linkage["prediction_journal_damaged"] = prediction_journal_damaged
    linkage["outcome_journal_damaged"] = outcome_journal_damaged

    current_rows = sum(
        int(item["row_count"])
        for item in datasets
        if item["current_feature_contract"]
    )
    passed_artifacts = sum(
        1 for item in artifacts if item["validation_state"] == "PASSED"
    )
    synthetic = evidence_scope != "real-market"
    if synthetic:
        status = "IN_PROGRESS"
    elif current_rows == 0 or passed_artifacts == 0:
        status = "FAILED_REQUIRES_REWORK"
    else:
        status = "IN_PROGRESS"

    limitations: list[str] = []
    if not datasets:
        limitations.append("No dataset manifest evidence was supplied.")
    elif current_rows == 0:
        limitations.append(
            "No real rows exist under the current feature contract; cross-session learning is not proven."
        )
    if not artifacts:
        limitations.append("No validated registry artifact exists.")
    if not approval_summary["exact_registry_match"]:
        limitations.append("No exact approved artifact ID and SHA-256 match a registry record.")
    if not decisions:
        limitations.append(
            "No authoritative decision records were supplied; historical prediction-to-decision impact is unavailable."
        )
    if not linkage["resolved_linked_outcomes"]:
        limitations.append("No completed outcome is linked to an authoritative ML-referenced decision.")
    if prediction_journal_damaged:
        limitations.append("The prediction journal contains malformed or torn evidence.")
    if outcome_journal_damaged:
        limitations.append("The outcome journal contains malformed or torn evidence.")
    if registry_integrity_error:
        limitations.append(f"Registry integrity validation failed: {registry_integrity_error}")
    if synthetic:
        limitations.append(
            "Synthetic evidence proves linkage mechanics only and is not market, trading, or profitability evidence."
        )
    limitations.append(
        "Authoritative decisions are currently retained in memory; durable cross-runtime linkage requires an explicit decision export."
    )

    return ModelEvaluationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        status=status,
        evidence_scope=evidence_scope,
        completion_claim=False,
        datasets=datasets,
        attempts=attempt_summaries,
        artifacts=artifacts,
        approval=approval_summary,
        evaluation=evaluation,
        decisions=decision_summary,
        linkage=linkage,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def render_model_evaluation_markdown(report: ModelEvaluationReport) -> str:
    """Render the report while marking unsupported measurements as N/A."""
    lines = [
        "# ML Evaluation and Decision-Impact Report",
        "",
        "_Paper/shadow evidence only. This is not a live trading record and does not prove profitability._",
        "",
        "## Status",
        f"- Persistent status: {report.status}",
        f"- Evidence scope: {report.evidence_scope}",
        "- Completion claimed: no",
        "",
        "## Datasets",
    ]
    if not report.datasets:
        lines.append("- N/A — no dataset manifests supplied")
    for item in report.datasets:
        current = "current contract" if item["current_feature_contract"] else "historical contract"
        lines.extend(
            [
                f"- `{item['dataset_id']}` ({current})",
                f"  - Feature contract: `{item['feature_contract_version']}` / `{item['feature_contract_sha256']}`",
                f"  - Rows: {item['row_count']}; labels 1/0: {item['positive_labels']}/{item['negative_labels']}",
                f"  - Included/excluded sessions: {item['included_sessions']}/{item['excluded_sessions']}",
                f"  - Rows SHA-256: `{item['rows_sha256']}`",
            ]
        )

    lines.extend(["", "## Lifecycle attempts and artifacts"])
    if not report.attempts:
        lines.append("- N/A — no attempt evidence supplied")
    for item in report.attempts:
        lines.extend(
            [
                f"- Attempt `{item['attempt_id']}`: {item['validation_state']}",
                f"  - Dataset: `{item['dataset_id']}`; OOS predictions: {item['oos_predictions']}; evaluated days: {item['evaluated_days']}",
                f"  - Base rate: {_metric_text(item['base_rate'])}",
                f"  - Accuracy: {_metric_text(item['model_accuracy'])}",
                f"  - Brier score: {_metric_text(item['brier_score'])}",
                f"  - Taken-trade win rate: {_metric_text(item['taken_win_rate'])}",
                f"  - Model / baseline expectancy: {_metric_text(item['expectancy_ticks'])} / {_metric_text(item['baseline_expectancy_ticks'])}",
                f"  - Calibration: {_metric_text(item['calibration'])}",
                f"  - Drift: {_metric_text(item['drift'])}",
            ]
        )
    if not report.artifacts:
        lines.append("- Published registry artifacts: 0")
    else:
        for item in report.artifacts:
            lines.append(
                f"- Artifact `{item['artifact_id']}` SHA `{item['artifact_sha256']}` ({item['validation_state']})"
            )
    lines.extend(
        [
            f"- Exact approved registry artifact: {'yes' if report.approval['exact_registry_match'] else 'no'}",
            f"- Runtime loading enabled: {report.approval['runtime_loading_enabled']}",
            f"- Shadow scoring enabled: {report.approval['shadow_scoring_enabled']}",
            "",
            "## Authoritative paper decisions",
            f"- Records supplied: {report.decisions['total']}",
            f"- Accepted / rejected: {report.decisions['accepted']}/{report.decisions['rejected']}",
        ]
    )
    for source in _DECISION_SOURCES:
        lines.append(f"- {source}: {report.decisions['source_counts'][source]}")
    if not report.decisions["available"]:
        lines.append(f"- N/A — {report.decisions['reason']}")

    lines.extend(
        [
            "",
            "## Prediction → decision → outcome linkage",
            f"- Predictions / decisions / outcomes supplied: {report.linkage['predictions']}/{report.linkage['decisions']}/{report.linkage['outcomes']}",
            f"- Exactly linked prediction-decisions: {report.linkage['linked_prediction_decisions']}",
            f"- Identity/causality mismatches: {report.linkage['identity_mismatches']}/{report.linkage['causality_mismatches']}",
            f"- Resolved / unresolved linked outcomes: {report.linkage['resolved_linked_outcomes']}/{report.linkage['unresolved_linked_outcomes']}",
            f"- ML vetoes with outcomes — avoided losses / missed wins / unresolved: {report.linkage['policy_impact']['avoided_losses']}/{report.linkage['policy_impact']['missed_wins']}/{report.linkage['policy_impact']['unresolved_vetoes']}",
        ]
    )
    if not report.linkage["policy_impact"]["available"]:
        lines.append(f"- Outcome-linked policy impact: N/A — {report.linkage['policy_impact']['reason']}")

    lines.extend(["", "## Limitations"])
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines) + "\n"


def write_model_evaluation_report(report_root: Path, report: ModelEvaluationReport) -> Path:
    """Write deterministic Markdown and JSON report files."""
    directory = Path(report_root)
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / "model_evaluation_report.md"
    markdown_path.write_text(render_model_evaluation_markdown(report), encoding="utf-8")
    (directory / "model_evaluation_report.json").write_text(
        json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return markdown_path


def _record(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    converter = getattr(value, "to_record", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return {str(key): item for key, item in converted.items()}
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        return {str(key): item for key, item in converted.items()}
    raise TypeError(f"report evidence must be a mapping or dataclass, got {type(value).__name__}")


def _dataset_summary(payload: Mapping[str, object]) -> dict[str, Any]:
    excluded = payload.get("excluded_sessions", ())
    included = payload.get("included_sessions", ())
    excluded_items = excluded if isinstance(excluded, Sequence) and not isinstance(excluded, str) else ()
    included_items = included if isinstance(included, Sequence) and not isinstance(included, str) else ()
    reasons: Counter[str] = Counter()
    for item in excluded_items:
        if not isinstance(item, Mapping):
            continue
        raw_reasons = item.get("reasons", ())
        if isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, str):
            reasons.update(str(reason) for reason in raw_reasons)
    version = str(payload.get("feature_contract_version", ""))
    return {
        "dataset_id": str(payload.get("dataset_id", "")),
        "feature_contract_version": version,
        "feature_contract_sha256": str(payload.get("feature_contract_sha256", "")),
        "label_contract_version": str(payload.get("label_contract_version", "")),
        "rows_sha256": str(payload.get("rows_sha256", "")),
        "row_count": _integer(payload.get("row_count")),
        "positive_labels": _integer(payload.get("positive_labels")),
        "negative_labels": _integer(payload.get("negative_labels")),
        "minimum_timestamp_ns": payload.get("minimum_timestamp_ns"),
        "maximum_timestamp_ns": payload.get("maximum_timestamp_ns"),
        "included_sessions": len(included_items),
        "excluded_sessions": len(excluded_items),
        "exclusion_reasons": dict(sorted(reasons.items())),
        "current_feature_contract": version == FEATURE_CONTRACT_VERSION,
    }


def _attempt_summary(payload: Mapping[str, object]) -> dict[str, Any]:
    validation = payload.get("validation", {})
    values = validation if isinstance(validation, Mapping) else {}
    oos = _integer(values.get("oos_predictions"))
    taken = _integer(values.get("taken_trades"))
    evaluated_days = _integer(values.get("evaluated_days"))
    rows = _integer(values.get("total_rows"))
    note = str(values.get("note", ""))
    no_oos_reason = note or "no out-of-sample predictions"
    no_trades_reason = note or "no model-selected trades"
    return {
        "attempt_id": str(payload.get("attempt_id", "")),
        "dataset_id": str(payload.get("dataset_id", "")),
        "model_type": str(payload.get("model_type", "")),
        "model_version": str(payload.get("model_version", "")),
        "validation_state": str(values.get("validation_state", "UNKNOWN")),
        "note": note,
        "gates": dict(values.get("gates", {})) if isinstance(values.get("gates"), Mapping) else {},
        "total_rows": rows,
        "trading_days": _integer(values.get("trading_days")),
        "evaluated_days": evaluated_days,
        "oos_predictions": oos,
        "fold_boundaries": list(values.get("fold_boundaries", ())) if isinstance(values.get("fold_boundaries"), Sequence) else [],
        "base_rate": _metric(values.get("base_rate"), oos > 0, no_oos_reason),
        "model_accuracy": _metric(values.get("model_accuracy"), oos > 0, no_oos_reason),
        "brier_score": _metric(values.get("brier_score"), oos > 0, no_oos_reason),
        "taken_trades": taken,
        "taken_win_rate": _metric(values.get("taken_win_rate"), taken > 0, no_trades_reason),
        "expectancy_ticks": _metric(values.get("expectancy_ticks"), taken > 0, no_trades_reason),
        "baseline_expectancy_ticks": _metric(values.get("baseline_expectancy_ticks"), oos > 0, no_oos_reason),
        "beats_baseline": _metric(values.get("beats_baseline"), oos > 0 and taken > 0, no_oos_reason),
        "calibration": _metric(None, False, "calibration bins were not persisted with this attempt"),
        "drift": _metric(None, False, "no comparable training/live feature distributions were supplied"),
    }


def _artifact_summary(payload: Mapping[str, object]) -> dict[str, Any]:
    return {
        "artifact_id": str(payload.get("artifact_id", "")),
        "artifact_sha256": str(payload.get("artifact_sha256", "")),
        "dataset_id": str(payload.get("dataset_id", "")),
        "dataset_sha256": str(payload.get("dataset_sha256", "")),
        "model_type": str(payload.get("model_type", "")),
        "model_version": str(payload.get("model_version", "")),
        "validation_state": str(payload.get("validation_state", "UNKNOWN")),
        "validation_detail": str(payload.get("validation_detail", "")),
        "oos_predictions": _integer(payload.get("oos_predictions")),
        "artifact_path": str(payload.get("artifact_path", "")),
        "runtime_loaded": bool(payload.get("runtime_loaded", False)),
    }


def _approval_summary(payload: Mapping[str, object], artifacts: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    artifact_id = payload.get("approved_artifact_id")
    artifact_sha = payload.get("approved_sha256")
    exact = bool(artifact_id and artifact_sha) and any(
        item["artifact_id"] == artifact_id and item["artifact_sha256"] == artifact_sha
        for item in artifacts
    )
    return {
        "schema_version": _integer(payload.get("schema_version"), default=1),
        "approved_artifact_id": artifact_id,
        "approved_sha256": artifact_sha,
        "runtime_loading_enabled": bool(payload.get("runtime_loading_enabled", False)),
        "shadow_scoring_enabled": bool(payload.get("shadow_scoring_enabled", False)),
        "exact_registry_match": exact,
    }


def _evaluation_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "attempts": len(attempts),
        "passed_attempts": sum(1 for item in attempts if item["validation_state"] == "PASSED"),
        "rejected_attempts": sum(1 for item in attempts if item["validation_state"] == "REJECTED"),
        "total_oos_predictions": sum(int(item["oos_predictions"]) for item in attempts),
        "total_evaluated_days": sum(int(item["evaluated_days"]) for item in attempts),
    }


def _decision_summary(decisions: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    source_counts: Counter[str] = Counter()
    sessions: dict[str, Counter[str]] = defaultdict(Counter)
    fallbacks: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    regimes: Counter[str] = Counter()
    accepted = 0
    for decision in decisions:
        source = str(decision.get("decision_source", "HEURISTIC"))
        source_counts[source] += 1
        session = str(decision.get("session_id", "")) or "unknown"
        sessions[session][source] += 1
        is_accepted = bool(decision.get("accepted", False))
        accepted += int(is_accepted)
        reason = str(decision.get("fallback_reason", ""))
        if reason:
            fallbacks[reason] += 1
        raw_rejections = decision.get("rejection_reasons", ())
        if isinstance(raw_rejections, Sequence) and not isinstance(raw_rejections, str):
            rejections.update(str(item) for item in raw_rejections)
        regime = str(decision.get("regime", ""))
        if regime:
            regimes[regime] += 1
    return {
        "available": bool(decisions),
        "reason": "" if decisions else "authoritative paper decision evidence was not supplied",
        "total": len(decisions),
        "accepted": accepted,
        "rejected": len(decisions) - accepted,
        "source_counts": {source: source_counts[source] for source in _DECISION_SOURCES},
        "unknown_source_counts": dict(sorted((key, value) for key, value in source_counts.items() if key not in _DECISION_SOURCES)),
        "session_breakdown": {
            session: {source: counts[source] for source in _DECISION_SOURCES}
            for session, counts in sorted(sessions.items())
        },
        "fallback_breakdown": dict(sorted(fallbacks.items())),
        "rejection_breakdown": dict(sorted(rejections.items())),
        "regime_breakdown": dict(sorted(regimes.items())),
        "regime_available": bool(regimes),
        "regime_unavailable_reason": "" if regimes else "authoritative decision schema contains no regime field",
    }


def _linkage_summary(
    predictions: Sequence[Mapping[str, object]],
    decisions: Sequence[Mapping[str, object]],
    outcomes: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    prediction_by_id = {
        str(item.get("prediction_id", "")): item
        for item in predictions
        if item.get("prediction_id")
    }
    outcomes_by_id: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for item in outcomes:
        prediction_id = str(item.get("prediction_id", ""))
        if prediction_id:
            outcomes_by_id[prediction_id].append(item)

    referenced = linked = identity_mismatches = causality_mismatches = 0
    resolved = unresolved = 0
    source_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    avoided_losses = missed_wins = unresolved_vetoes = 0

    for decision in decisions:
        prediction_id = str(decision.get("model_prediction_id", ""))
        if not prediction_id:
            continue
        referenced += 1
        prediction = prediction_by_id.get(prediction_id)
        if prediction is None or not _decision_identity_matches(decision, prediction):
            identity_mismatches += 1
            continue
        prediction_timestamp = _integer(prediction.get("timestamp_ns"), default=-1)
        decision_timestamp = _integer(decision.get("evaluated_at_ns"), default=-1)
        if prediction_timestamp < 0 or decision_timestamp < prediction_timestamp:
            causality_mismatches += 1
            continue
        linked += 1
        source = str(decision.get("decision_source", "HEURISTIC"))
        matched_outcome = None
        for outcome in outcomes_by_id.get(prediction_id, ()):
            if _outcome_identity_matches(prediction, outcome):
                matched_outcome = outcome
                break
        if matched_outcome is None:
            continue
        label = matched_outcome.get("label")
        resolved_timestamp = matched_outcome.get("resolved_timestamp_ns")
        entry_timestamp = _integer(matched_outcome.get("entry_timestamp_ns"), default=-1)
        if entry_timestamp != prediction_timestamp:
            causality_mismatches += 1
            continue
        if label is None or resolved_timestamp is None:
            unresolved += 1
            source_outcomes[source]["unresolved"] += 1
            if source == "BLENDED" and not bool(decision.get("accepted", False)):
                unresolved_vetoes += 1
            continue
        if _integer(resolved_timestamp, default=-1) < entry_timestamp or label not in (0, 1):
            causality_mismatches += 1
            continue
        resolved += 1
        source_outcomes[source]["label_1" if label == 1 else "label_0"] += 1
        if source == "BLENDED" and not bool(decision.get("accepted", False)):
            if label == 0:
                avoided_losses += 1
            else:
                missed_wins += 1

    policy_observations = avoided_losses + missed_wins
    return {
        "predictions": len(predictions),
        "decisions": len(decisions),
        "outcomes": len(outcomes),
        "model_referenced_decisions": referenced,
        "linked_prediction_decisions": linked,
        "identity_mismatches": identity_mismatches,
        "causality_mismatches": causality_mismatches,
        "resolved_linked_outcomes": resolved,
        "unresolved_linked_outcomes": unresolved,
        "source_outcome_breakdown": {
            source: {
                "label_1": counts["label_1"],
                "label_0": counts["label_0"],
                "unresolved": counts["unresolved"],
            }
            for source, counts in sorted(source_outcomes.items())
        },
        "policy_impact": {
            "available": policy_observations > 0,
            "reason": "" if policy_observations > 0 else "no resolved outcome is linked to an authoritative ML veto",
            "avoided_losses": avoided_losses,
            "missed_wins": missed_wins,
            "unresolved_vetoes": unresolved_vetoes,
        },
    }


def _decision_identity_matches(decision: Mapping[str, object], prediction: Mapping[str, object]) -> bool:
    return (
        str(decision.get("model_prediction_id", "")) == str(prediction.get("prediction_id", ""))
        and str(decision.get("model_version", "")) == str(prediction.get("artifact_id", ""))
        and str(decision.get("model_artifact_sha256", "")) == str(prediction.get("artifact_sha256", ""))
        and str(decision.get("session_id", "")) == str(prediction.get("session_id", ""))
        and str(decision.get("direction", "")) == str(prediction.get("direction", ""))
    )


def _outcome_identity_matches(prediction: Mapping[str, object], outcome: Mapping[str, object]) -> bool:
    return all(
        str(outcome.get(key, "")) == str(prediction.get(source_key, ""))
        for key, source_key in (
            ("prediction_id", "prediction_id"),
            ("artifact_id", "artifact_id"),
            ("artifact_sha256", "artifact_sha256"),
            ("session_id", "session_id"),
            ("direction", "direction"),
        )
    )


def _metric(value: object, available: bool, reason: str) -> dict[str, Any]:
    return {"available": available, "value": value if available else None, "reason": "" if available else reason}


def _metric_text(metric: object) -> str:
    if not isinstance(metric, Mapping) or not metric.get("available"):
        reason = str(metric.get("reason", "unavailable")) if isinstance(metric, Mapping) else "unavailable"
        return f"N/A — {reason}"
    return str(metric.get("value"))


def _integer(value: object, *, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
