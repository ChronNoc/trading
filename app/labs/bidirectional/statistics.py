"""Honest statistics for the Bidirectional Paper Trading Lab.

Computed purely from the engine's closed legs and setups. Nothing here is tuned
to flatter the strategy: costs are subtracted, both legs of every paired setup
are counted together, and open positions are excluded from realised figures.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import median

from app.labs.bidirectional.engine import ARMED, CANCELLED, COMPLETED, LabEngine


def _avg(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def open_positions(engine: LabEngine) -> list[dict[str, object]]:
    """The live open legs (long/short) with their current management state."""
    tick = engine.tick
    rows: list[dict[str, object]] = []
    for leg in engine.open_legs:
        rows.append({
            "setup_id": leg.setup_id,
            "side": leg.side,
            "qty": leg.qty,
            "entry": str(leg.entry_price),
            "stop": str(leg.stop),
            "break_even": leg.break_even_active,
            "trailing": leg.trailing_active,
            "mfe_ticks": str((leg.mfe_points / tick).quantize(Decimal("0.1"))),
            "mae_ticks": str((leg.mae_points / tick).quantize(Decimal("0.1"))),
            "unrealized": str(engine.leg_unrealized_pnl(leg).quantize(Decimal("0.01"))),
        })
    return rows


def recent_trades(engine: LabEngine, limit: int = 30) -> list[dict[str, object]]:
    """The most recent closed legs (newest first) for the live orders view."""
    rows: list[dict[str, object]] = []
    for leg in reversed(engine.closed_legs[-limit:]):
        rows.append({
            "setup_id": leg.setup_id,
            "side": leg.side,
            "qty": leg.qty,
            "entry": str(leg.entry_price),
            "exit": str(leg.exit_price) if leg.exit_price is not None else "",
            "reason": leg.exit_reason,
            "net": str(leg.net_pnl.quantize(Decimal("0.01"))),
        })
    return rows


def compute_statistics(engine: LabEngine) -> dict[str, object]:
    """Return a structured, section-keyed statistics snapshot (all realised)."""
    closed = engine.closed_legs
    wins = [leg for leg in closed if leg.net_pnl > 0]
    losses = [leg for leg in closed if leg.net_pnl <= 0]
    gross_profit = sum((leg.gross_pnl for leg in closed if leg.gross_pnl > 0), Decimal("0"))
    gross_loss = sum((leg.gross_pnl for leg in closed if leg.gross_pnl < 0), Decimal("0"))
    total_commission = sum((leg.commission for leg in closed), Decimal("0"))
    net = sum((leg.net_pnl for leg in closed), Decimal("0"))
    profit_factor = (
        (sum((leg.net_pnl for leg in wins), Decimal("0")) /
         abs(sum((leg.net_pnl for leg in losses), Decimal("0"))))
        if losses and sum((leg.net_pnl for leg in losses), Decimal("0")) != 0 else Decimal("0")
    )
    general = {
        "total_setups": len(engine.setups),
        "total_trades": len(closed),
        "open_legs": len(engine.open_legs),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (Decimal(len(wins)) / Decimal(len(closed))) if closed else Decimal("0"),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "total_commission": total_commission,
        "net_pnl": net,
        "avg_winner": _avg([leg.net_pnl for leg in wins]),
        "avg_loser": _avg([leg.net_pnl for leg in losses]),
        "largest_winner": max((leg.net_pnl for leg in closed), default=Decimal("0")),
        "largest_loser": min((leg.net_pnl for leg in closed), default=Decimal("0")),
        "profit_factor": profit_factor,
        "expectancy_per_trade": _avg([leg.net_pnl for leg in closed]),
        "stop_out_rate": (Decimal(sum(1 for leg in closed if leg.exit_reason == "stop"))
                          / Decimal(len(closed))) if closed else Decimal("0"),
        "break_even_rate": (Decimal(sum(1 for leg in closed if leg.break_even_active))
                            / Decimal(len(closed))) if closed else Decimal("0"),
        "trailing_rate": (Decimal(sum(1 for leg in closed if leg.trailing_active))
                          / Decimal(len(closed))) if closed else Decimal("0"),
        "avg_mfe_points": _avg([leg.mfe_points for leg in closed]),
        "avg_mae_points": _avg([leg.mae_points for leg in closed]),
        "cost_pct_of_gross": (total_commission / gross_profit) if gross_profit > 0 else Decimal("0"),
    }

    # Paired-setup analysis (both legs together) - the whole point of the experiment.
    completed_setups = [s for s in engine.setups if s.closed and len(s.legs()) == 2]
    net_setups = [s.net_pnl for s in completed_setups]
    both_stopped = one_won = survivor_failed = 0
    losing_losses: list[Decimal] = []
    survivor_profits: list[Decimal] = []
    for s in completed_setups:
        legs = s.legs()
        stopped = [leg for leg in legs if leg.exit_reason == "stop"]
        not_stopped = [leg for leg in legs if leg.exit_reason != "stop"]
        if len(stopped) == 2:
            both_stopped += 1
            losing_losses.extend(leg.net_pnl for leg in legs)
        elif len(stopped) == 1:
            losing_losses.append(stopped[0].net_pnl)
            survivor = not_stopped[0]
            survivor_profits.append(survivor.net_pnl)
            if s.net_pnl > 0:
                one_won += 1
            else:
                survivor_failed += 1
    n = len(completed_setups) or 1
    paired = {
        "paired_setups": len(completed_setups),
        "both_sides_stopped_pct": Decimal(both_stopped) / Decimal(n),
        "survivor_won_pct": Decimal(one_won) / Decimal(n),
        "survivor_failed_pct": Decimal(survivor_failed) / Decimal(n),
        "avg_net_setup": _avg(net_setups),
        "median_net_setup": (median(net_setups) if net_setups else Decimal("0")),
        "best_setup": max(net_setups, default=Decimal("0")),
        "worst_setup": min(net_setups, default=Decimal("0")),
        "avg_losing_leg_loss": _avg(losing_losses),
        "avg_surviving_leg_profit": _avg(survivor_profits),
        "expectancy_per_setup": _avg(net_setups),
    }

    triggered = sum(1 for st in engine.levels.values() if st.activations > 0)
    overshoots = [abs(s.overshoot_points) for s in engine.setups]
    activation = {
        "levels_created": len(engine.levels),
        "levels_triggered": triggered,
        "levels_armed": sum(1 for st in engine.levels.values() if st.status == ARMED),
        "levels_completed": sum(1 for st in engine.levels.values() if st.status == COMPLETED),
        "levels_cancelled": sum(1 for st in engine.levels.values() if st.status == CANCELLED),
        "total_activations": sum(st.activations for st in engine.levels.values()),
        "avg_overshoot_points": _avg(overshoots),
        "max_overshoot_points": max(overshoots, default=Decimal("0")),
    }

    return {
        "account": {
            "starting_balance": engine.account.starting_balance,
            "balance": engine.balance,
            "equity": engine.equity(),
            "realized_pnl": engine.realized_pnl,
            "unrealized_pnl": engine.unrealized_pnl(),
            "peak_equity": engine.peak_equity,
            "max_drawdown": engine.max_drawdown,
            "open_exposure_contracts": engine.gross_exposure_contracts(),
            "events_seen": engine.events_seen,
        },
        "general": general,
        "paired": paired,
        "activation": activation,
    }
