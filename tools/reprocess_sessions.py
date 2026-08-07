"""Recover REPROCESS-candidate sessions from disk, or prove they cannot be.

Read-only by default: it assesses every candidate against the bytes on disk and
prints the verdict, writing nothing. Pass ``--apply`` to write the certified
recoveries as NEW session directories (the originals are never touched).

    python -m tools.reprocess_sessions              # dry run (default)
    python -m tools.reprocess_sessions --apply      # write certified recoveries
    python -m tools.reprocess_sessions data/raw --apply

A session is certified ONLY when the authoritative catalog classifier accepts
the re-derived manifest as model-training eligible. Candidates whose real trades
were rejected at capture (gone from disk) are reported UNRECOVERABLE with the
exact blocking reasons. Even every recovery combined still leaves the challenger
needing >= 4 trading days with both win and loss labels before it can validate.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.session_catalog import build_catalog  # noqa: E402
from app.research.session_reprocessor import (  # noqa: E402
    OUTCOME_ALREADY_ELIGIBLE,
    OUTCOME_ALREADY_REPROCESSED,
    OUTCOME_NOT_CANDIDATE,
    OUTCOME_RECOVERED,
    OUTCOME_UNRECOVERABLE,
    reprocess_all,
)


def main(argv: list[str]) -> int:
    """Assess (default) or apply reprocessing over a recorded-session tree."""
    apply = "--apply" if "--apply" in argv else ""
    positional = [arg for arg in argv if not arg.startswith("--")]
    raw_root = positional[0] if positional else "data/raw"
    root = Path(raw_root)

    entries = build_catalog(root)
    results = reprocess_all(entries, output_root=root, apply=bool(apply))

    candidates = [
        r for r in results
        if r.outcome in (OUTCOME_RECOVERED, OUTCOME_UNRECOVERABLE)
    ]
    recovered = [r for r in candidates if r.outcome == OUTCOME_RECOVERED]
    unrecoverable = [r for r in candidates if r.outcome == OUTCOME_UNRECOVERABLE]
    already = sum(1 for r in results if r.outcome == OUTCOME_ALREADY_REPROCESSED)
    eligible = sum(1 for r in results if r.outcome == OUTCOME_ALREADY_ELIGIBLE)
    skipped = sum(1 for r in results if r.outcome == OUTCOME_NOT_CANDIDATE)

    mode = "APPLY (writing certified recoveries)" if apply else "DRY RUN (no writes)"
    print(f"Reprocess over {len(entries)} recorded session(s) in {raw_root}  [{mode}]\n")

    recovered.sort(key=lambda r: r.recovered_trades, reverse=True)
    if recovered:
        total = sum(r.recovered_trades for r in recovered)
        print(f"RECOVERABLE: {len(recovered)} session(s), ~{total:,} real trades")
        for r in recovered:
            dq = r.disk_quality
            dropped = f", dropped {dq.zero_size_dropped:,} zero-size" if dq and dq.zero_size_dropped else ""
            written = f"  -> {r.output_dir.name}" if r.output_dir else ""
            print(f"    {r.source_session_id}  {r.recovered_trades:,} trades{dropped}{written}")
        print()

    if unrecoverable:
        print(f"UNRECOVERABLE: {len(unrecoverable)} session(s) - data gone from disk")
        for r in unrecoverable:
            dq = r.disk_quality
            detail = ""
            if dq and (dq.trade_sequence_gaps or dq.missed_trade_events):
                detail = (f"  ({dq.trade_sequence_gaps:,} on-disk gaps, "
                          f"{dq.missed_trade_events:,} trades missing)")
            print(f"    {r.source_session_id}{detail}")
            for reason in r.reasons[:3]:
                print(f"        - {reason}")
        print()

    print(f"Already model-eligible:   {eligible}")
    print(f"Already reprocessed:      {already}")
    print(f"Not a reprocess candidate:{skipped}")
    if not apply and recovered:
        print("\nRe-run with --apply to write these recoveries as new eligible sessions.")
    print("\nNote: recovery certifies on-disk continuity against the authoritative catalog")
    print("classifier; it never fabricates data. Even every recovery combined still leaves")
    print("the challenger needing >= 4 trading days with both win and loss labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
