"""Tradovate DEMO gateway: auth, sync, orders, reconciliation - demo endpoints only.

The gateway boundary of the application:

- ``PaperExecutionGateway`` (in :mod:`app.execution.paper_gateway`) is the only
  gateway research/replay/shadow/paper paths may receive.
- ``TradovateDemoGateway`` (here) talks exclusively to Tradovate's DEMO REST/WS
  endpoints through injected clients, so every test runs against mocks and no
  live network call can occur in tests.
- ``TradovateLiveGateway`` (here) cannot even be constructed until the future
  LIVE gate (:mod:`app.execution.live_gate`) passes every requirement; today it
  always raises.

Credentials come only from environment variables (or a local ``.env`` the user
maintains outside Git). They are never logged, echoed, or persisted by this
module. Connecting is read-only; ARMING is a separate explicit step, and arming
never persists across restarts.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

from app.execution.brackets import BracketOrderRequest, BracketOrderResult, place_bracket_order
from app.execution.orders import (
    TRADOVATE_DEMO_REST_BASE_URL,
    AccountRef,
    ExecutionRejectedError,
    RiskApproval,
    TradovateAckClient,
    TradovateHttpClient,
    cancel_order,
    flatten_position,
)

ENVIRONMENT_DEMO = "TRADOVATE_DEMO"
ENVIRONMENT_LIVE = "TRADOVATE_LIVE"

# Environment variable names for demo credentials (values are never logged).
ENV_DEMO_USERNAME = "TRADOVATE_DEMO_USERNAME"
ENV_DEMO_PASSWORD = "TRADOVATE_DEMO_PASSWORD"
ENV_DEMO_APP_ID = "TRADOVATE_DEMO_APP_ID"
ENV_DEMO_APP_VERSION = "TRADOVATE_DEMO_APP_VERSION"
ENV_DEMO_CID = "TRADOVATE_DEMO_CID"
ENV_DEMO_SECRET = "TRADOVATE_DEMO_SECRET"

DUPLICATE_SIGNAL_WINDOW_SECONDS = 30.0


class CredentialsMissingError(RuntimeError):
    """Raised when demo credentials are not present in the environment."""


@dataclass(frozen=True, slots=True)
class DemoCredentials:
    """Demo credentials loaded from the environment; never printed or logged."""

    username: str
    password: str
    app_id: str
    app_version: str
    cid: str
    secret: str

    @classmethod
    def from_environment(cls) -> "DemoCredentials":
        """Load demo credentials from environment variables or raise clearly."""
        values = {
            name: os.environ.get(name, "")
            for name in (ENV_DEMO_USERNAME, ENV_DEMO_PASSWORD, ENV_DEMO_APP_ID,
                         ENV_DEMO_APP_VERSION, ENV_DEMO_CID, ENV_DEMO_SECRET)
        }
        missing = sorted(name for name, value in values.items() if not value)
        if missing:
            raise CredentialsMissingError(
                "Missing Tradovate demo credentials in environment: " + ", ".join(missing)
                + ". Set them in your shell or a local .env (never committed).",
            )
        return cls(
            username=values[ENV_DEMO_USERNAME], password=values[ENV_DEMO_PASSWORD],
            app_id=values[ENV_DEMO_APP_ID], app_version=values[ENV_DEMO_APP_VERSION],
            cid=values[ENV_DEMO_CID], secret=values[ENV_DEMO_SECRET],
        )

    def __repr__(self) -> str:  # pragma: no cover - defensive
        """Redact everything: credentials must never appear in logs or errors."""
        return "DemoCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A demo access token with its expiry for refresh handling."""

    token: str
    expires_at_epoch: float

    def needs_refresh(self, *, now: float | None = None, margin_seconds: float = 120.0) -> bool:
        """Return whether the token is expired or inside the refresh margin."""
        return (now if now is not None else time.time()) >= self.expires_at_epoch - margin_seconds

    def __repr__(self) -> str:  # pragma: no cover - defensive
        """Redact the token value."""
        return f"AccessToken(<redacted>, expires_at_epoch={self.expires_at_epoch})"


