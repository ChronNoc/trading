"""Demo-only Tradovate order-management primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, TypeAlias

# DEMO ONLY. This module is intentionally hardwired to Tradovate's demo REST/WS
# endpoints. Switching to live trading must be a separate reviewed change in
# app/execution/live_execution.py, which this task intentionally does not create.
TRADOVATE_DEMO_REST_BASE_URL = "https://demo.tradovateapi.com/v1"
TRADOVATE_DEMO_WS_URL = "wss://demo.tradovateapi.com/v1/websocket"

OrderAction: TypeAlias = Literal["Buy", "Sell"]
OrderType: TypeAlias = Literal["Market", "Limit", "Stop"]
ExecutionAction: TypeAlias = Literal["place_order", "cancel_order", "flatten_position"]
AckLogger: TypeAlias = Callable[["BrokerAcknowledgement"], None]


class RiskApproval(Protocol):
    """Protocol for the risk engine's required approval object."""

    allowed: bool
    reason: str | Sequence[str]


class TradovateHttpClient(Protocol):
    """Protocol for a mocked or real Tradovate REST client."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object] | None = None,
    ) -> object:
        """Perform a GET request and return decoded JSON-compatible data."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> object:
        """Perform a POST request and return decoded JSON-compatible data."""


class TradovateAckClient(Protocol):
    """Protocol for a Tradovate WebSocket acknowledgement listener."""

    def wait_for_acknowledgement(
        self,
        *,
        correlation_id: str,
        action: ExecutionAction,
        timeout_seconds: Decimal,
    ) -> "BrokerAcknowledgement":
        """Wait for the broker acknowledgement matching a REST request."""


