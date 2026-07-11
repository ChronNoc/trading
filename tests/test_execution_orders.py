"""Tests for demo-only Tradovate order primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.execution.orders import (
    AccountRef,
    BrokerAcknowledgement,
    ExecutionRejectedError,
    TRADOVATE_DEMO_REST_BASE_URL,
    cancel_order,
    check_correct_contract_month,
    fetch_existing_position,
    flatten_position,
)


@dataclass(frozen=True, slots=True)
class Approval:
    """Risk approval test double."""

    allowed: bool = True
    reason: str = "approved"


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
        self.get_calls: list[tuple[str, Mapping[str, str], Mapping[str, object] | None]] = []
        self.post_calls: list[tuple[str, Mapping[str, str], Mapping[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object] | None = None,
    ) -> object:
        """Record and return a queued GET response."""
        self.get_calls.append((url, headers, params))
        return self.get_responses.pop(0)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> object:
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
        """Record the wait request and return a queued acknowledgement."""
        self.calls.append((correlation_id, action, timeout_seconds))
        return self.acknowledgements.pop(0)


def test_contract_month_check_rejects_wrong_month() -> None:
    """Execution refuses a symbol that does not match the approved contract month."""
    with pytest.raises(ExecutionRejectedError, match="Contract month mismatch"):
        check_correct_contract_month(
            Approval(),
            symbol="MNQZ6",
            expected_contract_symbol="MNQU6",
        )


def test_fetch_existing_position_parses_matching_position() -> None:
    """Existing position lookup returns the matching account and contract position."""
    http = MockHttpClient(
        get_responses=[
            {
                "items": [
                    {"accountId": 11, "contract": {"name": "MNQU6"}, "netPos": 2, "contractId": 9001},
                ],
            },
        ],
    )

    position = fetch_existing_position(
        Approval(),
        account=_account(),
        symbol="MNQU6",
        http_client=http,
        access_token="token",
    )

    assert position.net_quantity == 2
    assert position.contract_id == 9001
    assert http.get_calls[0][0] == f"{TRADOVATE_DEMO_REST_BASE_URL}/position/list"
    assert http.get_calls[0][2] == {"accountId": 11}


def test_cancel_order_posts_demo_endpoint_and_logs_ack() -> None:
    """Cancel order uses the demo REST endpoint and logs broker acknowledgement."""
    http = MockHttpClient(post_responses=[{"orderId": "abc"}])
    ack = BrokerAcknowledgement(
        action="cancel_order",
        correlation_id="abc",
        accepted=True,
        broker_timestamp_ns=123,
        order_id="abc",
    )
    ws = MockAckClient([ack])
    logged: list[BrokerAcknowledgement] = []

    result = cancel_order(
        Approval(),
        account=_account(),
        order_id="abc",
        http_client=http,
        ws_client=ws,
        access_token="token",
        ack_logger=logged.append,
    )

    assert result.acknowledgement == ack
    assert logged == [ack]
    assert http.post_calls[0][0] == f"{TRADOVATE_DEMO_REST_BASE_URL}/order/cancelOrder"
    assert http.post_calls[0][2] == {"accountId": 11, "orderId": "abc"}
    assert ws.calls == [("abc", "cancel_order", Decimal("5"))]


def test_flatten_position_liquidates_existing_position() -> None:
    """Flatten posts liquidatePosition when the matching demo position is nonzero."""
    http = MockHttpClient(
        get_responses=[
            [
                {
                    "accountId": 11,
                    "symbol": "MNQU6",
                    "netPos": -1,
                    "contractId": 9001,
                },
            ],
        ],
        post_responses=[{"orderId": "flat-1"}],
    )
    ack = BrokerAcknowledgement(
        action="flatten_position",
        correlation_id="flat-1",
        accepted=True,
        broker_timestamp_ns=456,
        order_id="flat-1",
    )
    ws = MockAckClient([ack])

    result = flatten_position(
        Approval(),
        account=_account(),
        symbol="MNQU6",
        expected_contract_symbol="MNQU6",
        http_client=http,
        ws_client=ws,
        access_token="token",
    )

    assert result.flattened is True
    assert http.post_calls[0][0] == f"{TRADOVATE_DEMO_REST_BASE_URL}/order/liquidatePosition"
    assert http.post_calls[0][2] == {"accountId": 11, "contractId": 9001, "admin": False}
    assert ws.calls == [("flat-1", "flatten_position", Decimal("5"))]


def test_flatten_position_skips_when_already_flat() -> None:
    """Flatten returns without POSTing when the account is already flat."""
    http = MockHttpClient(get_responses=[[]])
    ws = MockAckClient([])

    result = flatten_position(
        Approval(),
        account=_account(),
        symbol="MNQU6",
        expected_contract_symbol="MNQU6",
        http_client=http,
        ws_client=ws,
        access_token="token",
    )

    assert result.flattened is False
    assert http.post_calls == []
    assert ws.calls == []


def test_order_functions_reject_missing_or_denied_risk_approval() -> None:
    """Public order functions require an allowed risk approval object."""
    with pytest.raises(TypeError, match="risk_approval"):
        check_correct_contract_month(object(), symbol="MNQU6", expected_contract_symbol="MNQU6")  # type: ignore[arg-type]

    with pytest.raises(ExecutionRejectedError, match="daily lock"):
        check_correct_contract_month(
            Approval(allowed=False, reason="daily lock"),
            symbol="MNQU6",
            expected_contract_symbol="MNQU6",
        )


def test_rejected_broker_ack_raises_after_cancel_response() -> None:
    """A broker-rejected acknowledgement is surfaced as an execution rejection."""
    http = MockHttpClient(post_responses=[{"orderId": "abc"}])
    ws = MockAckClient(
        [
            BrokerAcknowledgement(
                action="cancel_order",
                correlation_id="abc",
                accepted=False,
                broker_timestamp_ns=1,
                order_id="abc",
                message="not found",
            ),
        ],
    )

    with pytest.raises(ExecutionRejectedError, match="Broker rejected"):
        cancel_order(
            Approval(),
            account=_account(),
            order_id="abc",
            http_client=http,
            ws_client=ws,
            access_token="token",
        )


def _account() -> AccountRef:
    return AccountRef(account_id=11, account_spec="DEMO-ACCOUNT")
