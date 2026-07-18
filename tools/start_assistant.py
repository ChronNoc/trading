"""One-click automatic SHADOW runtime launcher for the MNQ assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.daily_learning import analyze_and_write_daily_learning_report
from app.market.feed_guard import FeedGuard, FeedGuardConfig
from app.runtime.controller import AutomaticRuntimeController
from app.runtime.server_state import ReceiverStatusHolder
from tools.start_receiver import ReceiverServerConfig, start_receiver_websocket_server, startup_message

DEFAULT_CONFIG_PATH = Path("config/session_profiles.yaml")
DEFAULT_REPORT_ROOT = Path("data/reports")
# The paper ledger is user-owned data: append-only and never overwritten.
DEFAULT_PAPER_LEDGER = Path("data/paper/paper_trades.jsonl")
# How long the app waits for capture to flush before reporting an unclean exit.
SHUTDOWN_DRAIN_SECONDS = 10.0
# Crash/hang evidence. The app was reported as crashing with nothing logged.
DEFAULT_LOG_DIR = Path("logs")
# A GUI unresponsive this long is a real stall worth a full thread dump.
GUI_STALL_SECONDS = 12.0


class AssistantStartupError(RuntimeError):
    """User-facing startup failure."""


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """Configuration for the automatic assistant launcher."""

    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/bookmap"
    output_root: Path = Path("data/raw")
    report_root: Path = DEFAULT_REPORT_ROOT
    session_config: Path = DEFAULT_CONFIG_PATH
    gui: bool = True
    delayed_data_minutes: int = 0
    paper_ledger_path: Path = DEFAULT_PAPER_LEDGER
    log_dir: Path = DEFAULT_LOG_DIR
    # Process isolation: the default GUI attaches to a detached backend via
    # runtime/ files. --in-process keeps everything in one process (dev/tests).
    in_process: bool = False
    runtime_dir: Path = Path("runtime")

    @property
    def receiver_config(self) -> ReceiverServerConfig:
        """Return the receiver-server configuration."""
        return ReceiverServerConfig(
            host=self.host,
            port=self.port,
            path=self.path,
            output_root=self.output_root,
        )


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root containing this launcher."""
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "AGENTS.md").exists() and (candidate / "tools" / "start_assistant.py").exists():
            return candidate
    raise AssistantStartupError("Could not locate the mnq_bot repository root.")


def validate_runtime_environment(repo_root: Path, *, gui: bool) -> None:
    """Raise a clear error when required runtime dependencies are missing."""
    missing: list[str] = []
    for module_name in ("yaml", "websockets", "pyarrow"):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(module_name)
    if gui:
        try:
            __import__("PySide6")
        except ImportError:
            missing.append("PySide6")

    if missing:
        packages = ", ".join(sorted(set(missing)))
        raise AssistantStartupError(
            "Missing Python runtime packages: "
            f"{packages}. From the repo root, install the app into .venv before starting.",
        )

    if not (repo_root / "config" / "session_profiles.yaml").exists():
        raise AssistantStartupError("Missing config/session_profiles.yaml.")


