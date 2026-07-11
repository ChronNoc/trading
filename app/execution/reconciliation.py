"""Execution-state reconciliation against demo Tradovate-reported state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

from app.execution.orders import (
    AckLogger,
    AccountRef,
    FlattenPositionResult,
    RiskApproval,
    TRADOVATE_DEMO_REST_BASE_URL,
    TradovateAckClient,
    TradovateHttpClient,
    flatten_position,
)
from app.risk.kill_switch import KillSwitchDecision, KillSwitchState, evaluate_kill_switch

DiscrepancyKind: TypeAlias = Literal[
    "position_mismatch",
    "order_mismatch",
    "connection_lost_mid_order",
    "sleep_resume",
]
OrderStatus: TypeAlias = Literal[
    "pending_ack",
    "working",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
]

ACTIVE_ORDER_STATUSES = frozenset({"pending_ack", "working", "partially_filled"})
DEFAULT_DEBOUNCE_WINDOW_NS = 1_000_000_000
DEFAULT_SLEEP_RESUME_THRESHOLD_NS = 60_000_000_000


class LocalStateProvider(Protocol):
    """Protocol for reading the app's local execution snapshot."""

    def get_state(self) -> "LocalExecutionState":
        """Return the current local execution state."""


class BrokerStateProvider(Protocol):
    """Protocol for reading broker-reported execution state."""

    def get_state(self) -> "BrokerExecutionState":
        """Return the current broker-reported execution state."""


class ReconciliationLogger(Protocol):
    """Protocol for append-only reconciliation discrepancy logging."""

    def log(self, entry: "ReconciliationLogEntry") -> None:
        """Persist one reconciliation log entry."""


class FlattenPath(Protocol):
    """Protocol for the kill-switch flatten path."""

    def __call__(
        self,
        risk_approval: RiskApproval,
        *,
        account: AccountRef,
        symbol: str,
        expected_contract_symbol: str,
    ) -> FlattenPositionResult:
        """Flatten the current position through the configured execution path."""


@dataclass(frozen=True, slots=True)
class ReconciledPosition:
    """Position state used by local and broker reconciliation snapshots."""

    symbol: str
    net_quantity: int
    contract_id: int | None = None


@dataclass(frozen=True, slots=True)
class ReconciledOrder:
    """Order state used by local and broker reconciliation snapshots."""

    order_id: str
    symbol: str
    status: OrderStatus
    remaining_quantity: int


@dataclass(frozen=True, slots=True)
class LocalExecutionState:
    """Local execution state tracked by the app."""

    symbol: str
    expected_contract_symbol: str
    position: ReconciledPosition
    orders: tuple[ReconciledOrder, ...]
    connection_healthy: bool
    account_synchronized: bool = True


@dataclass(frozen=True, slots=True)
class BrokerExecutionState:
    """Tradovate-reported execution state used for reconciliation."""

    position: ReconciledPosition
    orders: tuple[ReconciledOrder, ...]
    connection_healthy: bool


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    """One local-vs-broker discrepancy that requires safety handling."""

    kind: DiscrepancyKind
    reason: str
    local: Mapping[str, object]
    broker: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ReconciliationLogEntry:
    """Append-only reconciliation discrepancy log entry."""

    timestamp_ns: int
    symbol: str
    discrepancy_kind: DiscrepancyKind
    reason: str
    local: Mapping[str, object]
    broker: Mapping[str, object]
    kill_switch_reason: str
    flatten_attempted: bool
    flattened: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return this entry as a JSON-compatible dictionary."""
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        return {
            "timestamp_ns": self.timestamp_ns,
            "symbol": self.symbol,
            "discrepancy_kind": self.discrepancy_kind,
            "reason": self.reason,
            "local": _json_safe_mapping(self.local),
            "broker": _json_safe_mapping(self.broker),
            "kill_switch_reason": self.kill_switch_reason,
            "flatten_attempted": self.flatten_attempted,
            "flattened": self.flattened,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Result of one reconciliation pass."""

    discrepancies: tuple[ReconciliationDiscrepancy, ...]
    kill_switch_decision: KillSwitchDecision | None
    flatten_result: FlattenPositionResult | None


@dataclass(frozen=True, slots=True)
class SignalFingerprint:
    """Stable identity for a detected strategy setup signal."""

    symbol: str
    direction: str
    setup_id: str


@dataclass(frozen=True, slots=True)
class DuplicateSignalDecision:
    """Decision returned by the duplicate-signal debouncer."""

    accepted: bool
    reason: str


