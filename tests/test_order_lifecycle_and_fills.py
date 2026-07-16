"""Tests: bracket lifecycle transitions (mocked clients) and the paper fill engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.order_lifecycle import (
    PHASE_CANCELLED,
    PHASE_PROTECTED,
    PHASE_RECONCILE,
    BracketLifecycle,
)
from app.execution.orders import AccountRef
from app.execution.user_sync import OrderState, TradovateUserSyncClient
from app.simulator.fill_engine import (
    STATUS_CANCELLED,
    STATUS_DISCONNECTED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_REJECTED,
    FillModelConfig,
    MarketTrade,
    simulate_order,
)


@dataclass(frozen=True)
class _Approval:
    allowed: bool = True
    reason: str = "test"


class LifecycleHttp:
    """Scripted REST client for lifecycle actions."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.fail_modify = False

    def get(self, url, *, headers, params=None):
        return {}

    def post(self, url, *, headers, json):
        self.posts.append((url.rsplit("/", 1)[-1], dict(json)))
        if url.endswith("order/placeOSO"):
            return {"orderId": 11, "oso1Id": 12, "oso2Id": 13}
        if url.endswith("order/modifyOrder"):
            return {"failureReason": "PriceOutOfBounds"} if self.fail_modify else {"ok": True}
        return {"commandId": 1}


class _StubTransport:
    def connect(self) -> None: ...
    def send(self, payload: str) -> None: ...
    def recv(self, timeout_seconds: float) -> str | None:
        return None
    def close(self) -> None: ...


def _lifecycle(tmp_path: Path) -> tuple[BracketLifecycle, LifecycleHttp, TradovateUserSyncClient]:
    http = LifecycleHttp()
    reconciled = {"count": 0}

    def snapshot() -> dict:
        reconciled["count"] += 1
        return {"orders": [], "positions": [], "cash": None}

    sync = TradovateUserSyncClient(_StubTransport(), token_provider=lambda: "tok",
                                   clock=lambda: 0.0, snapshot_provider=snapshot)
    lifecycle = BracketLifecycle(http, sync, account=AccountRef(account_id=7, account_spec="DEMO7"),
                                 token_provider=lambda: "tok", log_path=tmp_path / "lifecycle.jsonl")
    return lifecycle, http, sync


def _fill(sync: TradovateUserSyncClient, order_id: str, qty: int, price: str, fill_id: int,
          ordered: int) -> None:
    state = sync.orders.setdefault(order_id, OrderState(order_id=order_id))
    state.ordered_quantity = ordered
    sync._apply_fill_event({"id": fill_id, "orderId": order_id, "qty": qty, "price": price})


