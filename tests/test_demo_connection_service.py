"""Backend-owned Tradovate DEMO service: read-only, secret-free, idempotent.

All transports are fakes - no test may talk to a real broker. The confirmed
defect these pin: the launched GUI showed hardcoded execution state while the
real gateway sat unused; now the backend owns the gateway and the GUI sees its
real status through the snapshot and drives it only via the command file.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.execution.demo_service import (
    ARMING_BLOCKERS,
    CREDENTIAL_VARIABLES,
    STATE_CONNECTED_READONLY,
    STATE_DISCONNECTED,
    STATE_ERROR,
    DemoConnectionService,
    credential_checklist,
)

SECRET = "hunter2-super-secret"  # noqa: S105 - fake value for redaction tests


@dataclass
class _FakeSnapshot:
    connected: bool = True
    authenticated: bool = True
    account_spec: str = "DEMO12345"
    balance: str = "52000.00"
    position_net: int = 0
    working_orders: int = 0
    contract: str = "MNQZ6"
    reconnects: int = 0
    orphan_orders: int = 0
    last_error: str = ""


@dataclass
class _FakeGateway:
    """Mimics TradovateDemoGateway's read-only surface."""

    fail_sync: bool = False
    calls: list[str] = field(default_factory=list)
    snapshot: _FakeSnapshot = field(default_factory=_FakeSnapshot)

    def connect(self):  # noqa: ANN201
        self.calls.append("connect")
        return self.snapshot

    def select_account(self):  # noqa: ANN201
        self.calls.append("select_account")
        return object()

    def synchronize(self, **_kwargs):  # noqa: ANN201, ANN003
        self.calls.append("synchronize")
        if self.fail_sync:
            raise RuntimeError(f"broker down (token {SECRET})")
        return self.snapshot

    def disconnect(self):  # noqa: ANN201
        self.calls.append("disconnect")
        return self.snapshot


def _credentialed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.setenv(name, SECRET if "SECRET" in name else f"value-{name}")


def _service(gateway: _FakeGateway) -> DemoConnectionService:
    return DemoConnectionService(gateway_factory=lambda: gateway,
                                 sync_interval_seconds=3600.0)


def test_starts_disconnected_disarmed_with_checklist() -> None:
    status = _service(_FakeGateway()).status()
    assert status.state == STATE_DISCONNECTED
    assert status.connected is False
    assert status.arming_blockers == ARMING_BLOCKERS
    assert tuple(name for name, _ in status.credential_checklist) == CREDENTIAL_VARIABLES


def test_connect_requires_credentials_and_names_the_missing_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    service = _service(_FakeGateway())
    outcome = service.handle_command({"command_id": "c1", "name": "connect_readonly"})
    assert outcome["ok"] is False
    assert "TRADOVATE_DEMO_USERNAME" in str(outcome["error"])
    assert service.status().state in (STATE_DISCONNECTED, STATE_ERROR)


def test_connect_readonly_publishes_real_gateway_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentialed(monkeypatch)
    gateway = _FakeGateway()
    service = _service(gateway)
    outcome = service.handle_command({"command_id": "c1", "name": "connect_readonly"})
    assert outcome["ok"] is True, outcome
    status = service.status()
    assert status.state == STATE_CONNECTED_READONLY
    assert status.selected_account == "DEMO12345"
    assert status.balance == "52000.00"
    assert status.contract == "MNQZ6"
    assert status.sync_age_seconds is not None
    assert gateway.calls[:3] == ["connect", "select_account", "synchronize"]
    service.stop()


def test_duplicate_command_id_is_acknowledged_not_reexecuted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentialed(monkeypatch)
    gateway = _FakeGateway()
    service = _service(gateway)
    service.handle_command({"command_id": "same", "name": "connect_readonly"})
    connects_after_first = gateway.calls.count("connect")
    duplicate = service.handle_command({"command_id": "same", "name": "connect_readonly"})
    assert duplicate.get("duplicate") is True
    assert gateway.calls.count("connect") == connects_after_first, "no re-execution"
    service.stop()