@dataclass(slots=True)
class DuplicateSignalDebouncer:
    """Reject duplicate setup signals within a deterministic debounce window."""

    debounce_window_ns: int = DEFAULT_DEBOUNCE_WINDOW_NS
    _last_seen: dict[SignalFingerprint, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Validate the debounce window after initialization."""
        if self.debounce_window_ns <= 0:
            raise ValueError("debounce_window_ns must be greater than zero")

    def check(self, signal: SignalFingerprint, timestamp_ns: int) -> DuplicateSignalDecision:
        """Accept a signal only if it is not a duplicate inside the debounce window."""
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        self._expire_old_entries(timestamp_ns)
        previous_timestamp = self._last_seen.get(signal)
        if previous_timestamp is not None and timestamp_ns - previous_timestamp <= self.debounce_window_ns:
            return DuplicateSignalDecision(
                accepted=False,
                reason=(
                    f"Duplicate signal {signal.setup_id} for {signal.symbol} "
                    f"inside {self.debounce_window_ns}ns debounce window."
                ),
            )
        self._last_seen[signal] = timestamp_ns
        return DuplicateSignalDecision(accepted=True, reason="Signal accepted.")

    def _expire_old_entries(self, timestamp_ns: int) -> None:
        expired = tuple(
            signal
            for signal, last_seen_ns in self._last_seen.items()
            if timestamp_ns - last_seen_ns > self.debounce_window_ns
        )
        for signal in expired:
            del self._last_seen[signal]


@dataclass(frozen=True, slots=True)
class JsonLinesReconciliationLogger:
    """Append reconciliation discrepancy entries to newline-delimited JSON."""

    path: Path

    def log(self, entry: ReconciliationLogEntry) -> None:
        """Append one reconciliation entry to disk."""
        append_reconciliation_log_entry(self.path, entry)


@dataclass(frozen=True, slots=True)
class DemoTradovateFlattenPath:
    """Kill-switch flatten path using the demo-only Tradovate execution helper."""

    http_client: TradovateHttpClient
    ws_client: TradovateAckClient
    access_token: str
    ack_logger: AckLogger | None = None
    timeout_seconds: Decimal = Decimal("5")

    def __call__(
        self,
        risk_approval: RiskApproval,
        *,
        account: AccountRef,
        symbol: str,
        expected_contract_symbol: str,
    ) -> FlattenPositionResult:
        """Flatten via the demo-only Tradovate path."""
        return flatten_position(
            risk_approval,
            account=account,
            symbol=symbol,
            expected_contract_symbol=expected_contract_symbol,
            http_client=self.http_client,
            ws_client=self.ws_client,
            access_token=self.access_token,
            ack_logger=self.ack_logger,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class DemoTradovateStateProvider:
    """Read demo Tradovate position and order state through an injected HTTP client."""

    account: AccountRef
    symbol: str
    http_client: TradovateHttpClient
    access_token: str
    connection_healthy: bool = True

    def get_state(self) -> BrokerExecutionState:
        """Return the current demo Tradovate-reported execution state."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        position_response = self.http_client.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/position/list",
            headers=headers,
            params={"accountId": self.account.account_id},
        )
        order_response = self.http_client.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/order/list",
            headers=headers,
            params={"accountId": self.account.account_id},
        )
        return BrokerExecutionState(
            position=_extract_position(position_response, self.symbol),
            orders=_extract_orders(order_response, self.symbol),
            connection_healthy=self.connection_healthy,
        )


def compare_execution_state(
    *,
    local_state: LocalExecutionState,
    broker_state: BrokerExecutionState,
    last_check_timestamp_ns: int | None = None,
    now_timestamp_ns: int | None = None,
    sleep_resume_threshold_ns: int = DEFAULT_SLEEP_RESUME_THRESHOLD_NS,
) -> tuple[ReconciliationDiscrepancy, ...]:
    """Compare local and broker state and return every safety-relevant discrepancy."""
    if sleep_resume_threshold_ns <= 0:
        raise ValueError("sleep_resume_threshold_ns must be greater than zero")

    discrepancies: list[ReconciliationDiscrepancy] = []
    if local_state.position.net_quantity != broker_state.position.net_quantity:
        discrepancies.append(
            ReconciliationDiscrepancy(
                kind="position_mismatch",
                reason=(
                    f"Local {local_state.symbol} position {local_state.position.net_quantity} "
                    f"does not match broker {broker_state.position.net_quantity}."
                ),
                local=_position_snapshot(local_state.position),
                broker=_position_snapshot(broker_state.position),
            ),
        )

    order_discrepancy = _compare_orders(local_state.orders, broker_state.orders)
    if order_discrepancy is not None:
        discrepancies.append(order_discrepancy)

    if _connection_lost_mid_order(local_state):
        discrepancies.append(
            ReconciliationDiscrepancy(
                kind="connection_lost_mid_order",
                reason="Connection lost while an order was still active.",
                local={
                    "connection_healthy": local_state.connection_healthy,
                    "orders": [_order_snapshot(order) for order in local_state.orders],
                },
                broker={"connection_healthy": broker_state.connection_healthy},
            ),
        )

    if (
        last_check_timestamp_ns is not None
        and now_timestamp_ns is not None
        and now_timestamp_ns - last_check_timestamp_ns > sleep_resume_threshold_ns
    ):
        discrepancies.append(
            ReconciliationDiscrepancy(
                kind="sleep_resume",
                reason=(
                    f"Reconciliation gap {now_timestamp_ns - last_check_timestamp_ns}ns "
                    f"exceeded threshold {sleep_resume_threshold_ns}ns."
                ),
                local={"last_check_timestamp_ns": last_check_timestamp_ns},
                broker={"now_timestamp_ns": now_timestamp_ns},
            ),
        )

    return tuple(discrepancies)


