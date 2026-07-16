"""Tradovate user/order WebSocket client (demo) - verified framing, mockable transport.

Implements the officially documented protocol (see
``docs/tradovate_api_verification.md``): ``'o'``/``'h'``/``'a'``/``'c'`` frames,
``authorize\\n{id}\\n\\n{token}``, requests as ``{url}\\n{id}\\n{query}\\n{body}``,
responses as ``{s, i, d}``, client heartbeat ``[]`` when >= 2.5 s elapsed since
the last message, and ``user/syncrequest`` subscriptions whose updates arrive as
``{entity, entityType, eventType}``.

The transport is injected (:class:`WsTransport` protocol) so every test runs
against scripted frames - no live network call can occur in tests. The client
tracks order state transitions with partial-fill accumulation and weighted
average prices, ignores duplicate events, buffers out-of-order events, renews
tokens, reconnects with bounded backoff, and reconciles from a REST snapshot
after every reconnect (sleep/resume recovery).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Protocol

HEARTBEAT_INTERVAL_SECONDS = 2.5
MAX_RECONNECT_ATTEMPTS = 8


class WsTransport(Protocol):
    """Injected transport: real WebSocket in production, scripted in tests."""

    def connect(self) -> None:
        """Open the socket."""

    def send(self, payload: str) -> None:
        """Send one text frame."""

    def recv(self, timeout_seconds: float) -> str | None:
        """Receive one text frame, or None on timeout."""

    def close(self) -> None:
        """Close the socket."""


@dataclass(slots=True)
class OrderState:
    """Local order state built from broker events (Decimal money math)."""

    order_id: str
    status: str = "PendingNew"
    ordered_quantity: int = 0
    filled_quantity: int = 0
    fill_value: Decimal = Decimal("0")  # sum(price * qty) for weighted average
    last_event_id: int = -1

    @property
    def average_fill_price(self) -> Decimal:
        """Weighted average fill price across all partial fills."""
        if self.filled_quantity == 0:
            return Decimal("0")
        return (self.fill_value / Decimal(self.filled_quantity)).quantize(Decimal("0.01"))

    @property
    def remaining_quantity(self) -> int:
        """Quantity still working at the broker."""
        return max(0, self.ordered_quantity - self.filled_quantity)


@dataclass(slots=True)
class SyncStats:
    """Counters proving how the stream behaved (for the GUI and tests)."""

    frames: int = 0
    heartbeats_received: int = 0
    heartbeats_sent: int = 0
    duplicates_ignored: int = 0
    out_of_order_buffered: int = 0
    reconnects: int = 0
    snapshots_reconciled: int = 0


class TradovateUserSyncClient:
    """Stateful user/order sync over the verified WebSocket framing."""

    def __init__(
        self,
        transport: WsTransport,
        *,
        token_provider: Callable[[], str],
        token_renewer: Callable[[], str] | None = None,
        clock: Callable[[], float],
        snapshot_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        """Create a disconnected client; ``clock`` is injected for determinism."""
        self._transport = transport
        self._token_provider = token_provider
        self._token_renewer = token_renewer
        self._clock = clock
        self._snapshot_provider = snapshot_provider
        self._request_id = 0
        self._last_message_at = 0.0
        self._authorized = False
        self.orders: dict[str, OrderState] = {}
        self.positions: dict[str, int] = {}
        self.cash_balance: Decimal | None = None
        self.stats = SyncStats()
        self._pending_out_of_order: dict[str, list[Mapping[str, object]]] = {}
        self._seen_event_ids: set[tuple[str, int]] = set()
        self._closed = False

    # -- connection -----------------------------------------------------------

    def connect_and_authorize(self) -> bool:
        """Open the socket, wait for 'o', authorize, and subscribe to user data."""
        self._transport.connect()
        opened = self._await_frame_type("o", timeout_seconds=10.0)
        if not opened:
            return False
        auth_id = self._next_id()
        self._transport.send(f"authorize\n{auth_id}\n\n{self._token_provider()}")
        response = self._await_response(auth_id, timeout_seconds=10.0)
        if response is None or int(response.get("s", 0)) != 200:
            # Token may be expired: renew once, then retry authorization.
            if self._token_renewer is not None:
                token = self._token_renewer()
                retry_id = self._next_id()
                self._transport.send(f"authorize\n{retry_id}\n\n{token}")
                response = self._await_response(retry_id, timeout_seconds=10.0)
        self._authorized = response is not None and int(response.get("s", 0)) == 200
        if self._authorized:
            sync_id = self._next_id()
            self._transport.send(f"user/syncrequest\n{sync_id}\n\n{json.dumps({})}")
        return self._authorized

    def reconnect_with_backoff(self, *, sleep: Callable[[float], None]) -> bool:
        """Reconnect with bounded exponential backoff, then REST-reconcile."""
        delay = 0.25
        for _ in range(MAX_RECONNECT_ATTEMPTS):
            try:
                if self.connect_and_authorize():
                    self.stats.reconnects += 1
                    self.reconcile_from_snapshot()
                    return True
            except Exception:  # noqa: BLE001 - transport errors retry
                pass
            sleep(delay)
            delay = min(delay * 2, 30.0)
        return False

    def reconcile_from_snapshot(self) -> None:
        """Overwrite local order/position state from a REST snapshot (post-reconnect).

        This is the sleep/resume and disconnect-mid-order recovery path: local
        state may have missed events, so the broker snapshot wins.
        """
        if self._snapshot_provider is None:
            return
        snapshot = self._snapshot_provider()
        for order in snapshot.get("orders", []):  # type: ignore[union-attr]
            if not isinstance(order, Mapping):
                continue
            order_id = str(order.get("id", ""))
            state = self.orders.setdefault(order_id, OrderState(order_id=order_id))
            state.status = str(order.get("ordStatus", state.status))
            state.ordered_quantity = int(order.get("orderQty", state.ordered_quantity))
            filled = int(order.get("cumQty", state.filled_quantity))
            if filled > state.filled_quantity and order.get("avgPx") is not None:
                state.fill_value = Decimal(str(order["avgPx"])) * Decimal(filled)
                state.filled_quantity = filled
        for position in snapshot.get("positions", []):  # type: ignore[union-attr]
            if isinstance(position, Mapping):
                self.positions[str(position.get("contractId", ""))] = int(position.get("netPos", 0))
        if snapshot.get("cash") is not None:
            self.cash_balance = Decimal(str(snapshot["cash"]))
        self.stats.snapshots_reconciled += 1

    def close(self) -> None:
        """Clean shutdown: close the transport once."""
        if not self._closed:
            self._closed = True
            self._transport.close()

    # -- pumping ----------------------------------------------------------------

    def pump_once(self, *, timeout_seconds: float = 0.5) -> bool:
        """Receive and process one frame; send heartbeat if 2.5 s elapsed.

        Returns False when the server closed the stream.
        """
        now = self._clock()
        if self._last_message_at and now - self._last_message_at >= HEARTBEAT_INTERVAL_SECONDS:
            self._transport.send("[]")  # verified client heartbeat
            self.stats.heartbeats_sent += 1
            self._last_message_at = now
        frame = self._transport.recv(timeout_seconds)
        if frame is None:
            return True
        self._last_message_at = self._clock()
        self.stats.frames += 1
        if frame == "h":
            self.stats.heartbeats_received += 1
            return True
        if frame == "c" or frame.startswith("c["):
            return False
        if frame.startswith("a"):
            for item in json.loads(frame[1:]):
                if isinstance(item, Mapping):
                    self._handle_message(item)
        return True

    # -- event handling -----------------------------------------------------------

    def _handle_message(self, item: Mapping[str, object]) -> None:
        if "e" in item and item.get("e") == "props":
            data = item.get("d")
            if isinstance(data, Mapping):
                self._handle_entity_event(data)
        elif "entityType" in item:
            self._handle_entity_event(item)

    def _handle_entity_event(self, event: Mapping[str, object]) -> None:
        entity_type = str(event.get("entityType", ""))
        entity = event.get("entity")
        if not isinstance(entity, Mapping):
            return
        if entity_type == "order":
            self._apply_order_event(entity)
        elif entity_type == "fill":
            self._apply_fill_event(entity)
        elif entity_type == "position":
            self.positions[str(entity.get("contractId", ""))] = int(entity.get("netPos", 0))
        elif entity_type == "cashBalance":
            amount = entity.get("amount")
            if amount is not None:
                self.cash_balance = Decimal(str(amount))

    def _apply_order_event(self, entity: Mapping[str, object]) -> None:
        order_id = str(entity.get("id", ""))
        event_id = int(entity.get("eventId", entity.get("timestamp", 0)) or 0)
        if (order_id, event_id) in self._seen_event_ids and event_id:
            self.stats.duplicates_ignored += 1
            return
        state = self.orders.setdefault(order_id, OrderState(order_id=order_id))
        if event_id and event_id < state.last_event_id:
            # Out-of-order: an older status must not overwrite a newer one.
            self.stats.out_of_order_buffered += 1
            self._pending_out_of_order.setdefault(order_id, []).append(entity)
            return
        if event_id:
            self._seen_event_ids.add((order_id, event_id))
            state.last_event_id = event_id
        state.status = str(entity.get("ordStatus", state.status))
        if entity.get("orderQty") is not None:
            state.ordered_quantity = int(entity["orderQty"])

    def _apply_fill_event(self, entity: Mapping[str, object]) -> None:
        order_id = str(entity.get("orderId", ""))
        fill_id = int(entity.get("id", 0) or 0)
        if (f"fill:{order_id}", fill_id) in self._seen_event_ids and fill_id:
            self.stats.duplicates_ignored += 1
            return
        if fill_id:
            self._seen_event_ids.add((f"fill:{order_id}", fill_id))
        state = self.orders.setdefault(order_id, OrderState(order_id=order_id))
        quantity = int(entity.get("qty", 0))
        price = Decimal(str(entity.get("price", "0")))
        state.filled_quantity += quantity
        state.fill_value += price * Decimal(quantity)
        if state.ordered_quantity and state.filled_quantity >= state.ordered_quantity:
            state.status = "Filled"
        elif state.filled_quantity > 0 and state.status not in {"Canceled", "Rejected"}:
            state.status = "PartiallyFilled"

    # -- internals --------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _await_frame_type(self, frame_type: str, *, timeout_seconds: float) -> bool:
        deadline = self._clock() + timeout_seconds
        while self._clock() < deadline:
            frame = self._transport.recv(0.25)
            if frame is None:
                continue
            self._last_message_at = self._clock()
            if frame.startswith(frame_type):
                return True
        return False

    def _await_response(self, request_id: int, *, timeout_seconds: float) -> Mapping[str, object] | None:
        deadline = self._clock() + timeout_seconds
        while self._clock() < deadline:
            frame = self._transport.recv(0.25)
            if frame is None:
                continue
            self._last_message_at = self._clock()
            if frame.startswith("a"):
                for item in json.loads(frame[1:]):
                    if isinstance(item, Mapping) and int(item.get("i", -1)) == request_id:
                        return item
                    if isinstance(item, Mapping):
                        self._handle_message(item)
        return None
