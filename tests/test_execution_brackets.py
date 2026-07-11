"""Tests for demo-only Tradovate bracket-order orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.execution.brackets import (
    BracketOrderRequest,
    build_bracket_order_payload,
    place_bracket_order,
)
from app.execution.orders import (
    AccountRef,
    BrokerAcknowledgement,
    ExecutionRejectedError,
    TRADOVATE_DEMO_REST_BASE_URL,
)


@dataclass(frozen=True, slots=True)
class Approval:
    """Risk approval test double."""

    allowed: bool = True
    reason: tuple[str, ...] = ("approved",)


class MockHttpClient:
    """HTTP client test double that records calls and returns queued responses."""

    def __init__(
        self,
        *,
        get_responses: list[object] | None = None,
        post_responses: list[object] | None = None,
    ) -> None:
        """Create a mock HTTP client."""
        self.get_responses = get_responses or []
        self.post_responses = post_responses or []
        self.get_calls: list[tuple[str, object, object]] = []
        self.post_calls: list[tuple[str, object, object]] = []

    def get(self, url: str, *, headers: object, params: object = None) -> object:
        """Record and return a queued GET response."""
        self.get_calls.append((url, headers, params))
        return self.get_responses.pop(0)

    def post(self, url: str, *, headers: object, json: object) -> object:
        """Record and return a queued POST response."""
        self.post_calls.append((url, headers, json))
        return self.post_responses.pop(0)


class MockAckClient:
    """WebSocket acknowledgement client test double."""

    def __init__(self, acknowledgements: list[BrokerAcknowledgement]) -> None:
        """Create a mock ack client."""
        self.acknowledgements = acknowledgements
        self.calls: list[tuple[str, str, Decimal]] = []

    def wait_for_acknowledgement(
        self,
        *,
        correlation_id: str,
        action: str,
        timeout_seconds: Decimal,
    ) -> BrokerAcknowledgement:
        """Record and return a queued acknowledgement."""
        self.calls.append((correlation_id, action, timeout_seconds))
        return self.acknowledgements.pop(0)


def test_build_bracket_order_payload_uses_decimal_strings_and_exit_side() -> None:
    """Bracket payload includes entry, stop, and target using Decimal-safe strings."""
    payload = build_bracket_order_payload(Approval(), request=_request())

    assert payload["accountSpec"] == "DEMO-ACCOUNT"
    assert payload["accountId"] == 11
    assert payload["action"] == "Buy"
    assert payload["symbol"] == "MNQU6"
    assert payload["orderQty"] == 2
    assert payload["orderType"] == "Limit"
    assert payload["price"] == "100.25"
    assert payload["isAutomated"] is False
    assert payload["bracket1"] == {
        "action": "Sell",
        "orderType": "Stop",
        "stopPrice": "99.25",
        "orderQty": 2,
    }
    assert payload["bracket2"] == {
        "action": "Sell",
        "orderType": "Limit",
        "price": "102.25",
        "orderQty": 2,
    }


def test_place_bracket_order_checks_position_submits_and_logs_ack() -> None:
    """Placing a bracket checks flat state, posts to demo endpoint, waits for ack, and logs it."""
    http = MockHttpClient(
        get_responses=[[]],
        post_responses=[
            {
                "orderId": "entry-1",
                "bracket1": {"orderId": "stop-1"},
                "bracket2": {"orderId": "target-1"},
            },
        ],
    )
    ack = BrokerAcknowledgement(
        action="place_order",
        correlation_id="entry-1",
        accepted=True,
        broker_timestamp_ns=999,
        order_id="entry-1",
    )
    ws = MockAckClient([ack])
    logged: list[BrokerAcknowledgement] = []

    result = place_bracket_order(
        Approval(),
        request=_request(),
        http_client=http,
        ws_client=ws,
        access_token="token",
        ack_logger=logged.append,
    )

    assert result.entry_order_id == "entry-1"
    assert result.stop_order_id == "stop-1"
    assert result.target_order_id == "target-1"
    assert result.acknowledgement == ack
    assert logged == [ack]
    assert http.get_calls[0][0] == f"{TRADOVATE_DEMO_REST_BASE_URL}/position/list"
    assert http.post_calls[0][0] == f"{TRADOVATE_DEMO_REST_BASE_URL}/order/placeOrder"
    assert ws.calls == [("entry-1", "place_order", Decimal("5"))]


def test_place_bracket_order_rejects_existing_position_before_posting() -> None:
    """Bracket placement refuses to submit when the account already has a position."""
    http = MockHttpClient(
        get_responses=[
            [
                {
                    "accountId": 11,
                    "symbol": "MNQU6",
                    "netPos": 1,
                    "contractId": 9001,
                },
            ],
        ],
    )
    ws = MockAckClient([])

    with pytest.raises(ExecutionRejectedError, match="Existing MNQU6 position"):
        place_bracket_order(
            Approval(),
            request=_request(),
            http_client=http,
            ws_client=ws,
            access_token="token",
        )

    assert http.post_calls == []
    assert ws.calls == []


def test_place_bracket_order_rejects_wrong_contract_before_http_calls() -> None:
    """Contract-month mismatch is checked before position or order endpoints are called."""
    http = MockHttpClient()
    ws = MockAckClient([])
    request = _request(symbol="MNQZ6")

    with pytest.raises(ExecutionRejectedError, match="Contract month mismatch"):
        place_bracket_order(
            Approval(),
            request=request,
            http_client=http,
            ws_client=ws,
            access_token="token",
        )

    assert http.get_calls == []
    assert http.post_calls == []


def test_place_bracket_order_rejects_denied_risk_approval() -> None:
    """Denied risk approval makes bracket submission impossible before HTTP calls."""
    http = MockHttpClient()
    ws = MockAckClient([])

    with pytest.raises(ExecutionRejectedError, match="approved"):
        place_bracket_order(
            Approval(allowed=False, reason=("not approved",)),
            request=_request(),
            http_client=http,
            ws_client=ws,
            access_token="token",
        )

    assert http.get_calls == []
    assert http.post_calls == []


def test_market_entry_does_not_require_entry_price() -> None:
    """Market bracket entries omit the entry price while retaining stop and target children."""
    request = _request(entry_order_type="Market", entry_price=None)

    payload = build_bracket_order_payload(Approval(), request=request)

    assert "price" not in payload
    assert payload["orderType"] == "Market"


def _request(
    *,
    symbol: str = "MNQU6",
    entry_order_type: str = "Limit",
    entry_price: Decimal | None = Decimal("100.25"),
) -> BracketOrderRequest:
    return BracketOrderRequest(
        account=AccountRef(account_id=11, account_spec="DEMO-ACCOUNT"),
        symbol=symbol,
        expected_contract_symbol="MNQU6",
        action="Buy",
        quantity=2,
        entry_order_type=entry_order_type,  # type: ignore[arg-type]
        entry_price=entry_price,
        stop_price=Decimal("99.25"),
        target_price=Decimal("102.25"),
    )