def run_reconciliation_once(
    risk_approval: RiskApproval,
    *,
    account: AccountRef,
    local_state_provider: LocalStateProvider,
    broker_state_provider: BrokerStateProvider,
    kill_switch_state: KillSwitchState,
    flatten_path: FlattenPath,
    timestamp_ns: int,
    logger: ReconciliationLogger | None = None,
    last_check_timestamp_ns: int | None = None,
    sleep_resume_threshold_ns: int = DEFAULT_SLEEP_RESUME_THRESHOLD_NS,
) -> ReconciliationResult:
    """Run one reconciliation pass and trigger the kill-switch flatten path on mismatch."""
    local_state = local_state_provider.get_state()
    broker_state = broker_state_provider.get_state()
    discrepancies = compare_execution_state(
        local_state=local_state,
        broker_state=broker_state,
        last_check_timestamp_ns=last_check_timestamp_ns,
        now_timestamp_ns=timestamp_ns,
        sleep_resume_threshold_ns=sleep_resume_threshold_ns,
    )
    if not discrepancies:
        return ReconciliationResult(discrepancies=(), kill_switch_decision=None, flatten_result=None)

    kill_switch_decision = evaluate_kill_switch(
        replace(kill_switch_state, manual_override_flatten=True),
    )
    flatten_result = None
    if kill_switch_decision.flatten_now:
        flatten_result = flatten_path(
            risk_approval,
            account=account,
            symbol=local_state.symbol,
            expected_contract_symbol=local_state.expected_contract_symbol,
        )

    if logger is not None:
        for discrepancy in discrepancies:
            logger.log(
                ReconciliationLogEntry(
                    timestamp_ns=timestamp_ns,
                    symbol=local_state.symbol,
                    discrepancy_kind=discrepancy.kind,
                    reason=discrepancy.reason,
                    local=discrepancy.local,
                    broker=discrepancy.broker,
                    kill_switch_reason=kill_switch_decision.reason,
                    flatten_attempted=kill_switch_decision.flatten_now,
                    flattened=bool(flatten_result and flatten_result.flattened),
                ),
            )

    return ReconciliationResult(
        discrepancies=discrepancies,
        kill_switch_decision=kill_switch_decision,
        flatten_result=flatten_result,
    )


async def run_reconciliation_loop(
    risk_approval: RiskApproval,
    *,
    account: AccountRef,
    local_state_provider: LocalStateProvider,
    broker_state_provider: BrokerStateProvider,
    kill_switch_state: KillSwitchState,
    flatten_path: FlattenPath,
    clock_ns: Callable[[], int],
    interval_seconds: Decimal,
    logger: ReconciliationLogger | None = None,
    iterations: int | None = None,
    sleep_resume_threshold_ns: int = DEFAULT_SLEEP_RESUME_THRESHOLD_NS,
) -> tuple[ReconciliationResult, ...]:
    """Periodically run reconciliation with injectable timing for deterministic tests."""
    if not isinstance(interval_seconds, Decimal):
        raise TypeError("interval_seconds must be a Decimal")
    if interval_seconds <= Decimal("0"):
        raise ValueError("interval_seconds must be greater than zero")
    if iterations is not None and iterations <= 0:
        raise ValueError("iterations must be greater than zero when provided")

    results: list[ReconciliationResult] = []
    last_check_timestamp_ns: int | None = None
    completed = 0
    while iterations is None or completed < iterations:
        timestamp_ns = int(clock_ns())
        results.append(
            run_reconciliation_once(
                risk_approval,
                account=account,
                local_state_provider=local_state_provider,
                broker_state_provider=broker_state_provider,
                kill_switch_state=kill_switch_state,
                flatten_path=flatten_path,
                timestamp_ns=timestamp_ns,
                logger=logger,
                last_check_timestamp_ns=last_check_timestamp_ns,
                sleep_resume_threshold_ns=sleep_resume_threshold_ns,
            ),
        )
        last_check_timestamp_ns = timestamp_ns
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        await asyncio.sleep(float(interval_seconds))
    return tuple(results)


