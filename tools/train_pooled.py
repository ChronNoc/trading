"""Pooled walk-forward learning across all recorded sessions.

    .venv\\Scripts\\python.exe -m tools.train_pooled

Reads every per-session dataset under --models-root (build them first with
tools.train_per_session --all), pools them, and runs an honest walk-forward
out-of-sample test. Prints whether the model beats taking every trade after
costs. Makes no profitability claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Build one provenance-gated offline challenger or report rejection."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.machine_learning.challenger_pipeline import (
        DatasetBuildConfig,
        build_validated_challenger,
    )
    from app.machine_learning.session_training import SessionTrainingConfig
    from decimal import Decimal

    parser = argparse.ArgumentParser(description="Build an offline pooled challenger.")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--models-root", type=Path, default=Path("data/models"))
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--target-ticks", type=float, default=12.0)
    parser.add_argument("--stop-ticks", type=float, default=8.0)
    parser.add_argument("--cost-ticks", type=float, default=2.0)
    parser.add_argument("--horizon-seconds", type=float, default=300.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=30.0)
    parser.add_argument("--min-train-days", type=int, default=3)
    parser.add_argument("--min-oos-predictions", type=int, default=50)
    args = parser.parse_args(argv)

    config = DatasetBuildConfig(SessionTrainingConfig(
        target_ticks=Decimal(str(args.target_ticks)),
        stop_ticks=Decimal(str(args.stop_ticks)),
        horizon_seconds=args.horizon_seconds,
        sample_interval_seconds=args.sample_interval_seconds,
    ))
    result = build_validated_challenger(
        args.raw_root, args.models_root, config=config, model_version=args.version,
        target_ticks=args.target_ticks, stop_ticks=args.stop_ticks,
        cost_ticks=args.cost_ticks, min_train_days=args.min_train_days,
        min_oos_predictions=args.min_oos_predictions,
    )
    validation = result["validation"]
    print(f"dataset:                {result['dataset_id']}")
    print(f"status:                 {result['status']}")
    print(f"out-of-sample trades:   {validation['oos_predictions']}")
    print(f"brier score:            {validation['brier_score']}")
    print(f"beats take-everything:  {validation['beats_baseline']}")
    print("runtime loaded:         False")
    print("decision impact:        none")
    if result["status"] != "challenger":
        print(f"verdict: {validation['note']}", file=sys.stderr)
        return 2
    print(f"challenger:             {result['artifact_id']}")
    print("approval:               NOT APPROVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
