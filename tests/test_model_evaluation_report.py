"""Honest ML evaluation/decision-impact reporting (Team 6, ML evaluation).

Every synthetic prediction/decision/outcome fixture below exists only to prove
the report's identity+causal-timing linkage mechanics. It is never presented
as market, trading, or profitability evidence — tests that build such
fixtures pass ``evidence_scope="synthetic-mechanics-test"`` so the report
itself would carry that disclaimer if it were ever emitted for real.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.machine_learning.evaluation_report import (
    build_model_evaluation_report,
    render_model_evaluation_markdown,
    write_model_evaluation_report,
)
from app.machine_learning.outcome_journal import OutcomeRecord
from app.machine_learning.shadow_predictor import ShadowPrediction
from app.paper.streaming_engine import ConditionResult, EvaluationRecord

# -- Real-shaped dataset manifest fixtures (mirrors data/models/datasets/*) ---

CURRENT_DATASET = {
    "dataset_id": "dataset-test-current0000",
    "feature_contract_version": "shared-causal-market-features-v3",
    "feature_contract_sha256": "c" * 64,
    "label_contract_version": "causal-triple-barrier-v2-resolution-provenance",
    "rows_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "row_count": 0,
    "positive_labels": 0,
    "negative_labels": 0,
    "minimum_timestamp_ns": None,
    "maximum_timestamp_ns": None,
    "included_sessions": [],
    "excluded_sessions": [
        {
            "session_id": "session-2026-07-11",
            "manifest_path": "raw/session-2026-07-11/manifest.json",
            "reasons": ["no trades recorded (depth-only)"],
        },
        {
            "session_id": "session-2026-07-12",
            "manifest_path": "raw/session-2026-07-12/manifest.json",
            "reasons": ["continuity: websocket_closed"],
        },
    ],
}

HISTORICAL_DATASET = {
    "dataset_id": "dataset-test-historical0",
    "feature_contract_version": "shared-causal-market-features-v2",
    "feature_contract_sha256": "d" * 64,
    "label_contract_version": "causal-triple-barrier-v2-resolution-provenance",
    "rows_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "row_count": 0,
    "positive_labels": 0,
    "negative_labels": 0,
    "minimum_timestamp_ns": None,
    "maximum_timestamp_ns": None,
    "included_sessions": [],
    "excluded_sessions": [],
}

# -- Real-shaped attempt fixture (mirrors data/models/attempts/*.json) --------

REJECTED_ATTEMPT = {
    "attempt_id": "attempt-test0000000000001",
    "dataset_id": CURRENT_DATASET["dataset_id"],
    "model_type": "logistic_regression",
    "model_version": "0.1.0",
    "validation": {
        "base_rate": 0.0,
        "baseline_expectancy_ticks": 0.0,
        "beats_baseline": False,
        "brier_score": 0.0,
        "cost_ticks": "2.0",
        "evaluated_days": 0,
        "expectancy_ticks": 0.0,
        "fold_boundaries": [],
        "gates": {
            "both_label_classes": False,
            "has_rows": False,
            "minimum_evaluated_days": False,
            "minimum_oos_predictions": False,
            "positive_incremental_expectancy": False,
        },
        "minimum_oos_predictions": 50,
        "minimum_train_days": 3,
        "model_accuracy": 0.0,
        "note": "insufficient walk-forward data (need >= 4 trading days with both label classes)",
        "oos_predictions": 0,
        "scope": "offline walk-forward only; no runtime loading or decision impact",
        "stop_ticks": "8.0",
        "taken_trades": 0,
        "taken_win_rate": 0.0,
        "target_ticks": "12.0",
        "total_rows": 0,
        "trading_days": 0,
        "validation_state": "REJECTED",
    },
}

PASSED_ATTEMPT = {
    "attempt_id": "attempt-test0000000000002",
    "dataset_id": CURRENT_DATASET["dataset_id"],
    "model_type": "logistic_regression",
    "model_version": "0.1.0",
    "validation": {
        "base_rate": 0.52,
        "baseline_expectancy_ticks": 0.1,
        "beats_baseline": True,
        "brier_score": 0.19,
        "cost_ticks": "2.0",
        "evaluated_days": 5,
        "expectancy_ticks": 0.4,
        "fold_boundaries": [],
        "gates": {
            "both_label_classes": True,
            "has_rows": True,
            "minimum_evaluated_days": True,
            "minimum_oos_predictions": True,
            "positive_incremental_expectancy": True,
        },
        "minimum_oos_predictions": 50,
        "minimum_train_days": 3,
        "model_accuracy": 0.55,
        "note": "",
        "oos_predictions": 60,
        "scope": "offline walk-forward only; no runtime loading or decision impact",
        "stop_ticks": "8.0",
        "taken_trades": 12,
        "taken_win_rate": 0.58,
        "target_ticks": "12.0",
        "total_rows": 400,
        "trading_days": 5,
        "validation_state": "PASSED",
    },
}

PASSED_ARTIFACT = {
    "artifact_id": "challenger-test000000000",
    "artifact_sha256": "f" * 64,
    "dataset_id": CURRENT_DATASET["dataset_id"],
    "dataset_sha256": CURRENT_DATASET["rows_sha256"],
    "model_type": "logistic_regression",
    "model_version": "0.1.0",
    "validation_state": "PASSED",
    "validation_detail": "",
    "oos_predictions": 60,
    "artifact_path": "challengers/challenger-test000000000/model.joblib",
    "runtime_loaded": False,
}


def test_empty_report_is_honest_not_fabricated() -> None:
    report = build_model_evaluation_report()
    assert report.status == "FAILED_REQUIRES_REWORK"
    assert report.completion_claim is False
    assert report.datasets == ()
    assert report.attempts == ()
    assert report.artifacts == ()
    assert report.approval["exact_registry_match"] is False
    assert report.evaluation == {
        "attempts": 0,
        "passed_attempts": 0,
        "rejected_attempts": 0,
        "total_oos_predictions": 0,
        "total_evaluated_days": 0,
    }
    assert report.decisions["available"] is False
    assert "No dataset manifest evidence was supplied." in report.limitations
    assert "No validated registry artifact exists." in report.limitations
    markdown = render_model_evaluation_markdown(report)
    assert "N/A — no dataset manifests supplied" in markdown
    assert "Completion claimed: no" in markdown


def test_zero_row_current_dataset_reports_failed_requires_rework() -> None:
    """The real current-v3 corpus has zero eligible rows; this must read as a
    rejection, never as silent success."""
    report = build_model_evaluation_report(
        dataset_manifests=(CURRENT_DATASET, HISTORICAL_DATASET),
        attempts=(REJECTED_ATTEMPT,),
    )
    assert report.status == "FAILED_REQUIRES_REWORK"
    current = next(item for item in report.datasets if item["dataset_id"] == CURRENT_DATASET["dataset_id"])
    historical = next(item for item in report.datasets if item["dataset_id"] == HISTORICAL_DATASET["dataset_id"])
    assert current["current_feature_contract"] is True
    assert historical["current_feature_contract"] is False
    assert current["row_count"] == 0
    assert current["excluded_sessions"] == 2
    assert current["exclusion_reasons"] == {
        "continuity: websocket_closed": 1,
        "no trades recorded (depth-only)": 1,
    }
    assert "No real rows exist under the current feature contract; cross-session learning is not proven." in (
        report.limitations
    )


def test_rejected_attempt_metrics_are_unavailable_not_zero() -> None:
    report = build_model_evaluation_report(attempts=(REJECTED_ATTEMPT,))
    attempt = report.attempts[0]
    assert attempt["validation_state"] == "REJECTED"
    assert attempt["oos_predictions"] == 0
    assert attempt["base_rate"]["available"] is False
    assert attempt["base_rate"]["value"] is None
    # The real fixture's validation note explains the rejection; that note
    # becomes the unavailable-metric reason instead of a generic fallback.
    assert attempt["base_rate"]["reason"] == (
        "insufficient walk-forward data (need >= 4 trading days with both label classes)"
    )
    assert attempt["taken_win_rate"]["available"] is False
    # Undefined-forever metrics: never proven available regardless of gates.
    assert attempt["calibration"]["available"] is False
    assert attempt["drift"]["available"] is False

    # Confirm JSON serialization renders unavailable metrics as null, not 0.
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["attempts"][0]["base_rate"]["value"] is None
    assert payload["attempts"][0]["calibration"]["value"] is None

    markdown = render_model_evaluation_markdown(report)
    assert (
        "N/A — insufficient walk-forward data (need >= 4 trading days with both label classes)"
        in markdown
    )
    assert "N/A — calibration bins were not persisted with this attempt" in markdown


def test_passed_attempt_still_leaves_calibration_and_drift_unavailable() -> None:
    """A PASSED walk-forward attempt has real metrics, but calibration/drift
    are never fabricated because no bins or comparison distributions exist."""
    report = build_model_evaluation_report(attempts=(PASSED_ATTEMPT,))
    attempt = report.attempts[0]
    assert attempt["validation_state"] == "PASSED"
    assert attempt["base_rate"]["available"] is True
    assert attempt["base_rate"]["value"] == 0.52
    assert attempt["taken_win_rate"]["available"] is True
    assert attempt["calibration"]["available"] is False
    assert attempt["drift"]["available"] is False
    assert report.evaluation["passed_attempts"] == 1
    assert report.evaluation["total_oos_predictions"] == 60
    assert report.evaluation["total_evaluated_days"] == 5


def test_exact_registry_approval_match_and_mismatch() -> None:
    approval = {
        "schema_version": 1,
        "approved_artifact_id": PASSED_ARTIFACT["artifact_id"],
        "approved_sha256": PASSED_ARTIFACT["artifact_sha256"],
        "runtime_loading_enabled": False,
        "shadow_scoring_enabled": False,
    }
    report = build_model_evaluation_report(
        dataset_manifests=(CURRENT_DATASET,),
        registry_records=(PASSED_ARTIFACT,),
        approval=approval,
    )
    assert report.approval["exact_registry_match"] is True
    assert report.approval["runtime_loading_enabled"] is False

    mismatched_approval = {**approval, "approved_sha256": "0" * 64}
    mismatched = build_model_evaluation_report(
        dataset_manifests=(CURRENT_DATASET,),
        registry_records=(PASSED_ARTIFACT,),
        approval=mismatched_approval,
    )
    assert mismatched.approval["exact_registry_match"] is False
    assert "No exact approved artifact ID and SHA-256 match a registry record." in mismatched.limitations


def _shadow_prediction(**overrides: object) -> ShadowPrediction:
    base = dict(
        prediction_id="challenger-test-000000000001",
        artifact_id="challenger-test000000000",
        artifact_sha256="f" * 64,
        session_id="session-synthetic",
        timestamp_ns=1_000_000_000,
        direction="long",
        feature_vector_sha256="a" * 64,
        success_probability=0.6,
    )
    base.update(overrides)
    return ShadowPrediction(**base)  # type: ignore[arg-type]


def _decision(**overrides: object) -> EvaluationRecord:
    base = dict(
        session_id="session-synthetic",
        setup_id="setup-1",
        strategy_version="v1",
        direction="long",
        evaluated_at_ns=2_000_000_000,
        event_index=1,
        accepted=False,
        conditions=(ConditionResult(name="ml_policy", passed=False, message="ml_veto"),),
        rejection_reasons=("ml_veto",),
        decision_source="BLENDED",
        confidence=0.6,
        model_version="challenger-test000000000",
        model_prediction_id="challenger-test-000000000001",
        model_artifact_sha256="f" * 64,
    )
    base.update(overrides)
    return EvaluationRecord(**base)  # type: ignore[arg-type]


def _outcome(**overrides: object) -> OutcomeRecord:
    base = dict(
        prediction_id="challenger-test-000000000001",
        artifact_id="challenger-test000000000",
        artifact_sha256="f" * 64,
        session_id="session-synthetic",
        direction="long",
        entry_timestamp_ns=1_000_000_000,
        entry_price="29500.00",
        target_price="29503.00",
        stop_price="29498.00",
        success_probability=0.6,
        resolved_timestamp_ns=1_100_000_000,
        resolution_reason="target",
        label=1,
        resolution_latency_ns=100_000_000,
    )
    base.update(overrides)
    return OutcomeRecord(**base)  # type: ignore[arg-type]


def test_synthetic_linkage_mechanics_are_labeled_not_presented_as_evidence() -> None:
    prediction = _shadow_prediction()
    decision = _decision()
    outcome = _outcome()
    report = build_model_evaluation_report(
        prediction_records=(prediction,),
        decision_records=(decision,),
        outcome_records=(outcome,),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.status == "IN_PROGRESS"
    assert report.linkage["linked_prediction_decisions"] == 1
    assert report.linkage["identity_mismatches"] == 0
    assert report.linkage["causality_mismatches"] == 0
    assert report.linkage["resolved_linked_outcomes"] == 1
    assert report.linkage["policy_impact"]["avoided_losses"] == 0
    assert report.linkage["policy_impact"]["missed_wins"] == 1
    assert (
        "Synthetic evidence proves linkage mechanics only and is not market, trading, or profitability evidence."
        in report.limitations
    )
    markdown = render_model_evaluation_markdown(report)
    assert "not a live trading record and does not prove profitability" in markdown


def test_identity_mismatch_is_never_silently_linked() -> None:
    prediction = _shadow_prediction()
    decision = _decision(model_artifact_sha256="0" * 64)  # wrong SHA -> no identity match
    report = build_model_evaluation_report(
        prediction_records=(prediction,),
        decision_records=(decision,),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.linkage["model_referenced_decisions"] == 1
    assert report.linkage["identity_mismatches"] == 1
    assert report.linkage["linked_prediction_decisions"] == 0


def test_decision_before_prediction_is_a_causality_mismatch() -> None:
    prediction = _shadow_prediction(timestamp_ns=5_000_000_000)
    decision = _decision(evaluated_at_ns=1_000_000_000)  # earlier than the prediction
    report = build_model_evaluation_report(
        prediction_records=(prediction,),
        decision_records=(decision,),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.linkage["causality_mismatches"] == 1
    assert report.linkage["linked_prediction_decisions"] == 0


def test_unresolved_outcome_is_never_reported_as_resolved() -> None:
    prediction = _shadow_prediction()
    decision = _decision()
    outcome = _outcome(resolved_timestamp_ns=None, resolution_reason="session_end_unresolved", label=None,
                       resolution_latency_ns=None)
    report = build_model_evaluation_report(
        prediction_records=(prediction,),
        decision_records=(decision,),
        outcome_records=(outcome,),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.linkage["resolved_linked_outcomes"] == 0
    assert report.linkage["unresolved_linked_outcomes"] == 1
    assert report.linkage["policy_impact"]["available"] is False
    assert "No completed outcome is linked to an authoritative ML-referenced decision." in report.limitations


def test_missing_authoritative_decisions_is_a_named_limitation() -> None:
    report = build_model_evaluation_report(prediction_records=(_shadow_prediction(),))
    assert report.decisions["available"] is False
    assert (
        "No authoritative decision records were supplied; historical prediction-to-decision impact is unavailable."
        in report.limitations
    )


def test_journal_damage_flags_are_surfaced_as_limitations() -> None:
    report = build_model_evaluation_report(
        prediction_journal_damaged=True,
        outcome_journal_damaged=True,
    )
    assert report.linkage["prediction_journal_damaged"] is True
    assert report.linkage["outcome_journal_damaged"] is True
    assert "The prediction journal contains malformed or torn evidence." in report.limitations
    assert "The outcome journal contains malformed or torn evidence." in report.limitations


def test_registry_integrity_error_is_surfaced_verbatim() -> None:
    report = build_model_evaluation_report(registry_integrity_error="ValueError: tampered artifact hash")
    assert "Registry integrity validation failed: ValueError: tampered artifact hash" in report.limitations


def test_authoritative_evaluation_record_has_no_regime_field() -> None:
    """EvaluationRecord (the real authoritative decision type) carries no
    regime field; the report must say so rather than fabricate a breakdown."""
    decision = _decision()
    report = build_model_evaluation_report(
        decision_records=(decision,),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.decisions["regime_available"] is False
    assert report.decisions["regime_unavailable_reason"] == (
        "authoritative decision schema contains no regime field"
    )
    assert report.decisions["regime_breakdown"] == {}


def test_decision_source_and_fallback_breakdowns_are_counted() -> None:
    accepted_ml = _decision(accepted=True, decision_source="ML", fallback_reason="")
    fallback = _decision(
        accepted=True,
        decision_source="FALLBACK",
        fallback_reason="no approved model",
        model_prediction_id="",
        model_version="",
        model_artifact_sha256="",
        confidence=None,
    )
    report = build_model_evaluation_report(
        decision_records=(accepted_ml, fallback),
        evidence_scope="synthetic-mechanics-test",
    )
    assert report.decisions["total"] == 2
    assert report.decisions["accepted"] == 2
    assert report.decisions["source_counts"]["ML"] == 1
    assert report.decisions["source_counts"]["FALLBACK"] == 1
    assert report.decisions["fallback_breakdown"] == {"no approved model": 1}


def test_write_model_evaluation_report_writes_markdown_and_json(tmp_path: Path) -> None:
    report = build_model_evaluation_report(dataset_manifests=(CURRENT_DATASET,))
    markdown_path = write_model_evaluation_report(tmp_path, report)
    assert markdown_path == tmp_path / "model_evaluation_report.md"
    assert markdown_path.is_file()
    json_path = tmp_path / "model_evaluation_report.json"
    assert json_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == report.status
    assert payload["completion_claim"] is False


def test_cli_runs_against_real_shaped_on_disk_evidence(tmp_path: Path) -> None:
    """Mirrors tests/test_paper_daily_report.py::test_cli_runs_against_a_real_ledger:
    the CLI must read actual files from disk (not fixtures passed in-process) and
    produce a written report without fabricating anything."""
    from tools.model_evaluation_report import main

    models_root = tmp_path / "models"
    datasets_dir = models_root / "datasets" / CURRENT_DATASET["dataset_id"]
    datasets_dir.mkdir(parents=True)
    (datasets_dir / "dataset_manifest.json").write_text(
        json.dumps(CURRENT_DATASET), encoding="utf-8",
    )
    attempts_dir = models_root / "attempts"
    attempts_dir.mkdir(parents=True)
    (attempts_dir / f"{REJECTED_ATTEMPT['attempt_id']}.json").write_text(
        json.dumps(REJECTED_ATTEMPT), encoding="utf-8",
    )

    rc = main([
        "--models-root", str(models_root),
        "--model-approval", str(tmp_path / "absent_approval.yaml"),
        "--report-root", str(tmp_path / "reports"),
    ])
    assert rc == 0
    markdown_path = tmp_path / "reports" / "model_evaluation_report.md"
    assert markdown_path.is_file()
    payload = json.loads((tmp_path / "reports" / "model_evaluation_report.json").read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED_REQUIRES_REWORK"
    assert payload["completion_claim"] is False
    assert len(payload["datasets"]) == 1
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["validation_state"] == "REJECTED"


def test_cli_handles_missing_models_root_without_crashing(tmp_path: Path) -> None:
    """A brand-new checkout with no data/models directory yet must produce an
    honest empty report, not an error - matching the paper CLI's missing-ledger
    contract (tests/test_paper_daily_report.py::test_cli_handles_a_missing_ledger_without_crashing)."""
    from tools.model_evaluation_report import main

    rc = main([
        "--models-root", str(tmp_path / "absent_models"),
        "--model-approval", str(tmp_path / "absent_approval.yaml"),
        "--report-root", str(tmp_path / "reports"),
    ])
    assert rc == 0, "a missing models root is an honest empty report, not an error"
    payload = json.loads((tmp_path / "reports" / "model_evaluation_report.json").read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED_REQUIRES_REWORK"
    assert payload["completion_claim"] is False
    assert payload["datasets"] == []
    assert payload["attempts"] == []
