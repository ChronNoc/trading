"""Append-only prediction-to-outcome journal: ML-004.

Joins each observe-only :class:`~app.machine_learning.shadow_predictor.ShadowPrediction`
to its eventual target/stop/timeout resolution using the SAME triple-barrier
rule offline training uses (``app.machine_learning.triple_barrier``), so
shadow outcomes and training labels can never silently diverge.

No leakage: :class:`PendingOutcomeTracker` is wired as an ``AnalysisFeed``
sink (see ``tools/start_assistant.py``) and only ever sees prices in true
causal event order, exactly like every other analysis-thread consumer in
this codebase. A resolution therefore only ever uses prices observed AFTER
the prediction it resolves, at the exact time they were observed - never a
lookahead. A prediction that never resolves before its session ends is
dropped as ``session_end_unresolved``, mirroring the "drop, never guess"
rule ``app.machine_learning.session_training`` already applies to training
labels.

Quality stamping: any prediction that was pending across a causality gap
(``AnalysisFeed`` skipped events for analysis) is marked
``quality_flags=("causality_gap_during_window",)`` so downstream evidence
(ML-005) can discount or exclude it rather than silently trusting a window
with a hole in it. This module has no import of, or dependency on,
``app.strategy``, ``app.paper``, ``app.risk``, ``app.execution``, or any
broker code - resolved outcomes are evidence only, never a decision.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.machine_learning.shadow_predictor import JournalRecovery, ShadowPrediction
from app.machine_learning.triple_barrier import barrier_prices, resolve_barrier_detail
from app.market.state import MarketState
from app.strategy.order_flow import _reference_price

OUTCOME_JOURNAL_SCHEMA_VERSION = 1
# A pending outcome that outlives this many concurrently open predictions is a
# runaway (bug, or an approved model firing far faster than horizons resolve).
# Bounded like every other analysis-thread queue in this codebase: the oldest
# pending entries are dropped and loudly counted rather than growing forever.
DEFAULT_MAX_PENDING = 20_000


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One resolved (or dropped-unresolved) prediction outcome. Evidence only."""

    prediction_id: str
    artifact_id: str
    artifact_sha256: str
    session_id: str
    direction: str
    entry_timestamp_ns: int
    entry_price: str
    target_price: str
    stop_price: str
    success_probability: float
    resolved_timestamp_ns: int | None
    resolution_reason: str
    label: int | None
    resolution_latency_ns: int | None
    quality_flags: tuple[str, ...] = ()
    schema_version: int = OUTCOME_JOURNAL_SCHEMA_VERSION
    decision_impact: str = "none"

    def to_record(self) -> dict[str, object]:
        """Return the JSON record. ``decision_impact`` is structural, not stored state."""
        return {
            "schema_version": self.schema_version,
            "prediction_id": self.prediction_id,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "session_id": self.session_id,
            "direction": self.direction,
            "entry_timestamp_ns": self.entry_timestamp_ns,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "success_probability": self.success_probability,
            "resolved_timestamp_ns": self.resolved_timestamp_ns,
            "resolution_reason": self.resolution_reason,
            "label": self.label,
            "resolution_latency_ns": self.resolution_latency_ns,
            "quality_flags": list(self.quality_flags),
            "decision_impact": "none",
        }


