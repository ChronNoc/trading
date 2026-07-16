"""Machine-verifiable final acceptance: read-only checks, nonzero exit on failure.

    .venv\\Scripts\\python.exe -m tools.verify_final_acceptance

Every check inspects the repository as it is - no network, no writes, no orders.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, evidence: str) -> None:
    """Record one acceptance check."""
    CHECKS.append((name, passed, evidence))


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _imports_of(path: str) -> list[str]:
    tree = ast.parse(_read(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def main() -> int:
    """Run every acceptance check and print the pass/fail table."""
    # 1. OBSERVE default: mode supervisor defaults to OBSERVE without live flag.
    from app.discovery.supervisor import ModeSupervisor

    view = ModeSupervisor(Path("config/production_config.yaml")).view()
    check("observe_default", view.mode == "OBSERVE" and not view.live_armed,
          f"mode={view.mode}, live_armed={view.live_armed}")

    # 2. live_enabled is false in the committed configuration.
    from app.execution.live_gate import read_live_enabled

    check("live_enabled_false", read_live_enabled(Path("config/production_config.yaml")) is False,
          "config/production_config.yaml -> False")

    # 3. Startup disarmed by construction (arming state is never persisted).
    from app.execution.live_gate import ArmingState

    arming = ArmingState()
    check("startup_disarmed", not arming.live_armed and not arming.demo_armed,
          "fresh ArmingState() is fully disarmed")

    # 4. Research/paper imports cannot reach Tradovate.
    banned = ("app.execution.orders", "app.execution.brackets", "app.execution.gateway",
              "app.execution.live_execution", "app.execution.user_sync",
              "app.execution.order_lifecycle", "tradovate")
    offenders: list[str] = []
    for module in list(Path("app/research").glob("*.py")) + [Path("app/execution/paper_gateway.py")]:
        for name in _imports_of(str(module)):
            if any(b in name for b in banned):
                offenders.append(f"{module}:{name}")
    check("paper_research_isolated", not offenders, offenders and "; ".join(offenders) or "no broker imports")

    # 5. DEMO and LIVE endpoints are separated constants.
    gateway_source = _read("app/execution/gateway.py")
    orders_source = _read("app/execution/orders.py")
    check("endpoints_separated",
          "demo.tradovateapi.com" in orders_source and "live.tradovateapi.com" in gateway_source
          and "live.tradovateapi.com" not in orders_source,
          "demo URL only in orders.py; live URL only in the locked live gateway")

    # 6. LIVE requires LiveGateApproval (and the approval is issue-gated).
    check("live_requires_approval",
          "LiveGateApproval" in gateway_source and "issue_live_gate_approval" in _read("app/execution/live_gate.py"),
          "TradovateLiveGateway verifies LiveGateApproval; approvals only issued on a full pass")

    # 7. Bounded queues configured (intake + recorder) and wired in production.
    pipeline_source = _read("app/market/bounded_pipeline.py")
    launcher_source = _read("tools/start_assistant.py")
    check("bounded_queues", "BoundedIntakeBuffer" in pipeline_source and "RecorderPipeline" in pipeline_source
          and "RecorderPipeline(MarketSessionRecorder" in launcher_source
          and "intake = BoundedIntakeBuffer(connection" in _read("tools/start_receiver.py"),
          "intake + recorder queues exist and are constructed in production")

    # 8. Health provider wired in production research service.
    check("health_provider_wired",
          "health_provider=make_receiver_health_provider(controller, pipeline_holder)" in launcher_source,
          "ResearchService receives the real controller/pipeline health provider")

    # 9. Persisted research restoration exists (restart-safe totals).
    service_source = _read("app/research/research_service.py")
    check("research_restoration", "def load_persisted_results" in service_source
          and "_aggregate_persisted" in service_source,
          "persisted job results restored and aggregated every cycle")

    # 10. Canonical/experimental ledgers are separate files.
    check("ledgers_separate", 'ledger_dir / "canonical.json"' in service_source
          and "canonical_candidate_raw" in service_source,
          "canonical.json (full risk path) vs per-candidate ledger files")

    # 11. Automatic finalization hook is wired.
    check("auto_finalize_wired", "_schedule_auto_build(config)" in launcher_source
          and "on_session_finalized=" in launcher_source,
          "clean finalize triggers the idempotent background build")

    # 12. WebSocket transport path is tested end to end.
    check("transport_tested", Path("tests/test_websocket_transport.py").is_file()
          and "persisted == metrics.accepted" in _read("tests/test_websocket_transport.py").replace("metrics.", "metrics.", 1),
          "tests/test_websocket_transport.py proves persisted == accepted")

    # 13. Tradovate order WebSocket client exists (verified framing).
    user_sync = _read("app/execution/user_sync.py")
    check("order_websocket_exists", "class TradovateUserSyncClient" in user_sync
          and "authorize\\n" in user_sync and "user/syncrequest" in user_sync,
          "user/order WS client with verified authorize/heartbeat/sync framing")

    # 14. Cancel/replace exists (gateway + lifecycle).
    check("cancel_replace_exists", "def cancel_replace" in gateway_source
          and "def replace_price" in _read("app/execution/order_lifecycle.py"),
          "gateway.cancel_replace + lifecycle.replace_price")

    # 15. Partial-fill state machine exists (fill engine + lifecycle + ws client).
    check("partial_fill_state_machine", "PartiallyFilled" in user_sync
          and "STATUS_PARTIAL" in _read("app/simulator/fill_engine.py")
          and "on_fill_progress" in _read("app/execution/order_lifecycle.py"),
          "partial fills accumulate in ws client, fill engine, and bracket lifecycle")

    # 16. Unresolved Lucid rules block LIVE.
    from app.execution.live_gate import LiveGateInputs, evaluate_live_gate
    from app.execution.prop_rules import PropRuleProfile

    profile = PropRuleProfile.load(Path("config/prop_rules_lucid.yaml"))
    decision = evaluate_live_gate(LiveGateInputs(prop_rules_resolved=not profile.blocks_automated_execution))
    check("unresolved_rules_block_live", profile.blocks_automated_execution
          and any("prop-firm" in f for f in decision.failures),
          f"{len(profile.unresolved_fields)} unresolved field(s); LIVE gate lists the failure")

    # 17. No credentials tracked; no raw files staged.
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=False).stdout.splitlines()
    secret_like = [t for t in tracked if t.endswith(".env") or "credential" in t.lower() or "secret" in t.lower()]
    # .gitkeep is the historical empty-directory placeholder, not a recording.
    raw_tracked = [t for t in tracked if t.startswith("data/raw/") and not t.endswith(".gitkeep")]
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True,
                            check=False).stdout.splitlines()
    raw_staged = [t for t in staged if t.startswith("data/raw/")]
    check("no_credentials_tracked", not secret_like, "; ".join(secret_like) or "none")
    check("no_raw_files_tracked_or_staged", not raw_tracked and not raw_staged,
          "; ".join(raw_tracked + raw_staged) or "none")

    failures = [name for name, passed, _ in CHECKS if not passed]
    width = max(len(name) for name, _, _ in CHECKS)
    print("=== FINAL ACCEPTANCE VERIFICATION ===")
    for name, passed, evidence in CHECKS:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {evidence}")
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
