"""Task 5 market-event schema formatting and parsing helpers."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias, cast

DepthSide: TypeAlias = Literal["bid", "ask"]
AggressorSide: TypeAlias = Literal["buy", "sell"]
RawMarketEvent: TypeAlias = dict[str, object]

DEPTH_UPDATE_KEYS = frozenset(
    {"type", "timestamp", "symbol", "side", "price", "previous_size", "new_size"},
)
TRADE_KEYS = frozenset(
    {"timestamp_ns", "price", "size", "aggressor_side", "instrument", "sequence_id"},
)


class EventSchemaError(ValueError):
    """Raised when a WebSocket message does not match a supported event schema."""


def format_depth_update(
    *,
    timestamp: int,
    symbol: str,
    side: str | bool,
    price: object,
    previous_size: object,
    new_size: object,
) -> RawMarketEvent:
    """Format one depth update with the exact Task 5 depth-update schema."""
    normalized_price = _decimal_wire_value(price, "price", minimum=Decimal("0"), inclusive=False)
    return {
        "type": "depth_update",
        "timestamp": _timestamp_value(timestamp, "timestamp"),
        "symbol": _required_text(symbol, "symbol"),
        "side": _normalize_depth_side(side),
        "price": normalized_price,
        "previous_size": _decimal_wire_value(
            previous_size,
            "previous_size",
            minimum=Decimal("0"),
            inclusive=True,
        ),
        "new_size": _decimal_wire_value(new_size, "new_size", minimum=Decimal("0"), inclusive=True),
    }


def format_trade(
    *,
    timestamp_ns: int,
    price: object,
    size: object,
    aggressor_side: str,
    instrument: str,
    sequence_id: int,
) -> RawMarketEvent:
    """Format one trade with the exact Task 5 trade schema."""
    return {
        "timestamp_ns": _timestamp_value(timestamp_ns, "timestamp_ns"),
        "price": _decimal_wire_value(price, "price", minimum=Decimal("0"), inclusive=False),
        "size": _decimal_wire_value(size, "size", minimum=Decimal("0"), inclusive=False),
        "aggressor_side": _normalize_aggressor_side(aggressor_side),
        "instrument": _required_text(instrument, "instrument"),
        "sequence_id": _sequence_value(sequence_id),
    }


def event_to_json(event: RawMarketEvent) -> str:
    """Serialize one validated market event as a compact JSON WebSocket payload."""
    normalized_event = _normalize_event(event)
    return json.dumps(normalized_event, separators=(",", ":"), sort_keys=True)


def parse_event_message(message: str | bytes) -> RawMarketEvent:
    """Parse and validate one JSON WebSocket payload into a market-event dictionary."""
    try:
        payload = json.loads(message)
    except json.JSONDecodeError as error:
        raise EventSchemaError("message must be valid JSON") from error

    if not isinstance(payload, dict):
        raise EventSchemaError("message must contain a JSON object")

    return _normalize_event(cast(RawMarketEvent, payload))


def _normalize_event(event: RawMarketEvent) -> RawMarketEvent:
    keys = frozenset(event)
    if keys == DEPTH_UPDATE_KEYS:
        if event.get("type") != "depth_update":
            raise EventSchemaError("depth update type must be depth_update")
        _reject_json_float_fields(event, ("price", "previous_size", "new_size"))
        return format_depth_update(
            timestamp=_raw_int(event["timestamp"], "timestamp"),
            symbol=str(event["symbol"]),
            side=event["side"],
            price=event["price"],
            previous_size=event["previous_size"],
            new_size=event["new_size"],
        )
    if keys == TRADE_KEYS:
        _reject_json_float_fields(event, ("price", "size"))
        return format_trade(
            timestamp_ns=_raw_int(event["timestamp_ns"], "timestamp_ns"),
            price=event["price"],
            size=event["size"],
            aggressor_side=str(event["aggressor_side"]),
            instrument=str(event["instrument"]),
            sequence_id=_raw_int(event["sequence_id"], "sequence_id"),
        )
    raise EventSchemaError("message must match the Task 5 depth_update or trade schema exactly")


def _reject_json_float_fields(event: RawMarketEvent, field_names: tuple[str, ...]) -> None:
    for field_name in field_names:
        if isinstance(event[field_name], float):
            raise EventSchemaError(f"{field_name} must be encoded as a decimal string")


def _normalize_depth_side(value: str | bool | object) -> DepthSide:
    if isinstance(value, bool):
        return "bid" if value else "ask"
    normalized = str(value).strip().lower()
    if normalized in {"bid", "b", "buy"}:
        return "bid"
    if normalized in {"ask", "offer", "a", "sell"}:
        return "ask"
    raise EventSchemaError("side must be bid or ask")


def _normalize_aggressor_side(value: str) -> AggressorSide:
    normalized = value.strip().lower()
    if normalized in {"buy", "buyer", "bid", "b"}:
        return "buy"
    if normalized in {"sell", "seller", "ask", "offer", "s"}:
        return "sell"
    raise EventSchemaError("aggressor_side must be buy or sell")


def _decimal_wire_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal,
    inclusive: bool,
) -> str:
    if isinstance(value, bool):
        raise EventSchemaError(f"{field_name} must be numeric, not bool")

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise EventSchemaError(f"{field_name} must be decimal-compatible") from error

    if not decimal_value.is_finite():
        raise EventSchemaError(f"{field_name} must be finite")
    if inclusive and decimal_value < minimum:
        raise EventSchemaError(f"{field_name} must be at least {minimum}")
    if not inclusive and decimal_value <= minimum:
        raise EventSchemaError(f"{field_name} must be greater than {minimum}")

    return str(value) if isinstance(value, str) else format(decimal_value, "f")


def _timestamp_value(value: int, field_name: str) -> int:
    timestamp = _raw_int(value, field_name)
    if timestamp < 0:
        raise EventSchemaError(f"{field_name} must be non-negative")
    return timestamp


def _sequence_value(value: int) -> int:
    sequence_id = _raw_int(value, "sequence_id")
    if sequence_id < 0:
        raise EventSchemaError("sequence_id must be non-negative")
    return sequence_id


def _raw_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise EventSchemaError(f"{field_name} must be an integer, not bool")
    if not isinstance(value, int):
        raise EventSchemaError(f"{field_name} must be an integer")
    return value


def _required_text(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise EventSchemaError(f"{field_name} is required")
    return text
