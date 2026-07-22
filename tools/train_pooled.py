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
    """Run the pooled walk-forward evaluation and print an honest summary."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.machine_learning.pooled_training import load_pooled_rows, walk_forward_evaluate

    parser = argparse.ArgumentParser(description="Pooled walk-forward learning.")
    parser.add_argument("--models-root", type=Path, default=Path("data/models/per_session"))
    parser.add_argument("--out", type=Path, default=Path("data/models/pooled_walkforward.json"))
    parser.add_argument("--target-ticks", type=float, default=12.0)
    parser.add_argument("--stop-ticks", type=float, default=8.0)
    parser.add_argument("--cost-ticks", type=float, default=2.0)
    parser.add_argument("--min-train-days", type=int, default=3)
    args = parser.parse_args(argv)

    rows = load_pooled_rows(args.models_root)
    if not rows:
        print(f"no datasets under {args.models_root}; run tools.train_per_session --all first",
              file=sys.stderr)
        return 1

    result = walk_forward_evaluate(
        rows, target_ticks=args.target_ticks, stop_ticks=args.stop_ticks,
        cost_ticks=args.cost_ticks, min_train_days=args.min_train_days)

    print(f"pooled rows:            {result.total_rows:,} across {result.trading_days} trading days")
    print(f"walk-forward days:      {result.evaluated_days} (train on past, test on each)")
    print(f"out-of-sample trades:   {result.oos_predictions:,}")
    print(f"base rate (take all):   {result.base_rate:.1%} win  "
          f"-> expectancy {result.baseline_expectancy_ticks:+.2f} ticks/trade after costs")
    print(f"model accuracy (OOS):   {result.model_accuracy:.1%}")
    print(f"model TAKES:            {result.taken_trades:,} trades, {result.taken_win_rate:.1%} win "
          f"-> expectancy {result.expectancy_ticks:+.2f} ticks/trade after costs")
    print(f"beats take-everything:  {result.beats_baseline}")
    print(f"verdict: {result.note}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
