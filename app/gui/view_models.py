"""Immutable snapshots the GUI renders — the only thing the Qt thread may read.

The old window called ``findChild`` per label on a 1 s timer and did catalog
scans, automation persistence, and report work inline. That starved the receiver
and froze the window. The rule now: **background workers build these frozen
snapshots; the Qt thread only formats them.** Nothing here touches disk, network,
or research, and every field is already a display-ready value or a Decimal.

Provenance is explicit: a snapshot always states whether data is delayed, how
stale the *application* is (distinct from the intentional 15-minute source
delay), and whether a capability is real, degraded, heuristic, or unavailable —
so the GUI can never imply a measurement the feed cannot support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Health(str, Enum):
    """Traffic-light state for a component, with text (never colour alone)."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    IDLE = "idle"
    LOCKED = "locked"


class Capability(str, Enum):
    """How trustworthy a displayed measurement is."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    HEURISTIC = "heuristic"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """One supervised component's state for the status bar."""

    name: str
    health: Health = Health.IDLE
    detail: str = "not started"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Coalesced market view (5–10 Hz), never per-event."""

    symbol: str = "MNQ"
    contract: str = "unknown"
    last_price: Decimal | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    mid_price: Decimal | None = None
    cumulative_delta: Decimal = Decimal("0")
    buy_volume: Decimal = Decimal("0")
    sell_volume: Decimal = Decimal("0")
    is_delayed: bool = True
    source_delay_minutes: int = 15
    processing_age_ms: int | None = None  # app lag, NOT the source delay
    event_rate_per_second: float = 0.0

    @property
    def provenance_text(self) -> str:
        """Return the honest provenance badge."""
        if self.is_delayed:
            return f"DELAYED {self.source_delay_minutes} min — offline research only"
        return "REAL-TIME"


@dataclass(frozen=True, slots=True)
class CaptureSnapshot:
    """Recording/capture health — the thing research must never starve."""

    receiver_listening: bool = False
    bookmap_connected: bool = False
    recording: bool = False
    session_id: str = ""
    current_session_drops: int = 0
    lifetime_bridge_drops: int = 0
    intake_occupancy: int = 0
    intake_capacity: int = 0
    recorder_occupancy: int = 0
    recorder_capacity: int = 0
    flush_latency_ms: float = 0.0
    persisted_per_second: float = 0.0

    @property
    def queue_pressure(self) -> float:
        """Return the fullest queue as a 0..1 fraction."""
        fractions = [
            occupancy / capacity
            for occupancy, capacity in (
                (self.intake_occupancy, self.intake_capacity),
                (self.recorder_occupancy, self.recorder_capacity),
            )
            if capacity
        ]
        return max(fractions) if fractions else 0.0

    @property
    def health(self) -> Health:
        """Any current-session loss is a failure, not a warning."""
        if self.current_session_drops:
            return Health.FAIL
        if not self.bookmap_connected:
            return Health.IDLE
        if self.queue_pressure >= 0.7:
            return Health.WARN
        return Health.OK if self.recording else Health.IDLE


@dataclass(frozen=True, slots=True)
class SetupCheck:
    """One explainable strategy sub-rule result."""

    name: str
    passed: bool
    observed: str = ""
    threshold: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProgressGateRow:
    """One profitability-ladder gate, formatted for display."""

    label: str
    status: str
    observed: str
    threshold: str


@dataclass(frozen=True, slots=True)
class ProfitabilitySnapshot:
    """How close the bot is to PROVEN profitable — never a claim, only evidence."""

    fraction: float = 0.0
    headline: str = ""
    claim_supported: bool = False
    gates: tuple[ProgressGateRow, ...] = ()
    computed: bool = False
    error: str = ""

    @property
    def summary(self) -> str:
        """A single honest line for the header."""
        if self.error:
            return f"Progress meter unavailable: {self.error}"
        if not self.computed:
            return "Progress meter: computing on the research thread…"
        claim = "supported by evidence" if self.claim_supported else "NOT claimed — unproven"
        return f"Progress to proven profitable: {self.fraction:.0%} — profitability {claim}"


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One closed simulated trade, formatted for display."""

    direction: str
    contracts: int
    entry: str
    exit: str
    net_pnl: str
    close_reason: str
    is_synthetic_fixture: bool = False

    @property
    def label(self) -> str:
        """Return a single-line description of the trade."""
        tag = " [FIXTURE]" if self.is_synthetic_fixture else ""
        return (f"{self.direction} {self.contracts} @ {self.entry} → {self.exit}  "
                f"{self.net_pnl}  ({self.close_reason}){tag}")


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    """Delayed-paper state: real outcomes only, or an honest zero."""

    mode: str = "DELAYED PAPER"
    profile_name: str = ""
    balance: Decimal = Decimal("0")
    starting_balance: Decimal = Decimal("0")
    profit_target: Decimal = Decimal("0")
    target_progress: Decimal = Decimal("0")
    drawdown_room: Decimal = Decimal("0")
    consistency_share: Decimal | None = None
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: Decimal = Decimal("0")
    open_position: str = "flat"
    setup_name: str = "none"
    setup_checks: tuple[SetupCheck, ...] = ()
    evaluations: int = 0
    top_rejections: tuple[tuple[str, int], ...] = ()
    risk_remaining: Decimal = Decimal("0")
    # --- live simulated execution --------------------------------------------
    pending_order: str = "none"
    position_entry: str = ""
    position_stop: str = ""
    position_target: str = ""
    unrealized_pnl: Decimal = Decimal("0")
    candidates: int = 0
    risk_rejected: int = 0
    risk_rejections: tuple[tuple[str, int], ...] = ()
    recent_trades: tuple[TradeRow, ...] = ()
    malformed_events: int = 0
    # (condition, passes, failures, last observed-vs-required evidence) -
    # the honest answer to "why zero candidates", worst failures first.
    condition_stats: tuple[tuple[str, int, int, str], ...] = ()
    # Analysis-stream integrity: skipped-for-analysis events and gaps.
    analysis_events_skipped: int = 0
    causality_breaks: int = 0
    window_span_seconds: float = 0.0

    @property
    def flat(self) -> bool:
        """Return whether there is no open simulated position."""
        return self.open_position in ("flat", "none", "")

    @property
    def empty_reason(self) -> str:
        """Explain zero trades honestly instead of showing a blank panel."""
        if self.trades:
            return ""
        if not self.evaluations:
            return "No setup has been evaluated yet on real delayed data."
        if self.candidates and self.risk_rejections:
            blocked = ", ".join(f"{name} ({count})" for name, count in self.risk_rejections[:3])
            return (
                f"{self.evaluations} evaluated, {self.candidates} setup(s) qualified, but no "
                f"position was opened. Blocked by: {blocked}. "
                "Risk rules are never relaxed to create a trade."
            )
        top = ", ".join(f"{name} ({count})" for name, count in self.top_rejections[:3])
        return (
            f"{self.evaluations} opportunities evaluated, none qualified. "
            f"Most common rejections: {top or 'not recorded'}. "
            "This is an honest result — thresholds are not loosened to create trades."
        )


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    """Research/worker state; canonical evidence stays separate from candidates."""

    state: str = "idle"
    active_workers: int = 0
    requested_workers: int = 0
    queued_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    canonical_trades: int = 0
    experimental_trades: int = 0
    unique_setups: int = 0
    duplicate_overlap: int = 0
    independent_days: int = 0
    throttle_reason: str = ""
    gpu_note: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Execution environment. LIVE is locked and says exactly why."""

    environment: str = "PAPER"
    connected: bool = False
    demo_armed: bool = False
    live_armed: bool = False
    live_blockers: tuple[str, ...] = ()
    prop_rules_resolved: bool = False

    @property
    def live_health(self) -> Health:
        """LIVE is always locked in this build."""
        return Health.LOCKED


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    """The single immutable object the GUI renders. Built off-thread."""

    lifecycle_state: str = "STARTING"
    market: MarketSnapshot = field(default_factory=MarketSnapshot)
    capture: CaptureSnapshot = field(default_factory=CaptureSnapshot)
    paper: PaperSnapshot = field(default_factory=PaperSnapshot)
    research: ResearchSnapshot = field(default_factory=ResearchSnapshot)
    profitability: ProfitabilitySnapshot = field(default_factory=ProfitabilitySnapshot)
    execution: ExecutionSnapshot = field(default_factory=ExecutionSnapshot)
    components: tuple[ComponentHealth, ...] = ()
    capabilities: tuple[tuple[str, Capability, str], ...] = ()
    next_action: str = ""
    blocker: str = ""

    @property
    def plain_state(self) -> str:
        """Plain-language status for a non-programmer."""
        return {
            "STARTING": "Starting…",
            "RECEIVER_LISTENING": "Waiting for Bookmap",
            "WAITING_FOR_BOOKMAP": "Waiting for Bookmap",
            "VALIDATING_STREAM": "Validating market stream",
            "RECORDING": "Recording delayed market data",
            "DELAYED_PAPER": "Delayed paper trading active",
            "RESEARCHING": "Researching finalized sessions",
            "DEGRADED": "Research paused to protect recording",
            "CAPTURE_INVALIDATED": "Session invalidated — data was dropped",
            "STOPPED": "Stopped",
        }.get(self.lifecycle_state, self.lifecycle_state.replace("_", " ").title())
