"""Automatic paper-trading daily report, computed from the real ledger.

This is the honest daily digest of what the paper engine actually did: how many
setups were evaluated, how many qualified, why the rest were refused, and — only
from **real, non-synthetic** ledger rows — the trade economics. It never invents
a result and never counts a fixture trade as real.

Two hard rules, both enforced by tests:

* **Synthetic fixtures are excluded.** Statistics come from
  ``LedgerRecovery.real_records`` only, so a deterministic test trade can never
  inflate a real report.
* **Zero trades is a first-class outcome.** An empty report states plainly that
  no setup qualified and shows the rejection reasons, rather than rendering a
  blank or implying a loss/win record that does not exist.

Money is ``Decimal`` throughout. The core builder is pure — it takes already
loaded rows and an optional live engine snapshot and returns a value object — so
it is fully deterministic and testable; ``write_paper_daily_report`` is the thin
disk wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from app.paper.ledger import LedgerRecovery, PaperLedger


@dataclass(frozen=True, slots=True)
class CloseReasonTally:
    """How trades closed, and their net contribution."""

    reason: str
    count: int
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class PaperDailyReport:
    """The computed daily paper-trading digest. Immutable and JSON-friendly."""

    report_date: str
    starting_balance: Decimal
    ending_balance: Decimal
    real_trades: int
    wins: int
    losses: int
    scratches: int
    realized_pnl: Decimal
    gross_pnl: Decimal
    total_commission: Decimal
    expectancy: Decimal          # mean net P&L per real trade
    max_drawdown: Decimal        # peak-to-trough of the equity curve (<= 0)
    max_consecutive_losses: int
    close_reasons: tuple[CloseReasonTally, ...] = ()
    # Live-engine context (optional; describes the current streaming session).
    evaluations: int = 0
    accepted_setups: int = 0
    candidates: int = 0
    risk_rejected: int = 0
    top_rejections: tuple[tuple[str, int], ...] = ()
    risk_rejections: tuple[tuple[str, int], ...] = ()
    synthetic_rows_excluded: int = 0
    damaged_tail: bool = False
    notes: tuple[str, ...] = ()

    @property
    def win_rate(self) -> Decimal:
        """Wins as a fraction of decided (non-scratch) trades, or 0."""
        decided = self.wins + self.losses
        return (Decimal(self.wins) / Decimal(decided)).quantize(Decimal("0.0001")) if decided else Decimal("0")

    @property
    def has_real_trades(self) -> bool:
        """Whether any real (non-fixture) trade closed."""
        return self.real_trades > 0


def build_paper_daily_report(
    report_date: str,
    recovery: LedgerRecovery,
    *,
    starting_balance: Decimal,
    engine_status: object | None = None,
) -> PaperDailyReport:
    """Compute the daily report from recovered ledger rows and optional live state.

    Pure: no disk, no clock. ``engine_status`` is a ``PaperEngineStatus``-shaped
    object (duck-typed) describing the current streaming session, used only for
    evaluation/candidate context — never as a source of trade economics.
    """
    real = recovery.real_records
    synthetic_excluded = len(recovery.records) - len(real)

    wins = sum(1 for r in real if Decimal(str(r["net_pnl"])) > 0)
    losses = sum(1 for r in real if Decimal(str(r["net_pnl"])) < 0)
    scratches = len(real) - wins - losses
    realized = sum((Decimal(str(r["net_pnl"])) for r in real), Decimal("0"))
    gross = sum((Decimal(str(r["gross_pnl"])) for r in real), Decimal("0"))
    commission = sum((Decimal(str(r["commission"])) for r in real), Decimal("0"))
    expectancy = (realized / Decimal(len(real))).quantize(Decimal("0.01")) if real else Decimal("0")
    ending = recovery.last_balance(starting_balance)

    report = PaperDailyReport(
        report_date=report_date,
        starting_balance=starting_balance,
        ending_balance=ending,
        real_trades=len(real),
        wins=wins,
        losses=losses,
        scratches=scratches,
        realized_pnl=realized,
        gross_pnl=gross,
        total_commission=commission,
        expectancy=expectancy,
        max_drawdown=_max_drawdown(real, starting_balance),
        max_consecutive_losses=_max_consecutive_losses(real),
        close_reasons=_close_reason_tallies(real),
        synthetic_rows_excluded=synthetic_excluded,
        damaged_tail=recovery.damaged_tail,
    )
    if engine_status is not None:
        report = _with_engine_context(report, engine_status)
    return _with_notes(report)


def _close_reason_tallies(rows: tuple[dict[str, object], ...]) -> tuple[CloseReasonTally, ...]:
    tallies: dict[str, list[Decimal]] = {}
    for row in rows:
        reason = str(row.get("close_reason", "unknown"))
        tallies.setdefault(reason, []).append(Decimal(str(row["net_pnl"])))
    ordered = sorted(tallies.items(), key=lambda kv: len(kv[1]), reverse=True)
    return tuple(
        CloseReasonTally(reason=reason, count=len(nets), net_pnl=sum(nets, Decimal("0")))
        for reason, nets in ordered
    )


def _max_drawdown(rows: tuple[dict[str, object], ...], starting_balance: Decimal) -> Decimal:
    """Return the largest peak-to-trough equity decline (<= 0)."""
    peak = starting_balance
    worst = Decimal("0")
    for row in rows:
        balance = Decimal(str(row["balance_after"]))
        peak = max(peak, balance)
        worst = min(worst, balance - peak)
    return worst


def _max_consecutive_losses(rows: tuple[dict[str, object], ...]) -> int:
    streak = worst = 0
    for row in rows:
        if Decimal(str(row["net_pnl"])) < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def _with_engine_context(report: PaperDailyReport, status: object) -> PaperDailyReport:
    from dataclasses import replace

    def _get(name: str, default: object) -> object:
        return getattr(status, name, default)

    return replace(
        report,
        evaluations=int(_get("evaluations", 0)),
        accepted_setups=int(_get("accepted_setups", 0)),
        candidates=int(_get("candidates", 0)),
        risk_rejected=int(_get("risk_rejected", 0)),
        top_rejections=tuple(_get("top_rejections", ()) or ()),
    )


def _with_notes(report: PaperDailyReport) -> PaperDailyReport:
    from dataclasses import replace

    notes: list[str] = []
    if not report.has_real_trades:
        if report.evaluations:
            notes.append(
                f"{report.evaluations} setups evaluated, {report.accepted_setups} qualified, "
                "but no real paper trade closed. This is an honest zero, not a failure."
            )
        else:
            notes.append("No setup has been evaluated on real data yet.")
    if report.synthetic_rows_excluded:
        notes.append(
            f"{report.synthetic_rows_excluded} synthetic fixture row(s) excluded from all statistics."
        )
    if report.damaged_tail:
        notes.append("The ledger had a torn final line (likely a crash); intact rows were kept.")
    return replace(report, notes=tuple(notes))


def render_paper_daily_markdown(report: PaperDailyReport) -> str:
    """Render the report as Markdown. Never claims a result it does not have."""
    lines = [
        f"# Paper Trading Report — {report.report_date}",
        "",
        "_Simulated delayed-paper results. Not a live trading record; not proof of "
        "profitability. Synthetic fixture trades are excluded from every number below._",
        "",
        "## Account",
        f"- Starting balance: {_money(report.starting_balance)}",
        f"- Ending balance: {_money(report.ending_balance)}",
        f"- Realized P&L: {_money(report.realized_pnl)}",
        f"- Max drawdown: {_money(report.max_drawdown)}",
        "",
        "## Trades",
    ]
    if report.has_real_trades:
        lines += [
            f"- Real trades: {report.real_trades}",
            f"- Wins / losses / scratches: {report.wins} / {report.losses} / {report.scratches}",
            f"- Win rate: {report.win_rate:.1%}",
            f"- Expectancy per trade: {_money(report.expectancy)}",
            f"- Gross P&L: {_money(report.gross_pnl)}   Commission: {_money(report.total_commission)}",
            f"- Max consecutive losses: {report.max_consecutive_losses}",
            "",
            "### How trades closed",
        ]
        for tally in report.close_reasons:
            lines.append(f"- {tally.reason}: {tally.count} ({_money(tally.net_pnl)})")
    else:
        lines.append("- No real paper trade closed today.")
    lines += ["", "## Evaluation activity"]
    lines += [
        f"- Setups evaluated: {report.evaluations:,}",
        f"- Qualified setups: {report.accepted_setups:,}",
        f"- Candidates generated: {report.candidates:,}",
        f"- Blocked by risk: {report.risk_rejected:,}",
    ]
    if report.top_rejections:
        lines.append("- Most common rejection reasons:")
        for name, count in report.top_rejections[:5]:
            lines.append(f"  - {name}: {count}")
    if report.notes:
        lines += ["", "## Notes"]
        lines += [f"- {note}" for note in report.notes]
    return "\n".join(lines) + "\n"


def write_paper_daily_report(
    report_root: Path,
    report_date: str,
    ledger_path: Path,
    *,
    starting_balance: Decimal,
    engine_status: object | None = None,
) -> Path:
    """Recover the ledger, build the report, and write Markdown + JSON. Returns the md path."""
    import json

    recovery = PaperLedger.recover(ledger_path)
    report = build_paper_daily_report(
        report_date, recovery, starting_balance=starting_balance, engine_status=engine_status,
    )
    directory = Path(report_root) / report_date
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / "paper_report.md"
    markdown_path.write_text(render_paper_daily_markdown(report), encoding="utf-8")
    (directory / "paper_report.json").write_text(
        json.dumps(_report_json(report), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return markdown_path


def _report_json(report: PaperDailyReport) -> dict[str, object]:
    """A JSON-safe view (money as strings, never floats)."""
    return {
        "report_date": report.report_date,
        "starting_balance": str(report.starting_balance),
        "ending_balance": str(report.ending_balance),
        "real_trades": report.real_trades,
        "wins": report.wins,
        "losses": report.losses,
        "scratches": report.scratches,
        "win_rate": str(report.win_rate),
        "realized_pnl": str(report.realized_pnl),
        "gross_pnl": str(report.gross_pnl),
        "total_commission": str(report.total_commission),
        "expectancy": str(report.expectancy),
        "max_drawdown": str(report.max_drawdown),
        "max_consecutive_losses": report.max_consecutive_losses,
        "close_reasons": [
            {"reason": t.reason, "count": t.count, "net_pnl": str(t.net_pnl)}
            for t in report.close_reasons
        ],
        "evaluations": report.evaluations,
        "accepted_setups": report.accepted_setups,
        "candidates": report.candidates,
        "risk_rejected": report.risk_rejected,
        "synthetic_rows_excluded": report.synthetic_rows_excluded,
        "damaged_tail": report.damaged_tail,
        "notes": list(report.notes),
    }


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"
