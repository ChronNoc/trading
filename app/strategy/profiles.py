"""Named strategy threshold profiles.

Two profiles, chosen explicitly (never silently):

* ``canonical`` - the honest, deterministic order-flow strategy. RTH-only,
  90-contract blocks, 400 absorption, CVD >= 25. This is the profile whose
  results may ever be discussed as the strategy's real behaviour.
* ``relaxed`` - a RESEARCH profile that lowers every liquidity/flow threshold
  and turns OFF the RTH opening-observation gate so setups actually form on
  thin books and outside regular hours. It exists to produce enough labeled
  outcomes for per-session model training. Its trades are NOT the canonical
  strategy and carry no profitability meaning; the GUI and ledger label them
  ``relaxed`` so the two can never be confused.

Choosing ``relaxed`` does not fabricate anything: it evaluates the same
deterministic checklist with lower numeric thresholds. Nothing here can place
a broker order - delayed data still cannot reach execution, and LIVE stays
locked regardless of profile.
"""

from __future__ import annotations

from decimal import Decimal

from app.strategy.order_flow import OrderFlowThresholds

CANONICAL = "canonical"
RELAXED = "relaxed"
PROFILES = (CANONICAL, RELAXED)


def normalize_profile(name: str | None) -> str:
    """Return a valid profile name, defaulting to canonical."""
    value = (name or CANONICAL).strip().lower()
    return value if value in PROFILES else CANONICAL


def thresholds_for_profile(
    profile: str,
    *,
    tick_size: Decimal,
    large_block_minimum: Decimal,
    absorption_volume_minimum: Decimal,
) -> OrderFlowThresholds:
    """Build the OrderFlowThresholds for a profile.

    ``canonical`` honours the config's block/absorption values (the real
    strategy). ``relaxed`` overrides them with lower research thresholds and
    disables the RTH gate.
    """
    if normalize_profile(profile) == RELAXED:
        # Measured real delayed MNQ book: ~20 levels/side, top sizes 13-63,
        # thin RTH stretches. These lower bounds let setups form there and
        # overnight, producing labeled outcomes for per-session training.
        return OrderFlowThresholds(
            tick_size=tick_size,
            large_block_minimum=Decimal("20"),
            absorption_volume_minimum=Decimal("80"),
            follow_through_volume_minimum=Decimal("15"),
            loading_liquidity_minimum=Decimal("15"),
            cvd_support_minimum=Decimal("8"),
            durable_block_min_snapshots=2,
            min_reload_count=0,
            enforce_opening_observation=False,
            # Judge the block over the recent tape and tolerate a wider zone -
            # a thin real book defends a range, not a single price.
            block_move_tolerance_ticks=Decimal("30"),
            block_stability_tail_seconds=Decimal("30"),
            absorption_max_progress_ticks=Decimal("16"),
        )
    return OrderFlowThresholds(
        tick_size=tick_size,
        large_block_minimum=large_block_minimum,
        absorption_volume_minimum=absorption_volume_minimum,
    )
