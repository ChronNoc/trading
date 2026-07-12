"""Daily observe-only market learning reports from recorded Bookmap sessions."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.market.features import compute_market_features
from app.market.state import MarketState

NANOSECONDS_PER_SECOND = 1_000_000_000
OBSERVE_ONLY_NOTICE = (
    "Observe-only learning report. It records market behavior and consistency, "
    "but it does not retrain a live model or authorize trading."
)


@dataclass(frozen=True, slots=True)
class DailyLearningConfig:
    """Thresholds used to summarize daily order-flow consistency."""

    large_block_size: Decimal = Decimal("75")
    reload_threshold: Decimal = Decimal("75")
    cvd_dominance_fraction: Decimal = Decimal("0.20")
    tick_size: Decimal = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class SessionLearningSummary:
    """Per-session market-learning observations derived from raw recorded data."""

    session_label: str
    source_mode: str
    analysis_scope: str
    valid_for_analysis: bool
    valid_for_real_training: bool
    valid_for_live_decisions: bool
    depth_updates: int
    trades: int
    start_timestamp_ns: int | None
    end_timestamp_ns: int | None
    first_price: Decimal | None
    last_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    price_direction: str
    aggressive_buy_volume: Decimal
    aggressive_sell_volume: Decimal
    cvd_delta: Decimal
    cvd_direction: str
    price_cvd_aligned: bool
    book_imbalance: Decimal
    liquidity_added: Decimal
    liquidity_cancelled: Decimal
    trade_velocity: Decimal
    short_term_volatility: Decimal
    bid_reload_count: int
    ask_reload_count: int
    large_bid_blocks: int
    large_ask_blocks: int
    possible_long_absorption: bool
    possible_short_absorption: bool
    dominant_side: str
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyConsistencySummary:
    """Whole-day consistency summary across all recorded sessions for a date."""

    trading_date: date
    generated_at_utc: datetime
    sessions: tuple[SessionLearningSummary, ...]
    total_depth_updates: int
    total_trades: int
    valid_session_count: int
    delayed_session_count: int
    consistency_score: Decimal
    recurring_patterns: tuple[str, ...]
    blockers: tuple[str, ...]
    safety_notes: tuple[str, ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return this summary as JSON-compatible values."""
        return {
            "trading_date": self.trading_date.isoformat(),
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "mode": "observe_only",
            "auto_retraining_enabled": False,
            "live_decision_ready": False,
            "total_depth_updates": self.total_depth_updates,
            "total_trades": self.total_trades,
            "session_count": len(self.sessions),
            "valid_session_count": self.valid_session_count,
            "delayed_session_count": self.delayed_session_count,
            "consistency_score": _decimal_text(self.consistency_score),
            "recurring_patterns": list(self.recurring_patterns),
            "blockers": list(self.blockers),
            "safety_notes": list(self.safety_notes),
            "sessions": [_session_to_json(session) for session in self.sessions],
        }


@dataclass(frozen=True, slots=True)
class DailyLearningReportPaths:
    """Paths written for one daily learning report."""

    directory: Path
    json_path: Path
    markdown_path: Path


def analyze_recorded_day(
    raw_root: str | Path,
    trading_date: date,
    *,
    config: DailyLearningConfig | None = None,
) -> DailyConsistencySummary:
    """Analyze all recorded Bookmap sessions under ``raw_root/YYYY-MM-DD``."""
    learning_config = config or DailyLearningConfig()
    date_dir = Path(raw_root) / trading_date.isoformat()
    sessions = tuple(
        _analyze_session(session, learning_config)
        for session in _discover_session_dirs(date_dir)
    )
    return build_daily_consistency_summary(trading_date, sessions)


