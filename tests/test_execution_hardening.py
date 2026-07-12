"""Tests for Part 4 hardening: dry run, breaker, reconciliation, sizing, config guard."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.circuit_breaker import LatencyCircuitBreaker
from app.execution.dry_run import DryRunExecutor, IntendedOrder
from app.execution.reconciliation_report import FillRecord, write_daily_reconciliation_report
from app.risk.sizing import SizingInputs
from app.runtime.config_guard import (
    UnapprovedConfigChangeError,
    approve_config,
    check_config_trusted,
    require_trusted_config,
)
from app.runtime.fresh_sizing import fresh_position_size
from tools.check_protected_files import MARKER_FILENAME, PROTECTED_FILES, check_diff


def test_dry_run_journals_orders_and_has_no_transport(tmp_path: Path) -> None:
    """Intended orders are journaled with sent_to_broker=false; no network code exists."""
    executor = DryRunExecutor(tmp_path / "intended_orders.jsonl")
    order = IntendedOrder(
        side="buy",
        contracts=1,
        entry_price=Decimal("100.25"),
        stop_price=Decimal("98.25"),
        target_price=Decimal("104.25"),
        reason="clean long absorption reclaim",
    )

    executor.submit(order, now=datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc))

    entry = json.loads((tmp_path / "intended_orders.jsonl").read_text(encoding="utf-8"))
    assert entry["mode"] == "DRY_RUN"
    assert entry["sent_to_broker"] is False
    assert entry["entry_price"] == "100.25"
    module_source = (
        Path(__file__).resolve().parents[1] / "app" / "execution" / "dry_run.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("http", "websocket", "socket", "requests", "urllib"):
        assert forbidden not in module_source.lower()


def test_dry_run_rejects_invalid_orders(tmp_path: Path) -> None:
    """Bad sides and non-positive sizes are rejected before journaling."""
    with pytest.raises(ValueError):
        IntendedOrder(
            side="hold",
            contracts=1,
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            target_price=Decimal("1"),
            reason="x",
        )
    with pytest.raises(ValueError):
        IntendedOrder(
            side="buy",
            contracts=0,
            entry_price=Decimal("1"),
            stop_price=Decimal("1"),
            target_price=Decimal("1"),
            reason="x",
        )


def test_circuit_breaker_trips_on_sustained_latency_and_needs_manual_reset() -> None:
    """Three consecutive spikes open the breaker; only reset() closes it."""
    breaker = LatencyCircuitBreaker(max_latency_ms=500, trip_after=3)

    assert breaker.record_latency(600).open is False
    assert breaker.record_latency(700).open is False
    status = breaker.record_latency(800)
    assert status.open is True
    assert breaker.allow_execution() is False

    breaker.record_latency(10)
    assert breaker.allow_execution() is False

    breaker.reset()
    assert breaker.allow_execution() is True
    assert breaker.status().trip_count == 1


def test_circuit_breaker_resets_spike_count_on_healthy_sample() -> None:
    """Interleaved healthy samples prevent tripping."""
    breaker = LatencyCircuitBreaker(max_latency_ms=500, trip_after=3)
    for latency in (600, 700, 100, 600, 700, 100):
        breaker.record_latency(latency)
    assert breaker.allow_execution() is True


def test_reconciliation_report_flags_every_kind_of_mismatch(tmp_path: Path) -> None:
    """Missing, unexpected, and disagreeing fills are all reported."""
    simulated = (
        FillRecord("A", "buy", 1, Decimal("100.00")),
        FillRecord("B", "buy", 1, Decimal("101.00")),
        FillRecord("C", "sell", 1, Decimal("102.00")),
    )
    actual = (
        FillRecord("B", "buy", 1, Decimal("101.25")),
        FillRecord("D", "sell", 2, Decimal("99.00")),
    )

    path = write_daily_reconciliation_report(
        tmp_path,
        report_date="2026-07-13",
        simulated=simulated,
        actual=actual,
    )
    text = path.read_text(encoding="utf-8")

    assert "DISCREPANCIES FOUND" in text
    assert "`A`" in text and "`C`" in text
    assert "`D`" in text
    assert "101.00" in text and "101.25" in text

    clean_path = write_daily_reconciliation_report(
        tmp_path / "clean",
        report_date="2026-07-13",
        simulated=simulated[:1],
        actual=simulated[:1],
    )
    assert "CLEAN" in clean_path.read_text(encoding="utf-8")


def test_fresh_sizing_recomputes_at_every_call() -> None:
    """Changed inputs change the result; nothing is cached."""
    base = SizingInputs(
        account_size=Decimal("50000"),
        remaining_allowable_drawdown=Decimal("2000"),
        stop_distance_ticks=Decimal("8"),
        tick_value=Decimal("0.50"),
    )
    first = fresh_position_size(base, now=datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc))

    smaller_account = SizingInputs(
        account_size=Decimal("5000"),
        remaining_allowable_drawdown=Decimal("200"),
        stop_distance_ticks=Decimal("8"),
        tick_value=Decimal("0.50"),
    )
    second = fresh_position_size(smaller_account, now=datetime(2026, 7, 13, 15, 1, tzinfo=timezone.utc))

    assert first.result.contracts != second.result.contracts
    assert second.computed_at > first.computed_at
    module_source = (
        Path(__file__).resolve().parents[1] / "app" / "runtime" / "fresh_sizing.py"
    ).read_text(encoding="utf-8")
    assert "lru_cache" not in module_source
    assert "functools" not in module_source


def test_config_guard_blocks_unapproved_changes(tmp_path: Path) -> None:
    """A changed production config is untrusted until explicitly approved."""
    config = tmp_path / "production_config.yaml"
    approval = tmp_path / ".approved"
    config.write_text("live_mode: false\n", encoding="utf-8")

    unapproved = check_config_trusted(config, approval)
    assert unapproved.trusted is False
    with pytest.raises(UnapprovedConfigChangeError):
        require_trusted_config(config, approval)

    approve_config(config, approval)
    assert check_config_trusted(config, approval).trusted is True

    config.write_text("live_mode: true\n", encoding="utf-8")
    changed = check_config_trusted(config, approval)
    assert changed.trusted is False
    assert "changed since the last approval" in changed.reason


def test_protected_files_check_enforces_the_marker() -> None:
    """Protected-file diffs fail without the marker and pass with it."""
    ok, message = check_diff(["app/gui/main_window.py"], marker_present=False)
    assert ok is True

    blocked, reason = check_diff(["app/risk/limits.py", "README.md"], marker_present=False)
    assert blocked is False
    assert MARKER_FILENAME in reason
    assert "app/risk/limits.py" in reason

    approved, _ = check_diff(["app/risk/limits.py"], marker_present=True)
    assert approved is True
    assert "config/production_config.yaml" in PROTECTED_FILES
