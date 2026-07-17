"""Typed models for simulated paper execution (Decimal money throughout).

These describe an order's whole life: intent -> risk decision -> simulated order
-> causal fill -> managed position -> close. Every record carries full lineage
(session, setup, event sequence, strategy/config version) so any row in the
ledger can be traced back to the exact real events that produced it.

Nothing here can reach a broker: there is no transport, and no execution module
is imported (enforced by test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

# MNQ economics. A micro contract is $0.50 per 0.25 tick => $2.00 per point.
MNQ_TICK_SIZE = Decimal("0.25")
MNQ_TICK_VALUE = Decimal("0.50")


class Direction(str, Enum):
    """Trade direction."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> Decimal:
        """+1 for long, -1 for short — used for signed P&L."""
        return Decimal("1") if self is Direction.LONG else Decimal("-1")

    @property
    def opposite(self) -> "Direction":
        """Return the opposing direction."""
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class OrderStatus(str, Enum):
    """Simulated order lifecycle states."""

    CANDIDATE = "candidate"
    REJECTED_BY_RISK = "rejected_by_risk"
    PENDING = "pending"          # accepted, awaiting a causal fill event
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CloseReason(str, Enum):
    """Why a position closed."""

    STOP = "stop"
    TARGET = "target"
    TIME_STOP = "time_stop"
    SESSION_CLOSE = "session_close"
    AMBIGUOUS = "ambiguous_same_event"
    KILL_SWITCH = "kill_switch"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class SetupProvenance:
    """Where a trade came from — enough to reproduce the decision exactly."""

    session_id: str
    setup_id: str
    strategy_version: str
    contract: str
    decision_event_index: int
    decision_ts_ns: int
    conditions_passed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Lineage may never be blank: an untraceable trade is not allowed."""
        if not self.session_id or not self.setup_id or not self.strategy_version:
            raise ValueError("paper trades require session, setup, and strategy lineage")


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    """A candidate produced by a qualifying setup, before any risk decision.

    Every field must be deterministic and supplied by the strategy. A stop or
    target is never invented to make a trade possible.
    """

    direction: Direction
    entry_reference: Decimal
    stop: Decimal
    target: Decimal
    provenance: SetupProvenance

    def __post_init__(self) -> None:
        """Reject structurally invalid intents before they can become orders."""
        if self.entry_reference <= 0 or self.stop <= 0 or self.target <= 0:
            raise ValueError("entry, stop, and target must be positive prices")
        if self.direction is Direction.LONG:
            if not (self.stop < self.entry_reference < self.target):
                raise ValueError("long requires stop < entry < target")
        elif not (self.target < self.entry_reference < self.stop):
            raise ValueError("short requires target < entry < stop")

    @property
    def risk_points(self) -> Decimal:
        """Distance from entry to stop, in points (always positive)."""
        return abs(self.entry_reference - self.stop)

    @property
    def reward_points(self) -> Decimal:
        """Distance from entry to target, in points (always positive)."""
        return abs(self.target - self.entry_reference)

    @property
    def stop_distance_ticks(self) -> Decimal:
        """Stop distance expressed in ticks (for risk sizing)."""
        return self.risk_points / MNQ_TICK_SIZE


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The risk engine's verdict, always with a stable reason code."""

    approved: bool
    contracts: int
    reason_code: str
    reason: str

    @classmethod
    def reject(cls, code: str, reason: str) -> "RiskDecision":
        """Build a rejection with a stable machine-readable code."""
        return cls(approved=False, contracts=0, reason_code=code, reason=reason)


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated fill, caused by a specific later event."""

    price: Decimal
    quantity: int
    ts_ns: int
    event_index: int


@dataclass(slots=True)
class PaperOrder:
    """A simulated order awaiting or having achieved a causal fill."""

    intent: PaperOrderIntent
    contracts: int
    status: OrderStatus
    created_event_index: int
    created_ts_ns: int
    fill: Fill | None = None
    reason_code: str = ""
    reason: str = ""

    @property
    def is_open(self) -> bool:
        """Return whether this order still awaits a fill."""
        return self.status is OrderStatus.PENDING


@dataclass(slots=True)
class PaperPosition:
    """An open simulated position being managed to its stop or target."""

    direction: Direction
    contracts: int
    entry_price: Decimal
    stop: Decimal
    target: Decimal
    opened_ts_ns: int
    opened_event_index: int
    provenance: SetupProvenance
    mae_points: Decimal = Decimal("0")  # worst adverse excursion
    mfe_points: Decimal = Decimal("0")  # best favourable excursion

    def unrealized_points(self, mark: Decimal) -> Decimal:
        """Signed points of open profit at ``mark``."""
        return (mark - self.entry_price) * self.direction.sign

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        """Signed dollars of open profit at ``mark`` (Decimal, MNQ economics)."""
        return points_to_dollars(self.unrealized_points(mark), self.contracts)

    def observe(self, mark: Decimal) -> None:
        """Track MAE/MFE from a new mark (called per event)."""
        excursion = self.unrealized_points(mark)
        self.mfe_points = max(self.mfe_points, excursion)
        self.mae_points = min(self.mae_points, excursion)


@dataclass(frozen=True, slots=True)
class PaperTrade:
    """One closed simulated trade with complete economics and lineage."""

    provenance: SetupProvenance
    direction: Direction
    contracts: int
    entry_price: Decimal
    exit_price: Decimal
    stop: Decimal
    target: Decimal
    opened_ts_ns: int
    closed_ts_ns: int
    opened_event_index: int
    closed_event_index: int
    close_reason: CloseReason
    gross_pnl: Decimal
    commission: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    r_multiple: Decimal
    mae_points: Decimal
    mfe_points: Decimal
    balance_after: Decimal
    is_synthetic_fixture: bool = False  # never merged into canonical results

    @property
    def won(self) -> bool:
        """Return whether the trade closed net-positive."""
        return self.net_pnl > 0

    def to_record(self) -> dict[str, object]:
        """Return an append-safe JSON record (money as strings, never floats)."""
        return {
            "session_id": self.provenance.session_id,
            "setup_id": self.provenance.setup_id,
            "strategy_version": self.provenance.strategy_version,
            "contract": self.provenance.contract,
            "decision_event_index": self.provenance.decision_event_index,
            "direction": self.direction.value,
            "contracts": self.contracts,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "stop": str(self.stop),
            "target": str(self.target),
            "opened_ts_ns": self.opened_ts_ns,
            "closed_ts_ns": self.closed_ts_ns,
            "opened_event_index": self.opened_event_index,
            "closed_event_index": self.closed_event_index,
            "close_reason": self.close_reason.value,
            "gross_pnl": str(self.gross_pnl),
            "commission": str(self.commission),
            "slippage_cost": str(self.slippage_cost),
            "net_pnl": str(self.net_pnl),
            "r_multiple": str(self.r_multiple),
            "mae_points": str(self.mae_points),
            "mfe_points": str(self.mfe_points),
            "balance_after": str(self.balance_after),
            "is_synthetic_fixture": self.is_synthetic_fixture,
        }


def points_to_dollars(points: Decimal, contracts: int) -> Decimal:
    """Convert MNQ points to dollars for a contract count (Decimal only).

    One MNQ point = 4 ticks x $0.50 = $2.00 per contract.
    """
    per_contract = (points / MNQ_TICK_SIZE) * MNQ_TICK_VALUE
    return (per_contract * Decimal(contracts)).quantize(Decimal("0.01"))
