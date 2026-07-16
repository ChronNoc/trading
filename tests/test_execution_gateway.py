"""Tests for the gateway boundary: paper isolation, demo lifecycle, LIVE lock."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.gateway import (
    AccessToken,
    CredentialsMissingError,
    DemoCredentials,
    TradovateDemoGateway,
    TradovateLiveGateway,
    authenticate_demo,
)
from app.execution.live_gate import (
    LIVE_CONFIRMATION_PHRASE,
    ArmingState,
    LiveGateInputs,
    SecondConfirmationSummary,
    evaluate_live_gate,
    read_live_enabled,
)
from app.execution.orders import ExecutionRejectedError
from app.execution.paper_gateway import PaperExecutionGateway
from app.execution.prop_rules import PropRuleProfile


@dataclass(frozen=True)
class _Approval:
    allowed: bool = True
    reason: str = "test approval"


class MockHttpClient:
    """Scripted REST client: URL suffix -> response."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def _lookup(self, url: str) -> object:
        for suffix, response in self.routes.items():
            if url.rstrip("?").split("?")[0].endswith(suffix):
                return response
        raise AssertionError(f"unexpected URL: {url}")

    def get(self, url, *, headers, params=None):
        self.calls.append(("GET", url))
        return self._lookup(url)

    def post(self, url, *, headers, json):
        self.calls.append(("POST", url))
        assert "password" not in str(headers), "credentials must never leak into headers logs"
        return self._lookup(url)


class MockAckClient:
    """Always-accepting acknowledgement client."""

    def wait_for_acknowledgement(self, *, correlation_id, action, timeout_seconds):
        from app.execution.orders import BrokerAcknowledgement

        return BrokerAcknowledgement(action=action, correlation_id=correlation_id, accepted=True,
                                     broker_timestamp_ns=1, order_id=correlation_id, message="Working")


def _credentials() -> DemoCredentials:
    return DemoCredentials(username="u", password="p", app_id="a", app_version="1", cid="c", secret="s")


def _gateway(tmp_path: Path, routes: dict[str, object], *, clock=None) -> TradovateDemoGateway:
    http = MockHttpClient(routes)
    ticks = {"now": 1000.0}

    def _clock() -> float:
        return ticks["now"] if clock is None else clock()

    gateway = TradovateDemoGateway(
        http, MockAckClient(), order_log_path=tmp_path / "orders.jsonl",
        credentials_loader=_credentials, clock=_clock,
    )
    gateway._ticks = ticks  # test hook for advancing time
    return gateway


_AUTH = {"auth/accesstokenrequest": {"accessToken": "tok-1", "expirationTime_seconds": 3600}}
_ACCOUNTS = {"account/list": [{"id": 7, "name": "DEMO7"}]}


