"""Demo order lifecycle: true partial fills, cancel/replace, races, reconciliation.

Drives one bracket (entry + protective stop/target) through every transition the
broker can produce, using the injected REST client for actions and the
:class:`~app.execution.user_sync.TradovateUserSyncClient` order states as the
source of truth for fills. Every transition is appended to an immutable JSONL
log with local and broker identifiers. Ambiguous transitions (a cancel racing a
fill, a rejected replace) always end in a REST snapshot reconciliation instead
of a guess.

DEMO only: URLs come from the demo module; risk approval is required for every
mutating action; nothing here can construct a live path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

from app.execution.orders import (
    TRADOVATE_DEMO_REST_BASE_URL,
    AccountRef,
    ExecutionRejectedError,
    RiskApproval,
    TradovateHttpClient,
    _require_approved,
)
from app.execution.user_sync import OrderState, TradovateUserSyncClient

PHASE_SUBMITTED = "entry_submitted"
PHASE_PARTIAL = "entry_partially_filled"
PHASE_FILLED = "entry_filled"
PHASE_PROTECTED = "protection_resized"
PHASE_CANCELLED = "entry_cancelled"
PHASE_EXITED = "exited"
PHASE_RECONCILE = "needs_reconciliation"


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    """Explicit lifecycle assumptions."""

    symbol: str = "MNQ"
    max_replace_attempts: int = 3


class BracketLifecycle:
    """One bracket's state machine from entry submission to exit."""

    def __init__(
        self,
        http_client: TradovateHttpClient,
        sync_client: TradovateUserSyncClient,
        *,
        account: AccountRef,
        token_provider: Callable[[], str],
        log_path: Path,
        config: LifecycleConfig | None = None,
    ) -> None:
        """Create a lifecycle bound to one account and one append-only log."""
        self._http = http_client
        self._sync = sync_client
        self._account = account
        self._token = token_provider
        self._log_path = log_path
        self._config = config or LifecycleConfig()
        self.phase = ""
        self.entry_order_id = ""
        self.stop_order_id = ""
        self.target_order_id = ""
        self.protected_quantity = 0

    # -- actions -----------------------------------------------------------------

    def submit_entry(self, risk_approval: RiskApproval, *, action: str, quantity: int,
                     stop_price: Decimal, target_price: Decimal) -> str:
        """Submit the bracket entry (market) with protective stop and target."""
        _require_approved(risk_approval)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        response = self._post("order/placeOSO", {
            "accountSpec": self._account.account_spec, "accountId": self._account.account_id,
            "action": action, "symbol": self._config.symbol, "orderQty": quantity,
            "orderType": "Market", "isAutomated": True,
            "bracket1": {"action": _opposite(action), "orderType": "Stop", "stopPrice": str(stop_price)},
            "bracket2": {"action": _opposite(action), "orderType": "Limit", "price": str(target_price)},
        })
        self.entry_order_id = str(response.get("orderId", ""))
        self.stop_order_id = str(response.get("oso1Id", "") or "")
        self.target_order_id = str(response.get("oso2Id", "") or "")
        if not self.entry_order_id:
            raise ExecutionRejectedError("placeOSO returned no orderId")
        self.phase = PHASE_SUBMITTED
        self._log("entry_submitted", quantity=quantity, entry_order_id=self.entry_order_id,
                  stop_order_id=self.stop_order_id, target_order_id=self.target_order_id)
        return self.entry_order_id

    def on_fill_progress(self, risk_approval: RiskApproval) -> OrderState:
        """React to the entry's current fill state: resize protection to filled qty.

        Partial fills resize the stop/target quantity DOWN to what is actually
        filled, so protection never exceeds the real position.
        """
        state = self._entry_state()
        if state.filled_quantity == 0:
            return state
        if state.remaining_quantity == 0:
            self.phase = PHASE_FILLED
        else:
            self.phase = PHASE_PARTIAL
        if state.filled_quantity != self.protected_quantity:
            for order_id, kind in ((self.stop_order_id, "stop"), (self.target_order_id, "target")):
                if order_id:
                    self._modify(risk_approval, order_id, {"orderQty": state.filled_quantity})
                    self._log("protection_resized", kind=kind, order_id=order_id,
                              quantity=state.filled_quantity,
                              average_fill=str(state.average_fill_price))
            self.protected_quantity = state.filled_quantity
            if self.phase == PHASE_FILLED:
                self.phase = PHASE_PROTECTED
        return state

    def cancel_remaining_entry(self, risk_approval: RiskApproval) -> str:
        """Cancel the unfilled remainder; a fill racing the cancel forces reconciliation."""
        _require_approved(risk_approval)
        before = self._entry_state().filled_quantity
        self._post("order/cancelOrder", {"orderId": self.entry_order_id})
        self._log("entry_cancel_requested", order_id=self.entry_order_id, filled_before=before)
        after = self._entry_state()
        if after.filled_quantity != before:
            # The race happened: more quantity filled while the cancel was in
            # flight. Never guess - reconcile from the broker snapshot.
            self.phase = PHASE_RECONCILE
            self._log("cancel_fill_race_detected", filled_before=before,
                      filled_after=after.filled_quantity)
            self._sync.reconcile_from_snapshot()
            self.on_fill_progress(risk_approval)  # re-protect the true filled quantity
            return PHASE_RECONCILE
        self.phase = PHASE_CANCELLED if after.filled_quantity == 0 else PHASE_PROTECTED
        self._log("entry_cancelled", order_id=self.entry_order_id,
                  final_filled=after.filled_quantity)
        return self.phase

    def replace_price(self, risk_approval: RiskApproval, *, order_id: str,
                      new_price: Decimal, price_field: str = "stopPrice") -> bool:
        """Cancel/replace one protective order's price; rejection reconciles."""
        _require_approved(risk_approval)
        for attempt in range(1, self._config.max_replace_attempts + 1):
            response = self._post("order/modifyOrder", {"orderId": order_id, price_field: str(new_price)})
            ok = str(response.get("failureReason", "")) == ""
            self._log("replace_attempted", order_id=order_id, attempt=attempt,
                      new_price=str(new_price), accepted=ok,
                      failure=str(response.get("failureReason", "")))
            if ok:
                return True
        # Replace kept failing: the working order's true state is ambiguous.
        self.phase = PHASE_RECONCILE
        self._sync.reconcile_from_snapshot()
        self._log("replace_rejected_reconciled", order_id=order_id)
        return False

    def on_exit_progress(self) -> tuple[int, int]:
        """Track partial/final exits via the protective orders' fill states."""
        exited = 0
        for order_id in (self.stop_order_id, self.target_order_id):
            if order_id and order_id in self._sync.orders:
                exited += self._sync.orders[order_id].filled_quantity
        remaining = max(0, self.protected_quantity - exited)
        if exited and remaining == 0:
            self.phase = PHASE_EXITED
            self._log("exit_complete", exited=exited)
        elif exited:
            self._log("partial_exit", exited=exited, remaining=remaining)
        return exited, remaining

    # -- internals -----------------------------------------------------------------

    def _entry_state(self) -> OrderState:
        return self._sync.orders.get(self.entry_order_id, OrderState(order_id=self.entry_order_id))

    def _modify(self, risk_approval: RiskApproval, order_id: str, fields: Mapping[str, object]) -> None:
        _require_approved(risk_approval)
        self._post("order/modifyOrder", {"orderId": order_id, **fields})

    def _post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        response = self._http.post(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/{path}",
            headers={"Authorization": f"Bearer {self._token()}"},
            json=dict(payload),
        )
        return response if isinstance(response, Mapping) else {}

    def _log(self, event: str, **fields: object) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"event": event, "phase": self.phase, "recorded_utc": datetime.now(UTC).isoformat(),
                   "entry_order_id": self.entry_order_id, **fields}
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _opposite(action: str) -> str:
    return "Sell" if action == "Buy" else "Buy"