def build_daily_consistency_summary(
    trading_date: date,
    sessions: Sequence[SessionLearningSummary],
    *,
    generated_at_utc: datetime | None = None,
) -> DailyConsistencySummary:
    """Build a consistency summary from already-analyzed sessions."""
    session_tuple = tuple(sessions)
    total_depth = sum(session.depth_updates for session in session_tuple)
    total_trades = sum(session.trades for session in session_tuple)
    valid_count = sum(1 for session in session_tuple if session.valid_for_analysis)
    delayed_count = sum(1 for session in session_tuple if session.source_mode == "delayed")
    consistency_score = _consistency_score(session_tuple)
    patterns, blockers = _patterns_and_blockers(session_tuple)
    safety_notes = (
        OBSERVE_ONLY_NOTICE,
        "Bookmap delayed/free data is valid for review and threshold research only.",
        "Manual labels or replay outcomes are required before any supervised model training.",
    )
    return DailyConsistencySummary(
        trading_date=trading_date,
        generated_at_utc=(generated_at_utc or datetime.now(UTC)).astimezone(UTC),
        sessions=session_tuple,
        total_depth_updates=total_depth,
        total_trades=total_trades,
        valid_session_count=valid_count,
        delayed_session_count=delayed_count,
        consistency_score=consistency_score,
        recurring_patterns=patterns,
        blockers=blockers,
        safety_notes=safety_notes,
    )