class OutcomeJournal:
    """Append-only JSONL sink for resolved shadow-prediction outcomes.

    Mirrors :class:`app.machine_learning.shadow_predictor.PredictionJournal`:
    a written record is a historical fact that is never rewritten, an
    existing file is continued rather than overwritten, and a crash mid-write
    leaves at most one torn final line, tolerated on the next open.
    """

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        """Open (or create) a journal at ``path`` without disturbing its contents."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._recovery = self.recover(self._path)
        self._count = len(self._recovery.records)

    @property
    def path(self) -> Path:
        """Filesystem location of this journal."""
        return self._path

    @property
    def count(self) -> int:
        """Total records written plus recovered."""
        return self._count

    def append(self, record: OutcomeRecord) -> None:
        """Append one resolution durably. Never rewrites earlier lines."""
        line = json.dumps(record.to_record(), sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
            self._count += 1

    @staticmethod
    def recover(path: Path | str) -> JournalRecovery:
        """Read an existing journal, tolerating a torn final line from a crash."""
        target = Path(path)
        if not target.is_file():
            return JournalRecovery(records=(), damaged_tail=False, path=target)
        records: list[dict[str, object]] = []
        damaged = False
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                damaged = True
        return JournalRecovery(records=tuple(records), damaged_tail=damaged, path=target)


@dataclass(slots=True)
class _Pending:
    prediction_id: str
    artifact_id: str
    artifact_sha256: str
    session_id: str
    direction: str
    entry_timestamp_ns: int
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    success_probability: float
    deadline_ns: int
    gap_tainted: bool = False


@dataclass(frozen=True, slots=True)
class OutcomeCoverageSnapshot:
    """Thread-safe truth about outcome resolution coverage; never a decision."""

    predictions_registered: int
    resolved: int
    resolved_target: int
    resolved_stop: int
    resolved_timeout: int
    dropped_unresolved: int
    dropped_overflow: int
    gap_tainted_resolutions: int
    pending: int
    last_resolution_timestamp_ns: int | None
    decision_impact: str = "none"


class PendingOutcomeTracker:
    """Register scored predictions and causally resolve them via triple-barrier.

    Wire :meth:`register` from the model loader immediately after it scores a
    prediction, and wire :meth:`observe_market` directly into
    ``AnalysisFeed.add_sink`` so every subsequent event's price - not just
    the sparser feature-sampling cadence - is checked for resolution, at the
    same fidelity ``app.machine_learning.session_training`` uses to build
    training labels.
    """

    def __init__(
        self,
        *,
        journal: OutcomeJournal,
        horizon_seconds: float,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        """Create a tracker bound to an outcome journal and labeling horizon."""
        if horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._journal = journal
        self._horizon_ns = int(horizon_seconds * 1_000_000_000)
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._session_id = "unbound"
        self._registered = 0
        self._resolved = 0
        self._resolved_target = 0
        self._resolved_stop = 0
        self._resolved_timeout = 0
        self._dropped_unresolved = 0
        self._dropped_overflow = 0
        self._gap_tainted_resolutions = 0
        self._last_resolution_timestamp_ns: int | None = None
        self._current_gap_tainted = False

    def bind_session(self, session_id: str) -> None:
        """At a session boundary, drop every still-open prediction unresolved.

        A prediction whose horizon has not completed by session end is never
        guessed - it is written as ``session_end_unresolved`` with
        ``label=None``, mirroring the drop-never-guess rule in
        ``app.machine_learning.session_training``.
        """
        with self._lock:
            if session_id == self._session_id:
                return
            stale = list(self._pending.values())
            self._pending.clear()
            self._session_id = session_id
            self._current_gap_tainted = False
        for pending in stale:
            self._flush_unresolved(pending, reason="session_end_unresolved")

    def register(
        self,
        prediction: ShadowPrediction,
        *,
        entry_price: Decimal,
        direction: str,
        target_ticks: Decimal,
        stop_ticks: Decimal,
        tick_size: Decimal,
    ) -> None:
        """Register one freshly scored prediction as pending resolution."""
        target_price, stop_price = barrier_prices(
            entry_price, direction,
            target_ticks=target_ticks, stop_ticks=stop_ticks, tick_size=tick_size,
        )
        pending = _Pending(
            prediction_id=prediction.prediction_id,
            artifact_id=prediction.artifact_id,
            artifact_sha256=prediction.artifact_sha256,
            session_id=prediction.session_id,
            direction=direction,
            entry_timestamp_ns=prediction.timestamp_ns,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            success_probability=prediction.success_probability,
            deadline_ns=prediction.timestamp_ns + self._horizon_ns,
        )
        overflowed: _Pending | None = None
        with self._lock:
            self._registered += 1
            if pending.prediction_id in self._pending:
                return  # already registered; never double-count
            if len(self._pending) >= self._max_pending:
                oldest_id = min(
                    self._pending, key=lambda key: self._pending[key].entry_timestamp_ns,
                )
                overflowed = self._pending.pop(oldest_id)
                self._dropped_overflow += 1
            self._pending[pending.prediction_id] = pending
        if overflowed is not None:
            self._flush_unresolved(overflowed, reason="dropped_overflow")

    def observe_market(self, event: object, state: object) -> None:
        """Check every open prediction against one causally observed price.

        Matches the ``AnalysisFeed`` sink contract exactly (``(event,
        state)``); a malformed state must never stop resolution of the rest
        of this analysis chain.
        """
        del event
        if not isinstance(state, MarketState):
            return
        price = _reference_price(state)
        if price is None:
            return
        timestamp_ns = state.timestamp_ns
        with self._lock:
            gap_tainted = self._current_gap_tainted
            resolvable: list[_Pending] = [
                item for item in self._pending.values()
                if resolve_barrier_detail(
                    item.direction, target=item.target_price, stop=item.stop_price,
                    price=price, timestamp_ns=timestamp_ns, deadline_ns=item.deadline_ns,
                ) is not None
            ]
            for item in resolvable:
                del self._pending[item.prediction_id]
        for item in resolvable:
            result = resolve_barrier_detail(
                item.direction, target=item.target_price, stop=item.stop_price,
                price=price, timestamp_ns=timestamp_ns, deadline_ns=item.deadline_ns,
            )
            assert result is not None
            label, reason = result
            self._write_resolution(
                item, resolved_timestamp_ns=timestamp_ns, reason=reason, label=label,
                gap_tainted=gap_tainted or item.gap_tainted,
            )

    def notify_causality_gap(self, skipped: int) -> None:
        """Taint every currently pending prediction: a hole sits in its window.

        The prediction is not dropped - it may still resolve honestly on
        later, uncorrupted prices - but its eventual resolution is stamped
        ``causality_gap_during_window`` so ML-005 evidence can discount it.
        """
        if skipped <= 0:
            return
        with self._lock:
            self._current_gap_tainted = True
            for item in self._pending.values():
                item.gap_tainted = True

    def snapshot(self) -> OutcomeCoverageSnapshot:
        """Return immutable status safe for GUI/status publication threads."""
        with self._lock:
            return OutcomeCoverageSnapshot(
                predictions_registered=self._registered,
                resolved=self._resolved,
                resolved_target=self._resolved_target,
                resolved_stop=self._resolved_stop,
                resolved_timeout=self._resolved_timeout,
                dropped_unresolved=self._dropped_unresolved,
                dropped_overflow=self._dropped_overflow,
                gap_tainted_resolutions=self._gap_tainted_resolutions,
                pending=len(self._pending),
                last_resolution_timestamp_ns=self._last_resolution_timestamp_ns,
            )

    def _write_resolution(
        self,
        pending: _Pending,
        *,
        resolved_timestamp_ns: int,
        reason: str,
        label: int,
        gap_tainted: bool,
    ) -> None:
        quality_flags = ("causality_gap_during_window",) if gap_tainted else ()
        record = OutcomeRecord(
            prediction_id=pending.prediction_id,
            artifact_id=pending.artifact_id,
            artifact_sha256=pending.artifact_sha256,
            session_id=pending.session_id,
            direction=pending.direction,
            entry_timestamp_ns=pending.entry_timestamp_ns,
            entry_price=_decimal_text(pending.entry_price),
            target_price=_decimal_text(pending.target_price),
            stop_price=_decimal_text(pending.stop_price),
            success_probability=pending.success_probability,
            resolved_timestamp_ns=resolved_timestamp_ns,
            resolution_reason=reason,
            label=label,
            resolution_latency_ns=resolved_timestamp_ns - pending.entry_timestamp_ns,
            quality_flags=quality_flags,
        )
        try:
            self._journal.append(record)
        except Exception:  # noqa: BLE001 - a full disk must not stop resolution accounting
            return
        with self._lock:
            self._resolved += 1
            if reason == "target":
                self._resolved_target += 1
            elif reason == "stop":
                self._resolved_stop += 1
            else:
                self._resolved_timeout += 1
            if gap_tainted:
                self._gap_tainted_resolutions += 1
            self._last_resolution_timestamp_ns = resolved_timestamp_ns

    def _flush_unresolved(self, pending: _Pending, *, reason: str) -> None:
        record = OutcomeRecord(
            prediction_id=pending.prediction_id,
            artifact_id=pending.artifact_id,
            artifact_sha256=pending.artifact_sha256,
            session_id=pending.session_id,
            direction=pending.direction,
            entry_timestamp_ns=pending.entry_timestamp_ns,
            entry_price=_decimal_text(pending.entry_price),
            target_price=_decimal_text(pending.target_price),
            stop_price=_decimal_text(pending.stop_price),
            success_probability=pending.success_probability,
            resolved_timestamp_ns=None,
            resolution_reason=reason,
            label=None,
            resolution_latency_ns=None,
            quality_flags=("causality_gap_during_window",) if pending.gap_tainted else (),
        )
        try:
            self._journal.append(record)
        except Exception:  # noqa: BLE001 - a full disk must not stop resolution accounting
            return
        with self._lock:
            self._dropped_unresolved += 1


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
