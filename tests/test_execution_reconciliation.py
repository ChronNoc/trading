"""Tests for execution reconciliation and safety flatten handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.execution.orders import AccountRef, BrokerAcknowledgement, FlattenPositionResult
from app.execution.reconciliation import (
    BrokerExecutionState,
    DemoTradovateStateProvider,
    DuplicateSignalDebouncer,
    JsonLinesReconciliationLogger,
    LocalExecutionState,
    ReconciledOrder,
    ReconciledPosition,
    SignalFingerprint,
    compare_execution_state,
    read_reconciliation_log,
    run_reconciliation_loop,
    run_reconciliation_once,
)
from app.risk.kill_switch import KillSwitchState
from app.risk.limits import EntryLimitState, InstrumentConfig


@dataclass(frozen=True, slots=True)
class Approval:
    """Risk approval test double."""

    allowed: bool = True
    reason: str = "approved"


@dataclass(frozen=True, slots=True)
class StaticLocalProvider:
    """Local state provider test double."""

    state: LocalExecutionState

    def get_state(self) -> LocalExecutionState:
        """Return the configured local state."""
        return self.state


@dataclass(frozen=True, slots=True)
class StaticBrokerProvider:
    """Broker state provider test double."""

    state: BrokerExecutionState

    def get_state(self) -> BrokerExecutionState:
        """Return the configured broker state."""
        return self.state


class CaptureFlattenPath:
    """Flatten path test double."""

    def __init__(self, *, flattened: bool = True) -> None:
        """Create a capture flatten path."""
        self.calls: list[tuple[AccountRef, str, str]] = []
        self.flattened = flattened

    def __call__(
        self,
        risk_approval: Approval,
        *,
        account: AccountRef,
        symbol: str,
        expected_contract_symbol: str,
    ) -> FlattenPositionResult:
        """Record the flatten request and return a fake result."""
        assert risk_approval.allowed is True
        self.calls.append((account, symbol, expected_contract_symbol))
        return FlattenPositionResult(
            symbol=symbol,
            flattened=self.flattened,
            response={"orderId": "flatten-1"},
            acknowledgement=BrokerAcknowledgement(
                action="flatten_position",
                correlation_id="flatten-1",
                accepted=True,
                broker_timestamp_ns=123,
                order_id="flatten-1",
            ),
        )


class MockHttpClient:
    """HTTP client test double for demo Tradovate state provider."""

    def __init__(self, responses: list[object]) -> None:
        """Create a mock HTTP client with queued responses."""
        self.responses = responses
        self.calls: list[tuple[str, object]] = []

    def get(self, url: str, *, headers: object, params: object = None) -> object:
        """Record a GET call and return the next response."""
        self.calls.append((url, params))
        return self.responses.pop(0)

    def post(self, url: str, *, headers: object, json: object) -> object:
        """Fail if a state provider unexpectedly posts."""
        raise AssertionError("state provider should not POST")


def test_position_mismatch_triggers_kill_switch_flatten_and_log(tmp_path: Path) -> None:
    """A broker/local position mismatch logs the discrepancy and flattens."""
    flatten_path = CaptureFlattenPath()
    log_path = tmp_path / "reconciliation.ndjson"

    result = run_reconciliation_once(
        Approval(),
        account=_account(),
        local_state_provider=StaticLocalProvider(_local_state(position_qty=0)),
        broker_state_provider=StaticBrokerProvider(_broker_state(position_qty=1)),
        kill_switch_state=_kill_switch_state(),
        flatten_path=flatten_path,
        timestamp_ns=1_000,
        logger=JsonLinesReconciliationLogger(log_path),
    )

    assert [discrepancy.kind for discrepancy in result.discrepancies] == ["position_mismatch"]
    assert result.kill_switch_decision is not None
    assert result.kill_switch_decision.flatten_now is True
    assert result.flatten_result is not None
    assert result.flatten_result.flattened is True
    assert flatten_path.calls == [(_account(), "MNQU6", "MNQU6")]
    log_entries = read_reconciliation_log(log_path)
    assert log_entries[0]["discrepancy_kind"] == "position_mismatch"
    assert log_entries[0]["flatten_attempted"] is True
    assert log_entries[0]["flattened"] is True


def test_order_mismatch_triggers_flatten() -> None:
    """A local/broker active-order mismatch triggers the kill-switch flatten path."""
    flatten_path = CaptureFlattenPath()

    result = run_reconciliation_once(
        Approval(),
        account=_account(),
        local_state_provider=StaticLocalProvider(
            _local_state(orders=(ReconciledOrder("entry-1", "MNQU6", "working", 1),)),
        ),
        broker_state_provider=StaticBrokerProvider(_broker_state(orders=())),
        kill_switch_state=_kill_switch_state(),
        flatten_path=flatten_path,
        timestamp_ns=2_000,
    )

    assert [discrepancy.kind for discrepancy in result.discrepancies] == ["order_mismatch"]
    assert flatten_path.calls == [(_account(), "MNQU6", "MNQU6")]


def test_connection_lost_mid_order_triggers_flatten() -> None:
    """Connection loss while an order is active is treated as a safety discrepancy."""
    flatten_path = CaptureFlattenPath()

    result = run_reconciliation_once(
        Approval(),
        account=_account(),
        local_state_provider=StaticLocalProvider(
            _local_state(
                connection_healthy=False,
                orders=(ReconciledOrder("entry-1", "MNQU6", "pending_ack", 2),),
            ),
        ),
        broker_state_provider=StaticBrokerProvider(_broker_state(connection_healthy=False)),
        kill_switch_state=_kill_switch_state(),
        flatten_path=flatten_path,
        timestamp_ns=3_000,
    )

    assert [discrepancy.kind for discrepancy in result.discrepancies] == [
        "order_mismatch",
        "connection_lost_mid_order",
    ]
    assert flatten_path.calls == [(_account(), "MNQU6", "MNQU6")]


def test_sleep_resume_gap_triggers_flatten() -> None:
    """A long reconciliation gap after computer sleep/resume triggers flatten handling."""
    flatten_path = CaptureFlattenPath()

    result = run_reconciliation_once(
        Approval(),
        account=_account(),
        local_state_provider=StaticLocalProvider(_local_state()),
        broker_state_provider=StaticBrokerProvider(_broker_state()),
        kill_switch_state=_kill_switch_state(),
        flatten_path=flatten_path,
        timestamp_ns=10_000,
        last_check_timestamp_ns=1_000,
        sleep_resume_threshold_ns=5_000,
    )

    assert [discrepancy.kind for discrepancy in result.discrepancies] == ["sleep_resume"]
    assert flatten_path.calls == [(_account(), "MNQU6", "MNQU6")]


def test_duplicate_signal_debouncer_rejects_second_signal_inside_window() -> None:
    """The same setup signal is rejected when repeated within the debounce window."""
    debouncer = DuplicateSignalDebouncer(debounce_window_ns=1_000)
    signal = SignalFingerprint(symbol="MNQU6", direction="long", setup_id="overnight-low-reclaim")

    first = debouncer.check(signal, timestamp_ns=10_000)
    second = debouncer.check(signal, timestamp_ns=10_500)
    third = debouncer.check(signal, timestamp_ns=11_500)

    assert first.accepted is True
    assert second.accepted is False
    assert "Duplicate signal" in second.reason
    assert third.accepted is True


def test_matching_states_do_not_flatten_or_log(tmp_path: Path) -> None:
    """Matching local and broker states produce no discrepancy and no flatten call."""
    flatten_path = CaptureFlattenPath()
    log_path = tmp_path / "reconciliation.ndjson"

    result = run_reconciliation_once(
        Approval(),
        account=_account(),
        local_state_provider=StaticLocalProvider(_local_state()),
        broker_state_provider=StaticBrokerProvider(_broker_state()),
        kill_switch_state=_kill_switch_state(),
        flatten_path=flatten_path,
        timestamp_ns=4_000,
        logger=JsonLinesReconciliationLogger(log_path),
    )

    assert result.discrepancies == ()
    assert result.kill_switch_decision is None
    assert result.flatten_result is None
    assert flatten_path.calls == []
    assert read_reconciliation_log(log_path) == ()


def test_compare_execution_state_reports_position_and_order_mismatches() -> None:
    """Pure comparison returns all independent discrepancies without side effects."""
    discrepancies = compare_execution_state(
        local_state=_local_state(
            position_qty=0,
            orders=(ReconciledOrder("entry-1", "MNQU6", "working", 1),),
        ),
        broker_state=_broker_state(
            position_qty=1,
            orders=(ReconciledOrder("other", "MNQU6", "working", 1),),
        ),
    )

    assert [discrepancy.kind for discrepancy in discrepancies] == [
        "position_mismatch",
        "order_mismatch",
    ]


def test_demo_tradovate_state_provider_parses_position_and_orders() -> None:
    """The demo state provider parses injected Tradovate-style JSON responses."""
    http = MockHttpClient(
        responses=[
            [{"accountId": 11, "contract": {"name": "MNQU6"}, "netPos": -1, "contractId": 44}],
            [{"id": "entry-1", "contract": {"name": "MNQU6"}, "status": "Working", "orderQty": 2}],
        ],
    )
    provider = DemoTradovateStateProvider(
        account=_account(),
        symbol="MNQU6",
        http_client=http,
        access_token="token",
    )

    state = provider.get_state()

    assert state.position == ReconciledPosition(symbol="MNQU6", net_quantity=-1, contract_id=44)
    assert state.orders == (ReconciledOrder("entry-1", "MNQU6", "working", 2),)


def test_reconciliation_loop_runs_periodically_with_injected_clock() -> None:
    """The periodic loop can run a bounded number of deterministic iterations."""
    times = iter((1_000, 2_000))
    flatten_path = CaptureFlattenPath()

    results = asyncio.run(
        run_reconciliation_loop(
            Approval(),
            account=_account(),
            local_state_provider=StaticLocalProvider(_local_state()),
            broker_state_provider=StaticBrokerProvider(_broker_state()),
            kill_switch_state=_kill_switch_state(),
            flatten_path=flatten_path,
            clock_ns=lambda: next(times),
            interval_seconds=Decimal("0.001"),
            iterations=2,
        ),
    )

    assert len(results) == 2
    assert all(result.discrepancies == () for result in results)
    assert flatten_path.calls == []


def _account() -> AccountRef:
    return AccountRef(account_id=11, account_spec="DEMO-ACCOUNT")


def _local_state(
    *,
    position_qty: int = 0,
    orders: tuple[ReconciledOrder, ...] = (),
    connection_healthy: bool = True,
) -> LocalExecutionState:
    return LocalExecutionState(
        symbol="MNQU6",
        expected_contract_symbol="MNQU6",
        position=ReconciledPosition(symbol="MNQU6", net_quantity=position_qty, contract_id=44),
        orders=orders,
        connection_healthy=connection_healthy,
    )


def _broker_state(
    *,
    position_qty: int = 0,
    orders: tuple[ReconciledOrder, ...] = (),
    connection_healthy: bool = True,
) -> BrokerExecutionState:
    return BrokerExecutionState(
        position=ReconciledPosition(symbol="MNQU6", net_quantity=position_qty, contract_id=44),
        orders=orders,
        connection_healthy=connection_healthy,
    )


def _kill_switch_state() -> KillSwitchState:
    return KillSwitchState(entry_limits=_passing_entry_limits())


def _passing_entry_limits() -> EntryLimitState:
    return EntryLimitState(
        instrument_configs=(InstrumentConfig(symbol="MNQ"),),
        open_positions=1,
        losing_trades_today=0,
        entries_today=0,
        daily_realized_loss=Decimal("0"),
        daily_risk_budget=Decimal("90"),
        proposed_trade_risk=Decimal("20"),
        max_risk_per_trade=Decimal("30"),
        has_open_losing_position=False,
        adds_to_existing_position=False,
        stop_distance_ticks=Decimal("8"),
        connection_healthy=True,
        account_synchronized=True,
        unresolved_order_cancellation_pending=False,
        current_date=date(2026, 7, 10),
    )
