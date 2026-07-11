"""Tests for append-only newline-delimited JSON logging."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.database.logging import (
    DecisionLogEntry,
    OrderLogEntry,
    append_decision_log_entry,
    append_order_log_entry,
    read_ndjson_log,
)


def test_append_decision_log_entry_writes_one_ndjson_line(tmp_path: Path) -> None:
    """Decision entries are appended as JSON lines with exact Decimal strings."""
    log_path = tmp_path / "decision.ndjson"
    entry = _decision_entry()

    append_decision_log_entry(log_path, entry)

    rows = read_ndjson_log(log_path)
    assert rows == (
        {
            "timestamp": 1_000,
            "mode": "observe",
            "symbol": "MNQ",
            "direction": "long",
            "strategy_version": "strategy-v1",
            "model_version": None,
            "context": {
                "important_level": "overnight_low",
                "distance_ticks": "3.0",
                "nested": {"bid_size": "12"},
            },
            "checks": {
                "at_important_level": True,
                "bid_reload_count": False,
            },
            "decision": "rejected",
            "reason": ["Bid liquidity did not reload"],
        },
    )


def test_append_decision_log_entry_is_append_only(tmp_path: Path) -> None:
    """Appending a decision entry preserves existing JSON lines."""
    log_path = tmp_path / "decision.ndjson"

    append_decision_log_entry(log_path, _decision_entry(timestamp=1_000))
    append_decision_log_entry(log_path, _decision_entry(timestamp=2_000))

    rows = read_ndjson_log(log_path)
    assert [row["timestamp"] for row in rows] == [1_000, 2_000]
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 2


def test_append_decision_log_entry_rejects_invalid_mode(tmp_path: Path) -> None:
    """Decision entries validate the allowed mode values."""
    log_path = tmp_path / "decision.ndjson"
    entry = DecisionLogEntry(
        timestamp=1,
        mode="paper",  # type: ignore[arg-type]
        symbol="MNQ",
        direction="long",
        strategy_version="strategy-v1",
        model_version=None,
        context={},
        checks={"check": True},
        decision="accepted",
        reason=(),
    )

    with pytest.raises(ValueError, match="mode"):
        append_decision_log_entry(log_path, entry)

    assert not log_path.exists()


def test_append_decision_log_entry_rejects_float_context_values(tmp_path: Path) -> None:
    """Float context values are rejected so money and price precision stays explicit."""
    log_path = tmp_path / "decision.ndjson"
    entry = DecisionLogEntry(
        timestamp=1,
        mode="observe",
        symbol="MNQ",
        direction="long",
        strategy_version="strategy-v1",
        model_version=None,
        context={"price": 100.25},
        checks={"check": True},
        decision="accepted",
        reason=(),
    )

    with pytest.raises(ValueError, match="float"):
        append_decision_log_entry(log_path, entry)


def test_append_order_log_entry_writes_nullable_order_fields(tmp_path: Path) -> None:
    """Order entries preserve nullable lifecycle fields and Decimal values."""
    log_path = tmp_path / "orders.ndjson"
    entry = _order_entry()

    append_order_log_entry(log_path, entry)

    rows = read_ndjson_log(log_path)
    assert rows == (
        {
            "signal_timestamp": 1_000,
            "request_timestamp": 1_100,
            "broker_ack_timestamp": None,
            "fill_timestamp": None,
            "requested_price": "100.25",
            "actual_fill_price": None,
            "slippage": None,
            "stop_submitted_at": 1_200,
            "stop_confirmed_at": 1_300,
            "target_submitted_at": 1_400,
            "target_confirmed_at": 1_500,
            "exit_timestamp": None,
            "realized_pnl": None,
        },
    )


def test_append_order_log_entry_is_append_only(tmp_path: Path) -> None:
    """Appending order entries does not overwrite prior order rows."""
    log_path = tmp_path / "orders.ndjson"

    append_order_log_entry(log_path, _order_entry(signal_timestamp=1_000))
    append_order_log_entry(log_path, _order_entry(signal_timestamp=2_000))

    rows = read_ndjson_log(log_path)
    assert [row["signal_timestamp"] for row in rows] == [1_000, 2_000]


def test_append_order_log_entry_writes_filled_order_values(tmp_path: Path) -> None:
    """Filled order fields serialize Decimal prices, slippage, and PnL as strings."""
    log_path = tmp_path / "orders.ndjson"
    entry = OrderLogEntry(
        signal_timestamp=1_000,
        request_timestamp=1_100,
        broker_ack_timestamp=1_150,
        fill_timestamp=1_200,
        requested_price=Decimal("100.25"),
        actual_fill_price=Decimal("100.50"),
        slippage=Decimal("0.25"),
        stop_submitted_at=1_250,
        stop_confirmed_at=1_300,
        target_submitted_at=1_350,
        target_confirmed_at=1_400,
        exit_timestamp=2_000,
        realized_pnl=Decimal("15.50"),
    )

    append_order_log_entry(log_path, entry)

    row = read_ndjson_log(log_path)[0]
    assert row["actual_fill_price"] == "100.50"
    assert row["slippage"] == "0.25"
    assert row["realized_pnl"] == "15.50"


def test_append_order_log_entry_rejects_negative_timestamp(tmp_path: Path) -> None:
    """Order entries reject invalid timestamp values before writing."""
    log_path = tmp_path / "orders.ndjson"
    entry = _order_entry(signal_timestamp=-1)

    with pytest.raises(ValueError, match="signal_timestamp"):
        append_order_log_entry(log_path, entry)

    assert not log_path.exists()


def _decision_entry(timestamp: int = 1_000) -> DecisionLogEntry:
    return DecisionLogEntry(
        timestamp=timestamp,
        mode="observe",
        symbol="MNQ",
        direction="long",
        strategy_version="strategy-v1",
        model_version=None,
        context={
            "important_level": "overnight_low",
            "distance_ticks": Decimal("3.0"),
            "nested": {"bid_size": Decimal("12")},
        },
        checks={
            "at_important_level": True,
            "bid_reload_count": False,
        },
        decision="rejected",
        reason=("Bid liquidity did not reload",),
    )


def _order_entry(signal_timestamp: int = 1_000) -> OrderLogEntry:
    return OrderLogEntry(
        signal_timestamp=signal_timestamp,
        request_timestamp=1_100,
        broker_ack_timestamp=None,
        fill_timestamp=None,
        requested_price=Decimal("100.25"),
        actual_fill_price=None,
        slippage=None,
        stop_submitted_at=1_200,
        stop_confirmed_at=1_300,
        target_submitted_at=1_400,
        target_confirmed_at=1_500,
        exit_timestamp=None,
        realized_pnl=None,
    )