def resilient_handler(
    handler: Callable[[dict[str, object]], None],
    label: str,
) -> Callable[[dict[str, object]], None]:
    """Wrap a controller callback so one bad event cannot kill the session.

    Recording happens upstream of these callbacks, so a controller error
    must never tear down the WebSocket connection and lose the recording
    session. The first error prints a full traceback; afterwards every
    500th error prints one summary line.
    """
    error_count = 0

    def wrapped(event: dict[str, object]) -> None:
        nonlocal error_count
        try:
            handler(event)
        except Exception as error:  # noqa: BLE001 - deliberate resilience boundary
            error_count += 1
            if error_count == 1:
                print(f"{label} handler error (recording continues):", file=sys.stderr, flush=True)
                traceback.print_exc()
            elif error_count % 500 == 0:
                print(
                    f"{label} handler error #{error_count}: {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    return wrapped


async def run_headless_assistant(
    config: AssistantConfig,
    controller: AutomaticRuntimeController,
    *,
    status_holder: ReceiverStatusHolder | None = None,
    research_service: object | None = None,
    pipeline_holder: object | None = None,
    paper_engine_holder: object | None = None,
    shutdown: object | None = None,
    analysis_feed: object | None = None,
) -> None:
    """Run the receiver/recorder/controller service until interrupted.

    Once the WebSocket server has genuinely bound its socket (recording can
    start), the actual bind state is published and - if a research service is
    attached and recording health is good - automatic research resumes without
    any button press.
    """
    from app.runtime.diagnostics import install_asyncio_handler

    # An unhandled error inside the receiver's loop must be logged, not vanish.
    install_asyncio_handler(asyncio.get_running_loop())
    controller.start()
    delayed_events = _delayed_control_events(config.delayed_data_minutes)
    for event in delayed_events:
        controller.handle_control_event(event)
    guard_source_mode = "delayed" if config.delayed_data_minutes > 0 else "live"
    feed_guard = FeedGuard(FeedGuardConfig(source_mode=guard_source_mode))
    from app.market.bounded_pipeline import RecorderPipeline

    # The delayed-paper engine evaluates the LIVE stream causally. Delayed data
    # must never reach a broker, but it must absolutely be evaluated on paper -
    # conflating those two rules is what left the engine idle while recording.
    paper_engine = paper_engine_holder
    if paper_engine is not None:
        paper_engine.bind_session("pending", "MNQ")

    # ALL analysis (controller context + paper evaluation) runs on the feed's
    # dedicated thread. Running it inline on this loop starved recv(), which
    # backpressured TCP into the Java bridge queue and dropped real events -
    # the measured 17k->43k session-drop failure. The loop now only parses,
    # guards, records, and offers.
    from app.market.analysis_feed import AnalysisFeed

    def _capture_pressure() -> bool:
        # Recording always outranks analysis: pause paper work while the
        # recorder/intake queues are filling so the writer wins the GIL.
        if pipeline_holder is None:
            return False
        try:
            return pipeline_holder.worst_queue_occupancy_fraction() > 0.25  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - a broken gauge must not stall analysis
            return False

    feed = analysis_feed if analysis_feed is not None else AnalysisFeed(
        pressure_check=_capture_pressure,
    )
    # The controller feeds GUI context and lifecycle - consumers read 1-2 Hz
    # snapshots, so driving it per event (session resolve + zoneinfo each
    # time) was pure waste that slowed the whole analysis thread. Coalesce
    # to 10 Hz of market time; control events still reach it directly.
    last_controller_ns = [0]

    def _controller_sink(event: dict[str, object], state: object) -> None:
        ts = getattr(state, "timestamp_ns", 0)
        if ts - last_controller_ns[0] >= 100_000_000:  # 100 ms market time
            last_controller_ns[0] = ts
            controller.handle_prebuilt_state(event, state)

    feed.add_sink(_controller_sink)
    if paper_engine is not None:
        feed.add_sink(lambda event, state: paper_engine.ingest(event, state))
        feed.add_gap_sink(paper_engine.notify_causality_gap)
    feed.start()

    def _make_session_pipeline() -> RecorderPipeline:
        # Bounded recorder stage: a dedicated writer thread persists batches so
        # a slow GUI or research burst can never stall capture. Metrics are real.
        recorder = MarketSessionRecorder(root_dir=config.output_root)
        # Bind the REAL session id the moment recording starts, so the GUI, the
        # paper engine, the ledger, and the logs never show "unknown".
        if paper_engine is not None:
            paper_engine.bind_session(recorder.session_id, recorder.symbol or "MNQ")
        # events_prevalidated: everything reaching this pipeline came out of
        # parse_stream_message; re-validating per event in the writer thread
        # (a JSON round-trip each) made the writer the throughput ceiling.
        return RecorderPipeline(recorder, events_prevalidated=True)

    def _pipelined_recorder() -> MarketSessionRecorder:
        # A session invalidated by data loss must not keep swallowing clean
        # events forever (the real failure: drops climbed 17k -> 43k in ONE
        # session). The rotator finalizes the damaged session once the
        # pipeline has been healthy for a quiet window and starts a fresh one;
        # the damaged session keeps its full loss accounting and stays
        # research-ineligible. Paper is told at the FIRST drop - bridge drops
        # mean its tape already has holes - so it flattens and re-warms.
        from app.runtime.session_rotation import RotatingRecorder

        return RotatingRecorder(  # type: ignore[return-value]
            _make_session_pipeline,
            on_rotated_out=lambda old: _finalize_assistant_session(
                controller, config, old, research_service, paper_engine,
            ),
            on_damage=(paper_engine.notify_causality_gap if paper_engine is not None else None),
        )

    server = await start_receiver_websocket_server(
        config.receiver_config,
        on_event_state=feed.offer,
        on_control_event=resilient_handler(
            lambda event: controller.handle_control_event(event),
            "control-event",
        ),
        recorder_factory=_pipelined_recorder,
        initial_control_events=delayed_events,
        on_session_finalized=lambda recorder: _finalize_assistant_session(
            controller, config, recorder, research_service, paper_engine,
        ),
        feed_guard=feed_guard,
        on_connection_started=(pipeline_holder.attach if pipeline_holder is not None else None),  # type: ignore[union-attr]
    )
    actual_config = ReceiverServerConfig(
        host=config.host,
        port=server.port,
        path=config.path,
        output_root=config.output_root,
    )
    if status_holder is not None:
        status_holder.mark_bound(config.host, server.port)
    print("MNQ Assistant running in SHADOW mode.", flush=True)
    if config.delayed_data_minutes > 0:
        print(
            f"Bookmap delayed data mode: {config.delayed_data_minutes} minutes. "
            "Recording only; shadow decisions disabled.",
            flush=True,
        )
    print(startup_message(actual_config), flush=True)
    # Recording health is good once the socket is bound and the recorder is ready;
    # resume automatic research in the background (it yields to the receiver).
    if research_service is not None and hasattr(research_service, "start"):
        try:
            research_service.start()
            print("Automatic paper research resumed (background; data capture has priority).", flush=True)
        except Exception as error:  # pragma: no cover - service must never break recording
            print(f"Automatic research could not start: {error}", file=sys.stderr, flush=True)
    # A future the GUI can resolve from its own thread. Without this the loop
    # waits forever and the daemon thread is killed at process exit, losing the
    # tail of the recording and skipping finalization entirely.
    stop_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    if shutdown is not None:
        shutdown.bind(asyncio.get_running_loop(), stop_future)  # type: ignore[attr-defined]
    drain_error = ""
    try:
        await stop_future
    finally:
        try:
            if status_holder is not None:
                status_holder.mark_unbound()
            if research_service is not None and hasattr(research_service, "stop"):
                research_service.stop()
            # Drain queued analysis first so the paper engine has seen every
            # event it is going to see, THEN close any open simulated position -
            # the ledger reflects what the simulation actually knows.
            feed.stop()
            if paper_engine is not None and hasattr(paper_engine, "flatten"):
                paper_engine.flatten()  # type: ignore[attr-defined]
            controller.stop()
            await server.close()
        except Exception as error:  # noqa: BLE001 - a drain failure must be reported
            drain_error = f"{type(error).__name__}: {error}"
            raise
        finally:
            if shutdown is not None:
                shutdown.mark_drained(drain_error)  # type: ignore[attr-defined]


def make_receiver_health_provider(
    controller: AutomaticRuntimeController,
    pipeline_holder: object | None = None,
):
    """Build the REAL receiver-health provider for research throttling.

    Reports the delta of current-session bridge queue drops since the last check
    (any new drop stops research), and marks capture unhealthy when a connected
    Bookmap feed has gone stale. It never fabricates queue/lag numbers the
    receiver does not measure.
    """
    from app.research.auto_research import ReceiverHealth

    last_seen = {"drops": 0}

    def provider() -> ReceiverHealth:
        current = controller.health.current_session_dropped_message_count
        delta = max(0, current - last_seen["drops"])
        last_seen["drops"] = current
        lag_ms = 0
        if controller.health.bookmap_connected and not (controller.data_delay_minutes or 0):
            import time as _time

            # Wall-clock staleness is only meaningful for a REAL-TIME entitlement.
            # Bookmap's delayed feed carries event timestamps ~15 minutes behind
            # wall clock, so comparing them here always looked "stale" and pinned
            # research at zero workers forever. Delayed feeds are judged by drops
            # and queue pressure only.
            if controller.health.is_data_stale(_time.time_ns()):
                lag_ms = 1000
        occupancy = 0.0
        if pipeline_holder is not None:
            # REAL measured queue pressure from the bounded capture pipeline.
            occupancy = float(pipeline_holder.worst_queue_occupancy_fraction())  # type: ignore[attr-defined]
        return ReceiverHealth(queue_occupancy=occupancy, current_session_drops_delta=delta, lag_ms=lag_ms)

    return provider


def _build_research_service(
    config: AssistantConfig,
    controller: AutomaticRuntimeController,
    pipeline_holder: object | None = None,
) -> object | None:
    """Create the persistent research service wired to REAL receiver health."""
    try:
        from app.research.research_service import ResearchService

        return ResearchService(
            config.output_root,
            Path("data/processed"),
            state_dir=Path("data/research_state"),
            health_provider=make_receiver_health_provider(controller, pipeline_holder),
        )
    except Exception as error:  # pragma: no cover - never block the app on research
        print(f"Research service unavailable: {error}", file=sys.stderr, flush=True)
        return None


def run_assistant(config: AssistantConfig) -> int:
    """Run the automatic assistant with an optional GUI."""
    repo_root = find_repo_root()
    validate_runtime_environment(repo_root, gui=config.gui)
    # Install BEFORE anything can fail: a crash, a dying worker thread, or a
    # frozen UI must leave evidence in logs/ instead of vanishing.
    from app.runtime.diagnostics import install_diagnostics

    logger = install_diagnostics(config.log_dir)
    logger.info("assistant starting (gui=%s, port=%s)", config.gui, config.port)

    if config.gui and not config.in_process:
        # PROCESS ISOLATION (the default). The capture backend runs as its own
        # detached process (started here if absent, attached if already alive -
        # repeated launches never double-record) and the GUI only READS its
        # runtime/status.json heartbeat. Closing or restarting this GUI process
        # cannot interrupt capture, recording, paper, or research: the GUI holds
        # no socket, thread, or object of the backend's.
        from tools.backend_supervisor import ensure_backend

        if not ensure_backend(config.runtime_dir, port=config.port,
                              delayed_data_minutes=config.delayed_data_minutes):
            print("Backend did not start; see runtime/backend.out", file=sys.stderr, flush=True)
            return 2
        from app.gui.file_snapshot import FileSnapshotProvider

        exit_code = _run_gui(
            AutomaticRuntimeController.from_config(
                config.session_config, report_root=config.report_root,
            ),
            provider=FileSnapshotProvider(config.runtime_dir),
        )
        print(
            "GUI closed. The capture backend is still running and recording.\n"
            "  status: python -m tools.backend_supervisor --status\n"
            "  stop:   python -m tools.backend_supervisor --stop",
            flush=True,
        )
        return exit_code

    controller = AutomaticRuntimeController.from_config(
        config.session_config,
        report_root=config.report_root,
    )
    status_holder = ReceiverStatusHolder()
    from app.market.bounded_pipeline import PipelineStateHolder

    pipeline_holder = PipelineStateHolder()
    from app.paper.ledger import PaperLedger
    from app.paper.streaming_engine import DelayedPaperEngine

    # Automatic by construction: the engine is created at startup and fed by the
    # receiver. No button, no finalized session, no user action required.
    paper_engine = DelayedPaperEngine()
    # Every closed simulated trade is appended to the durable ledger. Opening an
    # existing ledger continues it - a prior run's trades are never overwritten.
    paper_ledger = PaperLedger(config.paper_ledger_path)
    paper_engine.on_trade_closed(paper_ledger.append)
    research_service = _build_research_service(config, controller, pipeline_holder)

    if not config.gui:
        try:
            asyncio.run(run_headless_assistant(
                config, controller, status_holder=status_holder,
                research_service=research_service, pipeline_holder=pipeline_holder,
                paper_engine_holder=paper_engine,
            ))
        except KeyboardInterrupt:
            controller.stop()
            return 0
        return 0

    from app.market.analysis_feed import AnalysisFeed
    from app.runtime.shutdown import ShutdownSignal

    shutdown = ShutdownSignal()
    # The feed is created HERE so the GUI can display its conservation counters
    # (offered/processed/skipped, lag) alongside intake/recorder metrics.
    analysis_feed = AnalysisFeed(
        pressure_check=lambda: (
            pipeline_holder.worst_queue_occupancy_fraction() > 0.25
        ),
    )
    receiver_thread = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, research_service, pipeline_holder,
              paper_engine, shutdown, analysis_feed),
        name="mnq-assistant-receiver",
        daemon=True,
    )
    receiver_thread.start()
    try:
        return _run_gui(controller, status_holder, research_service, pipeline_holder,
                        paper_engine, analysis_feed)
    finally:
        # The window is gone; drain capture instead of letting process exit kill
        # the daemon thread mid-write.
        _shutdown_receiver(shutdown, receiver_thread)


