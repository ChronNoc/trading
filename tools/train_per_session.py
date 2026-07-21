"""Train one model per recorded session via causal triple-barrier labeling.

    .venv\\Scripts\\python.exe -m tools.train_per_session --all
    .venv\\Scripts\\python.exe -m tools.train_per_session --session data/raw/2026-07-17/session_...

Each session is labeled and trained independently; a session that cannot yield
both win and loss labels is reported honestly and skipped (never fabricated).
Models and reports are written under --output-root (default data/models/per_session/).
No profitability is claimed; per-session models are in-sample and descriptive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def _discover_sessions(raw_root: Path) -> list[Path]:
    return sorted({p.parent for p in raw_root.rglob("trades.parquet")})


def main(argv: Sequence[str] | None = None) -> int:
    """Train per-session models for one session or every recorded session."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.machine_learning.session_training import (
        SessionTrainingConfig,
        train_session_model,
    )

    parser = argparse.ArgumentParser(description="Per-session triple-barrier training.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", type=Path, help="one session directory")
    group.add_argument("--all", action="store_true", help="every session under --raw-root")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-root", type=Path, default=Path("data/models/per_session"))
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--target-ticks", type=int, default=12)
    parser.add_argument("--stop-ticks", type=int, default=8)
    parser.add_argument("--horizon-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)

    from decimal import Decimal

    config = SessionTrainingConfig(
        target_ticks=Decimal(args.target_ticks),
        stop_ticks=Decimal(args.stop_ticks),
        horizon_seconds=args.horizon_seconds,
    )

    sessions = [args.session] if args.session else _discover_sessions(args.raw_root)
    if not sessions:
        print("no sessions found", file=sys.stderr)
        return 1

    trained = 0
    errored = 0
    for session in sessions:
        if not session.is_dir():
            print(f"SKIP     {session} (not a directory)", flush=True)
            continue
        # One corrupt or unreadable session must never abort the whole run.
        try:
            summary, report = train_session_model(
                session, args.output_root, config=config, version=args.version)
        except Exception as error:  # noqa: BLE001 - report and continue, never crash
            errored += 1
            print(f"ERROR    {session.name}: {type(error).__name__}: {error}", flush=True)
            continue
        status = "TRAINED" if report["trained"] else "skipped"
        trained += 1 if report["trained"] else 0
        print(f"{status:8s} {summary.session_id}: rows={summary.rows} "
              f"win={summary.wins} loss={summary.losses} "
              f"dropped={summary.dropped_incomplete} | {summary.note}", flush=True)
    print(f"\n{trained}/{len(sessions)} session(s) trained "
          f"({errored} errored); reports under {args.output_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