def authenticate_demo(
    credentials: DemoCredentials,
    http_client: TradovateHttpClient,
    *,
    now: float | None = None,
) -> AccessToken:
    """Request a demo access token (POST auth/accesstokenrequest, demo host only)."""
    response = http_client.post(
        f"{TRADOVATE_DEMO_REST_BASE_URL}/auth/accesstokenrequest",
        headers={"Content-Type": "application/json"},
        json={
            "name": credentials.username,
            "password": credentials.password,
            "appId": credentials.app_id,
            "appVersion": credentials.app_version,
            "cid": credentials.cid,
            "sec": credentials.secret,
        },
    )
    if not isinstance(response, Mapping) or "accessToken" not in response:
        raise ExecutionRejectedError("demo authentication failed: no accessToken in response")
    lifetime = float(response.get("expirationTime_seconds", 0) or 0)
    if lifetime <= 0:
        # Tradovate returns an ISO expirationTime; fall back to a conservative 60 min.
        lifetime = 3600.0
    return AccessToken(token=str(response["accessToken"]),
                       expires_at_epoch=(now if now is not None else time.time()) + lifetime)


@dataclass(slots=True)
class GatewaySnapshot:
    """GUI-facing snapshot of the demo connection; contains no secrets."""

    environment: str = ENVIRONMENT_DEMO
    connected: bool = False
    authenticated: bool = False
    armed: bool = False
    account_id: int | None = None
    account_spec: str = ""
    balance: str = "unknown"
    buying_power: str = "unknown"
    position_net: int = 0
    working_orders: int = 0
    contract: str = ""
    reconnects: int = 0
    last_reconciliation_utc: str = ""
    orphan_orders: int = 0
    last_error: str = ""


