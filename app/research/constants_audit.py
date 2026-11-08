"""Audit of every tunable number that can change what is traded, how outcomes are
labelled, or how simulated P&L is produced - rated by how fragile a FIXED value is.

Why: a public MNQ study catalogued 134 constants and found its edge was fitted to
one volatility regime, with fixed point thresholds failing when the regime changed.
Knowing which of OUR numbers have that shape is the first step to checking them.

What a rating means. It is a judgement about FRAGILITY - how likely a fixed value is
to stop fitting when volatility, liquidity or the time of day changes. It is not
evidence that any value was tuned, and not a claim that any value is wrong.

The rubric (applied in this order):
  HIGH    a fixed market-scale value (ticks, points, contracts) that gates entries,
          defines a label, or sets an exit, with no scaling to volatility or liquidity
          and not at the one-tick granularity floor.
  MEDIUM  a fixed time or count window that shapes decisions; a value near the tick
          floor; a hand-picked ratio; or a cost/fill assumption that flatters results
          when it is too optimistic.
  LOW     an exchange fact, an account or safety limit (conservative by design), an
          evidence gate (stricter is the safe direction), a mode switch, a disabled
          (zero) placeholder, or a small fixed-capacity model setting.

``RATINGS`` must cover every discovered parameter; ``tests/test_constants_audit.py``
fails when a new tunable number appears unrated, or a rating outlives its parameter,
or the generated report in ``docs/audits`` is out of date. Regenerate it with
``python -m tools.constants_audit``.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

KINDS = ("market-scale", "time", "count", "ratio", "cost", "limit", "structural", "switch", "model")
RISKS = ("HIGH", "MEDIUM", "LOW")
REPORT_RELATIVE_PATH = "docs/audits/strategy_constants_audit.md"

# Scope: the classes whose defaults decide entries, labels, exits, simulated costs, the
# paper account, risk limits, or evidence gates. Status/counter dataclasses (for example
# PaperEngineStatus, ReplayStats) hold observations, not tunable choices, and are out.
AUDITED_CLASSES = (
    "app.strategy.order_flow.OrderFlowThresholds",
    "app.strategy.momentum.MomentumThresholds",
    "app.market.regime_classifier.RegimeClassifier",
    "app.strategy.shadow.ShadowPipelineConfig",
    "app.research.episode_builder.EpisodeConfig",
    "app.machine_learning.session_training.SessionTrainingConfig",
    "app.machine_learning.train.ModelTrainingConfig",
    "app.paper.execution.ExecutionConfig",
    "app.simulator.fill_engine.FillModelConfig",
    "app.simulator.slippage.SlippageModel",
    "app.simulator.engine.SimulatorConfig",
    "app.discovery.backtest.CostModel",
    "app.discovery.paper_account.PaperAccountConfig",
    "app.labs.bidirectional.config.AccountConfig",
    "app.labs.bidirectional.config.ActivationSpec",
    "app.labs.bidirectional.config.BreakEvenConfig",
    "app.labs.bidirectional.config.TrailConfig",
    "app.risk.account_profile.ProjectSafetyLimits",
    "app.research.profitability_progress.ProgressConfig",
    "app.machine_learning.daily_learning.DailyLearningConfig",
)

_SCALAR = (bool, int, float, Decimal, str)
_GENERIC_NAMES = frozenset({"enabled"})


@dataclass(frozen=True, slots=True)
class Parameter:
    """One tunable default found by introspection."""

    owner: str
    name: str
    default: object

    @property
    def key(self) -> str:
        return f"{self.owner}.{self.name}"


@dataclass(frozen=True, slots=True)
class Rating:
    """A reviewed judgement for one parameter."""

    kind: str
    role: str
    risk: str
    note: str


def _r(kind: str, role: str, risk: str, note: str) -> Rating:
    return Rating(kind, role, risk, note)


_TICK = _r("structural", "instrument", "LOW", "MNQ tick size, an exchange fact.")
_TICK_VALUE = _r("structural", "instrument", "LOW", "MNQ dollars per tick per contract, an exchange fact.")
_OFF = "Zero or off by default; no effect until a profile turns it on."


def _off(kind: str, role: str) -> Rating:
    return _r(kind, role, "LOW", _OFF)


_LIMIT = _r("limit", "risk", "LOW", "Daily limit from the account and safety rules; conservative by design.")

# Ratings by class, then field. Notes are about fragility, not about tuning history.
RATINGS: dict[str, dict[str, Rating]] = {
    "OrderFlowThresholds": {
        "tick_size": _TICK,
        "large_block_minimum": _r("market-scale", "entry-gate", "HIGH", "Fixed contracts for a 'large' resting block. Displayed size varies with time of day, contract month and regime and is not scaled. Same number is copied into EpisodeConfig; DailyLearningConfig uses 75."),
        "durable_block_min_snapshots": _r("count", "entry-gate", "MEDIUM", "Observations a block must survive; depends on snapshot cadence."),
        "min_reload_count": _r("count", "entry-gate", "LOW", "One reload is the smallest meaningful bar, not a fitted value."),
        "absorption_volume_minimum": _r("market-scale", "entry-gate", "HIGH", "Fixed contracts that must trade into a level. Volume per minute varies several-fold across sessions and regimes; not scaled. Copied into EpisodeConfig."),
        "follow_through_volume_minimum": _r("market-scale", "entry-gate", "HIGH", "Fixed contracts of follow-through volume; not scaled to the session's activity."),
        "loading_liquidity_minimum": _r("market-scale", "entry-gate", "HIGH", "Fixed contracts of new resting liquidity; not scaled to normal book depth."),
        "cvd_support_minimum": _r("market-scale", "entry-gate", "HIGH", "Fixed contracts of cumulative-delta support; not scaled to activity."),
        "absorption_max_progress_ticks": _r("market-scale", "entry-gate", "HIGH", "Fixed ticks of allowed progress while absorbing; the same 3 ticks means different things in calm and fast markets."),
        "min_reaction_ticks": _r("market-scale", "entry-gate", "LOW", "One tick is the smallest possible move, so there is nothing to fit."),
        "min_continuation_ticks": _r("market-scale", "entry-gate", "MEDIUM", "Two ticks, near the tick floor."),
        "minimum_target_ticks": _r("market-scale", "exit", "MEDIUM", "A floor, not a tuned target; costs make anything smaller unattractive."),
        "minimum_stop_ticks": _r("market-scale", "exit", "MEDIUM", "A floor near the tick granularity so a stop is not inside the noise."),
        "max_spread_ticks": _r("market-scale", "entry-gate", "MEDIUM", "A liquidity safety filter; MNQ normally trades one tick wide, so four is rarely binding."),
        "max_volatility_ticks": _r("market-scale", "entry-gate", "HIGH", "Fixed ticks of allowed volatility. The code comment records a measurement (74-82% of real 180 s windows exceed it, so it is judged over a 10 s tail), which is evidence, yet the number is still a fixed tick count that moves with the regime."),
        "max_price_velocity_ticks_per_second": _r("market-scale", "entry-gate", "HIGH", "Fixed speed cap; what counts as fast depends on the regime."),
        "speed_tail_seconds": _r("time", "context", "MEDIUM", "Fixed horizon for the speed check."),
        "first_observation_minutes": _r("time", "entry-gate", "MEDIUM", "Fixed opening-observation length."),
        "enforce_opening_observation": _r("switch", "entry-gate", "LOW", "Visible profile choice (canonical keeps it on)."),
        "min_reaction_snapshots": _r("count", "entry-gate", "MEDIUM", "Fixed number of snapshots; depends on snapshot cadence."),
        "max_dol_distance_ticks": _r("market-scale", "entry-gate", "HIGH", "Fixed 120 ticks (30 points) of target reach. Exactly the kind of fixed point threshold that stops fitting when volatility changes."),
        "block_move_tolerance_ticks": _r("market-scale", "entry-gate", "MEDIUM", "Two ticks, near the tick floor."),
        "block_stability_tail_seconds": _r("time", "context", "LOW", "Zero in canonical (whole window); the relaxed profile sets it."),
        "use_initial_balance_levels": _r("switch", "entry-gate", "LOW", "Opt-in: adds the first-hour high/low as targets. Off by default, so behaviour is unchanged."),
    },
    "MomentumThresholds": {
        "min_progress_ticks": _r("market-scale", "entry-gate", "HIGH", "Fixed ticks of required progress; not scaled to volatility."),
        "min_pullback_ticks": _r("market-scale", "entry-gate", "LOW", "One tick is the smallest possible move."),
        "max_pullback_ticks": _r("market-scale", "entry-gate", "HIGH", "Fixed ticks of allowed pullback; a calm-market limit that a fast market breaches constantly."),
        "pullback_window_seconds": _r("time", "context", "MEDIUM", "Fixed window for the pullback."),
    },
    "RegimeClassifier": {
        "min_samples": _r("count", "regime", "MEDIUM", "History needed before a regime is called."),
        "stale_after_ns": _r("time", "regime", "LOW", "Five-second freshness guard."),
        "low_volatility": _r("market-scale", "regime", "HIGH", "Absolute tick-volatility cut-off used to route profiles. Regime boundaries are where fixed cut-offs drift; recalibrate from the observed distribution."),
        "high_volatility": _r("market-scale", "regime", "HIGH", "Absolute tick-volatility cut-off; same concern as the low cut-off."),
        "extreme_volatility": _r("market-scale", "regime", "HIGH", "Absolute tick-volatility cut-off; same concern."),
        "thin_liquidity": _r("market-scale", "regime", "HIGH", "Fixed contract count for 'thin' depth; depth varies with time of day and contract month."),
        "deep_liquidity": _r("market-scale", "regime", "HIGH", "Fixed contract count for 'deep' depth; same concern."),
    },
    "ShadowPipelineConfig": {
        "feature_window_size": _r("count", "context", "MEDIUM", "Snapshots per feature window."),
        "bid_reload_level": _r("count", "context", "LOW", "A book level index, not a market-scale value."),
        "bid_reload_threshold": _r("market-scale", "context", "LOW", "One contract, the minimum possible size."),
        "log_mode": _r("switch", "infra", "LOW", "Observe-only logging mode."),
    },
    "EpisodeConfig": {
        "tick_size": _TICK,
        "tick_value": _TICK_VALUE,
        "stop_buffer_points": _r("market-scale", "exit", "HIGH", "Fixed 10 points (40 ticks) beyond the defended level; not scaled to volatility."),
        "fixed_slippage_ticks": _r("cost", "simulation", "MEDIUM", "Fixed one tick; optimistic when the market is fast."),
        "volatility_slippage_multiplier": _r("cost", "simulation", "MEDIUM", "Zero, so simulated slippage ignores volatility and flatters fast markets."),
        "commission_per_round_turn": _r("cost", "simulation", "LOW", "A broker fee; verify it against the account's real fee."),
        "queue_position_fraction": _r("cost", "simulation", "MEDIUM", "Zero assumes the front of the queue for resting orders, which is optimistic."),
        "window_size": _r("count", "context", "MEDIUM", "Events per analysis window."),
        "warmup_events": _r("count", "context", "LOW", "Events skipped while the window fills."),
        "decision_stride": _r("count", "context", "MEDIUM", "How often decisions are evaluated."),
        "window_span_seconds": _r("time", "context", "MEDIUM", "Seconds of market time per window."),
        "depth_sample_interval_ms": _r("time", "context", "MEDIUM", "Depth sampling cadence."),
        "evaluation_interval_ms": _r("time", "context", "MEDIUM", "Evaluation cadence."),
        "warmup_span_seconds": _r("time", "context", "MEDIUM", "Seconds of warm-up before evaluating."),
        "large_block_minimum": _r("market-scale", "entry-gate", "HIGH", "Copy of OrderFlowThresholds.large_block_minimum; two copies can drift apart."),
        "absorption_volume_minimum": _r("market-scale", "entry-gate", "HIGH", "Copy of OrderFlowThresholds.absorption_volume_minimum; two copies can drift apart."),
        "timeout_seconds": _r("time", "exit", "MEDIUM", "Fixed episode timeout."),
        "dedupe_price_ticks": _r("market-scale", "label", "MEDIUM", "Fixed ticks used to treat nearby setups as one."),
        "dedupe_seconds": _r("time", "label", "MEDIUM", "Fixed seconds used to treat close setups as one."),
        "momentum_enabled": _r("switch", "entry-gate", "LOW", "Visible profile choice."),
        "ml_decision_policy_enabled": _r("switch", "entry-gate", "LOW", "Visible profile choice (off)."),
        "strategy_profile": _r("switch", "entry-gate", "LOW", "Names the profile."),
        "instrument": _r("structural", "instrument", "LOW", "The traded instrument."),
        "break_even_trigger_ticks": _off("market-scale", "exit"),
        "break_even_lock_ticks": _off("market-scale", "exit"),
        "trail_activation_ticks": _off("market-scale", "exit"),
        "trail_distance_ticks": _off("market-scale", "exit"),
        "max_entries_per_day": _LIMIT,
        "max_losses_per_day": _LIMIT,
        "fixed_contracts": _off("count", "risk"),
        "max_risk_per_trade_usd": _off("limit", "risk"),
        "min_reward_risk": _off("ratio", "entry-gate"),
        "target_reward_risk": _off("ratio", "exit"),
        "entry_order_type": _r("switch", "entry-gate", "LOW", "Market orders by default."),
        "entry_limit_offset_ticks": _off("market-scale", "entry-gate"),
        "entry_limit_timeout_seconds": _off("time", "entry-gate"),
        "entry_limit_cancel_ticks": _off("market-scale", "entry-gate"),
        "entry_require_trade_through": _r("switch", "simulation", "LOW", "Off, so a touch fills a resting order, which is optimistic."),
        "entry_cooldown_seconds": _r("time", "entry-gate", "MEDIUM", "Fixed five-minute cooldown."),
        "time_stop_seconds": _r("time", "exit", "MEDIUM", "Fixed 15-minute time stop."),
    },
    "SessionTrainingConfig": {
        "target_ticks": _r("market-scale", "label", "HIGH", "Fixed 12-tick target defines every ML label. Base rate and difficulty change with volatility, so labels are not comparable across regimes."),
        "stop_ticks": _r("market-scale", "label", "HIGH", "Fixed 8-tick stop defines every ML label; same concern as the target."),
        "horizon_seconds": _r("time", "label", "MEDIUM", "Fixed 300 s label horizon."),
        "sample_interval_seconds": _r("time", "context", "MEDIUM", "Fixed sampling cadence for training rows."),
        "window_span_seconds": _r("time", "context", "MEDIUM", "Seconds of market time per window."),
        "sample_interval_ms": _r("time", "context", "MEDIUM", "Depth sampling cadence."),
        "warmup_seconds": _r("time", "context", "MEDIUM", "Seconds of warm-up per session."),
        "tick_size": _TICK,
    },
    "ModelTrainingConfig": {
        "random_state": _r("model", "model", "LOW", "A seed."),
        "logistic_max_iter": _r("model", "model", "LOW", "Solver iteration cap."),
        "xgboost_estimators": _r("model", "model", "LOW", "Small fixed capacity (25 trees)."),
        "xgboost_max_depth": _r("model", "model", "LOW", "Shallow trees (depth 2)."),
        "xgboost_learning_rate": _r("model", "model", "LOW", "A conventional learning rate."),
    },
    "ExecutionConfig": {
        "commission_per_contract": _r("cost", "simulation", "LOW", "A broker fee; the Lab uses 0.62 per side, so confirm both mean the same unit."),
        "entry_slippage_ticks": _r("cost", "simulation", "MEDIUM", "Fixed one tick; optimistic when the market is fast."),
        "stop_slippage_ticks": _r("cost", "simulation", "MEDIUM", "Fixed one tick; stops can slip more in fast markets."),
        "cooldown_ns": _r("time", "entry-gate", "MEDIUM", "Fixed five-minute cooldown."),
        "time_stop_ns": _r("time", "exit", "MEDIUM", "Fixed 15-minute time stop."),
        "max_entries_per_day": _LIMIT,
        "max_losses_per_day": _LIMIT,
        "break_even_trigger_ticks": _off("market-scale", "exit"),
        "break_even_lock_ticks": _off("market-scale", "exit"),
        "trail_activation_ticks": _off("market-scale", "exit"),
        "trail_distance_ticks": _off("market-scale", "exit"),
        "fixed_contracts": _off("count", "risk"),
        "max_risk_per_trade_usd": _off("limit", "risk"),
        "min_reward_risk": _off("ratio", "entry-gate"),
        "entry_order_type": _r("switch", "entry-gate", "LOW", "Market orders by default."),
        "entry_limit_offset_ticks": _off("market-scale", "entry-gate"),
        "entry_limit_timeout_ns": _off("time", "entry-gate"),
        "entry_limit_cancel_ticks": _off("market-scale", "entry-gate"),
        "entry_require_trade_through": _r("switch", "simulation", "LOW", "Off, so a touch fills a resting order, which is optimistic."),
    },
    "FillModelConfig": {
        "queue_ahead_contracts": _r("cost", "simulation", "MEDIUM", "Zero assumes nothing is ahead in the queue, which is optimistic."),
        "liquidity_fraction": _r("ratio", "simulation", "MEDIUM", "Assumes 100% of displayed liquidity can be taken, which is optimistic."),
        "ack_delay_ns": _r("cost", "simulation", "MEDIUM", "Zero latency is optimistic."),
        "cancel_delay_ns": _r("cost", "simulation", "MEDIUM", "Zero latency is optimistic."),
        "slippage_per_contract": _r("cost", "simulation", "MEDIUM", "Zero slippage is optimistic."),
        "commission_per_contract": _r("cost", "simulation", "LOW", "A broker fee."),
    },
    "SlippageModel": {
        "fixed_ticks": _r("cost", "simulation", "MEDIUM", "Zero fixed slippage is optimistic."),
        "volatility_multiplier": _r("cost", "simulation", "MEDIUM", "Zero means slippage ignores volatility."),
    },
    "SimulatorConfig": {
        "acknowledgement_delay_ns": _r("cost", "simulation", "MEDIUM", "Zero latency is optimistic."),
        "cancel_replace_delay_ns": _r("cost", "simulation", "MEDIUM", "Zero latency is optimistic."),
        "feature_window_size": _r("count", "context", "MEDIUM", "Snapshots per feature window."),
        "short_term_volatility": _r("market-scale", "context", "LOW", "An input default of zero, not a tuned threshold."),
    },
    "CostModel": {
        "commission_r": _r("ratio", "simulation", "MEDIUM", "Commission as a fraction of risk (0.02 R); hand-picked."),
        "slippage_r": _r("ratio", "simulation", "MEDIUM", "Slippage as a fraction of risk (0.05 R); hand-picked."),
        "worst_case_multiplier": _r("ratio", "simulation", "MEDIUM", "Hand-picked stress multiple."),
    },
    "PaperAccountConfig": {
        "starting_balance": _r("structural", "risk", "LOW", "Account size."),
        "stop_points": _r("market-scale", "exit", "HIGH", "Fixed 10-point stop; not scaled to volatility."),
        "tick_value": _TICK_VALUE,
        "ticks_per_point": _r("structural", "instrument", "LOW", "Four ticks per point on MNQ, an exchange fact."),
        "max_trades_per_day": _LIMIT,
        "max_losses_per_day": _LIMIT,
        "daily_loss_fraction": _r("limit", "risk", "LOW", "Daily loss cap from the account rules."),
        "commission": _r("cost", "simulation", "LOW", "A broker fee."),
        "slippage_ticks": _r("cost", "simulation", "MEDIUM", "Fixed one tick; optimistic when the market is fast."),
        "max_account_attempts": _r("count", "risk", "LOW", "A search cap."),
    },
    "AccountConfig": {
        "starting_balance": _r("structural", "risk", "LOW", "Account size."),
        "tick_size": _TICK,
        "tick_value": _TICK_VALUE,
        "commission_per_contract": _r("cost", "simulation", "LOW", "Per side, per contract (0.62); ExecutionConfig uses 1.24, confirm both mean the same unit."),
        "entry_slippage_ticks": _r("cost", "simulation", "MEDIUM", "Zero entry slippage is optimistic."),
        "exit_slippage_ticks": _r("cost", "simulation", "MEDIUM", "Zero exit slippage is optimistic."),
        "stop_slippage_ticks": _r("cost", "simulation", "MEDIUM", "One tick on stops; can be more in fast markets."),
        "max_gross_contracts": _r("limit", "risk", "LOW", "A sanity cap on simulated exposure."),
    },
    "ActivationSpec": {
        "long_qty": _r("count", "entry-gate", "LOW", "Contracts per side, chosen by the user."),
        "short_qty": _r("count", "entry-gate", "LOW", "Contracts per side, chosen by the user."),
        "stop_ticks": _r("market-scale", "exit", "HIGH", "Fixed 2-tick stop defines the whole Lab outcome; tight stops lose to noise and costs."),
        "source": _r("switch", "entry-gate", "LOW", "Which price triggers a level."),
        "comparison": _r("switch", "entry-gate", "LOW", "How a level triggers."),
        "mode": _r("switch", "entry-gate", "LOW", "Paired or one-cancels-other."),
        "enabled": _r("switch", "entry-gate", "LOW", "A level on/off flag."),
        "one_shot": _r("switch", "entry-gate", "LOW", "Fire once."),
        "max_activations": _r("count", "entry-gate", "LOW", "One activation."),
        "rearm_cooldown_ns": _off("time", "entry-gate"),
        "require_leave_reenter": _r("switch", "entry-gate", "LOW", "Off by default."),
        "leave_distance_ticks": _r("market-scale", "entry-gate", "MEDIUM", "Fixed 4 ticks to re-arm; only matters when re-arming is on."),
        "reenter_distance_ticks": _off("market-scale", "entry-gate"),
        "expire_after_ns": _off("time", "entry-gate"),
        "cancel_if_moves_away_ticks": _off("market-scale", "entry-gate"),
    },
    "BreakEvenConfig": {
        "enabled": _r("switch", "exit", "LOW", "Off by default."),
        "trigger_ticks": _off("market-scale", "exit"),
        "offset_ticks": _off("market-scale", "exit"),
    },
    "TrailConfig": {
        "enabled": _r("switch", "exit", "LOW", "Off by default."),
        "immediate": _r("switch", "exit", "LOW", "Off by default."),
        "activation_ticks": _off("market-scale", "exit"),
        "distance_ticks": _off("market-scale", "exit"),
        "step_ticks": _off("market-scale", "exit"),
    },
    "ProjectSafetyLimits": {
        "max_entries_per_day": _LIMIT,
        "max_losses_per_day": _LIMIT,
        "daily_risk_fraction": _r("limit", "risk", "LOW", "Daily risk cap from the account rules."),
        "allow_averaging_down": _r("switch", "risk", "LOW", "Forbidden by default, a safety rule."),
    },
    "ProgressConfig": {
        "max_drop_rate": _r("ratio", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "min_eligible_sessions": _r("count", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "min_completed_setups": _r("count", "evidence-gate", "LOW", "Promotion gate; revisit if the setups-per-day rate makes it easy to satisfy."),
        "min_independent_days": _r("count", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "min_net_expectancy_r": _r("ratio", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "min_profit_factor": _r("ratio", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "max_drawdown_fraction": _r("ratio", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "max_consecutive_losses": _r("count", "evidence-gate", "LOW", "Promotion gate; stricter is the safe direction."),
        "max_day_concentration": _r("ratio", "evidence-gate", "LOW", "Promotion gate against one lucky day."),
        "max_hour_concentration": _r("ratio", "evidence-gate", "LOW", "Promotion gate against one lucky hour."),
        "min_side_balance": _r("ratio", "evidence-gate", "LOW", "Promotion gate against one-sided results."),
    },
    "DailyLearningConfig": {
        "large_block_size": _r("market-scale", "report", "MEDIUM", "Report-only, but it defines 'large block' as 75 contracts while OrderFlowThresholds uses 90: two definitions."),
        "reload_threshold": _r("market-scale", "report", "MEDIUM", "Report-only fixed contract count."),
        "cvd_dominance_fraction": _r("ratio", "report", "LOW", "Report-only fraction."),
        "tick_size": _TICK,
    },
}


def discover_parameters() -> list[Parameter]:
    """Every scalar default of the audited classes, in class then field order."""
    found: list[Parameter] = []
    for path in AUDITED_CLASSES:
        module_name, class_name = path.rsplit(".", 1)
        owner = getattr(importlib.import_module(module_name), class_name)
        for field in dataclasses.fields(owner):
            default = field.default
            if default is not dataclasses.MISSING and isinstance(default, _SCALAR):
                found.append(Parameter(class_name, field.name, default))
    return found


def unrated(parameters: list[Parameter] | None = None) -> list[str]:
    """Parameters that exist in code but have no rating."""
    items = parameters if parameters is not None else discover_parameters()
    return [item.key for item in items if item.name not in RATINGS.get(item.owner, {})]


def stale_ratings(parameters: list[Parameter] | None = None) -> list[str]:
    """Ratings whose parameter no longer exists."""
    items = parameters if parameters is not None else discover_parameters()
    present = {item.key for item in items}
    return [f"{owner}.{name}" for owner, fields in RATINGS.items() for name in fields if f"{owner}.{name}" not in present]


def _show(value: object) -> str:
    return repr(value) if isinstance(value, str) else str(value)


def render_report() -> str:
    """The audit as markdown, deterministic so the committed copy can be checked."""
    parameters = discover_parameters()
    rows = [(item, RATINGS[item.owner][item.name]) for item in parameters]
    by_risk = {risk: sum(1 for _, rating in rows if rating.risk == risk) for risk in RISKS}
    by_kind: dict[str, int] = defaultdict(int)
    for _, rating in rows:
        by_kind[rating.kind] += 1

    lines = [
        "# Strategy constants audit",
        "",
        "Generated from the code by `python -m tools.constants_audit`; do not edit by hand. "
        "`tests/test_constants_audit.py` fails if a tunable number is added without a rating or if this file is stale.",
        "",
        "## What a rating means",
        "",
        "A rating is a judgement about **fragility**: how likely a fixed value is to stop fitting when volatility, "
        "liquidity or the time of day changes. It is not evidence that any value was tuned, and not a claim that any "
        "value is wrong. The reason for the audit: a public MNQ study catalogued its own constants and found its edge "
        "fitted to one volatility regime, with fixed point thresholds failing when the regime changed.",
        "",
        "- **HIGH**: a fixed market-scale value (ticks, points, contracts) that gates entries, defines a label or sets an "
        "exit, with no scaling to volatility or liquidity, and not at the one-tick floor.",
        "- **MEDIUM**: a fixed time or count window that shapes decisions; a value near the tick floor; a hand-picked "
        "ratio; or a cost or fill assumption that flatters results when too optimistic.",
        "- **LOW**: an exchange fact, an account or safety limit, an evidence gate (stricter is the safe direction), a "
        "mode switch, a disabled placeholder, or a small fixed-capacity model setting.",
        "",
        "## Summary",
        "",
        f"{len(rows)} parameters across {len(AUDITED_CLASSES)} classes: "
        f"**{by_risk['HIGH']} high**, {by_risk['MEDIUM']} medium, {by_risk['LOW']} low.",
        "",
        "By kind: " + ", ".join(f"{kind} {by_kind[kind]}" for kind in KINDS if by_kind[kind]) + ".",
        "",
        "## Validate or scale these first (HIGH)",
        "",
        "Every one is a fixed number in market units. The suggested handling for a market-scale value is to express it as "
        "a fraction of the walk-forward typical first-hour range (`reference_multiple` in "
        "`app/strategy/dynamic_thresholds.py`, fed by `VolatilityRegimeTable.typical_range_for`), then check results per "
        "volatility regime. For a label, report results per regime before trusting any pooled number.",
        "",
        "| Parameter | Default | Role | Why it is fragile |",
        "|---|---|---|---|",
    ]
    for item, rating in rows:
        if rating.risk == "HIGH":
            lines.append(f"| `{item.key}` | `{_show(item.default)}` | {rating.role} | {rating.note} |")

    lines += ["", "## Same name in several places", "",
              "A parameter defined in more than one class can drift apart. Rows marked **differ** need a human to confirm "
              "the values mean the same thing.", "",
              "| Name | Values | |", "|---|---|---|"]
    grouped: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for item in parameters:
        if item.name in _GENERIC_NAMES:
            continue  # a generic flag name shared by unrelated classes is a collision, not a duplicate
        grouped[item.name].append((item.owner, item.default))
    for name, places in grouped.items():
        if len(places) < 2:
            continue
        values = {_show(value) for _, value in places}
        flag = "**differ**" if len(values) > 1 else "agree"
        listed = ", ".join(f"{owner} `{_show(value)}`" for owner, value in places)
        lines.append(f"| `{name}` | {listed} | {flag} |")

    lines += ["", "## Every parameter", ""]
    for path in AUDITED_CLASSES:
        class_name = path.rsplit(".", 1)[1]
        lines += [f"### {class_name}", "", f"`{path}`", "",
                  "| Field | Default | Kind | Role | Risk | Note |", "|---|---|---|---|---|---|"]
        for item, rating in rows:
            if item.owner == class_name:
                lines.append(
                    f"| `{item.name}` | `{_show(item.default)}` | {rating.kind} | {rating.role} | {rating.risk} | {rating.note} |",
                )
        lines.append("")
    lines += [
        "## Not covered",
        "",
        "- Numbers written in strategy specification YAML files are chosen per strategy file by the operator and are not defaults in code.",
        "- Status and counter dataclasses hold observations, not tunable choices.",
        "- Ratings judge fragility from the value's shape; they do not know any tuning history.",
        "",
    ]
    return "\n".join(lines)