def test_secrets_never_appear_in_status_or_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker error embeds a credential value; the status must redact it."""
    _credentialed(monkeypatch)
    gateway = _FakeGateway()
    service = _service(gateway)
    service.handle_command({"command_id": "c1", "name": "connect_readonly"})
    gateway.fail_sync = True
    service.handle_command({"command_id": "c2", "name": "sync_now"})
    status = service.status()
    import dataclasses

    dumped = json.dumps(dataclasses.asdict(status), default=str)
    assert SECRET not in dumped, "credential values must never reach any snapshot"
    assert "<TRADOVATE_DEMO_SECRET>" in status.last_error
    assert status.state == STATE_ERROR
    service.stop()


def test_sync_failure_degrades_only_this_panel_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentialed(monkeypatch)
    gateway = _FakeGateway()
    service = _service(gateway)
    service.handle_command({"command_id": "c1", "name": "connect_readonly"})
    gateway.fail_sync = True
    assert service.handle_command({"command_id": "c2", "name": "sync_now"})["ok"] is False
    assert service.status().state == STATE_ERROR
    gateway.fail_sync = False
    assert service.handle_command({"command_id": "c3", "name": "sync_now"})["ok"] is True
    assert service.status().state == STATE_CONNECTED_READONLY
    service.stop()


def test_disconnect_returns_to_clean_disconnected_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentialed(monkeypatch)
    service = _service(_FakeGateway())
    service.handle_command({"command_id": "c1", "name": "connect_readonly"})
    outcome = service.handle_command({"command_id": "c2", "name": "disconnect"})
    assert outcome["ok"] is True
    status = service.status()
    assert status.state == STATE_DISCONNECTED
    assert status.connected is False
    assert status.selected_account == ""


def test_unknown_command_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(_FakeGateway())
    outcome = service.handle_command({"command_id": "c1", "name": "place_order"})
    assert outcome["ok"] is False
    assert "unknown command" in str(outcome["error"])


def test_checklist_reports_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADOVATE_DEMO_USERNAME", SECRET)
    monkeypatch.delenv("TRADOVATE_DEMO_PASSWORD", raising=False)
    listed = dict(credential_checklist())
    assert listed["TRADOVATE_DEMO_USERNAME"] is True
    assert listed["TRADOVATE_DEMO_PASSWORD"] is False
    assert SECRET not in json.dumps(credential_checklist())


def test_demo_service_never_imports_paper_or_strategy() -> None:
    """Delayed-data isolation: the broker side cannot reach the paper side."""
    import ast

    tree = ast.parse(Path("app/execution/demo_service.py").read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    offenders = [n for n in names if "app.paper" in n or "app.strategy" in n]
    assert not offenders, offenders


# --- the command file channel ------------------------------------------------------


def test_command_file_round_trip_and_consume_once(tmp_path: Path) -> None:
    from app.runtime.process_files import CommandFile

    channel = CommandFile(tmp_path)
    command_id = channel.submit("connect_readonly", {"a": 1})
    payload = channel.consume()
    assert payload is not None
    assert payload["command_id"] == command_id
    assert payload["name"] == "connect_readonly"
    assert payload["args"] == {"a": 1}
    assert channel.consume() is None, "a command is consumed exactly once"


def test_backend_polls_the_command_file() -> None:
    source = Path("tools/start_backend.py").read_text(encoding="utf-8")
    assert "command_file.consume()" in source
    assert "demo_service.handle_command(" in source
    assert "demo_service.stop()" in source, "backend exit must leave DEMO disconnected"


def test_gui_commander_writes_off_the_calling_thread(tmp_path: Path) -> None:
    from app.gui.backend_commands import ExecutionCommander

    commander = ExecutionCommander(tmp_path)
    commander.submit("sync_now")
    deadline = time.monotonic() + 5
    from app.runtime.process_files import CommandFile

    payload = None
    while time.monotonic() < deadline and payload is None:
        payload = CommandFile(tmp_path).consume()
        time.sleep(0.02)
    assert payload is not None and payload["name"] == "sync_now"