def append_reconciliation_log_entry(path: str | Path, entry: ReconciliationLogEntry) -> None:
    """Append one reconciliation discrepancy entry as newline-delimited JSON."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry.to_json_dict(), sort_keys=True, separators=(",", ":")) + "\n")


def read_reconciliation_log(path: str | Path) -> tuple[dict[str, object], ...]:
    """Read reconciliation log entries from newline-delimited JSON."""
    log_path = Path(path)
    if not log_path.exists():
        return ()
    return tuple(
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _compare_orders(
    local_orders: tuple[ReconciledOrder, ...],
    broker_orders: tuple[ReconciledOrder, ...],
) -> ReconciliationDiscrepancy | None:
    local_active = {order.order_id: order for order in local_orders if order.status in ACTIVE_ORDER_STATUSES}
    broker_active = {order.order_id: order for order in broker_orders if order.status in ACTIVE_ORDER_STATUSES}
    if local_active == broker_active:
        return None

    return ReconciliationDiscrepancy(
        kind="order_mismatch",
        reason="Local active orders do not match broker active orders.",
        local={"orders": [_order_snapshot(order) for order in local_active.values()]},
        broker={"orders": [_order_snapshot(order) for order in broker_active.values()]},
    )


def _connection_lost_mid_order(local_state: LocalExecutionState) -> bool:
    return (
        not local_state.connection_healthy
        and any(order.status in ACTIVE_ORDER_STATUSES for order in local_state.orders)
    )


def _position_snapshot(position: ReconciledPosition) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "net_quantity": position.net_quantity,
        "contract_id": position.contract_id,
    }


def _order_snapshot(order: ReconciledOrder) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "status": order.status,
        "remaining_quantity": order.remaining_quantity,
    }


def _extract_position(response: object, symbol: str) -> ReconciledPosition:
    for item in _items(response):
        if _symbol_from_item(item) == symbol:
            return ReconciledPosition(
                symbol=symbol,
                net_quantity=int(item.get("netPos", item.get("netQuantity", item.get("quantity", 0)))),
                contract_id=_optional_int(item.get("contractId")),
            )
    return ReconciledPosition(symbol=symbol, net_quantity=0)


def _extract_orders(response: object, symbol: str) -> tuple[ReconciledOrder, ...]:
    orders: list[ReconciledOrder] = []
    for item in _items(response):
        if _symbol_from_item(item) != symbol:
            continue
        orders.append(
            ReconciledOrder(
                order_id=str(item.get("id", item.get("orderId", ""))),
                symbol=symbol,
                status=_normalize_order_status(str(item.get("status", "working"))),
                remaining_quantity=int(item.get("remainingQuantity", item.get("orderQty", 0))),
            ),
        )
    return tuple(orders)


def _items(response: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(response, Mapping):
        maybe_items = response.get("items")
        if isinstance(maybe_items, Sequence) and not isinstance(maybe_items, (str, bytes, bytearray)):
            response = maybe_items
        else:
            response = (response,)
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise ValueError("Tradovate state response must be a JSON object or list")

    items: list[Mapping[str, object]] = []
    for item in response:
        if not isinstance(item, Mapping):
            raise ValueError("Tradovate state response items must be JSON objects")
        items.append(item)
    return tuple(items)


def _symbol_from_item(item: Mapping[str, object]) -> str:
    if isinstance(item.get("symbol"), str):
        return str(item["symbol"])
    contract = item.get("contract")
    if isinstance(contract, Mapping):
        for key in ("name", "symbol"):
            value = contract.get(key)
            if isinstance(value, str):
                return value
    if isinstance(item.get("contractName"), str):
        return str(item["contractName"])
    return ""


def _normalize_order_status(status: str) -> OrderStatus:
    normalized = status.strip().lower()
    if normalized in {"pending_ack", "pendingack", "pending"}:
        return "pending_ack"
    if normalized in {"working", "open"}:
        return "working"
    if normalized in {"partially_filled", "partial", "partfilled"}:
        return "partially_filled"
    if normalized in {"filled", "complete"}:
        return "filled"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "rejected":
        return "rejected"
    raise ValueError(f"Unsupported order status {status!r}")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _json_safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe_mapping(asdict(value))
    raise ValueError(f"value of type {type(value).__name__} is not reconciliation-log compatible")
