"""Backend-owned Tradovate DEMO connection service (read-only by default).

The confirmed defect this fixes: the launched eight-screen GUI showed a
hardcoded PAPER/disconnected execution panel while a real, tested
`TradovateDemoGateway` sat unused. The GUI must never talk to a broker; this
service lives in the BACKEND process, owns the gateway, and publishes a typed,
secret-free status that rides the same runtime/status.json snapshot the GUI
already reads. The GUI's only influence is a bounded, idempotent command file.

Hard rules, enforced here and by tests:

* Starts DISCONNECTED and permanently DISARMED. Read-only connection never
  requires profitability, and nothing in this service can place an order.
* Credentials are loaded from the environment ONLY at connect time, never
  stored on the service, never logged, never present in any status snapshot -
  the checklist exposes present/missing booleans for the variable NAMES only.
* Every command carries a command_id; a repeated id is acknowledged but not
  re-executed (duplicate clicks and retried files are safe).
* Sync runs on the service's own thread with bounded backoff; a broker outage
  degrades THIS panel only - capture, paper, and research never depend on it.
* Delayed market data remains structurally unable to reach broker execution:
  this module never imports the paper or strategy packages, and order arming
  is not implemented here at all.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

STATE_DISCONNECTED = "DISCONNECTED"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED_READONLY = "CONNECTED_READONLY"
STATE_ERROR = "ERROR"

SYNC_INTERVAL_SECONDS = 10.0
BACKOFF_INITIAL_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
REMEMBERED_COMMANDS = 64

CREDENTIAL_VARIABLES = (
    "TRADOVATE_DEMO_USERNAME",
    "TRADOVATE_DEMO_PASSWORD",
    "TRADOVATE_DEMO_APP_ID",
    "TRADOVATE_DEMO_APP_VERSION",
    "TRADOVATE_DEMO_CID",
    "TRADOVATE_DEMO_SECRET",
)

# Order arming is NOT provided by this service. These are the standing reasons,
# shown verbatim in the GUI so "why can't I arm?" always has an exact answer.
ARMING_BLOCKERS = (
    "DEMO order arming is not enabled in this build (read-only connection only)",
    "delayed market data may never drive broker orders",
    "arming resets on every process start and reconnect",
    "deterministic risk approval and fresh user confirmation are required",
)


@dataclass(frozen=True, slots=True)
class DemoStatus:
    """Secret-free view of the DEMO connection for the GUI snapshot."""

    state: str = STATE_DISCONNECTED
    connected: bool = False
    authenticated: bool = False
    accounts: tuple[str, ...] = ()
    selected_account: str = ""
    needs_account_selection: bool = False
    balance: str = "unknown"
    position_net: int = 0
    working_orders: int = 0
    contract: str = ""
    reconnects: int = 0
    orphan_orders: int = 0
    last_sync_unix: float = 0.0
    last_error: str = ""
    last_command_id: str = ""
    last_command_result: str = ""
    credential_checklist: tuple[tuple[str, bool], ...] = ()
    arming_blockers: tuple[str, ...] = ARMING_BLOCKERS

    @property
    def sync_age_seconds(self) -> float | None:
        """Seconds since the last successful synchronize, or None if never."""
        if self.last_sync_unix <= 0:
            return None
        return max(0.0, time.time() - self.last_sync_unix)


def credential_checklist() -> tuple[tuple[str, bool], ...]:
    """Which credential variables are present. Names and booleans ONLY."""
    return tuple((name, bool(os.environ.get(name))) for name in CREDENTIAL_VARIABLES)


def credentials_present() -> bool:
    """Whether every required DEMO credential variable is set."""
    return all(present for _, present in credential_checklist())


@dataclass(slots=True)
class _Session:
    """Internal live-connection state (never serialized)."""

    gateway: object
    accounts: tuple[str, ...] = ()
    selected_account: str = ""


class DemoConnectionService:
    """Owns the DEMO gateway inside the backend; the GUI only sees status."""

    def __init__(
        self,
        *,
        gateway_factory: Callable[[], object] | None = None,
        sync_interval_seconds: float = SYNC_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a disconnected, disarmed service.

        ``gateway_factory`` builds a connected-capable gateway on demand; tests
        inject fakes, production uses :func:`default_gateway_factory`. The
        factory is only invoked on an explicit connect command, so credentials
        are read only at connection time.
        """
        self._factory = gateway_factory or default_gateway_factory
        self._interval = sync_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._session: _Session | None = None
        self._status = DemoStatus(credential_checklist=credential_checklist())
        self._seen_commands: dict[str, str] = {}
        self._seen_order: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = BACKOFF_INITIAL_SECONDS

    # -- the GUI-visible read model -------------------------------------------

    def status(self) -> DemoStatus:
        """Return the current secret-free status (safe from any thread)."""
        with self._lock:
            return self._status

    # -- the bounded command surface -------------------------------------------

    def handle_command(self, command: dict[str, object]) -> dict[str, object]:
        """Execute one GUI command exactly once (idempotent by command_id)."""
        command_id = str(command.get("command_id", "")).strip()
        name = str(command.get("name", "")).strip()
        if not command_id or not name:
            return {"ok": False, "error": "command requires command_id and name"}
        with self._lock:
            if command_id in self._seen_commands:
                # Duplicate delivery: acknowledge the ORIGINAL outcome, do not redo.
                return {"ok": True, "duplicate": True,
                        "result": self._seen_commands[command_id]}
        try:
            result = self._dispatch(name, command)
            outcome = {"ok": True, "result": result}
        except Exception as error:  # noqa: BLE001 - a broker fault must not kill the backend
            result = f"{type(error).__name__}: {error}"
            outcome = {"ok": False, "error": result}
            self._set(state=STATE_ERROR, last_error=_redact(result))
        self._remember(command_id, result)
        self._set(last_command_id=command_id, last_command_result=_redact(str(result)))
        return outcome

    def _dispatch(self, name: str, command: dict[str, object]) -> str:
        if name == "connect_readonly":
            return self._connect_readonly()
        if name == "disconnect":
            return self._disconnect()
        if name == "select_account":
            return self._select_account(str(command.get("account", "")))
        if name == "sync_now":
            return self._sync_once()
        raise ValueError(f"unknown command {name!r}")

    # -- lifecycle ---------------------------------------------------------------

    def _connect_readonly(self) -> str:
        if self._session is not None:
            return "already connected"
        self._set(state=STATE_CONNECTING, last_error="",
                  credential_checklist=credential_checklist())
        if not credentials_present():
            missing = [name for name, present in credential_checklist() if not present]
            self._set(state=STATE_DISCONNECTED)
            raise RuntimeError(f"missing credentials: {', '.join(missing)}")
        gateway = self._factory()
        gateway.connect()  # type: ignore[attr-defined]
        # The gateway resolves the demo account (first available) and then
        # synchronizes balance/position/working orders in one read-only pass.
        gateway.select_account()  # type: ignore[attr-defined]
        snapshot = gateway.synchronize()  # type: ignore[attr-defined]
        selected = str(getattr(snapshot, "account_spec", "") or "")
        self._session = _Session(gateway=gateway, accounts=(selected,) if selected else (),
                                 selected_account=selected)
        self._publish_gateway(snapshot, accounts=self._session.accounts,
                              selected=selected, state=STATE_CONNECTED_READONLY)
        self._set(last_sync_unix=time.time())
        self._start_sync_thread()
        return "connected read-only"

    def _disconnect(self) -> str:
        session, self._session = self._session, None
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None
        if session is not None:
            try:
                session.gateway.disconnect()  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001 - disconnect best-effort
                self._set(last_error=_redact(f"disconnect: {error}"))
        self._set(state=STATE_DISCONNECTED, connected=False, authenticated=False,
                  accounts=(), selected_account="", needs_account_selection=False,
                  balance="unknown", position_net=0, working_orders=0)
        return "disconnected"

    def _select_account(self, account: str) -> str:
        # The current gateway API resolves the first demo account itself and
        # exposes no enumeration endpoint; a multi-account chooser would be
        # invented plumbing. Documented limitation, honest error here.
        session = self._session
        if session is None:
            raise RuntimeError("not connected")
        if account and account != session.selected_account:
            raise ValueError(
                f"only the gateway-resolved account {session.selected_account!r} "
                "is available (multi-account selection is not supported yet)"
            )
        return f"account {session.selected_account} selected"

    def _sync_once(self) -> str:
        session = self._session
        if session is None:
            raise RuntimeError("not connected")
        snapshot = session.gateway.synchronize()  # type: ignore[attr-defined]
        self._publish_gateway(snapshot, accounts=session.accounts,
                              selected=session.selected_account,
                              state=STATE_CONNECTED_READONLY)
        self._set(last_sync_unix=time.time())
        return "synchronized"

    # -- the background sync loop -------------------------------------------------

    def _start_sync_thread(self) -> None:
        self._stop.clear()
        self._backoff = BACKOFF_INITIAL_SECONDS
        self._thread = threading.Thread(target=self._sync_loop,
                                        name="tradovate-demo-sync", daemon=True)
        self._thread.start()

    def _sync_loop(self) -> None:
        while not self._stop.wait(self._interval):
            if self._session is None:
                return
            try:
                self._sync_once()
                self._backoff = BACKOFF_INITIAL_SECONDS
            except Exception as error:  # noqa: BLE001 - degrade THIS panel only
                self._set(state=STATE_ERROR, last_error=_redact(str(error)))
                if self._stop.wait(self._backoff):
                    return
                self._backoff = min(BACKOFF_MAX_SECONDS, self._backoff * 2)

    def stop(self) -> None:
        """Shut the service down (backend exit); always leaves DISARMED state."""
        try:
            self._disconnect()
        except Exception:  # noqa: BLE001,S110
            pass

    # -- helpers -------------------------------------------------------------------

    def _publish_gateway(self, snapshot: object, *, accounts: tuple[str, ...],
                         selected: str, state: str) -> None:
        self._set(
            state=state,
            connected=bool(getattr(snapshot, "connected", False)),
            authenticated=bool(getattr(snapshot, "authenticated", False)),
            accounts=accounts,
            selected_account=selected,
            needs_account_selection=len(accounts) > 1 and not selected,
            balance=str(getattr(snapshot, "balance", "unknown")),
            position_net=int(getattr(snapshot, "position_net", 0) or 0),
            working_orders=int(getattr(snapshot, "working_orders", 0) or 0),
            contract=str(getattr(snapshot, "contract", "")),
            reconnects=int(getattr(snapshot, "reconnects", 0) or 0),
            orphan_orders=int(getattr(snapshot, "orphan_orders", 0) or 0),
            last_error=_redact(str(getattr(snapshot, "last_error", ""))),
        )

    def _set(self, **fields: object) -> None:
        with self._lock:
            self._status = replace(self._status, **fields)  # type: ignore[arg-type]

    def _remember(self, command_id: str, result: str) -> None:
        with self._lock:
            self._seen_commands[command_id] = result
            self._seen_order.append(command_id)
            while len(self._seen_order) > REMEMBERED_COMMANDS:
                self._seen_commands.pop(self._seen_order.pop(0), None)


def _redact(text: str) -> str:
    """Strip any credential VALUE that could leak through an error message."""
    redacted = text
    for name in CREDENTIAL_VARIABLES:
        value = os.environ.get(name)
        if value:
            redacted = redacted.replace(value, f"<{name}>")
    return redacted


def default_gateway_factory() -> object:
    """Build the real gateway; credentials load lazily at connect() time."""
    from pathlib import Path

    from app.execution.gateway import (
        RestPollingAckClient,
        TradovateDemoGateway,
        UrllibRestClient,
    )

    http = UrllibRestClient()
    return TradovateDemoGateway(
        http, RestPollingAckClient(http),
        order_log_path=Path("data/execution_state/demo_orders.jsonl"),
    )
