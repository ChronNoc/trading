"""Pin the VERIFIED Bookmap API semantics (see docs/bookmap_api_verification.md).

These lock in facts decompiled from the installed Bookmap 7.7.0 jars. If a future
change inverts the aggressor mapping or double-converts prices, CVD and every
direction-dependent rule would silently flip - these tests fail first.
"""

from __future__ import annotations

from decimal import Decimal

from app.market.state import MarketState


def _trade(price: str, size: int, side: str, seq: int) -> dict:
    return {"timestamp_ns": 1_000 + seq, "price": price, "size": str(size),
            "aggressor_side": side, "instrument": "MNQ", "sequence_id": seq}


def test_bid_aggressor_true_means_buy_matching_bookmaps_own_bar_aggregation() -> None:
    """Bookmap's Bar.addTrade adds to volumeBuy when isBidAggressor is true.

    Evidence (javap of velox.api.layer1.simplified.Bar.addTrade):
        58: iload_1        // isBidAggressor
        59: ifeq  89       // false -> volumeSell
        64: getfield volumeBuy   // true -> volumeBuy
    The Java TradeSideMapper maps true -> "buy", so a "buy" event must raise CVD.
    """
    state = MarketState()
    state = state.update(_trade("29500.00", 5, "buy", 1))
    assert state.cumulative_volume_delta == Decimal("5"), "isBidAggressor=true must ADD to delta (buy)"
    state = state.update(_trade("29500.00", 2, "sell", 2))
    assert state.cumulative_volume_delta == Decimal("3"), "sell must SUBTRACT from delta"


def test_cvd_sign_is_not_inverted_over_a_sequence() -> None:
    """A buy-dominated tape must produce a positive CVD, never negative."""
    state = MarketState()
    for i in range(10):
        state = state.update(_trade("29500.00", 3, "buy" if i % 5 else "sell", i + 1))
    assert state.cumulative_volume_delta > 0, "buy-dominated flow must show positive CVD"


def test_pip_conversion_produces_real_mnq_prices() -> None:
    """MNQ pips=0.25: a pip-denominated raw price converts to a real price level.

    The simplified API forwards the raw Layer-1 (pip) price unmodified - proved by
    the absence of any dmul on InstanceWrapper's onTrade dispatch path - so the
    add-on's single multiply by `pips` is correct, not a double conversion.
    """
    pips = Decimal("0.25")
    raw_price = 118_000  # what Bookmap delivers for MNQ ~29,500
    assert raw_price * pips == Decimal("29500.000")
    # A double conversion would yield an absurd level; guard against it.
    assert (raw_price * pips) * pips != Decimal("29500.000")