def _shutdown_receiver(shutdown: object, thread: threading.Thread,
                       timeout: float = SHUTDOWN_DRAIN_SECONDS) -> bool:
    """Ask capture to stop and wait a bounded time for it to really drain.

    Returns whether the drain completed. An incomplete drain is REPORTED, never
    silently ignored: it means recorded data may be missing its tail.
    """
    shutdown.request_stop()  # type: ignore[attr-defined]
    drained = shutdown.wait_for_drain(timeout)  # type: ignore[attr-defined]
    thread.join(timeout=timeout)
    if not drained or thread.is_alive():
        detail = shutdown.drain_error or f"capture did not drain within {timeout:.0f}s"  # type: ignore[attr-defined]
        print(f"WARNING: unclean shutdown - {detail}. "
              "The last recorded events may not have been flushed.",
              file=sys.stderr, flush=True)
        return False
    return True


def parse_args(argv: Sequence[str] | None = None) -> AssistantConfig:
    """Parse command-line arguments into an assistant config."""
    parser = argparse.ArgumentParser(description="Start the MNQ assistant in automatic SHADOW mode.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/bookmap")
    parser.add_argument("--output-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--session-config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-gui", action="store_true", help="Run recording/controller services without the GUI.")
    parser.add_argument("--in-process", action="store_true",
                        help="Legacy single-process mode (GUI owns capture). Default is the "
                             "isolated backend + attached GUI.")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument(
        "--delayed-data-minutes",
        type=int,
        default=0,
        help="Mark incoming Bookmap data as delayed and force recording-only mode.",
    )
    args = parser.parse_args(argv)
    if args.port <= 0:
        raise AssistantStartupError("--port must be greater than zero.")
    if not str(args.path).startswith("/"):
        raise AssistantStartupError("--path must start with /.")
    if args.delayed_data_minutes < 0:
        raise AssistantStartupError("--delayed-data-minutes must be zero or greater.")
    return AssistantConfig(
        host=str(args.host),
        port=int(args.port),
        path=str(args.path),
        output_root=Path(args.output_root),
        report_root=Path(args.report_root),
        session_config=Path(args.session_config),
        gui=not bool(args.no_gui),
        delayed_data_minutes=int(args.delayed_data_minutes),
        in_process=bool(args.in_process),
        runtime_dir=Path(args.runtime_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the automatic assistant."""
    try:
        config = parse_args(argv)
        return run_assistant(config)
    except AssistantStartupError as error:
        print(f"MNQ Assistant could not start: {error}", file=sys.stderr)
        return 2


def _run_receiver_thread(
    config: AssistantConfig,
    controller: AutomaticRuntimeController,
    status_holder: ReceiverStatusHolder,
    research_service: object | None,
    pipeline_holder: object | None = None,
    paper_engine: object | None = None,
    shutdown: object | None = None,
    analysis_feed: object | None = None,
) -> None:
    try:
        asyncio.run(run_headless_assistant(
            config, controller, status_holder=status_holder,
            research_service=research_service, pipeline_holder=pipeline_holder,
            paper_engine_holder=paper_engine, shutdown=shutdown,
            analysis_feed=analysis_feed,
        ))
    except Exception as error:  # pragma: no cover - defensive service boundary
        controller.health.record_event("receiver", "failed", str(error))
        if shutdown is not None:
            # A crashed receiver must release the GUI's drain wait immediately.
            shutdown.mark_drained(str(error))  # type: ignore[attr-defined]


def _run_gui(
    controller: AutomaticRuntimeController,
    status_holder: ReceiverStatusHolder | None = None,
    research_service: object | None = None,
    pipeline_holder: object | None = None,
    paper_engine: object | None = None,
    analysis_feed: object | None = None,
    provider: object | None = None,
) -> int:
    try:
        from PySide6.QtWidgets import QApplication

        from app.gui.app_window import AppWindow
        from app.gui.snapshot_source import SnapshotSource
    except ImportError as error:
        controller.health.mark_gui_failed(str(error))
        raise AssistantStartupError("PySide6 is not installed; run with --no-gui or install the GUI dependency.") from error

    from app.market.receiver import get_current_market_state

    app = QApplication.instance() or QApplication(sys.argv)
    # The redesigned eight-screen window is the default GUI. It renders only
    # immutable snapshots, so the Qt thread never competes with capture.
    window = AppWindow(
        snapshot_provider=provider if provider is not None else SnapshotSource(
            controller=controller,
            pipeline_holder=pipeline_holder,
            research_service=research_service,
            receiver_status=status_holder.snapshot if status_holder is not None else None,
            market_state=get_current_market_state,
            paper_engine=paper_engine,
            analysis_feed=analysis_feed,
        ),
    )
    window.show()
    # A frozen UI crashes nothing, so nothing is logged and the window just stops
    # repainting. This heartbeat is the only way that becomes diagnosable: if the
    # Qt thread stops pumping it, every thread's stack is dumped to the log.
    from PySide6.QtCore import QTimer

    from app.runtime.diagnostics import StallWatchdog

    watchdog = StallWatchdog(stall_seconds=GUI_STALL_SECONDS)
    watchdog.heartbeat()
    heartbeat_timer = QTimer(window)
    heartbeat_timer.timeout.connect(watchdog.heartbeat)
    heartbeat_timer.start(1000)
    watchdog.start()
    try:
        return int(app.exec())
    except Exception as error:  # pragma: no cover - GUI event loop defensive boundary
        controller.health.mark_gui_failed(str(error))
        raise
    finally:
        watchdog.stop()


def _finalize_assistant_session(
    controller: AutomaticRuntimeController,
    config: AssistantConfig,
    recorder: MarketSessionRecorder,
    research_service: object | None = None,
    paper_engine: object | None = None,
) -> None:
    """Write reports and auto-build episodes when one Bookmap session ends cleanly."""
    session_date = recorder.session_start_utc.astimezone(UTC).date()
    manifest = _read_manifest(recorder.manifest_path)
    report_dir = controller.finalize_session_report(
        session_id=recorder.session_id,
        session_date=session_date,
        recorder_manifest=manifest,
    )
    _write_paper_daily_report(config, session_date, paper_engine, controller)
    # Automatically schedule the idempotent episode build for the finalized
    # session in the background (never opens an active session). The research
    # service will then pick up the new build on its next batch.
    if recorder.finalized and recorder.clean_shutdown:
        _schedule_auto_build(config)
    try:
        paths = analyze_and_write_daily_learning_report(
            config.output_root,
            config.report_root,
            session_date,
        )
    except Exception as error:  # pragma: no cover - defensive reporting boundary
        controller.health.record_event("learning", "failed", f"daily learning report failed: {error}")
        return
    controller.health.record_event(
        "learning",
        "daily_summary",
        f"daily learning summary written: {paths.markdown_path}",
    )
    print(f"Session report written: {report_dir}", flush=True)
    print(f"Daily learning summary written: {paths.markdown_path}", flush=True)


def _write_paper_daily_report(
    config: AssistantConfig,
    session_date: object,
    paper_engine: object | None,
    controller: AutomaticRuntimeController,
) -> None:
    """Generate the automatic paper-trading daily report from the real ledger."""
    try:
        from app.paper.daily_report import write_paper_daily_report
        from app.risk.account_profile import load_selected_profile

        status = paper_engine.status() if paper_engine is not None else None  # type: ignore[union-attr]
        report_root = Path(config.report_root) / "paper"
        markdown = write_paper_daily_report(
            report_root, str(session_date), config.paper_ledger_path,
            starting_balance=load_selected_profile().account_size,
            engine_status=status,
        )
        print(f"Paper trading report written: {markdown}", flush=True)
    except Exception as error:  # pragma: no cover - reporting must never break capture
        controller.health.record_event("paper_report", "failed", f"paper report failed: {error}")


def _schedule_auto_build(config: AssistantConfig) -> None:
    """Run the idempotent finalized-session builder on a background daemon thread."""
    try:
        from app.research.build_orchestrator import schedule_pending_builds

        schedule_pending_builds(config.output_root, Path("data/processed"), Path("data/labels"))
    except Exception as error:  # pragma: no cover - reporting must not break recording
        print(f"Automatic episode build could not start: {error}", file=sys.stderr, flush=True)


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _delayed_control_events(delay_minutes: int) -> tuple[dict[str, object], ...]:
    if delay_minutes <= 0:
        return ()
    return (
        {
            "type": "delayed_mode",
            "timestamp_ns": int(datetime.now(UTC).timestamp()) * 1_000_000_000,
            "source_mode": "delayed",
            "delay_minutes": delay_minutes,
            "reason": "Bookmap free delayed data feed",
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
