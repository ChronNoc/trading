"""One-click automatic SHADOW runtime launcher for the MNQ assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.daily_learning import analyze_and_write_daily_learning_report
from app.runtime.controller import AutomaticRuntimeController
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


async def run_headless_assistant(config: AssistantConfig, controller: AutomaticRuntimeController) -> None:
    """Run the receiver/recorder/controller service until interrupted."""
    controller.start()
    delayed_events = _delayed_control_events(config.delayed_data_minutes)
    for event in delayed_events:
        controller.handle_control_event(event)
    server = await start_receiver_websocket_server(
        config.receiver_config,
        on_market_event=lambda event: controller.handle_market_event(event),
        on_control_event=lambda event: controller.handle_control_event(event),
        initial_control_events=delayed_events,
        on_session_finalized=lambda recorder: _finalize_assistant_session(controller, config, recorder),
    )
    actual_config = ReceiverServerConfig(
        host=config.host,
        port=server.port,
        path=config.path,
        output_root=config.output_root,
    )
    print("MNQ Assistant running in SHADOW mode.", flush=True)
    if config.delayed_data_minutes > 0:
        print(
            f"Bookmap delayed data mode: {config.delayed_data_minutes} minutes. "
            "Recording only; shadow decisions disabled.",
            flush=True,
        )
    print(startup_message(actual_config), flush=True)
    try:
        await asyncio.Future()
    finally:
        controller.stop()
        await server.close()


def run_assistant(config: AssistantConfig) -> int:
    """Run the automatic assistant with an optional GUI."""
    repo_root = find_repo_root()
    validate_runtime_environment(repo_root, gui=config.gui)
    controller = AutomaticRuntimeController.from_config(
        config.session_config,
        report_root=config.report_root,
    )

    if not config.gui:
        try:
            asyncio.run(run_headless_assistant(config, controller))
        except KeyboardInterrupt:
            controller.stop()
            return 0
        return 0

    receiver_thread = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller),
        name="mnq-assistant-receiver",
        daemon=True,
    )
    receiver_thread.start()
    return _run_gui(controller)


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


def _run_receiver_thread(config: AssistantConfig, controller: AutomaticRuntimeController) -> None:
    try:
        asyncio.run(run_headless_assistant(config, controller))
    except Exception as error:  # pragma: no cover - defensive service boundary
        controller.health.record_event("receiver", "failed", str(error))


def _run_gui(controller: AutomaticRuntimeController) -> int:
    try:
        from PySide6.QtWidgets import QApplication

        from app.gui.main_window import MainWindow
    except ImportError as error:
        controller.health.mark_gui_failed(str(error))
        raise AssistantStartupError("PySide6 is not installed; run with --no-gui or install the GUI dependency.") from error

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(runtime_snapshot_provider=controller.snapshot)
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
) -> None:
    """Write session and daily learning reports when one Bookmap session ends."""
    session_date = recorder.session_start_utc.astimezone(UTC).date()
    manifest = _read_manifest(recorder.manifest_path)
    report_dir = controller.finalize_session_report(
        session_id=recorder.session_id,
        session_date=session_date,
        recorder_manifest=manifest,
    )
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
