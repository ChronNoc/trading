"""Typed feed capabilities, OBSERVED from the real stream rather than declared.

The dishonesty this replaces: the GUI showed a hardcoded constant claiming
"Trade prints: AVAILABLE" no matter what the feed actually delivered. In the real
recordings **71 of 200 finalized sessions contain no trades at all** (depth-only),
and every one of them would still have displayed trades as AVAILABLE. A panel
that reports a capability the feed is not supplying is worse than one that
reports nothing.

So a capability here is a measurement:

* ``UNVERIFIED`` — nothing has been observed yet. The honest start state; it is
  never silently upgraded to AVAILABLE.
* ``AVAILABLE`` — events of this kind have genuinely arrived and carried the
  required field.
* ``DEGRADED`` — the feed supplies this, but something is wrong with it (e.g.
  depth is flowing steadily while trades have never appeared).
* ``HEURISTIC`` — derived by this project, not supplied by the feed. Liquidity
  blocks are inferred from depth persistence; they are not native Bookmap truth.
* ``UNAVAILABLE`` — structurally impossible on this feed (the bridge exposes no
  order IDs, so MBO/native iceberg detection cannot exist).

Ids are an enum, not strings: ``caps.get("mbo")`` with a typo silently returns
"absent", which is how a capability check quietly becomes a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

# Depth events observed with zero trades before trades are called DEGRADED.
# A real MNQ session prints trades continuously; thousands of depth updates with
# no trade means the trade stream is genuinely not arriving.
DEPTH_ONLY_DEGRADED_THRESHOLD = 2_000


class FeedCapability(str, Enum):
    """How trustworthy a feed measurement is. Ordered worst -> best."""

    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"
    DEGRADED = "degraded"
    HEURISTIC = "heuristic"
    AVAILABLE = "available"

    @property
    def usable(self) -> bool:
        """Whether a strategy may rely on this at all."""
        return self in (FeedCapability.AVAILABLE, FeedCapability.HEURISTIC,
                        FeedCapability.DEGRADED)


class CapabilityId(str, Enum):
    """Typed capability keys. A string typo would silently disable a check."""

    DEPTH = "aggregated_depth"
    TRADES = "trade_prints"
    AGGRESSOR = "aggressor_side"
    CVD = "cumulative_delta"
    LIQUIDITY_BLOCKS = "liquidity_blocks"
    MBO = "mbo_order_by_order"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """One capability with the evidence for its current status."""

    id: CapabilityId
    label: str
    status: FeedCapability
    reason: str

    @property
    def usable(self) -> bool:
        """Whether a strategy may rely on this capability."""
        return self.status.usable


@dataclass(frozen=True, slots=True)
class FeedCapabilities:
    """The observed capability set for one session. Immutable; evolve via ``observe``."""

    depth_events: int = 0
    trade_events: int = 0
    aggressor_events: int = 0

    def observe_depth(self) -> "FeedCapabilities":
        """Record one observed depth update."""
        return replace(self, depth_events=self.depth_events + 1)

    def observe_trade(self, *, has_aggressor: bool) -> "FeedCapabilities":
        """Record one observed trade, noting whether it carried an aggressor side."""
        return replace(
            self,
            trade_events=self.trade_events + 1,
            aggressor_events=self.aggressor_events + (1 if has_aggressor else 0),
        )

    def observe_event(self, event: object) -> "FeedCapabilities":
        """Update from one raw market event; unknown shapes change nothing."""
        if not isinstance(event, dict):
            return self
        if event.get("type") == "depth_update":
            return self.observe_depth()
        if "timestamp_ns" in event and "sequence_id" in event:
            return self.observe_trade(has_aggressor=bool(event.get("aggressor_side")))
        return self

    # -- derived, honest status ------------------------------------------------

    def status_of(self, capability: CapabilityId) -> CapabilityState:
        """Return the evidence-backed status of one capability."""
        return {state.id: state for state in self.states()}[capability]

    def states(self) -> tuple[CapabilityState, ...]:
        """Return every capability with its observed status and evidence."""
        return (
            self._depth_state(),
            self._trades_state(),
            self._aggressor_state(),
            self._cvd_state(),
            CapabilityState(
                CapabilityId.LIQUIDITY_BLOCKS, "Liquidity blocks / reloads / absorption",
                FeedCapability.HEURISTIC,
                "inferred from depth persistence by this project; not native feed truth",
            ),
            CapabilityState(
                CapabilityId.MBO, "MBO / native iceberg", FeedCapability.UNAVAILABLE,
                "the bridge exposes no per-order IDs, so order-by-order data cannot exist",
            ),
        )

    def _depth_state(self) -> CapabilityState:
        if self.depth_events == 0:
            return CapabilityState(CapabilityId.DEPTH, "Aggregated depth",
                                   FeedCapability.UNVERIFIED, "no depth update observed yet")
        return CapabilityState(CapabilityId.DEPTH, "Aggregated depth", FeedCapability.AVAILABLE,
                               f"{self.depth_events:,} depth updates observed")

    def _trades_state(self) -> CapabilityState:
        if self.trade_events:
            return CapabilityState(CapabilityId.TRADES, "Trade prints", FeedCapability.AVAILABLE,
                                   f"{self.trade_events:,} trades observed")
        # The measured failure: 71/200 sessions were depth-only. Depth pouring in
        # with no trades is evidence the trade stream is missing, not "available".
        if self.depth_events >= DEPTH_ONLY_DEGRADED_THRESHOLD:
            return CapabilityState(
                CapabilityId.TRADES, "Trade prints", FeedCapability.DEGRADED,
                f"{self.depth_events:,} depth updates but ZERO trades - "
                "the trade stream is not arriving; order-flow setups cannot qualify",
            )
        return CapabilityState(CapabilityId.TRADES, "Trade prints",
                               FeedCapability.UNVERIFIED, "no trade observed yet")

    def _aggressor_state(self) -> CapabilityState:
        if self.trade_events == 0:
            return CapabilityState(CapabilityId.AGGRESSOR, "Aggressor side",
                                   FeedCapability.UNVERIFIED, "no trade observed yet")
        if self.aggressor_events == self.trade_events:
            return CapabilityState(
                CapabilityId.AGGRESSOR, "Aggressor side", FeedCapability.AVAILABLE,
                f"all {self.trade_events:,} trades carried an aggressor side "
                "(TradeInfo.isBidAggressor, verified)",
            )
        missing = self.trade_events - self.aggressor_events
        return CapabilityState(
            CapabilityId.AGGRESSOR, "Aggressor side", FeedCapability.DEGRADED,
            f"{missing:,} of {self.trade_events:,} trades had no aggressor side",
        )

    def _cvd_state(self) -> CapabilityState:
        aggressor = self._aggressor_state()
        if aggressor.status is FeedCapability.AVAILABLE:
            return CapabilityState(CapabilityId.CVD, "CVD / rolling delta",
                                   FeedCapability.AVAILABLE, "derived from verified aggressor side")
        # CVD can never be better than the aggressor data it is built from.
        return CapabilityState(CapabilityId.CVD, "CVD / rolling delta", aggressor.status,
                               f"inherits aggressor-side status: {aggressor.reason}")

    def as_mapping(self) -> dict[str, bool]:
        """Legacy view: capability id -> usable. Prefer ``status_of``."""
        return {state.id.value: state.usable for state in self.states()}
