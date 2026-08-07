"""Read-only salvage audit over recorded sessions in ``data/raw/``.

Answers one question honestly: of everything already recorded, how much could
become eligible training data without babysitting new live captures?

It re-runs the CURRENT eligibility logic (``app.research.session_catalog``) over
every recorded manifest and sorts each session into a disposition:

* ELIGIBLE NOW      - already model-training eligible; nothing to do.
* REPROCESS         - clean, continuous, has trades, blocked ONLY by the
                      zero-size-trade artifact (malformed / missed / gaps).
                      The real order flow is intact; reprocessing the raw with
                      the fixed pipeline could recover it - NO new recording.
* RE-RECORD         - blocked by something a reprocess cannot fix (unclean
                      shutdown with loss, old bridge protocol / missing
                      capabilities, undeclared provenance). Needs a fresh clean
                      capture with the redeployed add-on.
* INHERENT-DEAD     - nothing to learn from (no trades, or no depth).
* STILL ACTIVE      - not finalized yet.

It NEVER writes, repairs, or deletes anything. REPROCESS is an UPPER BOUND: it
flags sessions whose only blocker is now-fixable, but confirming recovery needs
the actual reprocessor (a separate step that re-derives from the raw Parquet).
Even fully recovered, the challenger still needs >= 4 trading days with both
win and loss labels before it can validate anything.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.session_catalog import SessionEntry, build_catalog  # noqa: E402

# Dispositions (what, if anything, could make the session count).
ELIGIBLE = "eligible_now"
REPROCESS = "reprocess"
RERECORD_BRIDGE = "rerecord_old_bridge"
RERECORD_UNCLEAN = "rerecord_unclean_or_discontinuous"
RERECORD_PROVENANCE = "rerecord_provenance_undeclared"
RERECORD_OVERFLOW = "rerecord_overflow_highvol"
DEAD_DEPTH_ONLY = "dead_depth_only_no_trades"
DEAD_NO_DEPTH = "dead_no_depth"
ACTIVE = "still_active_or_unfinalized"
OTHER = "other"

# Which dispositions are recoverable WITHOUT a new recording.
_REPROCESSABLE = frozenset({REPROCESS})
_RERECORD = frozenset({RERECORD_BRIDGE, RERECORD_UNCLEAN, RERECORD_PROVENANCE, RERECORD_OVERFLOW})
_DEAD = frozenset({DEAD_DEPTH_ONLY, DEAD_NO_DEPTH})


def categorize_entry(entry: SessionEntry) -> str:
    """Sort one session into a salvage disposition (pure, no I/O)."""
    if entry.eligible_for_model_training:
        return ELIGIBLE
    if entry.active or not entry.finalized:
        return ACTIVE
    if entry.depth_updates == 0:
        return DEAD_NO_DEPTH
    if entry.trades == 0:
        return DEAD_DEPTH_ONLY

    reasons = " || ".join(entry.reasons).lower()

    # Order-flow already clean; only the stricter model gate blocks it (protocol,
    # capabilities, aggressor coverage, storage). All are baked into the recording
    # - a reprocess cannot add an aggressor side that was never captured - so this
    # needs a fresh capture with the redeployed add-on.
    if entry.eligible_for_order_flow_replay:
        return RERECORD_BRIDGE

    # Not order-flow eligible. If it isn't even analysis-eligible, the break is
    # in continuity / clean-shutdown / provenance - not a zero-size artifact.
    if not entry.eligible_for_analysis:
        if "provenance" in reasons:
            return RERECORD_PROVENANCE
        return RERECORD_UNCLEAN

    # Analysis-eligible (finalized + clean + continuous + real + has depth) but
    # the order-flow quality gate failed. Split real loss (overflow / drops)
    # from the benign zero-size-trade artifact, which is the salvage case.
    overflow = entry.dropped_message_count > 0 or "overflow" in reasons or "drop" in reasons
    if overflow:
        return RERECORD_OVERFLOW
    zero_size = (
        entry.malformed_event_count > 0
        or entry.missed_trade_event_count > 0
        or "sequence gap" in reasons
        or "missing events" in reasons
        or "malformed" in reasons
    )
    if zero_size:
        return REPROCESS
    return OTHER


@dataclass(frozen=True, slots=True)
class SalvageReport:
    """Aggregate salvage picture over a set of recorded sessions."""

    total: int
    by_disposition: dict[str, int]
    reprocess_trades: int
    reprocess_examples: tuple[tuple[str, int], ...]  # (session_id, trades), richest first

    @property
    def eligible_now(self) -> int:
        return self.by_disposition.get(ELIGIBLE, 0)

    @property
    def reprocessable(self) -> int:
        return sum(self.by_disposition.get(name, 0) for name in _REPROCESSABLE)

    @property
    def rerecord(self) -> int:
        return sum(self.by_disposition.get(name, 0) for name in _RERECORD)

    @property
    def dead(self) -> int:
        return sum(self.by_disposition.get(name, 0) for name in _DEAD)


def audit(entries: list[SessionEntry]) -> SalvageReport:
    """Categorize every session and summarize what is recoverable (pure)."""
    counts: Counter[str] = Counter()
    reprocess: list[tuple[str, int]] = []
    for entry in entries:
        disposition = categorize_entry(entry)
        counts[disposition] += 1
        if disposition == REPROCESS:
            reprocess.append((entry.session_id, entry.trades))
    reprocess.sort(key=lambda item: item[1], reverse=True)
    return SalvageReport(
        total=len(entries),
        by_disposition=dict(counts),
        reprocess_trades=sum(trades for _, trades in reprocess),
        reprocess_examples=tuple(reprocess[:10]),
    )


_LABELS = {
    ELIGIBLE: "ELIGIBLE NOW (already model-training eligible)",
    REPROCESS: "REPROCESS (recover from disk, no new recording)",
    RERECORD_BRIDGE: "RE-RECORD - old bridge protocol / missing capabilities",
    RERECORD_UNCLEAN: "RE-RECORD - unclean shutdown / discontinuous",
    RERECORD_PROVENANCE: "RE-RECORD - provenance never declared",
    RERECORD_OVERFLOW: "RE-RECORD - bounded-queue overflow (high volatility)",
    DEAD_DEPTH_ONLY: "DEAD - depth only, no trades to learn from",
    DEAD_NO_DEPTH: "DEAD - no depth recorded",
    ACTIVE: "STILL ACTIVE / not finalized",
    OTHER: "OTHER",
}


def main(raw_root: str = "data/raw") -> int:
    """Print the salvage audit for a recorded-session tree."""
    entries = list(build_catalog(Path(raw_root)))
    report = audit(entries)
    print(f"Salvage audit over {report.total} recorded session(s) in {raw_root}\n")
    for name, count in sorted(report.by_disposition.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {count:>4}  {_LABELS.get(name, name)}")
    print()
    print(f"Eligible now:              {report.eligible_now}")
    print(f"Recoverable by REPROCESS:  {report.reprocessable}"
          f"  (~{report.reprocess_trades:,} real trades, no new recording)")
    print(f"Need a fresh RE-RECORD:    {report.rerecord}")
    print(f"Inherently unusable:       {report.dead}")
    if report.reprocess_examples:
        print("\nRichest reprocess candidates (session, trades):")
        for session_id, trades in report.reprocess_examples:
            print(f"    {session_id}  {trades:,} trades")
    print("\nNote: REPROCESS is an upper bound - it flags sessions whose only blocker is the")
    print("now-fixed zero-size-trade artifact; confirming recovery needs the actual reprocessor.")
    print("Even fully recovered, the challenger still needs >= 4 trading days with both win")
    print("and loss labels before it can validate a model. This audit wrote nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/raw"))
