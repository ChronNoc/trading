"""Generate the honest ML evaluation/decision-impact report from real evidence.

    .venv\\Scripts\\python.exe -m tools.model_evaluation_report

Read-only over dataset manifests, walk-forward attempts, the registry, the
approval file, and the prediction/outcome journals; writes only the report.
Never fabricates a metric, a linkage, or a completion claim: unavailable
evidence is reported as unavailable, not as zero or absent. Authoritative
paper-decision records are currently retained in memory only
(``DelayedPaperEngine.recent_evaluations()``), so this CLI cannot supply them
without a durable decision export — the report already names that as a
standing limitation rather than silently omitting it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from app.machine_learning.evaluation_report import (
    build_model_evaluation_report,
    write_model_evaluation_report,
)
from app.machine_learning.outcome_journal import OutcomeJournal
from app.machine_learning.registry import read_model_approval, read_registry
from app.machine_learning.shadow_predictor import PredictionJournal


def _dataset_manifests(models_root: Path) -> tuple[dict, ...]:
    datasets_dir = models_root / "datasets"
    if not datasets_dir.is_dir():
        return ()
    manifests: list[dict] = []
    for manifest_path in sorted(datasets_dir.glob("*/dataset_manifest.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            manifests.append(payload)
    return tuple(manifests)


def _attempts(models_root: Path) -> tuple[dict, ...]:
    attempts_dir = models_root / "attempts"
    if not attempts_dir.is_dir():
        return ()
    attempts: list[dict] = []
    for attempt_path in sorted(attempts_dir.glob("*.json")):
        payload = json.loads(attempt_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            attempts.append(payload)
    return tuple(attempts)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the report and echo it. Returns 0 (reporting is never a failure)."""
    parser = argparse.ArgumentParser(description="Honest ML evaluation/decision-impact report.")
    parser.add_argument("--models-root", type=Path, default=Path("data/models"))
    parser.add_argument("--model-approval", type=Path, default=Path("config/model_approval.yaml"))
    parser.add_argument("--report-root", type=Path, default=Path("data/reports/model_evaluation"))
    args = parser.parse_args(argv)

    dataset_manifests = _dataset_manifests(args.models_root)
    attempts = _attempts(args.models_root)

    registry_integrity_error = ""
    try:
        registry_records = read_registry(args.models_root)
    except Exception as exc:  # noqa: BLE001 - a tampered registry must still produce a report
        registry_records = ()
        registry_integrity_error = f"{type(exc).__name__}: {exc}"

    approval = read_model_approval(args.model_approval)

    prediction_recovery = PredictionJournal.recover(args.models_root / "shadow_predictions.jsonl")
    outcome_recovery = OutcomeJournal.recover(args.models_root / "shadow_outcomes.jsonl")

    report = build_model_evaluation_report(
        dataset_manifests=dataset_manifests,
        attempts=attempts,
        registry_records=registry_records,
        approval=approval,
        prediction_records=prediction_recovery.records,
        # Authoritative paper decisions are in-memory only today (no durable
        # decision export exists); never fabricate a decision record here.
        decision_records=(),
        outcome_records=outcome_recovery.records,
        prediction_journal_damaged=prediction_recovery.damaged_tail,
        outcome_journal_damaged=outcome_recovery.damaged_tail,
        registry_integrity_error=registry_integrity_error,
    )

    markdown_path = write_model_evaluation_report(args.report_root, report)
    markdown = markdown_path.read_text(encoding="utf-8")
    try:
        print(markdown)
    except UnicodeEncodeError:
        # A narrow console codepage (e.g. Windows cp1252) must never make
        # reporting fail; the report file itself is already written as UTF-8.
        sys.stdout.buffer.write(markdown.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
    print(f"Written: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
