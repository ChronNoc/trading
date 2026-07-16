"""Honest 'how close to profitable' progress meter, from real evidence only.

Profitability is modelled as an ordered ladder of evidence gates (STAGE 7). The
meter is deliberately conservative: a gate only counts once every earlier gate
has passed, so the bot cannot appear "80% profitable" while it still has zero
completed setups. Every gate past the completed-setup threshold reports
``insufficient_evidence`` - never a fabricated pass - until there is a real
sample to judge. Nothing here ever asserts profitability that the data does not
support.

All money/price math stays :class:`~decimal.Decimal`. The core function is pure:
it takes already-loaded inputs and returns an explainable result, so it is fully
deterministic and testable. :func:`load_progress` is the thin disk-reading wrapper.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from app.research.paper_ledger import RealPaperLedgerResult
from app.research.real_episodes import CompletedRealOutcome
from app.research.session_catalog import SessionEntry

# Gate status vocabulary. These are ordered from "cannot even start" to "met".
STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_PASSED = "passed"


@dataclass(frozen=True, slots=True)
class ProgressConfig:
    """Explicit, tunable evidence thresholds for the profitability ladder."""

    max_drop_rate: Decimal = Decimal("0.01")  # >=1% dropped => capture not healthy
    min_eligible_sessions: int = 20
    min_completed_setups: int = 100
    min_independent_days: int = 20
    min_net_expectancy_r: Decimal = Decimal("0.05")  # per-trade, after costs
    min_profit_factor: Decimal = Decimal("1.30")
    max_drawdown_fraction: Decimal = Decimal("0.10")  # of starting balance
    max_consecutive_losses: int = 6
    max_day_concentration: Decimal = Decimal("0.40")  # no single day > 40% of net
    max_hour_concentration: Decimal = Decimal("0.50")  # no single hour > 50% of setups
    min_side_balance: Decimal = Decimal("0.20")  # each of long/short >= 20% of trades

    def __post_init__(self) -> None:
        """Validate thresholds so a misconfiguration fails loudly, not silently."""
        if self.min_completed_setups <= 0 or self.min_independent_days <= 0:
            raise ValueError("sample thresholds must be positive")
        if not (Decimal("0") < self.max_drop_rate < Decimal("1")):
            raise ValueError("max_drop_rate must be in (0, 1)")


@dataclass(frozen=True, slots=True)
class Gate:
    """One explainable evidence gate with its measured value and next action."""

    gate_id: str
    label: str
    status: str
    observed: str
    threshold: str
    detail: str
    next_action: str

    @property
    def passed(self) -> bool:
        """Return whether this gate is fully satisfied."""
        return self.status == STATUS_PASSED


@dataclass(frozen=True, slots=True)
class ProfitabilityProgress:
    """The whole ladder plus a single honest completion fraction."""

    gates: tuple[Gate, ...]
    completed_gates: int
    total_gates: int
    fraction: Decimal
    stage_label: str
    headline: str
    profitable_claim_supported: bool
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def percent(self) -> int:
        """Return the completion percentage as a rounded integer for display."""
        return int((self.fraction * 100).quantize(Decimal("1")))


def compute_progress(
    catalog: Sequence[SessionEntry],
    outcomes: Sequence[CompletedRealOutcome],
    ledger: RealPaperLedgerResult | None,
    config: ProgressConfig | None = None,
) -> ProfitabilityProgress:
    """Build the evidence ladder from real inputs; pure and deterministic.

    ``catalog`` is the session catalog, ``outcomes`` the quality-gated completed
    real outcomes, and ``ledger`` the fixed $100k paper result (or None). The
    ladder is ordered; the completion fraction is the count of gates passed
    *consecutively from the start*, so unmet early evidence caps the meter.
    """
    cfg = config or ProgressConfig()
    gates: list[Gate] = []

    finalized = [e for e in catalog if e.finalized and not e.active]
    eligible = [e for e in finalized if e.eligible_for_order_flow_replay]
    completed = list(outcomes)
    independent_days = sorted({o.trading_day for o in completed})
    have_sample = len(completed) >= cfg.min_completed_setups

    # --- Gate 1: data capture / feed continuity & drop rate ------------------
    gates.append(_gate_data_capture(finalized, eligible, cfg))
    # --- Gate 2: session coverage -------------------------------------------
    gates.append(_gate_session_coverage(eligible, cfg))
    # --- Gate 3: completed-setup sample -------------------------------------
    gates.append(_gate_completed_sample(completed, cfg))
    # --- Gate 4: independent trading days -----------------------------------
    gates.append(_gate_independent_days(independent_days, cfg))
    # --- Gates 5-9: performance, only meaningful once a sample exists --------
    gates.append(_gate_net_expectancy(completed, have_sample, cfg))
    gates.append(_gate_profit_factor(completed, have_sample, cfg))
    gates.append(_gate_drawdown(ledger, have_sample, cfg))
    gates.append(_gate_consecutive_losses(ledger, have_sample, cfg))
    gates.append(_gate_day_concentration(completed, have_sample, cfg))
    gates.append(_gate_side_balance(completed, have_sample, cfg))
    gates.append(_gate_hour_stability(completed, have_sample, cfg))
    gates.append(_gate_walk_forward(completed, have_sample, cfg))
    gates.append(_gate_demo_soak())
    gates.append(_gate_calibration(completed, have_sample, cfg))
    # --- Final gate: prop-rule compliance (fixed-account survival) ----------
    gates.append(_gate_prop_compliance(ledger, have_sample))

    total = len(gates)
    completed_consecutive = 0
    for gate in gates:
        if gate.passed:
            completed_consecutive += 1
        else:
            break
    fraction = (Decimal(completed_consecutive) / Decimal(total)).quantize(Decimal("0.0001"))

    profitable = completed_consecutive == total
    stage_label = _stage_label(gates, completed_consecutive)
    headline = _headline(completed_consecutive, total, completed, profitable)
    caveats = (
        "Delayed Bookmap data: valid for offline research only, never live decisions.",
        "Progress measures evidence collected, not realized profit.",
    )
    return ProfitabilityProgress(
        gates=tuple(gates),
        completed_gates=completed_consecutive,
        total_gates=total,
        fraction=fraction,
        stage_label=stage_label,
        headline=headline,
        profitable_claim_supported=profitable,
        caveats=caveats,
    )


def _drop_rate(entry: SessionEntry) -> Decimal:
    total_events = entry.depth_updates + entry.trades
    if total_events <= 0:
        return Decimal("1")
    return (Decimal(entry.dropped_message_count) / Decimal(total_events + entry.dropped_message_count)).quantize(
        Decimal("0.000001"),
    )


def _gate_data_capture(
    finalized: Sequence[SessionEntry],
    eligible: Sequence[SessionEntry],
    cfg: ProgressConfig,
) -> Gate:
    # A clean, continuous session is denoted continuity_status == "continuous"
    # (matching the recorder/catalog); order-flow eligibility already implies it,
    # but the drop-rate floor is enforced explicitly here.
    clean = [e for e in eligible if e.continuity_status == "continuous" and _drop_rate(e) < cfg.max_drop_rate]
    if not finalized:
        status, nxt = STATUS_NOT_STARTED, "Record at least one complete Bookmap session."
    elif clean:
        status, nxt = STATUS_PASSED, "Continue collecting sessions; capture is healthy."
    else:
        status, nxt = STATUS_IN_PROGRESS, (
            "Capture is running but no order-flow-eligible session has clean continuity yet; "
            "keep recording and fix any feed drops/sequence gaps."
        )
    return Gate(
        gate_id="data_capture",
        label="Data capture healthy (feed continuity & drop rate)",
        status=status,
        observed=f"{len(finalized)} finalized, {len(eligible)} order-flow-eligible, {len(clean)} clean",
        threshold=f">=1 clean order-flow-eligible session, drop rate < {cfg.max_drop_rate}",
        detail="A single clean, continuous, low-drop session is the minimum trustworthy input.",
        next_action=nxt,
    )


def _gate_session_coverage(eligible: Sequence[SessionEntry], cfg: ProgressConfig) -> Gate:
    count = len(eligible)
    passed = count >= cfg.min_eligible_sessions
    return Gate(
        gate_id="session_coverage",
        label="Minimum complete-session coverage",
        status=STATUS_PASSED if passed else STATUS_IN_PROGRESS,
        observed=f"{count} order-flow-eligible finalized sessions",
        threshold=f">= {cfg.min_eligible_sessions} sessions",
        detail="Enough independent sessions so results are not a single day's luck.",
        next_action="Passed." if passed else f"Record {cfg.min_eligible_sessions - count} more eligible sessions.",
    )


def _gate_completed_sample(completed: Sequence[CompletedRealOutcome], cfg: ProgressConfig) -> Gate:
    count = len(completed)
    passed = count >= cfg.min_completed_setups
    if count == 0:
        status = STATUS_BLOCKED
        nxt = "Zero setups have completed. The strategy has accepted no setup on real data yet - this is a valid, honest state, not a failure. Keep collecting sessions."
    elif passed:
        status, nxt = STATUS_PASSED, "Passed."
    else:
        status, nxt = STATUS_IN_PROGRESS, f"{cfg.min_completed_setups - count} more completed real setups needed."
    return Gate(
        gate_id="completed_sample",
        label="Minimum completed-setup sample",
        status=status,
        observed=f"{count} completed, quality-gated real outcomes",
        threshold=f">= {cfg.min_completed_setups} completed setups",
        detail="Statistics need a real sample; a handful of trades prove nothing.",
        next_action=nxt,
    )


def _gate_independent_days(independent_days: Sequence[str], cfg: ProgressConfig) -> Gate:
    count = len(independent_days)
    passed = count >= cfg.min_independent_days
    return Gate(
        gate_id="independent_days",
        label="Minimum independent trading days",
        status=STATUS_PASSED if passed else (STATUS_IN_PROGRESS if count else STATUS_BLOCKED),
        observed=f"{count} distinct trading days with a completed setup",
        threshold=f">= {cfg.min_independent_days} days",
        detail="Independent days guard against one session dominating the evidence.",
        next_action="Passed." if passed else "Accumulate completed setups across more distinct trading days.",
    )


def _gate_net_expectancy(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "net_expectancy",
            "Out-of-sample net expectancy after costs",
            f"> {cfg.min_net_expectancy_r} R/trade",
            len(completed),
            cfg,
        )
    expectancy = sum((o.net_pnl_per_contract for o in completed), Decimal("0")) / Decimal(len(completed))
    per_r = sum((o.r_multiple for o in completed), Decimal("0")) / Decimal(len(completed))
    passed = per_r > cfg.min_net_expectancy_r and expectancy > 0
    return Gate(
        gate_id="net_expectancy",
        label="Out-of-sample net expectancy after costs",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"{per_r.quantize(Decimal('0.0001'))} R/trade (net ${expectancy.quantize(Decimal('0.01'))}/contract)",
        threshold=f"> {cfg.min_net_expectancy_r} R/trade after commission and slippage",
        detail="Positive expectancy after realistic costs is the core of an edge.",
        next_action="Passed." if passed else "Expectancy is not positive after costs; the edge is unproven.",
    )


def _gate_profit_factor(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "profit_factor",
            "Profit factor with uncertainty",
            f">= {cfg.min_profit_factor}",
            len(completed),
            cfg,
        )
    gains = sum((o.net_pnl_per_contract for o in completed if o.net_pnl_per_contract > 0), Decimal("0"))
    losses = -sum((o.net_pnl_per_contract for o in completed if o.net_pnl_per_contract < 0), Decimal("0"))
    factor = (gains / losses) if losses > 0 else Decimal("0")
    # Real uncertainty: a deterministic bootstrap 90% CI on the profit factor so a
    # single lucky sample cannot pass. The LOWER bound must clear the threshold.
    low, high = _bootstrap_profit_factor_ci([o.net_pnl_per_contract for o in completed])
    passed = losses > 0 and low >= cfg.min_profit_factor
    return Gate(
        gate_id="profit_factor",
        label="Profit factor with uncertainty",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"profit factor {factor.quantize(Decimal('0.01'))} (90% CI {low}-{high})",
        threshold=f"90% CI lower bound >= {cfg.min_profit_factor}",
        detail="Gross wins should outweigh gross losses with margin whose lower CI bound still clears the bar.",
        next_action="Passed." if passed else "Profit factor's lower confidence bound is below the required margin.",
    )


def _gate_drawdown(ledger: RealPaperLedgerResult | None, have_sample: bool, cfg: ProgressConfig) -> Gate:
    if not have_sample or ledger is None:
        return _insufficient(
            "max_drawdown",
            "Maximum drawdown within limit",
            f"<= {cfg.max_drawdown_fraction:%} of starting balance",
            0 if ledger is None else len(ledger.trades),
            cfg,
        )
    peak = ledger.starting_balance
    max_dd = Decimal("0")
    balance = ledger.starting_balance
    for trade in ledger.trades:
        balance = trade.balance_after
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
    limit = (ledger.starting_balance * cfg.max_drawdown_fraction)
    passed = max_dd <= limit
    return Gate(
        gate_id="max_drawdown",
        label="Maximum drawdown within limit",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"max drawdown ${max_dd.quantize(Decimal('0.01'))}",
        threshold=f"<= ${limit.quantize(Decimal('0.01'))}",
        detail="A survivable equity curve matters as much as the average trade.",
        next_action="Passed." if passed else "Drawdown exceeds the risk limit.",
    )


def _gate_consecutive_losses(ledger: RealPaperLedgerResult | None, have_sample: bool, cfg: ProgressConfig) -> Gate:
    if not have_sample or ledger is None:
        return _insufficient(
            "consecutive_losses",
            "Maximum consecutive losses within limit",
            f"<= {cfg.max_consecutive_losses}",
            0 if ledger is None else len(ledger.trades),
            cfg,
        )
    worst = run = 0
    for trade in ledger.trades:
        run = run + 1 if not trade.won else 0
        worst = max(worst, run)
    passed = worst <= cfg.max_consecutive_losses
    return Gate(
        gate_id="consecutive_losses",
        label="Maximum consecutive losses within limit",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"worst streak {worst} losing trades",
        threshold=f"<= {cfg.max_consecutive_losses}",
        detail="Long losing streaks break both accounts and discipline.",
        next_action="Passed." if passed else "Losing streaks exceed the tolerance.",
    )


def _gate_day_concentration(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "day_concentration",
            "Day-level concentration (no single day dominates)",
            f"top day <= {cfg.max_day_concentration:%} of net",
            len(completed),
            cfg,
        )
    # Measure P&L concentration (as labelled), not merely trade-count: a single
    # day must not supply most of the net profit.
    total_net = sum((o.net_pnl_per_contract for o in completed), Decimal("0"))
    by_day_pnl: dict[str, Decimal] = {}
    for o in completed:
        by_day_pnl[o.trading_day] = by_day_pnl.get(o.trading_day, Decimal("0")) + o.net_pnl_per_contract
    if total_net <= 0:
        return Gate(
            gate_id="day_concentration",
            label="Day-level P&L concentration (no single day dominates net)",
            status=STATUS_BLOCKED,
            observed="net P&L is not positive; concentration is not meaningful",
            threshold=f"top day <= {cfg.max_day_concentration:%} of net",
            detail="An edge must not depend on one exceptional session.",
            next_action="Achieve positive net P&L before concentration can pass.",
        )
    top_day_pnl = max((v for v in by_day_pnl.values()), default=Decimal("0"))
    share = top_day_pnl / total_net
    passed = share <= cfg.max_day_concentration
    return Gate(
        gate_id="day_concentration",
        label="Day-level P&L concentration (no single day dominates net)",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"busiest day supplies {share:.2%} of net P&L",
        threshold=f"<= {cfg.max_day_concentration:%} of net",
        detail="An edge must not depend on one exceptional session.",
        next_action="Passed." if passed else "Net profit is concentrated in too few days.",
    )


def _gate_side_balance(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "side_balance",
            "Long/short stability",
            f"each side >= {cfg.min_side_balance:%} of trades",
            len(completed),
            cfg,
        )
    longs = sum(1 for o in completed if o.direction == "long")
    shorts = len(completed) - longs
    long_share = Decimal(longs) / Decimal(len(completed))
    short_share = Decimal(shorts) / Decimal(len(completed))
    passed = long_share >= cfg.min_side_balance and short_share >= cfg.min_side_balance
    return Gate(
        gate_id="side_balance",
        label="Long/short stability",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"{longs} long / {shorts} short",
        threshold=f"each side >= {cfg.min_side_balance:%}",
        detail="A one-directional edge is usually a market regime, not a strategy.",
        next_action="Passed." if passed else "The edge leans too far to one direction.",
    )


def _gate_prop_compliance(ledger: RealPaperLedgerResult | None, have_sample: bool) -> Gate:
    if not have_sample or ledger is None:
        return _insufficient(
            "prop_compliance",
            "Prop-rule compliance (fixed account survived)",
            "account not blown; daily locks respected",
            0 if ledger is None else len(ledger.trades),
            None,
        )
    passed = not ledger.stopped and ledger.ending_balance > 0
    return Gate(
        gate_id="prop_compliance",
        label="Prop-rule compliance (fixed account survived)",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"ending balance ${ledger.ending_balance.quantize(Decimal('0.01'))}, stopped={ledger.stopped}",
        threshold="account survives with balance > 0 under prop-style locks",
        detail="Under Lucid-style rules, a blown account ends the evaluation regardless of expectancy.",
        next_action="Passed." if passed else "The fixed account did not survive the rules.",
    )


def _bootstrap_profit_factor_ci(
    net_pnls: Sequence[Decimal],
    *,
    iterations: int = 500,
    seed: int = 20260716,
) -> tuple[Decimal, Decimal]:
    """Return a deterministic 90% bootstrap CI (low, high) for the profit factor.

    Resamples with replacement using a fixed seed so the interval is reproducible.
    Iterations where the resample has no losing trade are treated as an infinite
    (capped) profit factor, which keeps the estimate conservative on the low side.
    """
    import random

    values = [pnl for pnl in net_pnls]
    if len(values) < 2:
        return (Decimal("0"), Decimal("0"))
    rng = random.Random(seed)
    factors: list[Decimal] = []
    n = len(values)
    cap = Decimal("100")
    for _ in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        gains = sum((v for v in sample if v > 0), Decimal("0"))
        losses = -sum((v for v in sample if v < 0), Decimal("0"))
        factors.append(min(cap, gains / losses) if losses > 0 else cap)
    factors.sort()
    low = factors[int(0.05 * (len(factors) - 1))]
    high = factors[int(0.95 * (len(factors) - 1))]
    return (low.quantize(Decimal("0.01")), high.quantize(Decimal("0.01")))


def _outcome_hour(decision_ts_ns: int) -> int:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(decision_ts_ns / 1_000_000_000, tz=UTC).hour


def _gate_hour_stability(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "hour_stability", "Hour-of-day stability", f"busiest hour <= {cfg.max_hour_concentration:%}",
            len(completed), cfg,
        )
    by_hour: Counter[int] = Counter(_outcome_hour(o.decision_ts_ns) for o in completed)
    top = max(by_hour.values()) if by_hour else 0
    share = Decimal(top) / Decimal(len(completed))
    passed = share <= cfg.max_hour_concentration and len(by_hour) >= 3
    return Gate(
        gate_id="hour_stability",
        label="Hour-of-day stability",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"{len(by_hour)} distinct hours; busiest holds {share:.2%}",
        threshold=f"busiest hour <= {cfg.max_hour_concentration:%}, >= 3 hours",
        detail="An edge concentrated in one hour is usually a session artefact, not a strategy.",
        next_action="Passed." if passed else "Setups are concentrated in too few hours of the day.",
    )


def _gate_walk_forward(
    completed: Sequence[CompletedRealOutcome],
    have_sample: bool,
    cfg: ProgressConfig,
) -> Gate:
    if not have_sample:
        return _insufficient(
            "walk_forward", "Walk-forward out-of-sample expectancy",
            "every fold's test days positive after costs", len(completed), cfg,
        )
    from app.research.validation import evaluate_walk_forward

    report = evaluate_walk_forward(completed)
    if not report.sufficient:
        return Gate(
            gate_id="walk_forward",
            label="Walk-forward out-of-sample expectancy",
            status=STATUS_INSUFFICIENT,
            observed=f"{len(report.folds)} fold(s), {report.total_test_trades} test trades",
            threshold=">= 2 folds, each with trades and positive net expectancy",
            detail="Whole-day expanding folds; test days never select parameters.",
            next_action="Accumulate outcomes across enough distinct days for real folds.",
        )
    passed = report.all_folds_positive
    return Gate(
        gate_id="walk_forward",
        label="Walk-forward out-of-sample expectancy",
        status=STATUS_PASSED if passed else STATUS_BLOCKED,
        observed=f"{report.positive_folds}/{len(report.folds)} folds positive "
                 f"({report.total_test_trades} strictly out-of-sample trades)",
        threshold="every fold's test days positive after costs",
        detail="An edge must survive periods it never trained on, fold after fold.",
        next_action="Passed." if passed else "At least one out-of-sample fold lost money.",
    )


def _gate_demo_soak() -> Gate:
    # Demo-soak evidence comes from an actual Tradovate DEMO run log; none has
    # been produced, so this is honestly insufficient (never a fabricated pass).
    from pathlib import Path as _Path

    soak_log = _Path("data/execution_state/demo_soak.jsonl")
    if soak_log.is_file() and soak_log.stat().st_size > 0:
        observed = "demo soak log present (review required)"
    else:
        observed = "no demo soak evidence recorded"
    return Gate(
        gate_id="demo_soak",
        label="Tradovate DEMO soak period",
        status=STATUS_INSUFFICIENT,
        observed=observed,
        threshold="a reviewed, clean multi-session DEMO soak",
        detail="LIVE consideration requires demonstrated stability on the demo account first.",
        next_action="After validation passes, run the strategy against Tradovate DEMO and review the soak log.",
    )


def _gate_calibration(completed: Sequence[CompletedRealOutcome], have_sample: bool, cfg: ProgressConfig) -> Gate:
    # Calibration/drift compares predicted probabilities to realised outcomes.
    # The deterministic strategy emits no probability, and no supervised model is
    # wired to score these outcomes yet, so this is honestly insufficient.
    return Gate(
        gate_id="calibration_drift",
        label="Calibration and drift",
        status=STATUS_INSUFFICIENT,
        observed="no probability model scores these outcomes yet",
        threshold="calibrated probabilities within tolerance; no significant drift",
        detail="Cannot be computed without a trained probability model over a real sample.",
        next_action="Train and calibrate a probability model once the completed-setup sample exists.",
    )


def _insufficient(
    gate_id: str,
    label: str,
    threshold: str,
    have: int,
    cfg: ProgressConfig | None,
) -> Gate:
    need = cfg.min_completed_setups if cfg is not None else 100
    return Gate(
        gate_id=gate_id,
        label=label,
        status=STATUS_INSUFFICIENT,
        observed=f"{have} completed setups (need >= {need} to judge)",
        threshold=threshold,
        detail="Cannot be calculated yet - there is no real sample to measure.",
        next_action="Collect the required completed-setup sample first.",
    )


def _stage_label(gates: Sequence[Gate], completed: int) -> str:
    if completed >= len(gates):
        return "Validated edge (all evidence gates passed)"
    current = gates[completed]
    return f"Stage {completed + 1}/{len(gates)}: {current.label}"


def _headline(completed: int, total: int, outcomes: Sequence[CompletedRealOutcome], profitable: bool) -> str:
    if profitable:
        return "All evidence gates passed on real delayed data. Ready for the reviewed demo gate."
    if not outcomes:
        return (
            "Data-collection stage: zero setups have completed on real data yet. "
            "This is honest, not a failure - profitability is unproven and cannot be claimed."
        )
    return f"{completed}/{total} evidence gates passed; still building the real completed-setup sample."


def load_progress(
    raw_root: Path,
    processed_root: Path,
    config: ProgressConfig | None = None,
) -> ProfitabilityProgress:
    """Load real inputs from disk and compute the progress meter."""
    from app.research.paper_ledger import run_real_paper_ledger
    from app.research.real_episodes import load_completed_real_outcomes
    from app.research.session_catalog import build_catalog

    catalog = build_catalog(raw_root)
    outcomes = load_completed_real_outcomes(processed_root)
    ledger = run_real_paper_ledger(outcomes) if outcomes else None
    return compute_progress(catalog, outcomes, ledger, config)