def test_credentials_come_from_environment_and_missing_is_loud(monkeypatch) -> None:
    for name in ("TRADOVATE_DEMO_USERNAME", "TRADOVATE_DEMO_PASSWORD", "TRADOVATE_DEMO_APP_ID",
                 "TRADOVATE_DEMO_APP_VERSION", "TRADOVATE_DEMO_CID", "TRADOVATE_DEMO_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CredentialsMissingError):
        DemoCredentials.from_environment()
    assert "redacted" in repr(_credentials())  # secrets never appear in repr


def test_authenticate_and_token_refresh(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {**_AUTH, **_ACCOUNTS})
    gateway.connect()
    assert gateway.snapshot().authenticated is True
    token_before = gateway._token
    gateway._ticks["now"] += 3600.0  # past expiry -> refresh on next use
    gateway.ensure_token()
    assert gateway._token is not token_before  # a new token was requested
    assert "redacted" in repr(gateway._token)


def test_connect_is_read_only_until_armed(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {**_AUTH, **_ACCOUNTS})
    gateway.connect()
    from app.execution.brackets import BracketOrderRequest
    from app.execution.orders import AccountRef

    request = BracketOrderRequest(
        account=AccountRef(account_id=7, account_spec="DEMO7"), symbol="MNQU6",
        expected_contract_symbol="MNQU6", action="Buy", quantity=1, entry_order_type="Market",
        entry_price=None, stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"),
    )
    with pytest.raises(ExecutionRejectedError, match="read-only"):
        gateway.place_bracket(_Approval(), request=request, signal_key="s1")


def test_duplicate_signal_is_debounced(tmp_path: Path) -> None:
    routes = {
        **_AUTH, **_ACCOUNTS,
        "position/list": [],
        "order/placeOrder": {"orderId": 101},
        "order/item": {"id": 101, "ordStatus": "Working"},
    }
    gateway = _gateway(tmp_path, routes)
    gateway.connect()
    gateway.select_account()
    gateway.arm_demo()
    from app.execution.brackets import BracketOrderRequest
    from app.execution.orders import AccountRef

    request = BracketOrderRequest(
        account=AccountRef(account_id=7, account_spec="DEMO7"), symbol="MNQU6",
        expected_contract_symbol="MNQU6", action="Buy", quantity=1, entry_order_type="Market",
        entry_price=None, stop_price=Decimal("29441.25"), target_price=Decimal("29471.25"),
    )
    first = gateway.place_bracket(_Approval(), request=request, signal_key="setup-A")
    assert first.entry_order_id
    with pytest.raises(ExecutionRejectedError, match="debounced"):
        gateway.place_bracket(_Approval(), request=request, signal_key="setup-A")


def test_sync_detects_orphan_orders_and_cancel_all_flatten(tmp_path: Path) -> None:
    routes = {
        **_AUTH, **_ACCOUNTS,
        "cashBalance/getcashbalancesnapshot": {"totalCashValue": "50000", "buyingPower": "48000"},
        "position/list": [{"accountId": 7, "netPos": 2, "contractId": 55}],
        "order/list": [{"id": 999, "ordStatus": "Working", "accountId": 7}],
        "order/cancelOrder": {"commandId": 5},
        "order/item": {"id": 5, "ordStatus": "Working"},
        "contract/find": {"id": 55, "name": "MNQU6"},
        "order/placeOrder": {"orderId": 77},
    }
    gateway = _gateway(tmp_path, routes)
    gateway.connect()
    gateway.synchronize()
    snap = gateway.snapshot()
    assert snap.balance == "50000" and snap.position_net == 2
    assert snap.working_orders == 1
    assert snap.orphan_orders == 1  # order 999 was never placed by this gateway
    assert snap.last_reconciliation_utc

    # cancel_all only touches orders it placed - the orphan is reported, not adopted.
    assert gateway.cancel_all(_Approval()) == 0
    log = (tmp_path / "orders.jsonl").read_text(encoding="utf-8")
    assert "orphan_order_detected" in log


def test_disconnect_mid_order_leaves_disarmed_and_reconnect_counts(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {**_AUTH, **_ACCOUNTS})
    gateway.connect()
    gateway.arm_demo()
    gateway.disconnect()  # e.g. transport died mid-order
    snap = gateway.snapshot()
    assert snap.connected is False and snap.armed is False
    gateway.record_reconnect()
    gateway.connect()
    assert gateway.snapshot().reconnects == 1
    # After reconnect the gateway is read-only again until explicitly re-armed.
    assert gateway.snapshot().armed is False


def test_paper_gateway_records_and_is_structurally_isolated(tmp_path: Path) -> None:
    gateway = PaperExecutionGateway(tmp_path / "paper.jsonl")
    assert gateway.can_submit_real_orders is False
    record = gateway.place_bracket(session_id="s", setup_id="x", direction="long", contracts=2,
                                   entry=Decimal("29451.25"), stop=Decimal("29441.25"),
                                   target=Decimal("29471.25"))
    assert record.status == "simulated_submitted"
    lines = (tmp_path / "paper.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["order_id"] == record.order_id
    # Structural proof: the paper module imports nothing broker-shaped.
    tree = ast.parse(Path("app/execution/paper_gateway.py").read_text(encoding="utf-8"))
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    banned = ("orders", "brackets", "gateway", "live", "tradovate", "urllib", "requests", "websocket")
    assert not any(any(b in name.lower() for b in banned) for name in imported), imported


def test_live_gateway_locked_without_approval_and_real_with_one(tmp_path: Path) -> None:
    """LIVE is a REAL gateway, but constructible only via an issued approval."""
    from app.execution.gateway import LiveExecutionLockedError
    from app.execution.live_gate import issue_live_gate_approval

    # 1. Garbage or a bare decision never constructs the gateway.
    with pytest.raises(LiveExecutionLockedError):
        TradovateLiveGateway(object(), MockHttpClient({}), MockAckClient(),
                             order_log_path=tmp_path / "live.jsonl")
    with pytest.raises(LiveExecutionLockedError):
        TradovateLiveGateway(evaluate_live_gate(_all_true_inputs()), MockHttpClient({}), MockAckClient(),
                             order_log_path=tmp_path / "live.jsonl")

    # 2. The SHIPPED config cannot even issue an approval (live_enabled false).
    with pytest.raises(PermissionError, match="live_enabled is false"):
        issue_live_gate_approval(_all_true_inputs(),
                                 production_config_path=Path("config/production_config.yaml"),
                                 account_spec="LIVE1")

    # 3. A user-enabled config + fully passed gate constructs a READ-ONLY gateway
    #    (mocked clients; no network). Orders still require in-process arming.
    enabled = tmp_path / "production_config.yaml"
    enabled.write_text("live_enabled: true\n", encoding="utf-8")
    approval = issue_live_gate_approval(_all_true_inputs(), production_config_path=enabled,
                                        account_spec="LIVE1")
    live_routes = {"account/list": [{"id": 1, "name": "LIVE1"}]}
    gateway = TradovateLiveGateway(approval, MockHttpClient(live_routes), MockAckClient(),
                                   order_log_path=tmp_path / "live.jsonl")
    gateway.connect_read_only("live-token")
    assert gateway.select_account().account_spec == "LIVE1"
    from app.execution.gateway import LiveExecutionLockedError as Locked

    with pytest.raises(Locked, match="read-only"):
        gateway.place_bracket(_Approval(), payload={"symbol": "MNQU6"})
    # Wrong phrase never arms; correct phrase + second confirmation does.
    with pytest.raises(Locked):
        gateway.arm_live_for_this_process("arm live", True)
    gateway.arm_live_for_this_process(LIVE_CONFIRMATION_PHRASE, True)
    gateway.disarm()  # returns to safe state; nothing persisted anywhere


def _all_true_inputs() -> LiveGateInputs:
    values = {f.name: True for f in fields(LiveGateInputs) if f.type == "bool"}
    values["typed_confirmation_phrase"] = LIVE_CONFIRMATION_PHRASE
    values["second_confirmation_acknowledged"] = True
    return LiveGateInputs(**values)


@pytest.mark.parametrize("missing", [f.name for f in fields(LiveGateInputs) if f.type == "bool"])
def test_live_gate_rejects_each_missing_requirement(missing: str) -> None:
    """Turning off ANY single requirement blocks LIVE arming."""
    inputs = replace(_all_true_inputs(), **{missing: False})
    decision = evaluate_live_gate(inputs)
    assert decision.allowed is False
    assert decision.failures  # a specific reason is always given


def test_live_gate_requires_exact_typed_phrase() -> None:
    decision = evaluate_live_gate(replace(_all_true_inputs(), typed_confirmation_phrase="arm live"))
    assert decision.allowed is False
    assert any("confirmation phrase" in f for f in decision.failures)


def test_startup_is_always_disarmed_and_arming_needs_passed_gate() -> None:
    arming = ArmingState()  # fresh construction == application startup
    assert arming.live_armed is False and arming.demo_armed is False
    with pytest.raises(PermissionError):
        arming.arm_live(evaluate_live_gate(LiveGateInputs()))
    arming.arm_live(evaluate_live_gate(_all_true_inputs()))
    assert arming.live_armed is True
    arming.disarm_all()
    assert arming.live_armed is False


def test_shipped_production_config_keeps_live_disabled() -> None:
    assert read_live_enabled(Path("config/production_config.yaml")) is False
    assert read_live_enabled(Path("config/missing.yaml")) is False


def test_prop_rules_unresolved_blocks_and_gate_reflects_it() -> None:
    profile = PropRuleProfile.load(Path("config/prop_rules_lucid.yaml"))
    assert profile.blocks_automated_execution is True
    assert profile.unresolved_fields  # nothing was guessed
    decision = evaluate_live_gate(replace(_all_true_inputs(), prop_rules_resolved=False))
    assert any("prop-firm rule" in f for f in decision.failures)


def test_prop_rules_resolved_profile_unblocks(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "profile_version: '2'\nfirm: Lucid Trading\nsource_url: https://example/rules\n"
        "retrieval_date: '2026-07-16'\naccount_type: 50k\naccount_size: '50000'\n"
        "drawdown_method: eod\ndaily_loss_rule: '1000'\nmax_contracts: '5'\n"
        "consistency_rule: '40%'\npermitted_instruments: MNQ\n"
        "permitted_trading_times: RTH\nnews_restrictions: none\novernight_rules: flat\n"
        "prohibited: none\neffective_date: '2026-07-01'\nresolved: true\n",
        encoding="utf-8",
    )
    profile = PropRuleProfile.load(path)
    assert profile.blocks_automated_execution is False
    assert profile.permitted_instruments == "MNQ"  # new fields parsed


def test_profile_listing_discovers_importable_profiles(tmp_path: Path) -> None:
    """A verified profile dropped into config needs zero code changes to select."""
    from app.execution.prop_rules import list_profiles

    (tmp_path / "prop_rules_lucid.yaml").write_text("firm: Lucid Trading\n", encoding="utf-8")
    profile_dir = tmp_path / "prop_profiles"
    profile_dir.mkdir()
    (profile_dir / "lucid_50k_verified.yaml").write_text("firm: Lucid Trading\n", encoding="utf-8")
    names = [p.name for p in list_profiles(tmp_path)]
    assert names == ["prop_rules_lucid.yaml", "lucid_50k_verified.yaml"]


def test_second_confirmation_summary_displays_all_risk_fields() -> None:
    summary = SecondConfirmationSummary(
        account_spec="DEMO7", contract="MNQU6", max_contracts=2,
        stop=Decimal("29441.25"), target=Decimal("29471.25"), worst_case_risk=Decimal("42.48"),
    )
    text = " ".join(summary.display_lines())
    for token in ("DEMO7", "MNQU6", "2", "29441.25", "29471.25", "42.48"):
        assert token in text


def test_research_and_paper_paths_cannot_import_broker_modules() -> None:
    """Structural proof over every research module: no broker import path exists."""
    banned = ("app.execution.orders", "app.execution.brackets", "app.execution.gateway",
              "app.execution.live_execution", "tradovate")
    for module_path in Path("app/research").glob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            for name in names:
                assert not any(b in name for b in banned), f"{module_path}: {name}"
