"""Append-only newline-delimited JSON logs for decisions and orders."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypeAlias

Mode: TypeAlias = Literal["observe", "simulate", "live"]
Decision: TypeAlias = Literal["accepted", "rejected"]
Direction: TypeAlias = Literal["long", "short"]
JsonScalar: TypeAlias = str | int | bool | None
JsonValue: TypeAlias = JsonScalar | Decimal | Mapping[str, object] | Sequence[object]


@dataclass(frozen=True, slots=True)
class DecisionLogEntry:
    """Append-only decision log entry."""

    timestamp: int
    mode: Mode
    symbol: str
    direction: Direction
    strategy_version: str
    model_version: str | None
    context: Mapping[str, object]
    checks: Mapping[str, bool]
    decision: Decision
    reason: tuple[str, ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return this decision entry as a JSON-compatible dictionary."""
        _validate_timestamp("timestamp", self.timestamp)
        if self.mode not in {"observe", "simulate", "live"}:
            raise ValueError("mode must be observe, simulate, or live")
        if self.direction not in {"long", "short"}:
            raise ValueError("direction must be long or short")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.strategy_version:
            raise ValueError("strategy_version is required")
        if self.decision not in {"accepted", "rejected"}:
            raise ValueError("decision must be accepted or rejected")
        if not all(isinstance(value, bool) for value in self.checks.values()):
            raise ValueError("checks values must all be bool")
        if not all(isinstance(item, str) for item in self.reason):
            raise ValueError("reason entries must all be strings")

        return {
            "timestamp": self.timestamp,
            "mode": self.mode,
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy_version": self.strategy_version,
            "model_version": self.model_version,
            "context": _to_json_compatible(self.context),
            "checks": dict(self.checks),
            "decision": self.decision,
            "reason": list(self.reason),
        }


@dataclass(frozen=True, slots=True)
class OrderLogEntry:
    """Append-only order lifecycle log entry."""

    signal_timestamp: int
    request_timestamp: int
    broker_ack_timestamp: int | None
    fill_timestamp: int | None
    requested_price: Decimal
    actual_fill_price: Decimal | None
    slippage: Decimal | None
    stop_submitted_at: int
    stop_confirmed_at: int
    target_submitted_at: int
    target_confirmed_at: int
    exit_timestamp: int | None
    realized_pnl: Decimal | None

    def to_json_dict(self) -> dict[str, object]:
        """Return this order entry as a JSON-compatible dictionary."""
        for field_name, value in (
            ("signal_timestamp", self.signal_timestamp),
            ("request_timestamp", self.request_timestamp),
            ("stop_submitted_at", self.stop_submitted_at),
            ("stop_confirmed_at", self.stop_confirmed_at),
            ("target_submitted_at", self.target_submitted_at),
            ("target_confirmed_at", self.target_confirmed_at),
        ):
            _validate_timestamp(field_name, value)

        for field_name, value in (
            ("broker_ack_timestamp", self.broker_ack_timestamp),
            ("fill_timestamp", self.fill_timestamp),
            ("exit_timestamp", self.exit_timestamp),
        ):
            if value is not None:
                _validate_timestamp(field_name, value)

        _validate_decimal("requested_price", self.requested_price)
        for field_name, value in (
            ("actual_fill_price", self.actual_fill_price),
            ("slippage", self.slippage),
            ("realized_pnl", self.realized_pnl),
        ):
            if value is not None:
                _validate_decimal(field_name, value)

        return {
            "signal_timestamp": self.signal_timestamp,
            "request_timestamp": self.request_timestamp,
            "broker_ack_timestamp": self.broker_ack_timestamp,
            "fill_timestamp": self.fill_timestamp,
            "requested_price": str(self.requested_price),
            "actual_fill_price": _optional_decimal_to_json(self.actual_fill_price),
            "slippage": _optional_decimal_to_json(self.slippage),
            "stop_submitted_at": self.stop_submitted_at,
            "stop_confirmed_at": self.stop_confirmed_at,
            "target_submitted_at": self.target_submitted_at,
            "target_confirmed_at": self.target_confirmed_at,
            "exit_timestamp": self.exit_timestamp,
            "realized_pnl": _optional_decimal_to_json(self.realized_pnl),
        }


def append_decision_log_entry(path: str | Path, entry: DecisionLogEntry) -> None:
    """Append one decision log entry to a newline-delimited JSON file."""
    _append_json_line(path, entry.to_json_dict())


def append_order_log_entry(path: str | Path, entry: OrderLogEntry) -> None:
    """Append one order log entry to a newline-delimited JSON file."""
    _append_json_line(path, entry.to_json_dict())


def read_ndjson_log(path: str | Path) -> tuple[dict[str, object], ...]:
    """Read a newline-delimited JSON log file into dictionaries."""
    log_path = Path(path)
    if not log_path.exists():
        return ()

    return tuple(
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _append_json_line(path: str | Path, payload: Mapping[str, object]) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _to_json_compatible(value: object) -> object:
    if isinstance(value, Decimal):
        _validate_decimal("decimal", value)
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise ValueError("float values are not allowed in logs; use Decimal for numeric precision")
    if isinstance(value, Mapping):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_json_compatible(item) for item in value]

    raise ValueError(f"value of type {type(value).__name__} is not JSON log compatible")


def _optional_decimal_to_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    _validate_decimal("decimal", value)
    return str(value)


def _validate_timestamp(field_name: str, value: int) -> None:
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer timestamp")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _validate_decimal(field_name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
