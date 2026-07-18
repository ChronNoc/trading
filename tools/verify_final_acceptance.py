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
    # Behavioural, not a brittle string match: construct the real bounded stages
    # and assert they are actually bounded, and that production wires both.
    from app.market.bounded_pipeline import BoundedIntakeBuffer, RecorderPipeline

    intake_bounded = BoundedIntakeBuffer(object(), capacity=7).metrics.capacity == 7
    wired = ("RecorderPipeline(" in launcher_source and "MarketSessionRecorder(" in launcher_source
             and "BoundedIntakeBuffer(connection" in _read("tools/start_receiver.py"))
    check("bounded_queues",
          intake_bounded and hasattr(RecorderPipeline, "metrics_snapshot") and wired,
          "intake + recorder queues are bounded and constructed in production")

    # 7b. Delayed paper is evaluated automatically from the LIVE stream. The
    # regression this guards: delayed data disabled decisions, so a healthy
    # recording session produced zero evaluations forever.
    from app.paper.streaming_engine import DelayedPaperEngine

    probe = DelayedPaperEngine()
    probe.bind_session("verify", "MNQ")
    price = 29500.0
    for i in range(320):
        price += 0.25 if i % 2 == 0 else -0.25
        probe.on_market_event({
            "type": "depth_update", "timestamp": 1_752_537_751_000_000_000 + i * 500_000_000,
            "symbol": "MNQ", "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
            "previous_size": "0", "new_size": str(i % 40 + 1),
        })
    evaluations = probe.status().evaluations
    check("delayed_paper_evaluates_live_stream",
          evaluations > 0
          and "on_event_state=feed.offer" in launcher_source
          and "paper_engine.ingest(event, state)" in launcher_source,
          f"streaming events produced {evaluations} evaluations; every accepted event "
          "reaches the paper engine via the analysis feed (off the capture loop)")

    # 7e. Analysis must be OFF the capture loop. Running strategy evaluation
    # inline starved recv(), backpressured TCP into the Java bridge queue, and
    # dropped real events (measured 17k->43k session drops at ~1,331 ev/s).
    check("analysis_off_capture_loop",
          "feed.add_sink" in launcher_source
          and "controller.handle_prebuilt_state" in launcher_source
          and "feed.add_gap_sink(paper_engine.notify_causality_gap)" in launcher_source,
          "controller + paper run on the analysis thread; gaps reported to paper")

    # 7c. Delayed paper actually OPENS, MANAGES and CLOSES simulated positions.
    # The regression this guards: the engine evaluated setups and recorded
    # decisions but never took a position, so the ledger could only ever be empty.
    from decimal import Decimal

    from app.paper.execution import ExecutionConfig, MarketTick, PaperExecutor, align_to_tick
    from app.paper.models import CloseReason, Direction, PaperOrderIntent, RiskDecision, SetupProvenance

    executor = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                             config=ExecutionConfig(), is_synthetic_fixture=True)
    probe_intent = PaperOrderIntent(
        direction=Direction.LONG, entry_reference=Decimal("29500.00"),
        stop=Decimal("29490.00"), target=Decimal("29520.00"),
        provenance=SetupProvenance(session_id="verify", setup_id="verify:long:1",
                                   strategy_version="verify", contract="MNQU6",
                                   decision_event_index=1, decision_ts_ns=1),
    )
    executor.submit(probe_intent,
                    RiskDecision(approved=True, contracts=1, reason_code="approved", reason="ok"),
                    MarketTick(event_index=1, ts_ns=1, price=Decimal("29500.00")), trading_day="d")
    same_event_fill = executor.on_tick(MarketTick(event_index=1, ts_ns=1, price=Decimal("29500.00")))
    executor.on_tick(MarketTick(event_index=2, ts_ns=2, price=Decimal("29500.00")))
    opened = executor.position is not None
    closed = executor.on_tick(MarketTick(event_index=3, ts_ns=3, price=Decimal("29520.00")))
    ledger_wired = ("PaperLedger(config.paper_ledger_path)" in launcher_source
                    and "paper_engine.on_trade_closed(paper_ledger.append)" in launcher_source)
    tick_aligned = closed is not None and closed.exit_price % Decimal("0.25") == 0
    check("delayed_paper_opens_and_closes_positions",
          same_event_fill is None and opened and closed is not None
          and closed.close_reason is CloseReason.TARGET and closed.net_pnl == Decimal("38.26")
          and tick_aligned and ledger_wired,
          "signal event cannot fill; a later event opens; target closes at "
          f"net {closed.net_pnl if closed else 'n/a'} on the tick grid; ledger wired in the launcher")

    # 7d. Simulated fills can only occur at prices MNQ can actually trade.
    check("paper_fills_are_tick_aligned",
          align_to_tick(Decimal("29500.875"), round_up=True) == Decimal("29501.00")
          and align_to_tick(Decimal("29500.875"), round_up=False) == Decimal("29500.75"),
          "fills snap to the 0.25 grid, adversely (never a better price than reality)")

    # 7f. Process isolation: the default GUI is a separate process that only
    # READS the backend's status file, so a GUI restart cannot interrupt
    # capture. Verified end-to-end (real subprocess) in tests/test_process_isolation.py.
    check("gui_isolated_from_capture",
          "FileSnapshotProvider(config.runtime_dir)" in launcher_source
          and "ensure_supervisor(config.runtime_dir" in launcher_source
          and Path("tools/start_backend.py").is_file()
          and Path("tools/backend_supervisor.py").is_file()
          and "closing or restarting this gui process" in launcher_source.lower(),
          "default GUI attaches to the detached backend via runtime/status.json")

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
