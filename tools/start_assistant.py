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
) -> None:
    """Run the receiver/recorder/controller service until interrupted.

    Once the WebSocket server has genuinely bound its socket (recording can
    start), the actual bind state is published and - if a research service is
    attached and recording health is good - automatic research resumes without
    any button press.
    """
    controller.start()
    delayed_events = _delayed_control_events(config.delayed_data_minutes)
    for event in delayed_events:
        controller.handle_control_event(event)
    guard_source_mode = "delayed" if config.delayed_data_minutes > 0 else "live"
    feed_guard = FeedGuard(FeedGuardConfig(source_mode=guard_source_mode))
    server = await start_receiver_websocket_server(
        config.receiver_config,
        on_market_event=resilient_handler(
            lambda event: controller.handle_market_event(event),
            "market-event",
        ),
        on_control_event=resilient_handler(
            lambda event: controller.handle_control_event(event),
            "control-event",
        ),
        initial_control_events=delayed_events,
        on_session_finalized=lambda recorder: _finalize_assistant_session(
            controller, config, recorder, research_service,
        ),
        feed_guard=feed_guard,
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
    try:
        await asyncio.Future()
    finally:
        if status_holder is not None:
            status_holder.mark_unbound()
        if research_service is not None and hasattr(research_service, "stop"):
            research_service.stop()
        controller.stop()
        await server.close()


def _build_research_service(config: AssistantConfig) -> object | None:
    """Create the persistent research service, or None if unavailable."""
    try:
        from app.research.research_service import ResearchService

        return ResearchService(
            config.output_root,
            Path("data/processed"),
            state_dir=Path("data/research_state"),
        )
    except Exception as error:  # pragma: no cover - never block the app on research
        print(f"Research service unavailable: {error}", file=sys.stderr, flush=True)
        return None


def run_assistant(config: AssistantConfig) -> int:
    """Run the automatic assistant with an optional GUI."""
    repo_root = find_repo_root()
    validate_runtime_environment(repo_root, gui=config.gui)
    controller = AutomaticRuntimeController.from_config(
        config.session_config,
        report_root=config.report_root,
    )
    status_holder = ReceiverStatusHolder()
    research_service = _build_research_service(config)

    if not config.gui:
        try:
            asyncio.run(run_headless_assistant(
                config, controller, status_holder=status_holder, research_service=research_service,
            ))
        except KeyboardInterrupt:
            controller.stop()
            return 0
        return 0

    receiver_thread = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, research_service),
        name="mnq-assistant-receiver",
        daemon=True,
    )
    receiver_thread.start()
    return _run_gui(controller, status_holder, research_service)


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
) -> None:
    try:
        asyncio.run(run_headless_assistant(
            config, controller, status_holder=status_holder, research_service=research_service,
        ))
    except Exception as error:  # pragma: no cover - defensive service boundary
        controller.health.record_event("receiver", "failed", str(error))


def _run_gui(
    controller: AutomaticRuntimeController,
    status_holder: ReceiverStatusHolder | None = None,
    research_service: object | None = None,
) -> int:
    try:
        from PySide6.QtWidgets import QApplication

        from app.gui.main_window import MainWindow
    except ImportError as error:
        controller.health.mark_gui_failed(str(error))
        raise AssistantStartupError("PySide6 is not installed; run with --no-gui or install the GUI dependency.") from error

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(
        runtime_snapshot_provider=controller.snapshot,
        research_service=research_service,
        receiver_status_provider=status_holder.snapshot if status_holder is not None else None,
    )
    window.show()
    try:
        return int(app.exec())
    except Exception as error:  # pragma: no cover - GUI event loop defensive boundary
        controller.health.mark_gui_failed(str(error))
        raise


def _finalize_assistant_session(
    controller: AutomaticRuntimeController,
    config: AssistantConfig,
    recorder: MarketSessionRecorder,
    research_service: object | None = None,
) -> None:
    """Write reports and auto-build episodes when one Bookmap session ends cleanly."""
    session_date = recorder.session_start_utc.astimezone(UTC).date()
    manifest = _read_manifest(recorder.manifest_path)
    report_dir = controller.finalize_session_report(
        session_id=recorder.session_id,
        session_date=session_date,
        recorder_manifest=manifest,
    )
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
