"""Feed capabilities must be observed, never declared.

The dishonesty this replaces: the GUI displayed a hardcoded constant claiming
"Trade prints: AVAILABLE" regardless of what the feed delivered. In the real
recordings **71 of 200 finalized sessions contain no trades at all**, and every
one of them would still have shown trades as AVAILABLE.
"""

from __future__ import annotations

from app.market.capabilities import (
    DEPTH_ONLY_DEGRADED_THRESHOLD,
    CapabilityId,
    FeedCapabilities,
    FeedCapability,
)


def _depth(i: int = 0) -> dict[str, object]:
    return {"type": "depth_update", "timestamp": i, "symbol": "MNQ", "side": "bid",
            "price": "29500.00", "previous_size": "0", "new_size": "10"}


def _trade(i: int = 0, *, aggressor: str | None = "buy") -> dict[str, object]:
    event: dict[str, object] = {"timestamp_ns": i, "price": "29500.00", "size": "1",
                                "instrument": "MNQ", "sequence_id": i}
    if aggressor is not None:
        event["aggressor_side"] = aggressor
    return event


def test_nothing_observed_is_unverified_not_available() -> None:
    """The honest start state: absence of evidence is not evidence of a feed."""
    caps = FeedCapabilities()
    assert caps.status_of(CapabilityId.DEPTH).status is FeedCapability.UNVERIFIED
    assert caps.status_of(CapabilityId.TRADES).status is FeedCapability.UNVERIFIED
    assert caps.status_of(CapabilityId.AGGRESSOR).status is FeedCapability.UNVERIFIED


def test_observed_depth_becomes_available_with_evidence() -> None:
    caps = FeedCapabilities()
    for i in range(5):
        caps = caps.observe_event(_depth(i))
    depth = caps.status_of(CapabilityId.DEPTH)
    assert depth.status is FeedCapability.AVAILABLE
    assert "5 depth updates observed" in depth.reason


def test_a_depth_only_feed_reports_trades_as_degraded_not_available() -> None:
    """The exact measured failure: 71/200 sessions were depth-only."""
    caps = FeedCapabilities()
    for i in range(DEPTH_ONLY_DEGRADED_THRESHOLD):
        caps = caps.observe_event(_depth(i))
    trades = caps.status_of(CapabilityId.TRADES)
    assert trades.status is FeedCapability.DEGRADED
    assert "ZERO trades" in trades.reason
    assert "order-flow setups cannot qualify" in trades.reason


def test_a_few_depth_events_with_no_trades_is_only_unverified() -> None:
    """Early in a session, no trade yet is normal - not a fault."""
    caps = FeedCapabilities()
    for i in range(10):
        caps = caps.observe_event(_depth(i))
    assert caps.status_of(CapabilityId.TRADES).status is FeedCapability.UNVERIFIED


def test_observed_trades_with_aggressor_make_aggressor_and_cvd_available() -> None:
    caps = FeedCapabilities()
    for i in range(3):
        caps = caps.observe_event(_trade(i))
    assert caps.status_of(CapabilityId.TRADES).status is FeedCapability.AVAILABLE
    assert caps.status_of(CapabilityId.AGGRESSOR).status is FeedCapability.AVAILABLE
    assert caps.status_of(CapabilityId.CVD).status is FeedCapability.AVAILABLE


def test_trades_without_aggressor_degrade_aggressor_and_cvd_together() -> None:
    """CVD is derived from aggressor side and can never be better than it."""
    caps = FeedCapabilities()
    caps = caps.observe_event(_trade(1))
    caps = caps.observe_event(_trade(2, aggressor=None))
    aggressor = caps.status_of(CapabilityId.AGGRESSOR)
    assert aggressor.status is FeedCapability.DEGRADED
    assert "1 of 2 trades had no aggressor side" in aggressor.reason
    cvd = caps.status_of(CapabilityId.CVD)
    assert cvd.status is FeedCapability.DEGRADED, "CVD must inherit its source's status"


def test_liquidity_blocks_are_always_heuristic_never_native_truth() -> None:
    """A project-derived inference must never be presented as feed truth."""
    state = FeedCapabilities().status_of(CapabilityId.LIQUIDITY_BLOCKS)
    assert state.status is FeedCapability.HEURISTIC
    assert "not native feed truth" in state.reason


def test_mbo_is_structurally_unavailable_and_says_why() -> None:
    caps = FeedCapabilities()
    for i in range(50_000):  # no amount of data can conjure order IDs
        caps = caps.observe_event(_trade(i))
    state = caps.status_of(CapabilityId.MBO)
    assert state.status is FeedCapability.UNAVAILABLE
    assert "no per-order IDs" in state.reason
    assert state.usable is False


def test_unverified_and_unavailable_are_not_usable() -> None:
    assert FeedCapability.UNVERIFIED.usable is False
    assert FeedCapability.UNAVAILABLE.usable is False
    assert FeedCapability.AVAILABLE.usable is True
    assert FeedCapability.HEURISTIC.usable is True


def test_capability_ids_are_typed_so_a_typo_cannot_silently_pass() -> None:
    """caps.get("mbo") with a typo silently means 'absent' - the enum prevents it."""
    ids = {c.value for c in CapabilityId}
    assert "mbo_order_by_order" in ids
    assert "mbo" not in ids, "the old untyped key must not linger"


def test_unknown_events_change_nothing() -> None:
    caps = FeedCapabilities()
    for junk in ({"type": "heartbeat"}, {"nonsense": True}, "not a dict", None):
        caps = caps.observe_event(junk)
    assert caps.depth_events == 0 and caps.trade_events == 0


def test_observation_is_immutable_so_a_snapshot_cannot_drift() -> None:
    """The GUI reads snapshots; a mutated capability set would be a race."""
    first = FeedCapabilities()
    second = first.observe_event(_depth(1))
    assert first.depth_events == 0, "observe must not mutate in place"
    assert second.depth_events == 1


def test_legacy_mapping_view_reports_usability() -> None:
    caps = FeedCapabilities().observe_event(_trade(1))
    mapping = caps.as_mapping()
    assert mapping["trade_prints"] is True
    assert mapping["mbo_order_by_order"] is False
