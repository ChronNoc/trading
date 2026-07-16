"""Tests for the Tradovate user/order WebSocket client (scripted transport)."""

from __future__ import annotations

import json
from decimal import Decimal

from app.execution.user_sync import TradovateUserSyncClient


class ScriptedTransport:
    """Deterministic transport: frames are consumed in order; sends recorded."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.connected = False
        self.closed = 0

    def connect(self) -> None:
        self.connected = True

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self, timeout_seconds: float) -> str | None:
        return self.frames.pop(0) if self.frames else None

    def close(self) -> None:
        self.closed += 1


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(frames: list[str], **kwargs) -> tuple[TradovateUserSyncClient, ScriptedTransport, Clock]:
    transport = ScriptedTransport(frames)
    clock = Clock()
    client = TradovateUserSyncClient(transport, token_provider=lambda: "tok-1",
                                     clock=clock, **kwargs)
    return client, transport, clock


def _ok(request_id: int) -> str:
    return "a" + json.dumps([{"s": 200, "i": request_id}])


def test_connect_authorizes_with_verified_framing_and_subscribes() -> None:
    client, transport, _ = _client(["o", _ok(1)])
    assert client.connect_and_authorize() is True
    assert transport.sent[0] == "authorize\n1\n\ntok-1"  # verified official format
    assert transport.sent[1].startswith("user/syncrequest\n2\n\n")


def test_expired_token_is_renewed_and_authorization_retried() -> None:
    renewed = {"count": 0}

    def renew() -> str:
        renewed["count"] += 1
        return "tok-2"

    client, transport, _ = _client(
        ["o", "a" + json.dumps([{"s": 401, "i": 1}]), _ok(2)],
        token_renewer=renew,
    )
    assert client.connect_and_authorize() is True
    assert renewed["count"] == 1
    assert transport.sent[1] == "authorize\n2\n\ntok-2"


def test_client_heartbeat_sent_after_2500ms_and_server_h_counted() -> None:
    client, transport, clock = _client(["o", _ok(1), "h", "h"])
    client.connect_and_authorize()
    client.pump_once()  # first 'h'
    clock.now += 3.0  # > 2.5 s elapsed -> heartbeat due
    client.pump_once()  # second 'h'
    assert "[]" in transport.sent  # verified heartbeat payload
    assert client.stats.heartbeats_received == 2


def test_partial_fills_accumulate_with_weighted_average() -> None:
    frames = [
        "o", _ok(1),
        "a" + json.dumps([{"e": "props", "d": {"entityType": "order",
                                               "entity": {"id": 7, "ordStatus": "Working",
                                                          "orderQty": 3, "eventId": 1}}}]),
        "a" + json.dumps([{"e": "props", "d": {"entityType": "fill",
                                               "entity": {"id": 100, "orderId": 7, "qty": 1, "price": "29450.25"}}}]),
        "a" + json.dumps([{"e": "props", "d": {"entityType": "fill",
                                               "entity": {"id": 101, "orderId": 7, "qty": 2, "price": "29450.75"}}}]),
    ]
    client, _, _ = _client(frames)
    client.connect_and_authorize()
    for _ in range(3):
        client.pump_once()
    order = client.orders["7"]
    assert order.filled_quantity == 3
    assert order.status == "Filled"
    # (29450.25*1 + 29450.75*2) / 3 = 29450.58 (Decimal, quantized)
    assert order.average_fill_price == Decimal("29450.58")


def test_duplicate_and_out_of_order_events_are_handled() -> None:
    fill = {"e": "props", "d": {"entityType": "fill",
                                "entity": {"id": 100, "orderId": 7, "qty": 1, "price": "29450.25"}}}
    newer = {"e": "props", "d": {"entityType": "order",
                                 "entity": {"id": 7, "ordStatus": "Filled", "eventId": 5}}}
    older = {"e": "props", "d": {"entityType": "order",
                                 "entity": {"id": 7, "ordStatus": "Working", "eventId": 2}}}
    frames = ["o", _ok(1),
              "a" + json.dumps([fill]), "a" + json.dumps([fill]),  # duplicate fill id
              "a" + json.dumps([newer]), "a" + json.dumps([older])]  # out-of-order status
    client, _, _ = _client(frames)
    client.connect_and_authorize()
    for _ in range(4):
        client.pump_once()
    order = client.orders["7"]
    assert order.filled_quantity == 1  # duplicate ignored
    assert client.stats.duplicates_ignored == 1
    assert order.status == "Filled"  # older Working did not overwrite newer Filled
    assert client.stats.out_of_order_buffered == 1


def test_rejected_order_and_position_and_cash_events() -> None:
    frames = ["o", _ok(1),
              "a" + json.dumps([{"e": "props", "d": {"entityType": "order",
                                                     "entity": {"id": 9, "ordStatus": "Rejected", "eventId": 1}}},
                                {"e": "props", "d": {"entityType": "position",
                                                     "entity": {"contractId": 55, "netPos": 2}}},
                                {"e": "props", "d": {"entityType": "cashBalance",
                                                     "entity": {"amount": "49985.50"}}}])]
    client, _, _ = _client(frames)
    client.connect_and_authorize()
    client.pump_once()
    assert client.orders["9"].status == "Rejected"
    assert client.positions["55"] == 2
    assert client.cash_balance == Decimal("49985.50")


def test_reconnect_backoff_and_snapshot_reconciliation() -> None:
    snapshot = {"orders": [{"id": 7, "ordStatus": "Filled", "orderQty": 2, "cumQty": 2, "avgPx": "29451.00"}],
                "positions": [{"contractId": 55, "netPos": 2}], "cash": "49900"}
    clock = Clock()
    attempts = {"n": 0}

    class FlakyTransport(ScriptedTransport):
        """Dead socket on the first attempt; healthy frames on the second."""

        def connect(self) -> None:
            attempts["n"] += 1
            if attempts["n"] >= 2:
                # Attempt 2 is the first to send authorize, so its id is 1.
                self.frames = ["o", "a" + json.dumps([{"s": 200, "i": 1}])]

        def recv(self, timeout_seconds: float) -> str | None:
            clock.now += 0.5  # deterministic time passage so awaits expire
            return super().recv(timeout_seconds)

    flaky = FlakyTransport([])
    client = TradovateUserSyncClient(flaky, token_provider=lambda: "tok",
                                     clock=clock, snapshot_provider=lambda: snapshot)
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    ok = client.reconnect_with_backoff(sleep=fake_sleep)
    assert ok is True
    assert slept and slept[0] == 0.25 and all(b <= 30.0 for b in slept)  # bounded backoff
    # Disconnect-mid-order / sleep-resume recovery: the snapshot won.
    assert client.orders["7"].status == "Filled"
    assert client.orders["7"].average_fill_price == Decimal("29451.00")
    assert client.positions["55"] == 2
    assert client.stats.snapshots_reconciled == 1


def test_clean_shutdown_closes_once_and_close_frame_stops_pump() -> None:
    client, transport, _ = _client(["o", _ok(1), "c[1000,\"bye\"]"])
    client.connect_and_authorize()
    assert client.pump_once() is False  # server closed
    client.close()
    client.close()  # idempotent
    assert transport.closed == 1