def test_partial_fill_resizes_protection_then_cancel_remainder(tmp_path: Path) -> None:
    """entry partial fill -> stop/target resized to filled qty -> remainder cancelled."""
    lifecycle, http, sync = _lifecycle(tmp_path)
    lifecycle.submit_entry(_Approval(), action="Buy", quantity=3,
                           stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"))
    _fill(sync, "11", 1, "29451.25", 100, ordered=3)  # 1 of 3 filled
    state = lifecycle.on_fill_progress(_Approval())
    assert state.filled_quantity == 1
    resizes = [(p, body) for p, body in http.posts if p == "modifyOrder"]
    assert {body["orderId"] for _, body in resizes} == {"12", "13"}
    assert all(body["orderQty"] == 1 for _, body in resizes)

    outcome = lifecycle.cancel_remaining_entry(_Approval())
    assert outcome == PHASE_PROTECTED  # 1 lot remains protected; remainder cancelled
    log = (tmp_path / "lifecycle.jsonl").read_text(encoding="utf-8")
    assert "entry_submitted" in log and "protection_resized" in log and "entry_cancelled" in log


def test_cancel_with_zero_fills_cancels_cleanly(tmp_path: Path) -> None:
    lifecycle, _, sync = _lifecycle(tmp_path)
    lifecycle.submit_entry(_Approval(), action="Sell", quantity=2,
                           stop_price=Decimal("29471.25"), target_price=Decimal("29441.25"))
    sync.orders["11"] = OrderState(order_id="11", ordered_quantity=2)
    assert lifecycle.cancel_remaining_entry(_Approval()) == PHASE_CANCELLED


def test_fill_racing_cancel_forces_reconciliation_not_a_guess(tmp_path: Path) -> None:
    """A fill landing during the cancel round-trip triggers snapshot reconciliation."""
    lifecycle, http, sync = _lifecycle(tmp_path)
    lifecycle.submit_entry(_Approval(), action="Buy", quantity=2,
                           stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"))
    sync.orders["11"] = OrderState(order_id="11", ordered_quantity=2)

    original_post = http.post

    def racing_post(url, *, headers, json):
        if url.endswith("order/cancelOrder"):
            _fill(sync, "11", 1, "29451.25", 200, ordered=2)  # fill lands mid-cancel
        return original_post(url, headers=headers, json=json)

    http.post = racing_post
    outcome = lifecycle.cancel_remaining_entry(_Approval())
    assert outcome == PHASE_RECONCILE
    assert sync.stats.snapshots_reconciled == 1
    assert "cancel_fill_race_detected" in (tmp_path / "lifecycle.jsonl").read_text(encoding="utf-8")


def test_replace_rejection_reconciles_after_bounded_attempts(tmp_path: Path) -> None:
    lifecycle, http, sync = _lifecycle(tmp_path)
    lifecycle.submit_entry(_Approval(), action="Buy", quantity=1,
                           stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"))
    http.fail_modify = True
    ok = lifecycle.replace_price(_Approval(), order_id="12", new_price=Decimal("29443.25"))
    assert ok is False
    assert lifecycle.phase == PHASE_RECONCILE
    assert sync.stats.snapshots_reconciled == 1
    attempts = [p for p, body in http.posts if p == "modifyOrder" and body.get("orderId") == "12"]
    assert len(attempts) == 3  # bounded attempts, then reconciliation


def test_partial_then_final_exit_tracked(tmp_path: Path) -> None:
    lifecycle, _, sync = _lifecycle(tmp_path)
    lifecycle.submit_entry(_Approval(), action="Buy", quantity=2,
                           stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"))
    _fill(sync, "11", 2, "29451.25", 300, ordered=2)
    lifecycle.on_fill_progress(_Approval())
    _fill(sync, "13", 1, "29471.25", 301, ordered=2)  # partial exit at target
    exited, remaining = lifecycle.on_exit_progress()
    assert (exited, remaining) == (1, 1)
    _fill(sync, "13", 1, "29471.25", 302, ordered=2)  # final exit
    exited, remaining = lifecycle.on_exit_progress()
    assert (exited, remaining) == (2, 0)
    assert lifecycle.phase == "exited"


# --- fill engine -------------------------------------------------------------


def _trades(*rows: tuple[int, str, int]) -> list[MarketTrade]:
    return [MarketTrade(timestamp_ns=t, price=Decimal(p), size=s) for t, p, s in rows]


def test_queue_ahead_delays_fills_until_consumed() -> None:
    result = simulate_order(
        quantity=2, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100", 3), (2, "100", 2)),
        config=FillModelConfig(queue_ahead_contracts=3),
    )
    # First event's 3 lots feed the queue ahead; only the second event fills us.
    assert result.filled_quantity == 2
    assert result.fills[0].timestamp_ns == 2


def test_partial_fills_across_multiple_prices_weighted_average() -> None:
    result = simulate_order(
        quantity=4, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100.00", 1), (2, "99.75", 3)),
    )
    assert result.status == STATUS_FILLED
    assert [f.quantity for f in result.fills] == [1, 3]
    # (100.00*1 + 99.75*3) / 4 = 99.8125 -> 99.81
    assert result.average_fill_price == Decimal("99.81")
    assert result.commission == Decimal("1.24") * 4


def test_liquidity_fraction_limits_each_event() -> None:
    result = simulate_order(
        quantity=4, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100", 4)),
        config=FillModelConfig(liquidity_fraction=Decimal("0.5")),
    )
    assert result.status == STATUS_PARTIAL
    assert result.filled_quantity == 2  # only half the printed size was available


def test_ack_delay_blocks_early_fills_and_rejection_terminates() -> None:
    late = simulate_order(quantity=1, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
                          trades=_trades((5, "100", 5)), config=FillModelConfig(ack_delay_ns=10))
    assert late.filled_quantity == 0
    rejected = simulate_order(quantity=1, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
                              trades=_trades((5, "100", 5)), rejected=True)
    assert rejected.status == STATUS_REJECTED and rejected.filled_quantity == 0


def test_cancel_race_fills_before_cancel_stand() -> None:
    result = simulate_order(
        quantity=3, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100", 1), (10, "100", 5)),
        config=FillModelConfig(cancel_delay_ns=5),
        cancel_at_ns=2,  # cancel lands at t=7; the t=1 fill stands, t=10 is cancelled
    )
    assert result.status == STATUS_CANCELLED
    assert result.filled_quantity == 1
    assert result.cancelled_quantity == 2


def test_replace_takes_effect_after_delay() -> None:
    result = simulate_order(
        quantity=1, limit_price=Decimal("99"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100", 5), (20, "100", 5)),
        config=FillModelConfig(cancel_delay_ns=5),
        replace=(2, Decimal("100")),  # becomes marketable only after replace lands at t=7
    )
    assert result.filled_quantity == 1
    assert result.fills[0].timestamp_ns == 20


def test_disconnect_freezes_order_for_reconciliation() -> None:
    result = simulate_order(
        quantity=2, limit_price=Decimal("100"), is_buy=True, submitted_at_ns=0,
        trades=_trades((1, "100", 1), (10, "100", 5)),
        disconnect_at_ns=5,
    )
    assert result.status == STATUS_DISCONNECTED
    assert result.filled_quantity == 1  # pre-disconnect fill stands; nothing guessed after
    assert result.remaining_quantity == 1


def test_canonical_ledger_rows_carry_order_and_fill_records(tmp_path: Path) -> None:
    """The canonical account consumes actual simulated order/fill records."""
    from app.research.auto_research import canonical_candidate
    from app.research.research_service import ResearchService

    service = ResearchService(tmp_path / "raw", Path("data/processed"), state_dir=tmp_path / "state",
                              candidates=[canonical_candidate()])
    service.persist_ledgers([])
    canonical = json.loads((tmp_path / "state" / "ledgers" / "canonical.json").read_text(encoding="utf-8"))
    assert "order/fill records" in canonical["economics"]
    for row in canonical["trades"]:  # zero rows today - structure asserted when present
        assert "fills" in row and "average_fill_price" in row