def write_daily_learning_report(
    summary: DailyConsistencySummary,
    report_root: str | Path,
) -> DailyLearningReportPaths:
    """Write JSON and Markdown daily learning reports and return their paths."""
    directory = Path(report_root) / "daily_learning" / summary.trading_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "daily_learning.json"
    markdown_path = directory / "daily_learning.md"
    json_path.write_text(
        json.dumps(summary.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_daily_learning_markdown(summary), encoding="utf-8")
    return DailyLearningReportPaths(directory=directory, json_path=json_path, markdown_path=markdown_path)


def analyze_and_write_daily_learning_report(
    raw_root: str | Path,
    report_root: str | Path,
    trading_date: date,
    *,
    config: DailyLearningConfig | None = None,
) -> DailyLearningReportPaths:
    """Analyze a recorded day and write the daily learning report artifacts."""
    summary = analyze_recorded_day(raw_root, trading_date, config=config)
    return write_daily_learning_report(summary, report_root)


def render_daily_learning_markdown(summary: DailyConsistencySummary) -> str:
    """Render a human-readable daily market-learning summary."""
    lines = [
        f"# Daily market learning - {summary.trading_date.isoformat()}",
        "",
        f"> {OBSERVE_ONLY_NOTICE}",
        "",
        "## Consistency",
        "",
        f"- Consistency score: {summary.consistency_score} / 100",
        f"- Sessions analyzed: {len(summary.sessions)}",
        f"- Valid sessions: {summary.valid_session_count}",
        f"- Delayed/free-data sessions: {summary.delayed_session_count}",
        f"- Depth updates: {summary.total_depth_updates}",
        f"- Trades: {summary.total_trades}",
        "",
    ]
    if summary.recurring_patterns:
        lines += ["## Recurring patterns", "", *[f"- {pattern}" for pattern in summary.recurring_patterns], ""]
    if summary.blockers:
        lines += ["## Blockers", "", *[f"- {blocker}" for blocker in summary.blockers], ""]
    lines += [
        "## Session table",
        "",
        "| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for session in summary.sessions:
        notes = "; ".join(session.notes) if session.notes else "none"
        lines.append(
            "| "
            f"{session.session_label} | "
            f"{session.source_mode} | "
            f"{'yes' if session.valid_for_analysis else 'no'} | "
            f"{session.price_direction} | "
            f"{session.cvd_direction} | "
            f"{'yes' if session.price_cvd_aligned else 'no'} | "
            f"{session.bid_reload_count} | "
            f"{session.ask_reload_count} | "
            f"{notes} |"
        )
    if not summary.sessions:
        lines.append("| none | none | no | unknown | unknown | no | 0 | 0 | no recordings found |")
    lines += [
        "",
        "## Safety",
        "",
        *[f"- {note}" for note in summary.safety_notes],
        "",
    ]
    return "\n".join(lines)


def _analyze_session(session_dir: Path, config: DailyLearningConfig) -> SessionLearningSummary:
    manifest = _read_manifest(session_dir / "session_manifest.json")
    depth_rows = _read_parquet_rows(session_dir / "depth.parquet")
    trade_rows = _read_parquet_rows(session_dir / "trades.parquet")
    events = _events_from_rows(depth_rows, trade_rows)
    snapshots = _snapshots_from_events(events)
    prices = _price_series(snapshots, trade_rows)
    features = _features_from_snapshots(snapshots, config)
    aggressive_buy = sum(
        (_decimal(row.get("size")) for row in trade_rows if str(row.get("aggressor_side")).lower() == "buy"),
        Decimal("0"),
    )
    aggressive_sell = sum(
        (_decimal(row.get("size")) for row in trade_rows if str(row.get("aggressor_side")).lower() == "sell"),
        Decimal("0"),
    )
    cvd_delta = aggressive_buy - aggressive_sell
    price_direction = _price_direction(prices)
    cvd_direction = _signed_direction(cvd_delta)
    price_cvd_aligned = _price_cvd_aligned(price_direction, cvd_direction)
    bid_reload_count = _side_reload_count(depth_rows, side="bid", threshold=config.reload_threshold)
    ask_reload_count = _side_reload_count(depth_rows, side="ask", threshold=config.reload_threshold)
    large_bid_blocks = _large_block_count(depth_rows, side="bid", threshold=config.large_block_size)
    large_ask_blocks = _large_block_count(depth_rows, side="ask", threshold=config.large_block_size)
    dominant_side = _dominant_side(aggressive_buy, aggressive_sell, config)
    notes = _session_notes(
        manifest,
        depth_rows,
        trade_rows,
        price_cvd_aligned=price_cvd_aligned,
        bid_reload_count=bid_reload_count,
        ask_reload_count=ask_reload_count,
        dominant_side=dominant_side,
    )

    return SessionLearningSummary(
        session_label=_session_label(session_dir),
        source_mode=str(manifest.get("source_mode", "unknown")),
        analysis_scope=str(manifest.get("analysis_scope", "unknown")),
        valid_for_analysis=bool(manifest.get("valid_for_analysis", False)),
        valid_for_real_training=bool(manifest.get("valid_for_real_training", False)),
        valid_for_live_decisions=bool(manifest.get("valid_for_live_decisions", False)),
        depth_updates=len(depth_rows),
        trades=len(trade_rows),
        start_timestamp_ns=_min_timestamp(events),
        end_timestamp_ns=_max_timestamp(events),
        first_price=prices[0] if prices else None,
        last_price=prices[-1] if prices else None,
        high_price=max(prices) if prices else None,
        low_price=min(prices) if prices else None,
        price_direction=price_direction,
        aggressive_buy_volume=aggressive_buy,
        aggressive_sell_volume=aggressive_sell,
        cvd_delta=cvd_delta,
        cvd_direction=cvd_direction,
        price_cvd_aligned=price_cvd_aligned,
        book_imbalance=features["book_imbalance"],
        liquidity_added=features["liquidity_added"],
        liquidity_cancelled=features["liquidity_cancelled"],
        trade_velocity=features["trade_velocity"],
        short_term_volatility=features["short_term_volatility"],
        bid_reload_count=bid_reload_count,
        ask_reload_count=ask_reload_count,
        large_bid_blocks=large_bid_blocks,
        large_ask_blocks=large_ask_blocks,
        possible_long_absorption=bid_reload_count > 0 and aggressive_sell >= aggressive_buy,
        possible_short_absorption=ask_reload_count > 0 and aggressive_buy >= aggressive_sell,
        dominant_side=dominant_side,
        notes=notes,
    )


def _discover_session_dirs(date_dir: Path) -> tuple[Path, ...]:
    if not date_dir.exists():
        return ()
    direct_depth = date_dir / "depth.parquet"
    direct_trades = date_dir / "trades.parquet"
    if direct_depth.exists() or direct_trades.exists():
        return (date_dir,)
    return tuple(
        child
        for child in sorted(date_dir.iterdir())
        if child.is_dir() and ((child / "depth.parquet").exists() or (child / "trades.parquet").exists())
    )


def _session_label(session_dir: Path) -> str:
    if session_dir.name.startswith("session_"):
        return f"{session_dir.parent.name}/{session_dir.name}"
    return session_dir.name


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "source_mode": "unknown",
            "analysis_scope": "unknown",
            "valid_for_analysis": False,
            "valid_for_real_training": False,
            "valid_for_live_decisions": False,
        }
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _read_parquet_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [dict(row) for row in pq.read_table(path).to_pylist()]


def _events_from_rows(
    depth_rows: Sequence[Mapping[str, object]],
    trade_rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for row in depth_rows:
        events.append(
            {
                "type": "depth_update",
                "timestamp": int(row["timestamp"]),
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "price": str(row["price"]),
                "previous_size": str(row["previous_size"]),
                "new_size": str(row["new_size"]),
            },
        )
    for row in trade_rows:
        events.append(
            {
                "type": "trade",
                "timestamp_ns": int(row["timestamp_ns"]),
                "price": str(row["price"]),
                "size": str(row["size"]),
                "aggressor_side": str(row["aggressor_side"]),
                "instrument": str(row["instrument"]),
                "sequence_id": int(row["sequence_id"]),
            },
        )
    return tuple(sorted(events, key=_event_timestamp_ns))


def _snapshots_from_events(events: Sequence[Mapping[str, object]]) -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    for event in events:
        state = state.update(event)
        snapshots.append(state)
    return tuple(snapshots)


def _features_from_snapshots(snapshots: Sequence[MarketState], config: DailyLearningConfig) -> dict[str, Decimal]:
    if not snapshots:
        return {
            "book_imbalance": Decimal("0"),
            "liquidity_added": Decimal("0"),
            "liquidity_cancelled": Decimal("0"),
            "trade_velocity": Decimal("0"),
            "short_term_volatility": Decimal("0"),
        }
    features = compute_market_features(snapshots, bid_reload_threshold=config.reload_threshold)
    return {
        "book_imbalance": features.book_imbalance,
        "liquidity_added": features.liquidity_added,
        "liquidity_cancelled": features.liquidity_cancelled,
        "trade_velocity": features.trade_velocity,
        "short_term_volatility": features.short_term_volatility,
    }


def _price_series(
    snapshots: Sequence[MarketState],
    trade_rows: Sequence[Mapping[str, object]],
) -> tuple[Decimal, ...]:
    values: list[tuple[int, Decimal]] = []
    for snapshot in snapshots:
        if snapshot.mid_price is not None:
            values.append((snapshot.timestamp_ns, snapshot.mid_price))
    for row in trade_rows:
        values.append((int(row["timestamp_ns"]), _decimal(row["price"])))
    return tuple(value for _timestamp, value in sorted(values, key=lambda item: item[0]))


def _side_reload_count(rows: Sequence[Mapping[str, object]], *, side: str, threshold: Decimal) -> int:
    by_price: dict[Decimal, bool] = {}
    waiting_for_reload: dict[Decimal, bool] = {}
    reload_count = 0
    sorted_rows = sorted(rows, key=lambda row: int(row["timestamp"]))
    for row in sorted_rows:
        if str(row.get("side")).lower() != side:
            continue
        price = _decimal(row["price"])
        size = _decimal(row["new_size"])
        was_at_or_above = by_price.get(price, False)
        is_at_or_above = size >= threshold
        if was_at_or_above and not is_at_or_above:
            waiting_for_reload[price] = True
        elif waiting_for_reload.get(price, False) and is_at_or_above:
            reload_count += 1
            waiting_for_reload[price] = False
        by_price[price] = is_at_or_above
    return reload_count


def _large_block_count(rows: Sequence[Mapping[str, object]], *, side: str, threshold: Decimal) -> int:
    prices = {
        _decimal(row["price"])
        for row in rows
        if str(row.get("side")).lower() == side and _decimal(row.get("new_size")) >= threshold
    }
    return len(prices)


def _price_direction(prices: Sequence[Decimal]) -> str:
    if len(prices) < 2:
        return "unknown"
    delta = prices[-1] - prices[0]
    if delta > Decimal("0"):
        return "up"
    if delta < Decimal("0"):
        return "down"
    return "flat"


def _signed_direction(value: Decimal) -> str:
    if value > Decimal("0"):
        return "up"
    if value < Decimal("0"):
        return "down"
    return "flat"


def _price_cvd_aligned(price_direction: str, cvd_direction: str) -> bool:
    if price_direction == "flat" or cvd_direction == "flat":
        return True
    if price_direction == "unknown" or cvd_direction == "unknown":
        return False
    return price_direction == cvd_direction


def _dominant_side(
    aggressive_buy: Decimal,
    aggressive_sell: Decimal,
    config: DailyLearningConfig,
) -> str:
    total = aggressive_buy + aggressive_sell
    if total <= Decimal("0"):
        return "unknown"
    dominance = abs(aggressive_buy - aggressive_sell) / total
    if dominance < config.cvd_dominance_fraction:
        return "balanced"
    return "buyers" if aggressive_buy > aggressive_sell else "sellers"


def _session_notes(
    manifest: Mapping[str, object],
    depth_rows: Sequence[Mapping[str, object]],
    trade_rows: Sequence[Mapping[str, object]],
    *,
    price_cvd_aligned: bool,
    bid_reload_count: int,
    ask_reload_count: int,
    dominant_side: str,
) -> tuple[str, ...]:
    notes: list[str] = []
    if str(manifest.get("source_mode", "unknown")) == "delayed":
        notes.append("delayed data: review only")
    if not bool(manifest.get("valid_for_analysis", False)):
        notes.append("not analysis-clean")
    if not depth_rows:
        notes.append("no depth data")
    if not trade_rows:
        notes.append("no trade prints")
    if not price_cvd_aligned:
        notes.append("price/CVD divergence")
    if bid_reload_count or ask_reload_count:
        notes.append("reload behavior observed")
    if dominant_side in {"buyers", "sellers"}:
        notes.append(f"{dominant_side} controlled tape")
    return tuple(notes)


def _patterns_and_blockers(
    sessions: Sequence[SessionLearningSummary],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not sessions:
        return (), ("No Bookmap recordings found for this date.",)
    patterns: list[str] = []
    blockers: list[str] = []
    dominant_counts = Counter(session.dominant_side for session in sessions)
    dominant_side, dominant_count = dominant_counts.most_common(1)[0]
    if dominant_side in {"buyers", "sellers"}:
        patterns.append(f"{dominant_side.title()} were dominant in {dominant_count} session(s).")
    aligned_count = sum(1 for session in sessions if session.price_cvd_aligned)
    if aligned_count:
        patterns.append(f"Price and CVD aligned in {aligned_count} session(s).")
    long_absorption = sum(1 for session in sessions if session.possible_long_absorption)
    short_absorption = sum(1 for session in sessions if session.possible_short_absorption)
    if long_absorption:
        patterns.append(f"Possible long absorption appeared in {long_absorption} session(s).")
    if short_absorption:
        patterns.append(f"Possible short absorption appeared in {short_absorption} session(s).")
    reload_sessions = sum(1 for session in sessions if session.bid_reload_count or session.ask_reload_count)
    if reload_sessions:
        patterns.append(f"Reload behavior appeared in {reload_sessions} session(s).")
    invalid_sessions = sum(1 for session in sessions if not session.valid_for_analysis)
    if invalid_sessions:
        blockers.append(f"{invalid_sessions} session(s) were not analysis-clean.")
    delayed_sessions = sum(1 for session in sessions if session.source_mode == "delayed")
    if delayed_sessions:
        blockers.append(f"{delayed_sessions} delayed/free-data session(s) cannot be used for live decisions.")
    if not any(session.trades for session in sessions):
        blockers.append("No trade prints were recorded, so CVD and bubble behavior cannot be learned.")
    if not any(session.depth_updates for session in sessions):
        blockers.append("No depth updates were recorded, so block/reload behavior cannot be learned.")
    return tuple(patterns), tuple(blockers)


def _consistency_score(sessions: Sequence[SessionLearningSummary]) -> Decimal:
    if not sessions:
        return Decimal("0")
    session_count = Decimal(len(sessions))
    valid_ratio = Decimal(sum(1 for session in sessions if session.valid_for_analysis)) / session_count
    aligned_ratio = Decimal(sum(1 for session in sessions if session.price_cvd_aligned)) / session_count
    reload_ratio = Decimal(sum(1 for session in sessions if session.bid_reload_count or session.ask_reload_count)) / session_count
    dominant_ratio = Decimal(sum(1 for session in sessions if session.dominant_side in {"buyers", "sellers"})) / session_count
    score = (valid_ratio * Decimal("35")) + (aligned_ratio * Decimal("25")) + (reload_ratio * Decimal("20")) + (
        dominant_ratio * Decimal("20")
    )
    return score.quantize(Decimal("0.01"))


def _event_timestamp_ns(event: Mapping[str, object]) -> int:
    if "timestamp_ns" in event:
        return int(event["timestamp_ns"])
    return int(event["timestamp"])


def _min_timestamp(events: Sequence[Mapping[str, object]]) -> int | None:
    if not events:
        return None
    return min(_event_timestamp_ns(event) for event in events)


def _max_timestamp(events: Sequence[Mapping[str, object]]) -> int | None:
    if not events:
        return None
    return max(_event_timestamp_ns(event) for event in events)


def _session_to_json(session: SessionLearningSummary) -> dict[str, object]:
    return {
        "session_label": session.session_label,
        "source_mode": session.source_mode,
        "analysis_scope": session.analysis_scope,
        "valid_for_analysis": session.valid_for_analysis,
        "valid_for_real_training": session.valid_for_real_training,
        "valid_for_live_decisions": session.valid_for_live_decisions,
        "depth_updates": session.depth_updates,
        "trades": session.trades,
        "start_timestamp_ns": session.start_timestamp_ns,
        "end_timestamp_ns": session.end_timestamp_ns,
        "first_price": _optional_decimal_text(session.first_price),
        "last_price": _optional_decimal_text(session.last_price),
        "high_price": _optional_decimal_text(session.high_price),
        "low_price": _optional_decimal_text(session.low_price),
        "price_direction": session.price_direction,
        "aggressive_buy_volume": _decimal_text(session.aggressive_buy_volume),
        "aggressive_sell_volume": _decimal_text(session.aggressive_sell_volume),
        "cvd_delta": _decimal_text(session.cvd_delta),
        "cvd_direction": session.cvd_direction,
        "price_cvd_aligned": session.price_cvd_aligned,
        "book_imbalance": _decimal_text(session.book_imbalance),
        "liquidity_added": _decimal_text(session.liquidity_added),
        "liquidity_cancelled": _decimal_text(session.liquidity_cancelled),
        "trade_velocity": _decimal_text(session.trade_velocity),
        "short_term_volatility": _decimal_text(session.short_term_volatility),
        "bid_reload_count": session.bid_reload_count,
        "ask_reload_count": session.ask_reload_count,
        "large_bid_blocks": session.large_bid_blocks,
        "large_ask_blocks": session.large_ask_blocks,
        "possible_long_absorption": session.possible_long_absorption,
        "possible_short_absorption": session.possible_short_absorption,
        "dominant_side": session.dominant_side,
        "notes": list(session.notes),
    }


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_text(value)