class TradovateDemoGateway:
    """Demo-only order gateway over injected REST/WS clients (mockable, testable)."""

    environment = ENVIRONMENT_DEMO

    def __init__(
        self,
        http_client: TradovateHttpClient,
        ws_client: TradovateAckClient,
        *,
        order_log_path: Path,
        credentials_loader: Callable[[], DemoCredentials] = DemoCredentials.from_environment,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a disconnected, DISARMED demo gateway (arming never persists)."""
        self._http = http_client
        self._ws = ws_client
        self._load_credentials = credentials_loader
        self._clock = clock
        self._order_log_path = order_log_path
        self._token: AccessToken | None = None
        self._account: AccountRef | None = None
        self._snapshot = GatewaySnapshot()
        self._armed = False  # in-memory only: every restart is disarmed
        self._recent_signals: dict[str, float] = {}
        self._known_order_ids: set[str] = set()

    # -- connection lifecycle ---------------------------------------------------

    def connect(self) -> GatewaySnapshot:
        """Authenticate read-only against the demo endpoint. Does NOT arm orders."""
        credentials = self._load_credentials()
        self._token = authenticate_demo(credentials, self._http, now=self._clock())
        self._snapshot.connected = True
        self._snapshot.authenticated = True
        self._snapshot.last_error = ""
        return self.snapshot()

    def ensure_token(self) -> None:
        """Refresh the access token when it is near expiry (re-auth flow)."""
        if self._token is None:
            raise ExecutionRejectedError("not connected")
        if self._token.needs_refresh(now=self._clock()):
            self._token = authenticate_demo(self._load_credentials(), self._http, now=self._clock())

    def disconnect(self) -> GatewaySnapshot:
        """Drop the session and disarm. Never persists any state."""
        self._token = None
        self._armed = False
        self._snapshot.connected = False
        self._snapshot.authenticated = False
        self._snapshot.armed = False
        return self.snapshot()

    def record_reconnect(self) -> None:
        """Count a transport reconnect (e.g. after sleep/resume)."""
        self._snapshot.reconnects += 1

    def test_connection(self) -> bool:
        """Round-trip a harmless authenticated GET to prove the session works."""
        self.ensure_token()
        assert self._token is not None
        response = self._http.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {self._token.token}"},
        )
        return isinstance(response, (list, tuple))

    # -- account / sync -----------------------------------------------------------

    def select_account(self) -> AccountRef:
        """Fetch and select the first demo account."""
        self.ensure_token()
        assert self._token is not None
        response = self._http.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {self._token.token}"},
        )
        accounts = list(response) if isinstance(response, (list, tuple)) else []
        if not accounts:
            raise ExecutionRejectedError("no demo account available")
        first = accounts[0]
        self._account = AccountRef(account_id=int(first["id"]), account_spec=str(first["name"]))
        self._snapshot.account_id = self._account.account_id
        self._snapshot.account_spec = self._account.account_spec
        return self._account

    def synchronize(self, *, contract_symbol: str = "MNQ") -> GatewaySnapshot:
        """Sync balance, position, and working orders; detect orphan orders.

        An orphan is a working broker order this gateway never placed (unknown
        order id) - it is counted and logged, never silently adopted.
        """
        self.ensure_token()
        assert self._token is not None
        account = self._account or self.select_account()
        headers = {"Authorization": f"Bearer {self._token.token}"}
        balances = self._http.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/cashBalance/getcashbalancesnapshot",
            headers=headers, params={"accountId": account.account_id},
        )
        if isinstance(balances, Mapping):
            self._snapshot.balance = str(balances.get("totalCashValue", "unknown"))
            self._snapshot.buying_power = str(balances.get("buyingPower", "unknown"))
        positions = self._http.get(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/position/list", headers=headers,
        )
        net = 0
        if isinstance(positions, (list, tuple)):
            for position in positions:
                if isinstance(position, Mapping) and int(position.get("accountId", -1)) == account.account_id:
                    net += int(position.get("netPos", 0))
        self._snapshot.position_net = net
        orders = self._http.get(f"{TRADOVATE_DEMO_REST_BASE_URL}/order/list", headers=headers)
        working = 0
        orphans = 0
        if isinstance(orders, (list, tuple)):
            for order in orders:
                if not isinstance(order, Mapping):
                    continue
                if str(order.get("ordStatus", "")) not in {"Working", "Suspended"}:
                    continue
                working += 1
                if str(order.get("id", "")) not in self._known_order_ids:
                    orphans += 1
                    self._append_log({"event": "orphan_order_detected", "order_id": str(order.get("id", ""))})
        self._snapshot.working_orders = working
        self._snapshot.orphan_orders = orphans
        self._snapshot.contract = contract_symbol
        self._snapshot.last_reconciliation_utc = datetime.now(UTC).isoformat()
        return self.snapshot()

    # -- arming -------------------------------------------------------------------

    def arm_demo(self) -> None:
        """Explicitly allow demo order placement for THIS process only."""
        if not self._snapshot.connected:
            raise ExecutionRejectedError("connect before arming demo")
        self._armed = True
        self._snapshot.armed = True

    def disarm(self) -> None:
        """Immediately revoke order permission (kept in memory only)."""
        self._armed = False
        self._snapshot.armed = False

    # -- orders ---------------------------------------------------------------------

    def place_bracket(
        self,
        risk_approval: RiskApproval,
        *,
        request: BracketOrderRequest,
        signal_key: str,
    ) -> BracketOrderResult:
        """Place a demo bracket after arming, debounce, and every risk check."""
        if not self._armed:
            raise ExecutionRejectedError("demo gateway is connected read-only; ARM DEMO to allow orders")
        now = self._clock()
        last = self._recent_signals.get(signal_key)
        if last is not None and now - last < DUPLICATE_SIGNAL_WINDOW_SECONDS:
            raise ExecutionRejectedError(f"duplicate signal debounced: {signal_key}")
        self._recent_signals[signal_key] = now
        self.ensure_token()
        assert self._token is not None
        result = place_bracket_order(
            risk_approval, request=request, http_client=self._http, ws_client=self._ws,
            access_token=self._token.token,
        )
        for order_id in (result.entry_order_id, result.stop_order_id, result.target_order_id):
            if order_id:
                self._known_order_ids.add(str(order_id))
        self._append_log({
            "event": "bracket_placed", "entry_order_id": result.entry_order_id,
            "stop_order_id": result.stop_order_id, "target_order_id": result.target_order_id,
            "symbol": request.symbol, "quantity": request.quantity, "action": request.action,
        })
        return result

    def cancel_replace(self, risk_approval: RiskApproval, *, order_id: str,
                       new_price: Decimal, price_field: str = "stopPrice") -> bool:
        """Cancel/replace one known working order's price (armed only).

        A rejected replace is logged and answered with a snapshot
        reconciliation - never a guess about the order's true state.
        """
        if not self._armed:
            raise ExecutionRejectedError("demo gateway is connected read-only; ARM DEMO to allow orders")
        from app.execution.orders import _require_approved

        _require_approved(risk_approval)
        self.ensure_token()
        assert self._token is not None
        response = self._http.post(
            f"{TRADOVATE_DEMO_REST_BASE_URL}/order/modifyOrder",
            headers={"Authorization": f"Bearer {self._token.token}"},
            json={"orderId": order_id, price_field: str(new_price)},
        )
        ok = isinstance(response, Mapping) and not response.get("failureReason")
        self._append_log({"event": "cancel_replace", "order_id": order_id,
                          "new_price": str(new_price), "accepted": bool(ok),
                          "failure": str(response.get("failureReason", "")) if isinstance(response, Mapping) else "bad response"})
        if not ok:
            self.synchronize()  # reconcile after the ambiguous transition
        return bool(ok)

    def cancel_all(self, risk_approval: RiskApproval) -> int:
        """Cancel every order this gateway knows it placed; returns the count."""
        self.ensure_token()
        assert self._token is not None
        account = self._account or self.select_account()
        cancelled = 0
        for order_id in sorted(self._known_order_ids):
            cancel_order(risk_approval, account=account, order_id=order_id,
                         http_client=self._http, ws_client=self._ws, access_token=self._token.token)
            cancelled += 1
            self._append_log({"event": "order_cancelled", "order_id": order_id})
        self._known_order_ids.clear()
        return cancelled

    def flatten(self, risk_approval: RiskApproval, *, symbol: str = "MNQ",
                expected_contract_symbol: str = "MNQ") -> None:
        """Flatten the demo position (also the kill-switch flatten path)."""
        self.ensure_token()
        assert self._token is not None
        account = self._account or self.select_account()
        flatten_position(risk_approval, account=account, symbol=symbol,
                         expected_contract_symbol=expected_contract_symbol,
                         http_client=self._http, ws_client=self._ws, access_token=self._token.token)
        self._append_log({"event": "flattened", "symbol": symbol})

    # -- misc ------------------------------------------------------------------------

    def snapshot(self) -> GatewaySnapshot:
        """Return a secrets-free snapshot for the GUI."""
        snap = self._snapshot
        return GatewaySnapshot(
            environment=snap.environment, connected=snap.connected, authenticated=snap.authenticated,
            armed=snap.armed, account_id=snap.account_id, account_spec=snap.account_spec,
            balance=snap.balance, buying_power=snap.buying_power, position_net=snap.position_net,
            working_orders=snap.working_orders, contract=snap.contract, reconnects=snap.reconnects,
            last_reconciliation_utc=snap.last_reconciliation_utc, orphan_orders=snap.orphan_orders,
            last_error=snap.last_error,
        )

    def _append_log(self, payload: Mapping[str, object]) -> None:
        self._order_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(payload)
        entry["recorded_utc"] = datetime.now(UTC).isoformat()
        with self._order_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


class UrllibRestClient:
    """Minimal real REST client for the demo endpoint (stdlib only, no secrets logged)."""

    def get(self, url: str, *, headers: Mapping[str, str], params: Mapping[str, object] | None = None) -> object:
        """Perform a GET and return decoded JSON."""
        import urllib.parse
        import urllib.request

        if params:
            url = f"{url}?{urllib.parse.urlencode({k: str(v) for k, v in params.items()})}"
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - https demo host only
            return json.loads(response.read().decode("utf-8"))

    def post(self, url: str, *, headers: Mapping[str, str], json_payload: Mapping[str, object] | None = None,
             json: Mapping[str, object] | None = None) -> object:  # noqa: A002 - protocol name
        """Perform a POST and return decoded JSON."""
        import json as json_module
        import urllib.request

        body = json_module.dumps(dict(json if json is not None else (json_payload or {}))).encode("utf-8")
        merged = {"Content-Type": "application/json", **dict(headers)}
        request = urllib.request.Request(url, data=body, headers=merged, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - https demo host only
            return json_module.loads(response.read().decode("utf-8"))


class RestPollingAckClient:
    """Order-acknowledgement client that POLLS demo REST order state.

    Honest fallback: the push user/order WebSocket is not implemented here, so
    acknowledgements come from polling ``order/item``. Slower than the push
    stream but truthful - it never fabricates an acceptance.
    """

    def __init__(self, http_client: TradovateHttpClient, token_provider: Callable[[], str],
                 *, clock: Callable[[], float] = time.time, poll_seconds: float = 0.5) -> None:
        """Create a polling ack client over an injected REST client."""
        self._http = http_client
        self._token_provider = token_provider
        self._clock = clock
        self._poll_seconds = poll_seconds

    def wait_for_acknowledgement(self, *, correlation_id: str, action: str, timeout_seconds: Decimal):
        """Poll order state until a terminal/working status or timeout."""
        from app.execution.orders import BrokerAcknowledgement

        deadline = self._clock() + float(timeout_seconds)
        message = "no status observed before timeout"
        while self._clock() < deadline:
            response = self._http.get(
                f"{TRADOVATE_DEMO_REST_BASE_URL}/order/item",
                headers={"Authorization": f"Bearer {self._token_provider()}"},
                params={"id": correlation_id},
            )
            if isinstance(response, Mapping):
                status = str(response.get("ordStatus", ""))
                if status in {"Working", "Filled", "Completed"}:
                    return BrokerAcknowledgement(action=action, correlation_id=correlation_id, accepted=True,
                                                 broker_timestamp_ns=time.time_ns(),
                                                 order_id=str(response.get("id", "")), message=status)
                if status in {"Rejected", "Canceled", "Expired"}:
                    return BrokerAcknowledgement(action=action, correlation_id=correlation_id, accepted=False,
                                                 broker_timestamp_ns=time.time_ns(),
                                                 order_id=str(response.get("id", "")), message=status)
                message = f"last status: {status or 'unknown'}"
            time.sleep(self._poll_seconds)
        return BrokerAcknowledgement(action=action, correlation_id=correlation_id, accepted=False,
                                     broker_timestamp_ns=time.time_ns(), order_id=None, message=message)


class LiveExecutionLockedError(RuntimeError):
    """Raised whenever live execution is requested while the LIVE gate is closed."""


# LIVE endpoints - used ONLY by TradovateLiveGateway, which cannot be
# constructed without an issued LiveGateApproval AND live_enabled in the
# user-controlled production config. Kept in this one place so the acceptance
# verifier can prove demo and live endpoints are separated.
TRADOVATE_LIVE_REST_BASE_URL = "https://live.tradovateapi.com/v1"
TRADOVATE_LIVE_WS_URL = "wss://live.tradovateapi.com/v1/websocket"


class TradovateLiveGateway:
    """REAL live gateway implementation, locked behind the full LIVE gate.

    Constructible only with a genuine :class:`LiveGateApproval` (issued
    exclusively by ``issue_live_gate_approval`` when EVERY requirement passes)
    AND ``live_enabled`` true in the production config, re-read at construction
    so a stale approval cannot outlive a config change. In the shipped
    configuration this always raises. Even when constructed, orders additionally
    require in-process arming (never persisted) plus a risk approval per action.
    """

    environment = ENVIRONMENT_LIVE

    def __init__(
        self,
        approval: object,
        http_client: TradovateHttpClient,
        ws_client: TradovateAckClient,
        *,
        order_log_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Verify the approval + config, then build a read-only, DISARMED gateway."""
        from app.execution.live_gate import LiveGateApproval, read_live_enabled

        if not isinstance(approval, LiveGateApproval) or not approval.decision.allowed:
            failures = getattr(getattr(approval, "decision", None), "failures",
                               ("no LiveGateApproval supplied",))
            raise LiveExecutionLockedError(
                "LIVE execution is locked. Unmet requirements: " + "; ".join(failures),
            )
        if not read_live_enabled(approval.production_config_path):
            raise LiveExecutionLockedError(
                "LIVE execution is locked: live_enabled is false in the production configuration.",
            )
        self._http = http_client
        self._ws = ws_client
        self._order_log_path = order_log_path
        self._clock = clock
        self._approval = approval
        self._token: AccessToken | None = None
        self._account: AccountRef | None = None
        self._armed = False  # in-memory only; restart always disarms

    def connect_read_only(self, credentials_token: str) -> None:
        """Attach an already-obtained LIVE token; read-only until armed."""
        self._token = AccessToken(token=credentials_token, expires_at_epoch=self._clock() + 3600)

    def select_account(self) -> AccountRef:
        """Fetch and select the LIVE account (read-only)."""
        if self._token is None:
            raise ExecutionRejectedError("not connected")
        response = self._http.get(
            f"{TRADOVATE_LIVE_REST_BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {self._token.token}"},
        )
        accounts = list(response) if isinstance(response, (list, tuple)) else []
        if not accounts:
            raise ExecutionRejectedError("no live account available")
        self._account = AccountRef(account_id=int(accounts[0]["id"]),
                                   account_spec=str(accounts[0]["name"]))
        return self._account

    def arm_live_for_this_process(self, typed_phrase: str, second_confirmation: bool) -> None:
        """Final in-process arming step; requires the typed phrase again."""
        from app.execution.live_gate import LIVE_CONFIRMATION_PHRASE

        if typed_phrase != LIVE_CONFIRMATION_PHRASE or second_confirmation is not True:
            raise LiveExecutionLockedError("live arming confirmation failed")
        self._armed = True

    def place_bracket(self, risk_approval: RiskApproval, *, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Submit one LIVE bracket - armed + risk-approved only."""
        from app.execution.orders import _require_approved

        if not self._armed:
            raise LiveExecutionLockedError("LIVE gateway is connected read-only; arming required")
        _require_approved(risk_approval)
        if self._token is None or self._account is None:
            raise ExecutionRejectedError("not connected / no account selected")
        response = self._http.post(
            f"{TRADOVATE_LIVE_REST_BASE_URL}/order/placeOSO",
            headers={"Authorization": f"Bearer {self._token.token}"},
            json=dict(payload),
        )
        self._append_log({"event": "live_bracket_placed", "response_keys": sorted(dict(response).keys())
                          if isinstance(response, Mapping) else []})
        return response if isinstance(response, Mapping) else {}

    def disarm(self) -> None:
        """Immediately revoke LIVE order permission."""
        self._armed = False

    def _append_log(self, payload: Mapping[str, object]) -> None:
        self._order_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(payload)
        entry["recorded_utc"] = datetime.now(UTC).isoformat()
        with self._order_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
