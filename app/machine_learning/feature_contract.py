"""Shared causal feature contract for offline training and online shadow scoring.

The module deliberately contains no model loading and no decision integration.  It
only turns an already-observed market-state prefix into an ordered, canonical
feature vector that can be used identically by dataset builders and a future
observe-only runtime scorer.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.market.features import compute_market_features
from app.market.state import MarketState
from app.research.causal_context import CausalLevelTracker
from app.strategy.causal_window import CausalWindow
from app.strategy.order_flow import StrategyLevels, _reference_price

NEW_YORK = ZoneInfo("America/New_York")
NANOSECONDS_PER_SECOND = 1_000_000_000
FEATURE_CONTRACT_VERSION = "shared-causal-market-features-v2"
FEATURE_PARITY_STATE = "SHARED_BUILDER_RUNTIME_DISCONNECTED"

FEATURE_COLUMNS: tuple[str, ...] = (
    "book_imbalance",
    "recent_aggressive_buy_volume",
    "recent_aggressive_sell_volume",
    "liquidity_added",
    "liquidity_cancelled",
    "reload_count",
    "distance_to_defended_level",
    "price_velocity",
    "trade_velocity",
    "spread",
    "short_term_volatility",
    "time_of_day",
    "distance_from_overnight_high_low",
    "distance_from_prior_day_levels",
    "direction",
    "stop_distance",
    "target_distance",
)

_FEATURE_FORMULAS: tuple[str, ...] = (
    "last-book bid-size-minus-ask-size over total-size",
    "last executed-buy-volume minus first executed-buy-volume",
    "last executed-sell-volume minus first executed-sell-volume",
    "sum of positive same-side depth changes over sampled prefix",
    "sum of negative same-side depth changes over sampled prefix",
    "best-bid depletion-to-reload cycles at default threshold",
    "ticks from reference price to largest same-direction depth level",
    "absolute first-to-last reference-price ticks per second",
    "executed volume delta per second",
    "last spread in ticks; zero when no two-sided spread is available",
    "population standard deviation of sampled mid-prices in ticks",
    "timestamp nanoseconds floor-truncated to whole seconds, then America/New_York HH:MM:SS",
    "ticks to nearest known causal overnight high/low; zero when unavailable",
    "ticks to nearest known causal prior-day high/low; zero when unavailable",
    "literal long or short",
    "configured triple-barrier stop distance in ticks",
    "configured triple-barrier target distance in ticks",
)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """One validated feature vector in the contract's exact column order."""

    values: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject vectors that cannot satisfy the declared column contract."""
        if len(self.values) != len(FEATURE_COLUMNS):
            raise ValueError(
                f"expected {len(FEATURE_COLUMNS)} feature values, got {len(self.values)}"
            )

    def as_record(self) -> dict[str, str]:
        """Return an insertion-ordered training/scoring record."""
        return dict(zip(FEATURE_COLUMNS, self.values, strict=True))

    def canonical_bytes(self) -> bytes:
        """Return byte-stable JSON containing values in exact model order."""
        return json.dumps(
            self.values,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


class CausalFeaturePipeline:
    """Incrementally maintain the same causal prefix used by offline training."""

    def __init__(
        self,
        *,
        window_span_seconds: float,
        sample_interval_ms: float,
        tick_size: Decimal,
        level_tracker: object | None = None,
    ) -> None:
        """Create an empty feature pipeline with explicit sampling assumptions."""
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        self._tick_size = tick_size
        self._window = CausalWindow(
            span_seconds=window_span_seconds,
            sample_interval_ms=sample_interval_ms,
        )
        self._level_tracker = level_tracker or CausalLevelTracker()
        self._event_index = 0

    def observe(self, state: MarketState, *, event_index: int | None = None) -> None:
        """Advance the prefix with one already-observed market state."""
        if event_index is None:
            self._event_index += 1
        else:
            self._event_index = event_index
        self._window.observe(state, event_index=self._event_index)
        self._level_tracker.observe(state.timestamp_ns, _reference_price(state))  # type: ignore[attr-defined]

    @property
    def span_seconds(self) -> float:
        """Return market time covered by the sampled prefix."""
        return self._window.span_seconds

    def snapshots(self) -> tuple[MarketState, ...]:
        """Return the sampled prefix oldest-to-newest."""
        return tuple(self._window.view())  # type: ignore[return-value]

    def feature_vector(
        self,
        *,
        direction: str,
        stop_distance: Decimal,
        target_distance: Decimal,
    ) -> FeatureVector:
        """Build the ordered vector for the current prefix without side effects."""
        snapshots = self.snapshots()
        if not snapshots:
            raise ValueError("cannot build features before observing a market state")
        levels = self._level_tracker.levels_at(snapshots[-1].timestamp_ns)  # type: ignore[attr-defined]
        return build_feature_vector(
            snapshots,
            levels=levels,
            direction=direction,
            stop_distance=stop_distance,
            target_distance=target_distance,
            tick_size=self._tick_size,
        )


@dataclass(frozen=True, slots=True)
class FeatureObservationSnapshot:
    """Thread-safe truth about feature construction; never a prediction."""

    state: str
    reason: str
    session_id: str
    observed_events: int
    feature_observations: int
    last_feature_timestamp_ns: int | None
    gap_resets: int
    session_resets: int
    skipped_events: int
    thread_name: str = ""
    decision_impact: str = "none"


class ObserveOnlyFeatureSink:
    """Build shared vectors on the analysis thread without loading any model."""

    def __init__(
        self,
        *,
        config: object | None = None,
        prediction_sink: object | None = None,
    ) -> None:
        """Create a sink using the offline training window and cadence contract.

        ``prediction_sink`` is optional and defaults to ``None`` so existing
        callers (and every prior test) are unaffected. When attached (an
        ``app.machine_learning.shadow_predictor.ObserveOnlyModelLoader``), each
        feature vector this sink builds is also handed to
        ``prediction_sink.score(...)`` — see that module for the observe-only,
        zero-decision-effect scoring contract.
        """
        from app.machine_learning.session_training import SessionTrainingConfig

        self._config = config if config is not None else SessionTrainingConfig()
        self._prediction_sink = prediction_sink
        self._lock = threading.Lock()
        self._pipeline: CausalFeaturePipeline | None = None
        self._session_id = "unbound"
        self._last_sample_ns = 0
        self._observed_events = 0
        self._feature_observations = 0
        self._last_feature_timestamp_ns: int | None = None
        self._gap_resets = 0
        self._session_resets = 0
        self._skipped_events = 0
        self._state = "UNBOUND"
        self._reason = "no recording session is bound"
        self._thread_name = ""

    def bind_session(self, session_id: str) -> None:
        """Reset at a recording-session boundary and require full warmup."""
        with self._lock:
            changed = session_id != self._session_id
            self._session_id = session_id
            self._reset_pipeline()
            self._last_sample_ns = 0
            if changed:
                self._session_resets += 1
            self._state = "WARMING"
            self._reason = "new session requires a complete causal warmup"
        if self._prediction_sink is not None:
            self._prediction_sink.bind_session(session_id)  # type: ignore[attr-defined]

    def ingest(self, event: Mapping[str, object], state: object) -> None:
        """Consume one accepted prebuilt state; output remains memory-only evidence."""
        del event
        if not isinstance(state, MarketState):
            raise TypeError("feature sink requires MarketState")
        with self._lock:
            self._thread_name = threading.current_thread().name
            if self._pipeline is None:
                self._reset_pipeline()
            assert self._pipeline is not None
            self._observed_events += 1
            self._pipeline.observe(state, event_index=self._observed_events)
            cfg = self._config
            warmup = float(getattr(cfg, "warmup_seconds"))
            if self._pipeline.span_seconds < warmup:
                self._state = "WARMING"
                self._reason = (
                    f"causal warmup {self._pipeline.span_seconds:.1f}/{warmup:.1f} seconds"
                )
                return
            cadence_ns = int(float(getattr(cfg, "sample_interval_seconds")) * 1e9)
            if state.timestamp_ns - self._last_sample_ns < cadence_ns:
                self._state = "READY"
                self._reason = "shared feature contract ready; waiting for observation cadence"
                return
            self._last_sample_ns = state.timestamp_ns
            session_id = self._session_id
            timestamp_ns = state.timestamp_ns
            vectors: list[tuple[str, FeatureVector]] = []
            for direction in tuple(getattr(cfg, "directions")):
                vector = self._pipeline.feature_vector(
                    direction=str(direction),
                    stop_distance=getattr(cfg, "stop_ticks"),
                    target_distance=getattr(cfg, "target_ticks"),
                )
                vectors.append((str(direction), vector))
                self._feature_observations += 1
            self._last_feature_timestamp_ns = state.timestamp_ns
            self._state = "OBSERVING"
            self._reason = "feature vectors observed in memory; no model loaded or scored"
        # Scoring runs outside this sink's lock: the loader has its own lock
        # and may do disk I/O (journal append), which must never block feature
        # observation on the analysis thread.
        if self._prediction_sink is not None:
            for direction, vector in vectors:
                try:
                    self._prediction_sink.score(  # type: ignore[attr-defined]
                        vector,
                        session_id=session_id,
                        timestamp_ns=timestamp_ns,
                        direction=direction,
                    )
                except Exception:  # noqa: BLE001,S110 - a broken sink must not stop features
                    pass

    def notify_causality_gap(self, skipped: int) -> None:
        """Invalidate the prefix after any skipped analysis event and rewarm."""
        if skipped <= 0:
            return
        with self._lock:
            self._skipped_events += skipped
            self._gap_resets += 1
            self._reset_pipeline()
            self._last_sample_ns = 0
            self._state = "WARMING"
            self._reason = f"analysis gap skipped {skipped} events; full rewarm required"

    def snapshot(self) -> FeatureObservationSnapshot:
        """Return immutable status safe for GUI/status publication threads."""
        with self._lock:
            return FeatureObservationSnapshot(
                state=self._state,
                reason=self._reason,
                session_id=self._session_id,
                observed_events=self._observed_events,
                feature_observations=self._feature_observations,
                last_feature_timestamp_ns=self._last_feature_timestamp_ns,
                gap_resets=self._gap_resets,
                session_resets=self._session_resets,
                skipped_events=self._skipped_events,
                thread_name=self._thread_name,
            )

    def _reset_pipeline(self) -> None:
        cfg = self._config
        self._pipeline = CausalFeaturePipeline(
            window_span_seconds=float(getattr(cfg, "window_span_seconds")),
            sample_interval_ms=float(getattr(cfg, "sample_interval_ms")),
            tick_size=getattr(cfg, "tick_size"),
        )


def build_feature_vector(
    snapshots: Sequence[MarketState],
    *,
    levels: StrategyLevels,
    direction: str,
    stop_distance: Decimal,
    target_distance: Decimal,
    tick_size: Decimal,
) -> FeatureVector:
    """Build one causal feature vector from an already-observed state prefix."""
    if not snapshots:
        raise ValueError("snapshots must contain at least one MarketState")
    if direction not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")

    features = compute_market_features(snapshots)
    first, last = snapshots[0], snapshots[-1]
    reference = _reference_price(last) or Decimal("0")
    spread_ticks = last.spread / tick_size if last.spread is not None else Decimal("0")
    overnight = _nearest_distance_ticks(
        reference,
        (levels.overnight_high, levels.overnight_low),
        tick_size,
    )
    prior_day = _nearest_distance_ticks(
        reference,
        (levels.prior_day_high, levels.prior_day_low),
        tick_size,
    )

    record: dict[str, str] = {
        "book_imbalance": _decimal_text(features.book_imbalance),
        "recent_aggressive_buy_volume": _decimal_text(
            last.executed_buy_volume - first.executed_buy_volume
        ),
        "recent_aggressive_sell_volume": _decimal_text(
            last.executed_sell_volume - first.executed_sell_volume
        ),
        "liquidity_added": _decimal_text(features.liquidity_added),
        "liquidity_cancelled": _decimal_text(features.liquidity_cancelled),
        "reload_count": str(int(features.bid_reload_count)),
        "distance_to_defended_level": _decimal_text(
            _defended_distance_ticks(last, direction, tick_size)
        ),
        "price_velocity": _decimal_text(_price_velocity_ticks(snapshots, tick_size)),
        "trade_velocity": _decimal_text(features.trade_velocity),
        "spread": _decimal_text(spread_ticks),
        "short_term_volatility": _decimal_text(
            features.short_term_volatility / tick_size
        ),
        "time_of_day": _time_of_day(last.timestamp_ns),
        "distance_from_overnight_high_low": _decimal_text(overnight),
        "distance_from_prior_day_levels": _decimal_text(prior_day),
        "direction": direction,
        "stop_distance": _decimal_text(stop_distance),
        "target_distance": _decimal_text(target_distance),
    }
    if tuple(record) != FEATURE_COLUMNS:
        raise RuntimeError("feature record order diverged from FEATURE_COLUMNS")
    return FeatureVector(tuple(record[column] for column in FEATURE_COLUMNS))


def feature_contract_descriptor(
    *,
    window_span_seconds: float,
    sample_interval_ms: float,
    tick_size: Decimal,
) -> dict[str, object]:
    """Return the machine-readable formula, order, encoding, and window contract."""
    return {
        "version": FEATURE_CONTRACT_VERSION,
        "columns": list(FEATURE_COLUMNS),
        "formulas": list(_FEATURE_FORMULAS),
        "window": {
            "span_seconds": window_span_seconds,
            "sample_interval_ms": sample_interval_ms,
            "head_policy": "newest state always replaces or appends the live head",
        },
        "normalization": {
            "price_distances": "ticks",
            "velocities": "per second",
            "decimal_encoding": "fixed-point strings without exponent",
        },
        "timestamp_encoding": {
            "input": "integer nanoseconds since Unix epoch",
            "subsecond_policy": "floor to the containing whole second",
            "timezone": "America/New_York",
            "output": "HH:MM:SS",
        },
        "missing_data": {
            "spread": "0 when no two-sided spread is available",
            "causal_higher_timeframe_level_distance": "0 when no level is known",
            "defended_level_distance": "0 when reference price or side depth is unavailable",
        },
        "tick_size": _decimal_text(tick_size),
        "canonical_vector_encoding": "UTF-8 compact JSON array in columns order",
        "runtime_effect": "none; feature construction only",
    }


def feature_contract_sha256(
    *,
    window_span_seconds: float,
    sample_interval_ms: float,
    tick_size: Decimal,
) -> str:
    """Return the content hash for the complete shared feature contract."""
    descriptor = feature_contract_descriptor(
        window_span_seconds=window_span_seconds,
        sample_interval_ms=sample_interval_ms,
        tick_size=tick_size,
    )
    payload = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_feature_record(row: Mapping[str, Any]) -> FeatureVector:
    """Validate and canonicalize a mapping supplied to a model scorer."""
    missing = [column for column in FEATURE_COLUMNS if column not in row]
    extra = [column for column in row if column not in FEATURE_COLUMNS]
    if missing or extra:
        raise ValueError(f"feature record schema mismatch; missing={missing}, extra={extra}")
    return FeatureVector(tuple(str(row[column]) for column in FEATURE_COLUMNS))


def _nearest_distance_ticks(
    reference: Decimal,
    levels: tuple[Decimal | None, ...],
    tick_size: Decimal,
) -> Decimal:
    present = [level for level in levels if level is not None]
    if reference <= 0 or not present:
        return Decimal("0")
    return min(abs(reference - level) for level in present) / tick_size


def _defended_distance_ticks(
    state: MarketState,
    direction: str,
    tick_size: Decimal,
) -> Decimal:
    depth = state.bid_depth if direction == "long" else state.ask_depth
    reference = _reference_price(state)
    if not depth or reference is None:
        return Decimal("0")
    largest = max(depth, key=lambda level: level.size)
    return abs(reference - largest.price) / tick_size


def _price_velocity_ticks(
    snapshots: Sequence[MarketState],
    tick_size: Decimal,
) -> Decimal:
    if len(snapshots) < 2:
        return Decimal("0")
    first_price = _reference_price(snapshots[0])
    last_price = _reference_price(snapshots[-1])
    if first_price is None or last_price is None:
        return Decimal("0")
    elapsed_ns = snapshots[-1].timestamp_ns - snapshots[0].timestamp_ns
    if elapsed_ns <= 0:
        return Decimal("0")
    elapsed_seconds = Decimal(elapsed_ns) / Decimal(NANOSECONDS_PER_SECOND)
    return abs(last_price - first_price) / tick_size / elapsed_seconds


def _time_of_day(timestamp_ns: int) -> str:
    seconds = timestamp_ns // NANOSECONDS_PER_SECOND
    instant = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(NEW_YORK)
    return instant.strftime("%H:%M:%S")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
