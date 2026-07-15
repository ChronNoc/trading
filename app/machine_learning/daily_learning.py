"""Daily observe-only market learning reports from recorded Bookmap sessions."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.market.state import MarketState
from app.research.real_episodes import load_completed_real_outcomes
from app.research.replay_loader import stream_session_events_with_stats
from app.research.session_catalog import SessionEntry, classify_manifest

NANOSECONDS_PER_SECOND = 1_000_000_000
OBSERVE_ONLY_NOTICE = (
    "Observe-only learning report. It records data quality and market behavior, "
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
    provenance: str = "UNKNOWN"
    finalized: bool = False
    data_quality_ok: bool = False
    ordering_mode: str = "unknown"
    same_timestamp_collisions: int = 0
    trade_sequence_gaps: int = 0
    missed_trade_events: int = 0


@dataclass(frozen=True, slots=True)
class DailyConsistencySummary:
    """Whole-day report with quality, observation, and performance separated."""

    trading_date: date
    generated_at_utc: datetime
    sessions: tuple[SessionLearningSummary, ...]
    total_depth_updates: int
    total_trades: int
    valid_session_count: int
    delayed_session_count: int
    market_observation_score: Decimal
    recurring_patterns: tuple[str, ...]
    blockers: tuple[str, ...]
    safety_notes: tuple[str, ...]
    completed_strategy_outcomes: int = 0
    ledger_eligible_outcomes: int = 0
    strategy_performance_status: str = "not_available_no_completed_real_outcomes"

    @property
    def consistency_score(self) -> Decimal:
        """Backward-compatible alias for the non-performance observation score."""
        return self.market_observation_score

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
            "data_quality": {
                "valid_sessions": self.valid_session_count,
                "total_sessions": len(self.sessions),
                "depth_updates": self.total_depth_updates,
                "trades": self.total_trades,
                "delayed_sessions": self.delayed_session_count,
                "quality_clean": bool(self.sessions) and all(
                    session.data_quality_ok for session in self.sessions
                ),
            },
            "market_observations": {
                "descriptive_score": _decimal_text(self.market_observation_score),
                "is_performance_metric": False,
                "recurring_patterns": list(self.recurring_patterns),
            },
            "strategy_performance": {
                "status": self.strategy_performance_status,
                "completed_outcomes": self.completed_strategy_outcomes,
                "ledger_eligible_outcomes": self.ledger_eligible_outcomes,
                "profitability_claim_available": False,
            },
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
    processed_root: str | Path | None = None,
) -> DailyConsistencySummary:
    """Analyze all recorded Bookmap sessions under ``raw_root/YYYY-MM-DD``."""
    learning_config = config or DailyLearningConfig()
    date_dir = Path(raw_root) / trading_date.isoformat()
    sessions = tuple(
        _analyze_session(session, learning_config)
        for session in _discover_session_dirs(date_dir)
    )
    derived_processed_root = Path(processed_root) if processed_root is not None else Path(raw_root).parent / "processed"
    outcomes = tuple(
        outcome
        for outcome in load_completed_real_outcomes(derived_processed_root)
        if outcome.trading_day == trading_date.isoformat()
    )
    return build_daily_consistency_summary(
        trading_date,
        sessions,
        completed_strategy_outcomes=len(outcomes),
        ledger_eligible_outcomes=len(outcomes),
    )


def build_daily_consistency_summary(
    trading_date: date,
    sessions: Sequence[SessionLearningSummary],
    *,
    generated_at_utc: datetime | None = None,
    completed_strategy_outcomes: int = 0,
    ledger_eligible_outcomes: int = 0,
) -> DailyConsistencySummary:
    """Build a consistency summary from already-analyzed sessions."""
    session_tuple = tuple(sessions)
    total_depth = sum(session.depth_updates for session in session_tuple)
    total_trades = sum(session.trades for session in session_tuple)
    valid_count = sum(1 for session in session_tuple if session.valid_for_analysis)
    delayed_count = sum(1 for session in session_tuple if session.source_mode == "delayed")
    observation_score = _consistency_score(session_tuple)
    patterns, base_blockers = _patterns_and_blockers(session_tuple)
    blockers = list(base_blockers)
    if ledger_eligible_outcomes == 0:
        blockers.append("No completed quality-gated real strategy outcomes exist for this trading day.")
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
        market_observation_score=observation_score,
        recurring_patterns=patterns,
        blockers=tuple(blockers),
        safety_notes=safety_notes,
        completed_strategy_outcomes=completed_strategy_outcomes,
        ledger_eligible_outcomes=ledger_eligible_outcomes,
        strategy_performance_status=(
            "preliminary_real_outcomes_not_validated"
            if ledger_eligible_outcomes > 0
            else "not_available_no_completed_real_outcomes"
        ),
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
    processed_root: str | Path | None = None,
) -> DailyLearningReportPaths:
    """Analyze a recorded day and write the daily learning report artifacts."""
    summary = analyze_recorded_day(
        raw_root,
        trading_date,
        config=config,
        processed_root=processed_root,
    )
    return write_daily_learning_report(summary, report_root)


def render_daily_learning_markdown(summary: DailyConsistencySummary) -> str:
    """Render a human-readable daily market-learning summary."""
    lines = [
        f"# Daily market learning - {summary.trading_date.isoformat()}",
        "",
        f"> {OBSERVE_ONLY_NOTICE}",
        "",
        "## Data quality and coverage",
        "",
        f"- Sessions analyzed: {len(summary.sessions)}",
        f"- Valid sessions: {summary.valid_session_count}",
        f"- Delayed/free-data sessions: {summary.delayed_session_count}",
        f"- Depth updates: {summary.total_depth_updates}",
        f"- Trades: {summary.total_trades}",
        "",
        "## Market observations (descriptive, not strategy performance)",
        "",
        f"- Observation coverage score: {summary.market_observation_score} / 100",
        "- This score describes recording/market features; it is not win rate, expectancy, or profitability.",
        "",
    ]
    if summary.recurring_patterns:
        lines += ["## Recurring patterns", "", *[f"- {pattern}" for pattern in summary.recurring_patterns], ""]
    if summary.blockers:
        lines += ["## Blockers", "", *[f"- {blocker}" for blocker in summary.blockers], ""]
    lines += [
        "## Strategy performance",
        "",
        f"- Status: {summary.strategy_performance_status}",
        f"- Completed real outcomes: {summary.completed_strategy_outcomes}",
        f"- Ledger-eligible outcomes: {summary.ledger_eligible_outcomes}",
        "- No profitability claim is available from this daily observation report.",
        "",
    ]
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


@dataclass(slots=True)
class _StreamingSessionAccumulator:
    """Constant-memory descriptive statistics for one replay stream."""

    config: DailyLearningConfig
    state: MarketState = field(default_factory=MarketState)
    depth_updates: int = 0
    trades: int = 0
    start_timestamp_ns: int | None = None
    end_timestamp_ns: int | None = None
    first_price: Decimal | None = None
    last_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    aggressive_buy: Decimal = Decimal("0")
    aggressive_sell: Decimal = Decimal("0")
    liquidity_added: Decimal = Decimal("0")
    liquidity_cancelled: Decimal = Decimal("0")
    bid_reload_count: int = 0
    ask_reload_count: int = 0
    price_count: int = 0
    price_mean: Decimal = Decimal("0")
    price_m2: Decimal = Decimal("0")
    _above_threshold: dict[tuple[str, Decimal], bool] = field(default_factory=dict)
    _waiting_reload: dict[tuple[str, Decimal], bool] = field(default_factory=dict)
    _large_bid_prices: set[Decimal] = field(default_factory=set)
    _large_ask_prices: set[Decimal] = field(default_factory=set)

    def apply(self, event_kind: str, timestamp_ns: int, payload: Mapping[str, object]) -> None:
        """Apply one replay event and update constant-memory statistics."""
        self.start_timestamp_ns = timestamp_ns if self.start_timestamp_ns is None else min(self.start_timestamp_ns, timestamp_ns)
        self.end_timestamp_ns = timestamp_ns if self.end_timestamp_ns is None else max(self.end_timestamp_ns, timestamp_ns)
        if event_kind == "depth":
            self.depth_updates += 1
            side = str(payload["side"]).lower()
            price = _decimal(payload["price"])
            previous_size = _decimal(payload["previous_size"])
            new_size = _decimal(payload["new_size"])
            change = new_size - previous_size
            if change > 0:
                self.liquidity_added += change
            elif change < 0:
                self.liquidity_cancelled += -change
            key = (side, price)
            was_above = self._above_threshold.get(key, False)
            is_above = new_size >= self.config.reload_threshold
            if was_above and not is_above:
                self._waiting_reload[key] = True
            elif self._waiting_reload.get(key, False) and is_above:
                if side == "bid":
                    self.bid_reload_count += 1
                else:
                    self.ask_reload_count += 1
                self._waiting_reload[key] = False
            self._above_threshold[key] = is_above
            if new_size >= self.config.large_block_size:
                (self._large_bid_prices if side == "bid" else self._large_ask_prices).add(price)
        else:
            self.trades += 1
            size = _decimal(payload["size"])
            if str(payload["aggressor_side"]).lower() == "buy":
                self.aggressive_buy += size
            else:
                self.aggressive_sell += size
            self.observe_price(_decimal(payload["price"]))
        self.state = self.state.update(payload)
        if self.state.mid_price is not None:
            self.observe_price(self.state.mid_price)

    def observe_price(self, price: Decimal) -> None:
        """Update first/last/extremes and Welford variance."""
        if self.first_price is None:
            self.first_price = price
            self.high_price = price
            self.low_price = price
        self.last_price = price
        self.high_price = price if self.high_price is None else max(self.high_price, price)
        self.low_price = price if self.low_price is None else min(self.low_price, price)
        self.price_count += 1
        delta = price - self.price_mean
        self.price_mean += delta / Decimal(self.price_count)
        self.price_m2 += delta * (price - self.price_mean)

    @property
    def volatility(self) -> Decimal:
        """Return population volatility of all observed reference prices."""
        if self.price_count < 2:
            return Decimal("0")
        return (self.price_m2 / Decimal(self.price_count)).sqrt()

    @property
    def book_imbalance(self) -> Decimal:
        """Return final visible-book imbalance."""
        bids = sum((level.size for level in self.state.bid_depth), Decimal("0"))
        asks = sum((level.size for level in self.state.ask_depth), Decimal("0"))
        total = bids + asks
        return (bids - asks) / total if total > 0 else Decimal("0")

    @property
    def trade_velocity(self) -> Decimal:
        """Return aggressive contracts per second over the analyzed span."""
        if self.start_timestamp_ns is None or self.end_timestamp_ns is None:
            return Decimal("0")
        elapsed = Decimal(self.end_timestamp_ns - self.start_timestamp_ns) / Decimal(NANOSECONDS_PER_SECOND)
        return (self.aggressive_buy + self.aggressive_sell) / elapsed if elapsed > 0 else Decimal("0")


def _analyze_session(session_dir: Path, config: DailyLearningConfig) -> SessionLearningSummary:
    manifest_path = session_dir / "session_manifest.json"
    manifest = _read_manifest(manifest_path)
    entry = classify_manifest(manifest, manifest_path) if manifest_path.exists() else None
    if entry is not None and entry.finalized and not entry.eligible_for_analysis:
        return _manifest_only_summary(
            session_dir,
            manifest,
            entry,
            "manifest-only coverage; raw replay skipped because session is invalid",
        )
    has_closed_parts = any(
        any((session_dir / name).glob("part-*.parquet"))
        for name in ("depth_parts", "trade_parts")
    )
    if entry is not None and entry.active and not has_closed_parts:
        return _manifest_only_summary(
            session_dir,
            manifest,
            entry,
            "manifest-only coverage; legacy active session has no safely closed parts",
        )
    iterator, replay_stats = stream_session_events_with_stats(session_dir)
    accumulator = _StreamingSessionAccumulator(config)
    for event in iterator:
        accumulator.apply(event.kind, event.timestamp_ns, event.payload)

    source_mode = (
        "delayed"
        if entry is not None and entry.is_delayed
        else str(manifest.get("source_mode", "unknown"))
    )
    provenance = entry.provenance if entry is not None else "UNKNOWN"
    analysis_scope = "delayed_market_data" if source_mode == "delayed" else str(manifest.get("analysis_scope", "unknown"))
    manifest_quality = manifest.get("data_quality", {})
    manifest_quality_ok = (
        bool(manifest_quality.get("ok", True))
        if isinstance(manifest_quality, dict)
        else True
    )
    data_quality_ok = replay_stats.continuity_ok and manifest_quality_ok
    valid_for_analysis = bool(entry and entry.eligible_for_analysis and data_quality_ok)
    cvd_delta = accumulator.aggressive_buy - accumulator.aggressive_sell
    prices = tuple(
        price
        for price in (accumulator.first_price, accumulator.last_price)
        if price is not None
    )
    price_direction = _price_direction(prices)
    cvd_direction = _signed_direction(cvd_delta)
    price_cvd_aligned = _price_cvd_aligned(price_direction, cvd_direction)
    dominant_side = _dominant_side(accumulator.aggressive_buy, accumulator.aggressive_sell, config)
    notes: list[str] = []
    if source_mode == "delayed":
        notes.append("delayed data: review only")
    if entry is not None and entry.active:
        notes.append("active session: closed parts only, partial coverage")
    if not valid_for_analysis:
        notes.append("not analysis-clean")
    if accumulator.depth_updates == 0:
        notes.append("no depth data")
    if accumulator.trades == 0:
        notes.append("no trade prints")
    if not price_cvd_aligned:
        notes.append("price/CVD divergence")
    if accumulator.bid_reload_count or accumulator.ask_reload_count:
        notes.append("reload behavior observed")
    if dominant_side in {"buyers", "sellers"}:
        notes.append(f"{dominant_side} controlled tape")
    if replay_stats.ordering_ambiguous:
        notes.append("old recording has same-timestamp ordering ambiguity")
    if replay_stats.missed_trade_events:
        notes.append(f"trade sequence indicates {replay_stats.missed_trade_events} missing event(s)")

    return SessionLearningSummary(
        session_label=_session_label(session_dir),
        source_mode=source_mode,
        analysis_scope=analysis_scope,
        valid_for_analysis=valid_for_analysis,
        valid_for_real_training=bool(entry and entry.eligible_for_order_flow_replay and data_quality_ok),
        valid_for_live_decisions=bool(entry and entry.valid_for_live_decisions and data_quality_ok),
        depth_updates=accumulator.depth_updates,
        trades=accumulator.trades,
        start_timestamp_ns=accumulator.start_timestamp_ns,
        end_timestamp_ns=accumulator.end_timestamp_ns,
        first_price=accumulator.first_price,
        last_price=accumulator.last_price,
        high_price=accumulator.high_price,
        low_price=accumulator.low_price,
        price_direction=price_direction,
        aggressive_buy_volume=accumulator.aggressive_buy,
        aggressive_sell_volume=accumulator.aggressive_sell,
        cvd_delta=cvd_delta,
        cvd_direction=cvd_direction,
        price_cvd_aligned=price_cvd_aligned,
        book_imbalance=accumulator.book_imbalance,
        liquidity_added=accumulator.liquidity_added,
        liquidity_cancelled=accumulator.liquidity_cancelled,
        trade_velocity=accumulator.trade_velocity,
        short_term_volatility=accumulator.volatility,
        bid_reload_count=accumulator.bid_reload_count,
        ask_reload_count=accumulator.ask_reload_count,
        large_bid_blocks=len(accumulator._large_bid_prices),
        large_ask_blocks=len(accumulator._large_ask_prices),
        possible_long_absorption=(
            accumulator.bid_reload_count > 0 and accumulator.aggressive_sell >= accumulator.aggressive_buy
        ),
        possible_short_absorption=(
            accumulator.ask_reload_count > 0 and accumulator.aggressive_buy >= accumulator.aggressive_sell
        ),
        dominant_side=dominant_side,
        notes=tuple(notes),
        provenance=provenance,
        finalized=bool(entry and entry.finalized),
        data_quality_ok=data_quality_ok,
        ordering_mode=replay_stats.ordering_mode,
        same_timestamp_collisions=replay_stats.same_timestamp_collisions,
        trade_sequence_gaps=replay_stats.trade_sequence_gaps,
        missed_trade_events=replay_stats.missed_trade_events,
    )


def _manifest_only_summary(
    session_dir: Path,
    manifest: Mapping[str, object],
    entry: SessionEntry,
    coverage_note: str,
) -> SessionLearningSummary:
    """Represent a session from its manifest without opening unsafe raw files."""
    source_mode = "delayed" if entry.is_delayed else str(manifest.get("source_mode", "unknown"))
    notes = (
        coverage_note,
        *entry.reasons,
    )
    return SessionLearningSummary(
        session_label=_session_label(session_dir),
        source_mode=source_mode,
        analysis_scope=(
            "delayed_market_data" if source_mode == "delayed" else str(manifest.get("analysis_scope", "unknown"))
        ),
        valid_for_analysis=False,
        valid_for_real_training=False,
        valid_for_live_decisions=False,
        depth_updates=entry.depth_updates,
        trades=entry.trades,
        start_timestamp_ns=None,
        end_timestamp_ns=None,
        first_price=None,
        last_price=None,
        high_price=None,
        low_price=None,
        price_direction="unknown",
        aggressive_buy_volume=Decimal("0"),
        aggressive_sell_volume=Decimal("0"),
        cvd_delta=Decimal("0"),
        cvd_direction="flat",
        price_cvd_aligned=False,
        book_imbalance=Decimal("0"),
        liquidity_added=Decimal("0"),
        liquidity_cancelled=Decimal("0"),
        trade_velocity=Decimal("0"),
        short_term_volatility=Decimal("0"),
        bid_reload_count=0,
        ask_reload_count=0,
        large_bid_blocks=0,
        large_ask_blocks=0,
        possible_long_absorption=False,
        possible_short_absorption=False,
        dominant_side="unknown",
        notes=tuple(str(note) for note in notes),
        provenance=entry.provenance,
        finalized=entry.finalized,
        data_quality_ok=False,
        ordering_mode="not_replayed_invalid_manifest",
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
        if child.is_dir()
        and (
            (child / "session_manifest.json").exists()
            or (child / "depth.parquet").exists()
            or (child / "trades.parquet").exists()
            or (child / "depth_parts").is_dir()
            or (child / "trade_parts").is_dir()
        )
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
        "provenance": session.provenance,
        "finalized": session.finalized,
        "data_quality_ok": session.data_quality_ok,
        "ordering_mode": session.ordering_mode,
        "same_timestamp_collisions": session.same_timestamp_collisions,
        "trade_sequence_gaps": session.trade_sequence_gaps,
        "missed_trade_events": session.missed_trade_events,
    }


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_text(value)
