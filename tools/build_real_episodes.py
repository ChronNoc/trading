"""Build real setup episodes and outcome labels from recorded sessions.

Runs the no-lookahead episode builder over every FINALIZED, eligible,
non-synthetic session in ``data/raw/`` (active recordings are skipped) and
writes traceable episodes to ``data/processed/`` and labels to
``data/labels/``. Reports exact counts and rejection reasons. Zero accepted
or completed trades is a valid, honest result.

    python -m tools.build_real_episodes
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from app.research.causal_context import CausalLevelTracker
from app.research.episode_builder import BuildResult, build_episodes, write_episode_artifacts
from app.research.session_catalog import build_catalog

DEFAULT_RAW_ROOT = Path("data/raw")
DEFAULT_PROCESSED_ROOT = Path("data/processed")
DEFAULT_LABELS_ROOT = Path("data/labels")


def build_all(
    raw_root: Path,
    processed_root: Path,
    labels_root: Path,
) -> tuple[list[BuildResult], Counter[str]]:
    """Build episodes for every eligible finalized session; return results."""
    catalog = build_catalog(raw_root)
    results: list[BuildResult] = []
    rejected_totals: Counter[str] = Counter()
    level_tracker = CausalLevelTracker()
    for entry in sorted(catalog, key=lambda item: (item.utc_start or "", item.session_id)):
        if entry.active:
            continue  # never open an actively-recording session
        if not entry.eligible_for_order_flow_replay:
            continue
        session_dir = entry.manifest_path.parent
        result = build_episodes(
            session_dir,
            session_id=entry.session_id,
            provenance=entry.provenance,
            level_tracker=level_tracker,
        )
        write_episode_artifacts(result, processed_root=processed_root, labels_root=labels_root)
        rejected_totals.update(result.rejected_condition_tally)
        results.append(result)
    return results, rejected_totals


def main(argv: Sequence[str] | None = None) -> int:
    """Build real episodes and print an honest summary."""
    parser = argparse.ArgumentParser(description="Build real setup episodes from recorded sessions.")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--processed-root", type=Path, default=DEFAULT_PROCESSED_ROOT)
    parser.add_argument("--labels-root", type=Path, default=DEFAULT_LABELS_ROOT)
    args = parser.parse_args(argv)

    results, rejected = build_all(args.raw_root, args.processed_root, args.labels_root)

    eligible_sessions = len(results)
    evaluations = sum(r.evaluations for r in results)
    accepted = sum(r.accepted_candidates for r in results)
    completed = sum(r.completed for r in results)
    ledger_eligible = sum(r.ledger_eligible_count for r in results)
    ambiguous = sum(r.ambiguous for r in results)
    unfinished = sum(r.unfinished for r in results)
    collisions = sum(r.same_timestamp_collisions for r in results)

    print("=== REAL EPISODE BUILD (delayed Bookmap data, offline) ===")
    print(f"eligible finalized sessions processed: {eligible_sessions}")
    print(f"strategy evaluations (long+short): {evaluations}")
    print(f"accepted setup candidates: {accepted}")
    print(f"completed outcomes before provenance/quality gates: {completed}")
    print(f"completed outcomes passing provenance/quality gates: {ledger_eligible}")
    print(f"excluded - ambiguous: {ambiguous}  unfinished: {unfinished}")
    print(f"same-timestamp depth/trade collisions (v1 ambiguity): {collisions}")
    quality_failed = sum(1 for result in results if not result.data_quality_ok)
    ambiguous_ordering = sum(1 for result in results if result.ordering_ambiguous)
    print(f"sessions failing replay continuity checks: {quality_failed}")
    print(f"old sessions with timestamp-order ambiguity: {ambiguous_ordering}")
    if accepted == 0:
        print("\nNo setup was accepted. Top rejection reasons (condition: count):")
        for key, count in rejected.most_common(10):
            print(f"  {key}: {count}")
        print("\nZero accepted trades is a valid result - thresholds were NOT loosened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
