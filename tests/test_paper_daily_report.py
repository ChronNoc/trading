"""Automatic paper-trading daily report: honest, ledger-derived, fixture-safe.

Every test writes to pytest's ``tmp_path`` (an external temp directory); no real
recording, ledger, or report is touched.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.paper.daily_report import (
    build_paper_daily_report,
    render_paper_daily_markdown,
    write_paper_daily_report,
)
from app.paper.ledger import PaperLedger
from app.paper.models import CloseReason, Direction, PaperTrade, SetupProvenance


def _trade(*, net: str, balance: str, reason: CloseReason = CloseReason.TARGET,
           synthetic: bool = False) -> PaperTrade:
    gross = Decimal(net) + Decimal("1.24")
    return PaperTrade(
        provenance=SetupProvenance(session_id="s1", setup_id=f"s1:{net}", strategy_version="v1",
                                   contract="MNQU6", decision_event_index=1, decision_ts_ns=1),
        direction=Direction.LONG, contracts=1,
        entry_price=Decimal("29500.00"), exit_price=Decimal("29520.00"),
        stop=Decimal("29490.00"), target=Decimal("29520.00"),
        opened_ts_ns=1, closed_ts_ns=2, opened_event_index=1, closed_event_index=2,
        close_reason=reason, gross_pnl=gross, commission=Decimal("1.24"),
        slippage_cost=Decimal("1.00"), net_pnl=Decimal(net), r_multiple=Decimal("1.0"),
        mae_points=Decimal("-2"), mfe_points=Decimal("20"),
        balance_after=Decimal(balance), is_synthetic_fixture=synthetic,
    )


def _ledger_with(tmp_path: Path, trades: list[PaperTrade]) -> PaperLedger:
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    for trade in trades:
        ledger.append(trade)
    return ledger


def _recover(ledger: PaperLedger):  # noqa: ANN202 - test helper
    """Re-read from disk: ``ledger.recovered`` is the pre-append open-time snapshot."""
    return PaperLedger.recover(ledger.path)


class _Status:
    """Duck-typed PaperEngineStatus for engine-context tests."""

    evaluations = 4821
    accepted_setups = 12
    candidates = 9
    risk_rejected = 3
    top_rejections = (("durable_defending_block", 2100), ("cvd_supports_direction", 900))


def test_empty_ledger_is_an_honest_zero_not_a_blank(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.has_real_trades is False
    assert report.real_trades == 0
    assert report.realized_pnl == Decimal("0")
    md = render_paper_daily_markdown(report)
    assert "No real paper trade closed today." in md
    assert "No setup has been evaluated on real data yet." in md


def test_real_trades_produce_correct_economics(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [
        _trade(net="38.26", balance="25038.26"),
        _trade(net="-21.74", balance="25016.52", reason=CloseReason.STOP),
        _trade(net="38.26", balance="25054.78"),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.real_trades == 3
    assert report.wins == 2 and report.losses == 1
    assert report.realized_pnl == Decimal("54.78")
    assert report.ending_balance == Decimal("25054.78")
    assert report.win_rate == Decimal("0.6667")
    # expectancy = 54.78 / 3
    assert report.expectancy == Decimal("18.26")


def test_synthetic_fixtures_are_excluded_from_every_number(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [
        _trade(net="5000.00", balance="30000.00", synthetic=True),  # must not count
        _trade(net="38.26", balance="25038.26"),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.real_trades == 1
    assert report.realized_pnl == Decimal("38.26"), "a fixture must never inflate results"
    assert report.synthetic_rows_excluded == 1
    assert "1 synthetic fixture row(s) excluded" in " ".join(report.notes)


def test_max_drawdown_is_peak_to_trough(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [
        _trade(net="100.00", balance="25100.00"),   # peak 25100
        _trade(net="-300.00", balance="24800.00", reason=CloseReason.STOP),  # -300 from peak
        _trade(net="50.00", balance="24850.00"),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.max_drawdown == Decimal("-300.00")


def test_max_consecutive_losses_counts_the_worst_streak(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [
        _trade(net="-1.00", balance="24999.00", reason=CloseReason.STOP),
        _trade(net="-1.00", balance="24998.00", reason=CloseReason.STOP),
        _trade(net="10.00", balance="25008.00"),
        _trade(net="-1.00", balance="25007.00", reason=CloseReason.STOP),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.max_consecutive_losses == 2


def test_close_reason_breakdown_is_grouped_and_summed(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [
        _trade(net="38.26", balance="25038.26", reason=CloseReason.TARGET),
        _trade(net="38.26", balance="25076.52", reason=CloseReason.TARGET),
        _trade(net="-21.74", balance="25054.78", reason=CloseReason.STOP),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    reasons = {t.reason: (t.count, t.net_pnl) for t in report.close_reasons}
    assert reasons["target"] == (2, Decimal("76.52"))
    assert reasons["stop"] == (1, Decimal("-21.74"))


def test_engine_context_adds_evaluation_activity_without_touching_economics(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    report = build_paper_daily_report("2026-07-17", ledger.recovered,
                                      starting_balance=Decimal("25000"), engine_status=_Status())
    assert report.evaluations == 4821
    assert report.accepted_setups == 12
    md = render_paper_daily_markdown(report)
    assert "Setups evaluated: 4,821" in md
    assert "durable_defending_block: 2100" in md
    # The honest zero note must reflect the evaluation activity.
    assert "4821 setups evaluated, 12 qualified, but no real paper trade closed" in " ".join(report.notes)


def test_write_produces_markdown_and_json_with_money_as_text(tmp_path: Path) -> None:
    ledger = _ledger_with(tmp_path, [_trade(net="38.26", balance="25038.26")])
    md_path = write_paper_daily_report(
        tmp_path / "reports", "2026-07-17", ledger.path, starting_balance=Decimal("25000"),
    )
    assert md_path.is_file()
    assert "Paper Trading Report" in md_path.read_text(encoding="utf-8")
    payload = json.loads((md_path.parent / "paper_report.json").read_text(encoding="utf-8"))
    assert isinstance(payload["realized_pnl"], str)
    assert payload["realized_pnl"] == "38.26"
    assert payload["real_trades"] == 1


def test_report_never_claims_a_result_it_does_not_have(tmp_path: Path) -> None:
    """The honesty guard: an all-fixture ledger reports zero real trades."""
    ledger = _ledger_with(tmp_path, [
        _trade(net="500.00", balance="25500.00", synthetic=True),
        _trade(net="500.00", balance="26000.00", synthetic=True),
    ])
    report = build_paper_daily_report("2026-07-17", _recover(ledger),
                                      starting_balance=Decimal("25000"))
    assert report.has_real_trades is False
    assert report.realized_pnl == Decimal("0")
    assert report.ending_balance == Decimal("25000"), "fixtures must not move the real balance"


def test_a_torn_ledger_tail_is_reported_in_the_notes(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    ledger = PaperLedger(path, fsync=False)
    ledger.append(_trade(net="38.26", balance="25038.26"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"net_pnl": "1.0')  # crash mid-write
    report = build_paper_daily_report("2026-07-17", PaperLedger.recover(path),
                                      starting_balance=Decimal("25000"))
    assert report.damaged_tail is True
    assert any("torn final line" in note for note in report.notes)
    assert report.real_trades == 1, "the intact trade must still count"


# --- the automatic wiring must stay in place -------------------------------------


def test_launcher_generates_the_paper_report_on_session_finalize() -> None:
    """The report must be automatic - no manual command required."""
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "_write_paper_daily_report(config, session_date, paper_engine, controller)" in source
    assert "write_paper_daily_report(" in source


def test_cli_runs_against_a_real_ledger(tmp_path: Path) -> None:
    from tools.paper_daily_report import main

    ledger = _ledger_with(tmp_path, [_trade(net="38.26", balance="25038.26")])
    rc = main(["--date", "2026-07-17", "--ledger", str(ledger.path),
               "--report-root", str(tmp_path / "reports")])
    assert rc == 0
    assert (tmp_path / "reports" / "2026-07-17" / "paper_report.md").is_file()


def test_cli_handles_a_missing_ledger_without_crashing(tmp_path: Path) -> None:
    from tools.paper_daily_report import main

    rc = main(["--date", "2026-07-17", "--ledger", str(tmp_path / "absent.jsonl"),
               "--report-root", str(tmp_path / "reports")])
    assert rc == 0, "a missing ledger is an honest zero, not an error"
