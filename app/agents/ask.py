"""Rule-based read-only Q&A over the current prototype state.

Answers questions strictly from the dashboard snapshot and captured
decision history. When the answer is not in that data, it says so
instead of guessing.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.narrator import narrate_record
from app.agents.records import DecisionRecord
from app.prototype.scenarios import PrototypeDashboardSnapshot

_FALLBACK = (
    "That is not in the recorded state. Ask me about: why a setup was accepted or rejected, "
    "event counters, the session or regime, warm-up progress, playback speed, the shadow order, "
    "or where the report was written."
)


def _latest(history: Sequence[DecisionRecord], *, accepted: bool | None = None) -> DecisionRecord | None:
    """Return the most recent decision record, optionally filtered by outcome."""
    for record in reversed(history):
        if accepted is None or record.accepted == accepted:
            return record
    return None


def answer_question(
    question: str,
    *,
    snapshot: PrototypeDashboardSnapshot,
    history: Sequence[DecisionRecord],
) -> str:
    """Answer one question using only the snapshot and decision history."""
    lowered = question.casefold().strip()
    if not lowered:
        return _FALLBACK

    if "reject" in lowered:
        record = _latest(history, accepted=False)
        if record is None:
            return "No rejected decisions have been recorded this session yet."
        return narrate_record(record)
    if "accept" in lowered:
        record = _latest(history, accepted=True)
        if record is None:
            return "No accepted decisions have been recorded this session yet."
        return narrate_record(record)
    if "why" in lowered or "explain" in lowered or "decision" in lowered:
        record = _latest(history)
        if record is None:
            return "No decisions have been recorded this session yet."
        return narrate_record(record)
    if "event" in lowered or "counter" in lowered or "how many" in lowered:
        return (
            f"Event counters: {snapshot.depth_events} depth, {snapshot.trade_events} trade, "
            f"{snapshot.control_events} control events."
        )
    if "session" in lowered or "regime" in lowered:
        return f"Session: {snapshot.session}. Regime: {snapshot.regime}. Profile: {snapshot.profile}."
    if "warm" in lowered:
        return f"Warm-up progress: {snapshot.warmup}."
    if "speed" in lowered or "pause" in lowered:
        state = "paused" if snapshot.paused else "playing"
        return f"Playback is {state} at speed {snapshot.playback_speed}x."
    if "shadow" in lowered or "order" in lowered:
        if snapshot.shadow_order:
            return (
                f"Shadow order: {snapshot.shadow_order}. It is hypothetical - "
                "no real order exists anywhere in this system."
            )
        return "No shadow order is active. No real order exists anywhere in this system."
    if "report" in lowered:
        return f"Report path: {snapshot.report_path}."
    if "status" in lowered or "feed" in lowered or "connect" in lowered:
        return (
            f"Runtime state: {snapshot.runtime_state}. Synthetic feed: {snapshot.synthetic_status}. "
            f"Instrument: {snapshot.instrument} ({snapshot.source_mode})."
        )
    return _FALLBACK
