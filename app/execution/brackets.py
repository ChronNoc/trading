"""Demo-only Tradovate bracket-order orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from app.execution.orders import (
    AckLogger,
    AccountRef,
    BrokerAcknowledgement,
    ExecutionRejectedError,
    OrderAction,
    OrderType,
    RiskApproval,
    TradovateAckClient,
    TradovateHttpClient,
    check_correct_contract_month,
    ensure_flat_before_entry,
    fetch_existing_position,
    submit_order,
)
from app.execution.orders import _decimal_to_wire


@dataclass(frozen=True, slots=True)
class BracketOrderRequest:
    """Request to place an entry order with attached stop and target exits."""

    account: AccountRef
    symbol: str
    expected_contract_symbol: str
    action: OrderAction
    quantity: int
    entry_order_type: OrderType
    entry_price: Decimal | None
    stop_price: Decimal
    target_price: Decimal
    is_automated: bool = False


@dataclass(frozen=True, slots=True)
class BracketOrderResult:
    """Result of a submitted bracket order and its broker acknowledgement."""

    entry_order_id: str
    stop_order_id: str | None
    target_order_id: str | None
    response: Mapping[str, object]
    acknowledgement: BrokerAcknowledgement


def place_bracket_order(
    risk_approval: RiskApproval,
    *,
    request: BracketOrderRequest,
    http_client: TradovateHttpClient,
    ws_client: TradovateAckClient,
    access_token: str,
    ack_logger: AckLogger | None = None,
    timeout_seconds: Decimal = Decimal("5"),
) -> BracketOrderResult:
    """Place a demo bracket order only after risk, position, and contract checks pass."""
    check_correct_contract_month(
        risk_approval,
        symbol=request.symbol,
        expected_contract_symbol=request.expected_contract_symbol,
    )
    position = fetch_existing_position(
        risk_approval,
        account=request.account,
        symbol=request.symbol,
        http_client=http_client,
        access_token=access_token,
    )
    ensure_flat_before_entry(risk_approval, position=position)

    payload = build_bracket_order_payload(risk_approval, request=request)
    response, acknowledgement = submit_order(
        risk_approval,
        payload=payload,
        http_client=http_client,
        ws_client=ws_client,
        access_token=access_token,
        ack_logger=ack_logger,
        timeout_seconds=timeout_seconds,
    )
    return BracketOrderResult(
        entry_order_id=_required_response_id(response),
        stop_order_id=_optional_nested_order_id(response, "bracket1"),
        target_order_id=_optional_nested_order_id(response, "bracket2"),
        response=response,
        acknowledgement=acknowledgement,
    )


def build_bracket_order_payload(
    risk_approval: RiskApproval,
    *,
    request: BracketOrderRequest,
) -> dict[str, object]:
    """Build the Tradovate demo bracket payload for entry, stop, and target orders."""
    check_correct_contract_month(
        risk_approval,
        symbol=request.symbol,
        expected_contract_symbol=request.expected_contract_symbol,
    )
    _validate_request(request)
    exit_action = _opposite_action(request.action)

    payload: dict[str, object] = {
        "accountSpec": request.account.account_spec,
        "accountId": request.account.account_id,
        "action": request.action,
        "symbol": request.symbol,
        "orderQty": request.quantity,
        "orderType": request.entry_order_type,
        "isAutomated": request.is_automated,
        "bracket1": {
            "action": exit_action,
            "orderType": "Stop",
            "stopPrice": _decimal_to_wire(request.stop_price, "stop_price"),
            "orderQty": request.quantity,
        },
        "bracket2": {
            "action": exit_action,
            "orderType": "Limit",
            "price": _decimal_to_wire(request.target_price, "target_price"),
            "orderQty": request.quantity,
        },
    }
    if request.entry_order_type == "Limit":
        if request.entry_price is None:
            raise ValueError("entry_price is required for Limit bracket entries")
        payload["price"] = _decimal_to_wire(request.entry_price, "entry_price")
    return payload


def _opposite_action(action: OrderAction) -> OrderAction:
    if action == "Buy":
        return "Sell"
    if action == "Sell":
        return "Buy"
    raise ValueError("action must be Buy or Sell")


def _validate_request(request: BracketOrderRequest) -> None:
    if request.quantity <= 0:
        raise ValueError("quantity must be greater than 0")
    if request.entry_order_type not in {"Market", "Limit"}:
        raise ValueError("entry_order_type must be Market or Limit")
    if request.action not in {"Buy", "Sell"}:
        raise ValueError("action must be Buy or Sell")
    if request.symbol != request.expected_contract_symbol:
        raise ValueError("request symbol must match expected_contract_symbol")


def _required_response_id(response: Mapping[str, object]) -> str:
    for key in ("orderId", "id"):
        value = response.get(key)
        if value is not None:
            return str(value)
    raise ValueError("broker response did not include an entry order id")


def _optional_nested_order_id(response: Mapping[str, object], key: str) -> str | None:
    nested = response.get(key)
    if isinstance(nested, Mapping):
        for id_key in ("orderId", "id"):
            value = nested.get(id_key)
            if value is not None:
                return str(value)
    return None
