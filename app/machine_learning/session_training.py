"""Per-session supervised training data via causal triple-barrier labeling.

The discretionary order-flow setup rarely fires on MNQ's thin book, so it
produces almost no labels and the ML never trains. This module builds training
data a different, standard way that works on EVERY session:

* walk the session once, in event order;
* every ``sample_interval_seconds`` of market time, snapshot a feature vector
  computed ONLY from past data (causal) for each modeled direction;
* label that snapshot by what price ACTUALLY did next - 1 if it reached
  +``target_ticks`` before -``stop_ticks`` within ``horizon_seconds``, else 0
  (the "triple-barrier" method).

Features are causal (no look-ahead); the label deliberately uses forward data,
which is exactly what supervised training needs. A sample whose horizon has not
completed by session end is DROPPED, never guessed - no fabricated outcomes.

Each session is trained on its own rows and produces its own model. That is
descriptive, per-session research: a session's model is not claimed to predict
any other session, and no profitability is asserted anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from app.market.features import compute_market_features
from app.market.state import MarketState
from app.research.replay_loader import stream_session_events
from app.strategy.order_flow import OrderFlowThresholds, _reference_price

NEW_YORK = ZoneInfo("America/New_York")
_NS_PER_SECOND = 1_000_000_000

# The exact contract train.py expects (features + label + timestamp_ns).
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


@dataclass(frozen=True, slots=True)
class SessionTrainingConfig:
    """Visible, deterministic triple-barrier labeling assumptions."""

    target_ticks: Decimal = Decimal("12")
    stop_ticks: Decimal = Decimal("8")
    horizon_seconds: float = 300.0
    sample_interval_seconds: float = 30.0
    window_span_seconds: float = 180.0
    sample_interval_ms: float = 250.0
    warmup_seconds: float = 60.0
    tick_size: Decimal = Decimal("0.25")
    directions: tuple[str, ...] = ("long", "short")

    def validate(self) -> None:
        """Reject impossible configurations before any work begins."""
        if self.target_ticks <= 0 or self.stop_ticks <= 0:
            raise ValueError("target_ticks and stop_ticks must be positive")
        if self.horizon_seconds <= 0 or self.sample_interval_seconds <= 0:
            raise ValueError("horizon and sample interval must be positive")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if not self.directions:
            raise ValueError("at least one direction is required")


@dataclass(slots=True)
class _Pending:
    features: dict[str, object]
    direction: str
    entry: Decimal
    target: Decimal
    stop: Decimal
    deadline_ns: int


@dataclass(slots=True)
class SessionDatasetSummary:
    """Honest per-session labeling outcome."""

    session_id: str
    rows: int = 0
    wins: int = 0
    losses: int = 0
    dropped_incomplete: int = 0
    trainable: bool = False
    note: str = ""


def build_session_training_rows(
    session_dir: Path,
    *,
    config: SessionTrainingConfig | None = None,
    level_tracker: object | None = None,
) -> tuple[list[dict[str, object]], SessionDatasetSummary]:
    """Return (rows, summary) of causal-feature/forward-label training rows."""
    from app.research.causal_context import CausalLevelTracker
    from app.strategy.causal_window import CausalWindow

    cfg = config or SessionTrainingConfig()
    cfg.validate()
    tracker = level_tracker or CausalLevelTracker()
    window = CausalWindow(span_seconds=cfg.window_span_seconds,
                          sample_interval_ms=cfg.sample_interval_ms)
    thresholds = OrderFlowThresholds(tick_size=cfg.tick_size)

    state = MarketState()
    pendings: list[_Pending] = []
    rows: list[dict[str, object]] = []
    summary = SessionDatasetSummary(session_id=session_dir.name)
    last_sample_ns = 0
    sample_ns = int(cfg.sample_interval_seconds * _NS_PER_SECOND)
    horizon_ns = int(cfg.horizon_seconds * _NS_PER_SECOND)
    event_index = 0

    for event in stream_session_events(session_dir):
        try:
            state = state.update(event.payload)
        except Exception:  # noqa: BLE001 - a malformed event must not kill the build
            continue
        event_index += 1
        window.observe(state, event_index=event_index)
        ref = _reference_price(state)
        tracker.observe(event.timestamp_ns, ref)

        price = _trade_price(event)
        resolve_price = price if price is not None else ref
        if resolve_price is not None:
            survivors: list[_Pending] = []
            for pending in pendings:
                label = _resolve(pending, resolve_price, event.timestamp_ns)
                if label is None:
                    survivors.append(pending)
                    continue
                row = dict(pending.features)
                row["label"] = label
                rows.append(row)
                summary.wins += label
                summary.losses += 1 - label
            pendings = survivors

        if (window.span_seconds >= cfg.warmup_seconds and ref is not None
                and event.timestamp_ns - last_sample_ns >= sample_ns):
            last_sample_ns = event.timestamp_ns
            snapshots = window.view()
            for direction in cfg.directions:
                features = _extract_features(snapshots, state, tracker, direction, cfg, thresholds)
                features["timestamp_ns"] = event.timestamp_ns
                target, stop = _barriers(ref, direction, cfg)
                pendings.append(_Pending(features, direction, ref, target, stop,
                                         event.timestamp_ns + horizon_ns))

    summary.dropped_incomplete = len(pendings)  # horizon never completed - dropped, never guessed
    summary.rows = len(rows)
    labels = {int(row["label"]) for row in rows}
    summary.trainable = len(rows) >= 2 and labels == {0, 1}
    if not rows:
        summary.note = "no completed labels in this session"
    elif labels != {0, 1}:
        summary.note = f"only one label class present ({labels}); need both 0 and 1"
    else:
        summary.note = "trainable"
    return rows, summary


def train_session_model(
    session_dir: Path,
    output_root: Path,
    *,
    config: SessionTrainingConfig | None = None,
    version: str = "0.1.0",
) -> tuple[SessionDatasetSummary, dict[str, object]]:
    """Build one session's dataset and train its own model, or report honestly why not.

    Writes, under ``output_root/<session_id>/``: a report.json always; and when
    the session yields >=2 rows with both label classes, the dataset plus the
    versioned per-session model artifacts. Returns (summary, report).
    """
    import json

    cfg = config or SessionTrainingConfig()
    rows, summary = build_session_training_rows(session_dir, config=cfg)
    session_out = Path(output_root) / summary.session_id
    session_out.mkdir(parents=True, exist_ok=True)

    report: dict[str, object] = {
        "session_id": summary.session_id,
        "labeling": "causal-feature triple-barrier",
        "target_ticks": str(cfg.target_ticks),
        "stop_ticks": str(cfg.stop_ticks),
        "horizon_seconds": cfg.horizon_seconds,
        "sample_interval_seconds": cfg.sample_interval_seconds,
        "rows": summary.rows,
        "wins": summary.wins,
        "losses": summary.losses,
        "dropped_incomplete_horizon": summary.dropped_incomplete,
        "trainable": summary.trainable,
        "note": summary.note,
        "scope": "per-session, in-sample only; not validated out-of-sample; "
                 "describes THIS session only and makes no profitability claim",
        "trained": False,
    }

    if not summary.trainable:
        _write_json(session_out / "report.json", report)
        return summary, report

    from app.machine_learning.train import train_models

    dataset_path = session_out / "dataset.jsonl"
    with dataset_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    result = train_models(dataset_path, session_out, version=version)
    report["trained"] = True
    report["dataset"] = dataset_path.name
    report["models"] = {
        "logistic_regression": result.logistic_regression.model_path.name,
        "xgboost": result.xgboost.model_path.name,
    }
    # HONEST out-of-sample estimate: the delivered models above are trained
    # in-sample; a model that only memorises its own session is worthless. Split
    # the session by TIME (first 70% train, last 30% test), train on the past,
    # and report accuracy on the unseen future vs the naive majority-class base
    # rate. A model that does not beat the base rate has learned nothing here.
    report["holdout"] = _temporal_holdout(rows)
    _write_json(session_out / "report.json", report)
    return summary, report


def _temporal_holdout(rows: list[dict[str, object]]) -> dict[str, object]:
    """Train on the session's past, score its future; compare to the base rate."""
    from app.machine_learning.train import build_feature_matrix, build_label_array

    ordered = sorted(rows, key=lambda row: int(row["timestamp_ns"]))
    split = int(len(ordered) * 0.7)
    train_rows, test_rows = ordered[:split], ordered[split:]
    train_labels = {int(row["label"]) for row in train_rows}
    if len(train_rows) < 2 or train_labels != {0, 1} or not test_rows:
        return {"evaluated": False,
                "note": "insufficient or one-class temporal split; no out-of-sample estimate"}

    from sklearn.linear_model import LogisticRegression

    x_train = build_feature_matrix(train_rows)
    y_train = build_label_array(train_rows)
    x_test = build_feature_matrix(test_rows)
    y_test = build_label_array(test_rows)
    model = LogisticRegression(max_iter=1_000, solver="liblinear", random_state=7)
    model.fit(x_train, y_train)
    accuracy = float((model.predict(x_test) == y_test).mean())
    positive_rate = float(y_test.mean())
    base_rate = max(positive_rate, 1.0 - positive_rate)  # naive majority-class guess
    return {
        "evaluated": True,
        "test_rows": len(test_rows),
        "accuracy": round(accuracy, 3),
        "base_rate": round(base_rate, 3),
        "beats_base_rate": bool(accuracy > base_rate),
        "note": "logistic-regression temporal holdout (first 70% train / last 30% test); "
                "small samples make this noisy - not a profitability claim",
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    import json

    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _barriers(entry: Decimal, direction: str, cfg: SessionTrainingConfig) -> tuple[Decimal, Decimal]:
    up = cfg.target_ticks * cfg.tick_size
    down = cfg.stop_ticks * cfg.tick_size
    if direction == "long":
        return entry + up, entry - down
    return entry - up, entry + down


def _resolve(pending: _Pending, price: Decimal, ts_ns: int) -> int | None:
    """1 if target-before-stop, 0 if stop-first or horizon expired, None if open."""
    if pending.direction == "long":
        if price >= pending.target:
            return 1
        if price <= pending.stop:
            return 0
    else:
        if price <= pending.target:
            return 1
        if price >= pending.stop:
            return 0
    if ts_ns >= pending.deadline_ns:
        return 0
    return None


def _trade_price(event: object) -> Decimal | None:
    kind = getattr(event, "kind", "")
    if kind != "trade":
        return None
    payload = getattr(event, "payload", {})
    raw = payload.get("price")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None


def _extract_features(
    snapshots: Sequence[MarketState],
    state: MarketState,
    tracker: object,
    direction: str,
    cfg: SessionTrainingConfig,
    thresholds: OrderFlowThresholds,
) -> dict[str, object]:
    """Compute the FEATURE_COLUMNS causally from the window and level context."""
    tick = cfg.tick_size
    features = compute_market_features(list(snapshots))
    first, last = snapshots[0], snapshots[-1]
    buy = last.executed_buy_volume - first.executed_buy_volume
    sell = last.executed_sell_volume - first.executed_sell_volume

    ref = _reference_price(state) or Decimal("0")
    spread_ticks = (state.spread / tick) if state.spread is not None else Decimal("0")
    volatility_ticks = features.short_term_volatility / tick

    levels = tracker.levels_at(last.timestamp_ns)  # type: ignore[attr-defined]
    overnight = _nearest_distance_ticks(ref, (levels.overnight_high, levels.overnight_low), tick)
    prior_day = _nearest_distance_ticks(ref, (levels.prior_day_high, levels.prior_day_low), tick)

    return {
        "book_imbalance": _text(features.book_imbalance),
        "recent_aggressive_buy_volume": _text(buy),
        "recent_aggressive_sell_volume": _text(sell),
        "liquidity_added": _text(features.liquidity_added),
        "liquidity_cancelled": _text(features.liquidity_cancelled),
        "reload_count": str(int(features.bid_reload_count)),
        "distance_to_defended_level": _text(_defended_distance_ticks(state, direction, tick)),
        "price_velocity": _text(_price_velocity_ticks(snapshots, tick)),
        "trade_velocity": _text(features.trade_velocity),
        "spread": _text(spread_ticks),
        "short_term_volatility": _text(volatility_ticks),
        "time_of_day": _time_of_day(last.timestamp_ns),
        "distance_from_overnight_high_low": _text(overnight),
        "distance_from_prior_day_levels": _text(prior_day),
        "direction": direction,
        "stop_distance": _text(cfg.stop_ticks),
        "target_distance": _text(cfg.target_ticks),
    }


def _nearest_distance_ticks(
    ref: Decimal, levels: tuple[Decimal | None, ...], tick: Decimal,
) -> Decimal:
    present = [level for level in levels if level is not None]
    if ref <= 0 or not present:
        return Decimal("0")
    return min(abs(ref - level) for level in present) / tick


def _defended_distance_ticks(state: MarketState, direction: str, tick: Decimal) -> Decimal:
    depth = state.bid_depth if direction == "long" else state.ask_depth
    ref = _reference_price(state)
    if not depth or ref is None:
        return Decimal("0")
    largest = max(depth, key=lambda level: level.size)
    return abs(ref - largest.price) / tick


def _price_velocity_ticks(snapshots: Sequence[MarketState], tick: Decimal) -> Decimal:
    if len(snapshots) < 2:
        return Decimal("0")
    first_price = _reference_price(snapshots[0])
    last_price = _reference_price(snapshots[-1])
    if first_price is None or last_price is None:
        return Decimal("0")
    elapsed_ns = snapshots[-1].timestamp_ns - snapshots[0].timestamp_ns
    if elapsed_ns <= 0:
        return Decimal("0")
    seconds = Decimal(elapsed_ns) / Decimal(_NS_PER_SECOND)
    return abs(last_price - first_price) / tick / seconds


def _time_of_day(timestamp_ns: int) -> str:
    seconds = timestamp_ns // _NS_PER_SECOND
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(NEW_YORK)
    return dt.strftime("%H:%M:%S")


def _text(value: Decimal) -> str:
    return format(value, "f")
