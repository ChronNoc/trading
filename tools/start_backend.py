"""The authoritative capture backend - a standalone process, no GUI attached.

    .venv\\Scripts\\python.exe -m tools.start_backend [--runtime-dir runtime]

Owns EVERYTHING stateful: the WebSocket receiver, recording, session rotation,
the analysis feed, the paper engine and ledger, and automatic research. It
publishes an atomically-replaced ``runtime/status.json`` heartbeat (encoded
AppSnapshot + PID) about twice a second; the GUI process only ever reads that
file. Closing or restarting the GUI therefore cannot interrupt capture -
structurally, not by convention.

Lifecycle contract:

* a PID lock refuses duplicate backends (repeated batch-file launches attach
  instead of double-recording); a stale lock from a crash is reported and
  cleaned - the forced-shutdown marker
* ``runtime/stop.request`` triggers the normal clean drain (recorder finalize,
  analysis-feed drain, paper flatten) - the same tested shutdown path
* on exit the final status is written (STOPPED, clean or not) and the lock is
  released
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

STATUS_INTERVAL_SECONDS = 0.5
STOP_POLL_SECONDS = 1.0


def run_backend(
    runtime_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    output_root: Path | None = None,
    report_root: Path | None = None,
    session_config: Path | None = None,
    paper_ledger_path: Path | None = None,
    log_dir: Path | None = None,
    processed_root: Path | None = None,
    labels_root: Path | None = None,
    research_state_root: Path | None = None,
    models_root: Path | None = None,
    config_fingerprint: str = "",
    max_seconds: float | None = None,
    delayed_data_minutes: int = 0,
) -> int:
    """Run the backend until a stop request (or ``max_seconds``, for tests)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.gui.snapshot_codec import encode_snapshot
    from app.gui.snapshot_source import SnapshotSource
    from app.market.analysis_feed import AnalysisFeed
    from app.market.bounded_pipeline import PipelineStateHolder
    from app.market.receiver import get_current_market_state
    from app.paper.ledger import PaperLedger
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.paper.options import (
        read_daily_limits,
        read_fixed_sizing,
        read_instrument,
        read_ml_decision_policy_enabled,
        read_momentum_enabled,
        read_stop_settings,
        read_strategy_profile,
    )
    from app.research.episode_builder import EpisodeConfig
    from app.runtime.controller import AutomaticRuntimeController
    from app.runtime.diagnostics import install_diagnostics
    from app.runtime.process_files import ProcessIdentity, SingletonLock, StatusFile, StopRequest
    from app.runtime.server_state import ReceiverStatusHolder
    from app.runtime.shutdown import ShutdownSignal
    from tools.start_assistant import (
        AssistantConfig,
        _build_feature_and_model_sinks,
        _build_research_service,
        _run_receiver_thread,
        _shutdown_receiver,
    )

    identity = ProcessIdentity.current(config_fingerprint=config_fingerprint)
    lock = SingletonLock(runtime_dir)
    result = lock.acquire(identity=identity)
    if not result.acquired:
        print(f"REFUSED: {result.reason}", file=sys.stderr, flush=True)
        return 3
    if result.stale_lock_cleaned:
        print(f"NOTE: {result.reason}", flush=True)

    explicit_roots = any(value is not None for value in (
        report_root, session_config, paper_ledger_path, log_dir,
        processed_root, labels_root, research_state_root, models_root,
    ))
    if output_root is not None and not explicit_roots:
        # A non-default output root sandboxes EVERY writable tree beside it,
        # so a test backend can never touch the real data directories.
        config = AssistantConfig.sandboxed(
            output_root, host=host, port=port, gui=False,
            delayed_data_minutes=delayed_data_minutes,
        )
    else:
        config = AssistantConfig(
            host=host,
            port=port,
            output_root=Path("data/raw") if output_root is None else output_root,
            report_root=Path("data/reports") if report_root is None else report_root,
            session_config=Path("config/session_profiles.yaml") if session_config is None else session_config,
            paper_ledger_path=(
                Path("data/paper/paper_trades.jsonl")
                if paper_ledger_path is None else paper_ledger_path
            ),
            log_dir=Path("logs") if log_dir is None else log_dir,
            processed_root=Path("data/processed") if processed_root is None else processed_root,
            labels_root=Path("data/labels") if labels_root is None else labels_root,
            research_state_root=(
                Path("data/research_state")
                if research_state_root is None else research_state_root
            ),
            models_root=Path("data/models") if models_root is None else models_root,
            gui=False,
            delayed_data_minutes=delayed_data_minutes,
            runtime_dir=runtime_dir,
        )
    logger = install_diagnostics(config.log_dir)
    logger.info("backend starting (pid=%s, runtime=%s)", __import__("os").getpid(), runtime_dir)

    controller = AutomaticRuntimeController.from_config(
        config.session_config, report_root=config.report_root,
    )
    status_holder = ReceiverStatusHolder()
    pipeline_holder = PipelineStateHolder()
    # Detached/default GUI mode must use the exact same shared loader graph as
    # in-process startup: feature scoring, paper veto policy, outcomes, and GUI
    # status all observe one instance rather than disconnected stand-ins.
    feature_sink, model_loader, outcome_tracker = _build_feature_and_model_sinks(config)
    paper_engine = DelayedPaperEngine(
        config=EpisodeConfig(
            momentum_enabled=read_momentum_enabled(Path("config/production_config.yaml")),
            ml_decision_policy_enabled=read_ml_decision_policy_enabled(
                Path("config/production_config.yaml"),
            ),
            strategy_profile=read_strategy_profile(Path("config/production_config.yaml")),
            instrument=read_instrument(Path("config/production_config.yaml")),
            **read_stop_settings(Path("config/production_config.yaml")),
            **read_daily_limits(Path("config/production_config.yaml")),
            **read_fixed_sizing(Path("config/production_config.yaml")),
        ),
        model_loader=model_loader,
    )
    paper_ledger = PaperLedger(config.paper_ledger_path)
    paper_engine.on_trade_closed(paper_ledger.append)
    from app.notify.telegram import TelegramNotifier
    _trade_notifier = TelegramNotifier()
    if _trade_notifier.enabled:
        paper_engine.on_trade_closed(_trade_notifier.notify_trade)
        print('Telegram trade alerts enabled.', flush=True)
    research_service = _build_research_service(config, controller, pipeline_holder)
    shutdown = ShutdownSignal()
    feed = AnalysisFeed(
        pressure_check=lambda: pipeline_holder.worst_queue_occupancy_fraction() > 0.25,
    )
    receiver = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, research_service, pipeline_holder,
              paper_engine, shutdown, feed, feature_sink, outcome_tracker),
        name="mnq-backend-receiver", daemon=True,
    )
    receiver.start()

    # Tradovate DEMO: backend-owned, read-only, permanently disarmed. The GUI
    # sees its status through the same snapshot and drives it only through
    # the bounded command file below - never a direct broker call.
    from app.execution.demo_service import DemoConnectionService
    from app.runtime.process_files import CommandFile

    demo_service = DemoConnectionService()
    command_file = CommandFile(runtime_dir)

    # Autonomous intelligence: propose shadow-only research candidates so the
    # Autonomous Intelligence page reflects real state. Bounded and idempotent
    # (a fixed research grid, deduplicated across restarts); it never runs gates,
    # trades, or touches the broker. Runs in a daemon thread AFTER capture is
    # set up so market recording always has priority.
    from app.paper.options import read_autonomous_enabled

    if read_autonomous_enabled(Path("config/production_config.yaml")):
        def _propose_autonomous_candidates() -> None:
            import time as _t

            _t.sleep(5.0)  # let capture initialise first (capture priority)
            try:
                from app.research.autonomous_proposer import run_proposer

                revision = __import__("subprocess").run(
                    ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                    text=True, timeout=10, check=False).stdout.strip() or "unknown"
                count = run_proposer(now_ns=time.time_ns(), software_revision=revision)
                logger.info("autonomous proposer: %s new candidate(s) (revision %s)", count, revision)
            except Exception as error:  # noqa: BLE001 - optional work never crashes the backend
                logger.warning("autonomous proposer skipped: %s", error)

        threading.Thread(target=_propose_autonomous_candidates,
                         name="mnq-autonomous-proposer", daemon=True).start()

    # Unattended-run guards: disk space under the recording root and sustained
    # writer deficit. Action order is fixed: pause research FIRST (optional
    # work never competes with recording); a critical disk is loudly surfaced
    # so the session's integrity risk is visible before flushes start failing.
    from app.runtime.disk_guard import DISK_CRITICAL, DISK_HEALTHY, DiskGuard, ThroughputGuard

    disk_guard = DiskGuard(config.output_root)
    throughput_guard = ThroughputGuard()
    last_disk_check = 0.0
    disk_state = disk_guard.check()
    source = SnapshotSource(
        controller=controller, pipeline_holder=pipeline_holder,
        research_service=research_service, receiver_status=status_holder.snapshot,
        market_state=get_current_market_state, paper_engine=paper_engine,
        analysis_feed=feed, feature_sink=feature_sink, demo_service=demo_service,
        models_root=config.models_root,
        model_loader=model_loader, outcome_tracker=outcome_tracker,
    )
    status = StatusFile(runtime_dir)
    stop = StopRequest(runtime_dir)

    # Publish the ACTUAL bound socket (port 0 resolves at bind time) so an
    # attaching GUI or harness knows where Bookmap should connect.
    import json as _json

    bind_deadline = time.monotonic() + 30
    receiver_bound = False
    while time.monotonic() < bind_deadline:
        if not receiver.is_alive():
            break
        binding = status_holder.snapshot()
        if binding.listening:
            (runtime_dir / "binding.json").write_text(
                _json.dumps({"host": host, "port": binding.port}), encoding="utf-8",
            )
            receiver_bound = True
            break
        time.sleep(0.1)
    if not receiver_bound:
        reason = "receiver exited before binding" if not receiver.is_alive() else "receiver bind deadline exceeded"
        logger.error(reason)
        clean = _shutdown_receiver(shutdown, receiver, timeout=20.0)
        try:
            status.write(
                encode_snapshot(source()),
                state="FAILED_BIND",
                identity=identity,
            )
        finally:
            lock.release()
        return 2 if clean else 1

    started = time.monotonic()
    clean = True
    status_failures = 0
    try:
        while True:
            if not receiver.is_alive():
                logger.error("authoritative receiver thread exited unexpectedly")
                clean = False
                try:
                    status.write(
                        encode_snapshot(source()),
                        state="FAILED_RECEIVER",
                        identity=identity,
                    )
                except Exception as error:  # noqa: BLE001
                    logger.error("failed to publish receiver failure: %s", error)
                break
            try:
                status.write(encode_snapshot(source()), identity=identity)
                status_failures = 0
            except Exception as error:  # noqa: BLE001 - a bad heartbeat must not kill capture
                status_failures += 1
                logger.error("status write failed: %s", error)
                if status_failures >= 3:
                    logger.error("status publication failed three consecutive times; restarting backend")
                    clean = False
                    break
            now = time.monotonic()
            if now - last_disk_check >= 10.0:
                last_disk_check = now
                previous_level = disk_state.level
                disk_state = disk_guard.check()
                pipe = pipeline_holder.snapshot()
                ingress = int(pipe.recorder.get("ingress", 0)) if pipe.recorder else 0
                persisted = int(pipe.recorder.get("egress", 0)) if pipe.recorder else 0
                writer_behind = throughput_guard.observe(ingress, persisted)
                if disk_state.level != DISK_HEALTHY or writer_behind:
                    if research_service is not None and hasattr(research_service, "request_pause"):
                        research_service.request_pause()
                    logger.warning(
                        "capture protection: disk=%s (%s) writer_deficit=%.0f ev/s%s",
                        disk_state.level, disk_state.detail,
                        throughput_guard.last_deficit_per_second,
                        " - research paused" if research_service is not None else "",
                    )
                elif previous_level != DISK_HEALTHY and disk_state.level == DISK_HEALTHY:
                    if research_service is not None and hasattr(research_service, "resume"):
                        research_service.resume()
                    logger.info("capture protection cleared: %s", disk_state.detail)
                if disk_state.level == DISK_CRITICAL:
                    controller.health.record_event(
                        "disk", "critical", disk_state.detail,
                    )
            command = command_file.consume()
            if command is not None:
                # Executed on the backend; idempotent by command_id inside the
                # service, so a re-delivered file can never double-execute.
                outcome = demo_service.handle_command({
                    "command_id": command.get("command_id", ""),
                    "name": command.get("name", ""),
                    **dict(command.get("args", {}) or {}),
                })
                logger.info("execution command %s -> %s",
                            command.get("name"), outcome.get("result", outcome.get("error")))
            if stop.pending():
                logger.info("stop requested; draining")
                break
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                break
            time.sleep(STATUS_INTERVAL_SECONDS)
        drained = _shutdown_receiver(shutdown, receiver, timeout=20.0)
        clean = clean and drained
    finally:
        try:
            status.write(
                encode_snapshot(source()),
                state="STOPPED_CLEAN" if clean else "STOPPED_UNCLEAN",
                identity=identity,
            )
        except Exception as error:  # noqa: BLE001
            failure_path = runtime_dir / "final_status_failure.txt"
            failure_path.write_text(
                f"{time.time()} {type(error).__name__}: {error}",
                encoding="utf-8",
            )
        demo_service.stop()  # always exits DISCONNECTED and DISARMED
        stop.clear()
        lock.release()
        logger.info("backend stopped (clean=%s)", clean)
    return 0 if clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI wrapper."""
    parser = argparse.ArgumentParser(description="MNQ capture backend (no GUI).")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--report-root", type=Path, default=None)
    parser.add_argument("--session-config", type=Path, default=None)
    parser.add_argument("--paper-ledger-path", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--processed-root", type=Path, default=None)
    parser.add_argument("--labels-root", type=Path, default=None)
    parser.add_argument("--research-state-root", type=Path, default=None)
    parser.add_argument("--models-root", type=Path, default=None)
    parser.add_argument("--config-fingerprint", default="")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="exit after this long (integration tests only)")
    parser.add_argument("--delayed-data-minutes", type=int, default=0)
    args = parser.parse_args(argv)
    return run_backend(
        args.runtime_dir,
        host=args.host,
        port=args.port,
        output_root=args.output_root,
        report_root=args.report_root,
        session_config=args.session_config,
        paper_ledger_path=args.paper_ledger_path,
        log_dir=args.log_dir,
        processed_root=args.processed_root,
        labels_root=args.labels_root,
        research_state_root=args.research_state_root,
        models_root=args.models_root,
        config_fingerprint=args.config_fingerprint,
        max_seconds=args.max_seconds,
        delayed_data_minutes=args.delayed_data_minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