class ExecutionRejectedError(RuntimeError):
    """Raised when execution is refused before or after a broker request."""


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Tradovate account identity needed by order endpoints."""

    account_id: int
    account_spec: str


@dataclass(frozen=True, slots=True)
class ExistingPosition:
    """Open position summary returned by the pre-submit position check."""

    account_id: int
    symbol: str
    net_quantity: int
    contract_id: int | None = None


@dataclass(frozen=True, slots=True)
class BrokerAcknowledgement:
    """Broker acknowledgement received from the Tradovate WebSocket stream."""

    action: ExecutionAction
    correlation_id: str
    accepted: bool
    broker_timestamp_ns: int
    order_id: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class CancelOrderResult:
    """Result of a demo cancel-order request."""

    order_id: str
    response: Mapping[str, object]
    acknowledgement: BrokerAcknowledgement


@dataclass(frozen=True, slots=True)
class FlattenPositionResult:
    """Result of a demo flatten-position request."""

    symbol: str
    flattened: bool
    response: Mapping[str, object] | None
    acknowledgement: BrokerAcknowledgement | None


def check_correct_contract_month(
    risk_approval: RiskApproval,
    *,
    symbol: str,
    expected_contract_symbol: str,
) -> None:
    """Reject execution unless the symbol exactly matches the approved contract month."""
    _require_approved(risk_approval)
    if not symbol:
        raise ExecutionRejectedError("Contract symbol is required.")
    if symbol != expected_contract_symbol:
        raise ExecutionRejectedError(
            f"Contract month mismatch: requested {symbol}, expected {expected_contract_symbol}.",
        )


def fetch_existing_position(
    risk_approval: RiskApproval,
    *,
    account: AccountRef,
    symbol: str,
    http_client: TradovateHttpClient,
    access_token: str,
) -> ExistingPosition:
    """Fetch the current position for an account and contract symbol."""
    _require_approved(risk_approval)
    response = http_client.get(
        _demo_url("position/list"),
        headers=_auth_headers(access_token),
        params={"accountId": account.account_id},
    )
    positions = _as_sequence_of_mappings(response, "position/list response")
    for position in positions:
        if int(position.get("accountId", account.account_id)) != account.account_id:
            continue
        if _position_symbol(position) != symbol:
            continue
        return ExistingPosition(
            account_id=account.account_id,
            symbol=symbol,
            net_quantity=_position_quantity(position),
            contract_id=_optional_int(position.get("contractId")),
        )

    return ExistingPosition(account_id=account.account_id, symbol=symbol, net_quantity=0)


def ensure_flat_before_entry(
    risk_approval: RiskApproval,
    *,
    position: ExistingPosition,
) -> None:
    """Reject new entry submission when an existing position is open."""
    _require_approved(risk_approval)
    if position.net_quantity != 0:
        raise ExecutionRejectedError(
            f"Existing {position.symbol} position is {position.net_quantity}; refusing new entry.",
        )


def wait_for_broker_acknowledgement(
    risk_approval: RiskApproval,
    *,
    ws_client: TradovateAckClient,
    correlation_id: str,
    action: ExecutionAction,
    ack_logger: AckLogger | None = None,
    timeout_seconds: Decimal = Decimal("5"),
) -> BrokerAcknowledgement:
    """Wait for and optionally log one broker acknowledgement from WebSocket state."""
    _require_approved(risk_approval)
    _require_positive_decimal("timeout_seconds", timeout_seconds)
    acknowledgement = ws_client.wait_for_acknowledgement(
        correlation_id=correlation_id,
        action=action,
        timeout_seconds=timeout_seconds,
    )
    if ack_logger is not None:
        ack_logger(acknowledgement)
    if not acknowledgement.accepted:
        raise ExecutionRejectedError(
            f"Broker rejected {action} acknowledgement {correlation_id}: {acknowledgement.message}",
        )
    return acknowledgement


def submit_order(
    risk_approval: RiskApproval,
    *,
    payload: Mapping[str, object],
    http_client: TradovateHttpClient,
    ws_client: TradovateAckClient,
    access_token: str,
    ack_logger: AckLogger | None = None,
    timeout_seconds: Decimal = Decimal("5"),
) -> tuple[Mapping[str, object], BrokerAcknowledgement]:
    """Submit a demo order payload and wait for its WebSocket acknowledgement."""
    _require_approved(risk_approval)
    response = _as_mapping(
        http_client.post(
            _demo_url("order/placeOrder"),
            headers=_auth_headers(access_token),
            json=payload,
        ),
        "order/placeOrder response",
    )
    correlation_id = _correlation_id(response)
    acknowledgement = wait_for_broker_acknowledgement(
        risk_approval,
        ws_client=ws_client,
        correlation_id=correlation_id,
        action="place_order",
        ack_logger=ack_logger,
        timeout_seconds=timeout_seconds,
    )
    return response, acknowledgement


def cancel_order(
    risk_approval: RiskApproval,
    *,
    account: AccountRef,
    order_id: str,
    http_client: TradovateHttpClient,
    ws_client: TradovateAckClient,
    access_token: str,
    ack_logger: AckLogger | None = None,
    timeout_seconds: Decimal = Decimal("5"),
) -> CancelOrderResult:
    """Cancel an existing demo order and wait for broker acknowledgement."""
    _require_approved(risk_approval)
    if not order_id:
        raise ValueError("order_id is required")
    response = _as_mapping(
        http_client.post(
            _demo_url("order/cancelOrder"),
            headers=_auth_headers(access_token),
            json={"accountId": account.account_id, "orderId": order_id},
        ),
        "order/cancelOrder response",
    )
    acknowledgement = wait_for_broker_acknowledgement(
        risk_approval,
        ws_client=ws_client,
        correlation_id=_correlation_id(response, fallback=order_id),
        action="cancel_order",
        ack_logger=ack_logger,
        timeout_seconds=timeout_seconds,
    )
    return CancelOrderResult(order_id=order_id, response=response, acknowledgement=acknowledgement)


def flatten_position(
    risk_approval: RiskApproval,
    *,
    account: AccountRef,
    symbol: str,
    expected_contract_symbol: str,
    http_client: TradovateHttpClient,
    ws_client: TradovateAckClient,
    access_token: str,
    ack_logger: AckLogger | None = None,
    timeout_seconds: Decimal = Decimal("5"),
) -> FlattenPositionResult:
    """Flatten the current demo position for a symbol and wait for acknowledgement."""
    _require_approved(risk_approval)
    check_correct_contract_month(
        risk_approval,
        symbol=symbol,
        expected_contract_symbol=expected_contract_symbol,
    )
    position = fetch_existing_position(
        risk_approval,
        account=account,
        symbol=symbol,
        http_client=http_client,
        access_token=access_token,
    )
    if position.net_quantity == 0:
        return FlattenPositionResult(symbol=symbol, flattened=False, response=None, acknowledgement=None)
    if position.contract_id is None:
        raise ExecutionRejectedError(f"Cannot flatten {symbol}: broker position did not include contractId.")

    response = _as_mapping(
        http_client.post(
            _demo_url("order/liquidatePosition"),
            headers=_auth_headers(access_token),
            json={
                "accountId": account.account_id,
                "contractId": position.contract_id,
                "admin": False,
            },
        ),
        "order/liquidatePosition response",
    )
    acknowledgement = wait_for_broker_acknowledgement(
        risk_approval,
        ws_client=ws_client,
        correlation_id=_correlation_id(response, fallback=symbol),
        action="flatten_position",
        ack_logger=ack_logger,
        timeout_seconds=timeout_seconds,
    )
    return FlattenPositionResult(
        symbol=symbol,
        flattened=True,
        response=response,
        acknowledgement=acknowledgement,
    )


def _require_approved(risk_approval: RiskApproval) -> None:
    allowed = getattr(risk_approval, "allowed", None)
    if allowed is None:
        raise TypeError("risk_approval with an allowed field is required")
    if not isinstance(allowed, bool):
        raise TypeError("risk_approval.allowed must be bool")
    if not allowed:
        raise ExecutionRejectedError(_risk_reason(risk_approval))


def _risk_reason(risk_approval: RiskApproval) -> str:
    reason = getattr(risk_approval, "reason", "Risk engine did not approve execution.")
    if isinstance(reason, str):
        return reason
    return "; ".join(str(item) for item in reason)


def _demo_url(path: str) -> str:
    return f"{TRADOVATE_DEMO_REST_BASE_URL}/{path}"


def _auth_headers(access_token: str) -> dict[str, str]:
    if not access_token:
        raise ValueError("access_token is required")
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _correlation_id(response: Mapping[str, object], fallback: str | None = None) -> str:
    for key in ("orderId", "id", "requestId", "commandId"):
        value = response.get(key)
        if value is not None:
            return str(value)
    if fallback is not None:
        return fallback
    raise ValueError("broker response did not include an order/request id")


def _position_symbol(position: Mapping[str, object]) -> str:
    for key in ("symbol", "contractName", "name"):
        value = position.get(key)
        if isinstance(value, str) and value:
            return value

    contract = position.get("contract")
    if isinstance(contract, Mapping):
        for key in ("name", "symbol"):
            value = contract.get(key)
            if isinstance(value, str) and value:
                return value

    return ""


def _position_quantity(position: Mapping[str, object]) -> int:
    for key in ("netPos", "netQuantity", "position", "quantity"):
        value = position.get(key)
        if value is not None:
            return int(value)
    return 0


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _as_sequence_of_mappings(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, Mapping):
        maybe_items = value.get("items")
        if isinstance(maybe_items, Sequence) and not isinstance(maybe_items, (str, bytes, bytearray)):
            value = maybe_items
        else:
            value = (value,)

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence of JSON objects")

    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} must contain only JSON objects")
        items.append(item)
    return tuple(items)


def _require_positive_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if value <= Decimal("0"):
        raise ValueError(f"{name} must be greater than 0")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _decimal_to_wire(value: Decimal, field_name: str) -> str:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be decimal-compatible") from error
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return str(normalized)
