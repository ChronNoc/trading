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
    """Return only finalized sessions that pass the strict ML provenance gate."""
    from app.research.session_catalog import build_catalog

    return [
        entry.manifest_path.parent
        for entry in build_catalog(raw_root)
        if entry.eligible_for_model_training
    ]


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

    if args.session:
        from app.research.session_catalog import classify_manifest
        import json

        manifest_path = args.session / "session_manifest.json"
        if not manifest_path.is_file():
            print(f"session manifest missing: {manifest_path}", file=sys.stderr)
            return 1
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = classify_manifest(payload, manifest_path)
        if not entry.eligible_for_model_training:
            print(
                f"session is not eligible for model training: {'; '.join(entry.model_training_reasons)}",
                file=sys.stderr,
            )
            return 1
        sessions = [args.session]
    else:
        sessions = _discover_sessions(args.raw_root)
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
