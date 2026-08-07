"""One-click free synthetic PROTOTYPE runtime for the MNQ assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.market.features import compute_market_features
from app.prototype.scenarios import (
    DEFAULT_SEED,
    PROTOTYPE_SYMBOL,
    PrototypeDashboardSnapshot,
    empty_prototype_dashboard_snapshot,
)
from app.runtime.controller import AutomaticRuntimeController
from bookmap_addon.events import is_market_event
from tools.start_assistant import AssistantStartupError, find_repo_root, validate_runtime_environment
from tools.start_receiver import ReceiverServerConfig, start_receiver_websocket_server, startup_message
from tools.synthetic_bookmap_feed import (
    SUPPORTED_SPEEDS,
    PrototypePlaybackController,
    SyntheticBookmapFeed,
    SyntheticFeedConfig,
)

DEFAULT_OUTPUT_ROOT = Path("data/prototype/raw")
DEFAULT_REPORT_ROOT = Path("data/prototype/reports")
DEFAULT_LOCK_PATH = Path("data/prototype/prototype.lock")


@dataclass(frozen=True, slots=True)
class PrototypeRuntimeConfig:
    """Configuration for the synthetic prototype launcher."""

    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/bookmap"
    output_root: Path = DEFAULT_OUTPUT_ROOT
    report_root: Path = DEFAULT_REPORT_ROOT
    session_config: Path = Path("config/session_profiles.yaml")
    seed: int = DEFAULT_SEED
    speed: int = 5
    gui: bool = True
    lock_path: Path = DEFAULT_LOCK_PATH
    wall_clock: bool = True
    scenario: Path | None = None

    @property
    def receiver_config(self) -> ReceiverServerConfig:
        """Return the receiver-server config for this prototype runtime."""
        return ReceiverServerConfig(
            host=self.host,
            port=self.port,
            path=self.path,
            output_root=self.output_root,
        )


@dataclass(slots=True)
class PrototypeRuntimeState:
    """Mutable launcher-owned state displayed by the prototype dashboard."""

    playback: PrototypePlaybackController
    runtime_state: str = "starting"
    synthetic_status: str = "waiting for receiver"
    scenario: str = "default deterministic prototype"
    session: str = "New York open synthetic session"
    regime: str = "warming"
    profile: str = "prototype"
    warmup: str = "0 / warming"
    depth_events: int = 0
    trade_events: int = 0
    control_events: int = 0
    current_setup: str = "waiting for synthetic feed"
    decision: str = "none"
    explanations: tuple[str, ...] = ()
    raw_features: str = "not available"
    normalized_features: str = "not available"
    shadow_order: str = "none"
    report_path: str = "not written yet"
    scenario_path: Path | None = None
    scenario_seed: int = DEFAULT_SEED
    _last_trade_price: str = ""

    def on_control_event(self, event: Mapping[str, object]) -> None:
        """Update prototype state from a receiver control event."""
        self.control_events += 1
        event_type = str(event["type"])
        self.runtime_state = event_type
        if event_type == "connected":
            self.synthetic_status = "connected"
        elif event_type == "disconnected":
            self.synthetic_status = "reconnecting"
        elif event_type == "data_gap":
            self.synthetic_status = "data gap exercise"
        elif event_type == "session_ended":
            self.synthetic_status = "complete"
        elif event_type == "prototype_mode":
            self.synthetic_status = "prototype stream active"

    def on_market_event(self, event: Mapping[str, object], controller: AutomaticRuntimeController) -> None:
        """Update prototype state from a receiver market event."""
        if event.get("type") == "depth_update":
            self.depth_events += 1
        else:
            self.trade_events += 1
            self._last_trade_price = str(event.get("price", ""))
        self.runtime_state = "playing"
        runtime = controller.snapshot(current_timestamp_ns=_event_timestamp_ns(event))
        self.session = runtime.session_name
        self.regime = runtime.regime
        self.profile = runtime.profile_id
        self.warmup = f"{runtime.sample_count} / {'complete' if runtime.warmup_complete else 'warming'}"
        self.raw_features, self.normalized_features = _feature_text(controller)
        self.current_setup = _setup_text(self.depth_events, self.trade_events)

    def record_clean_result(self, lines: tuple[str, ...], *, price: str) -> None:
        """Record the accepted clean prototype setup result."""
        self.current_setup = "Clean long absorption reclaim"
        self.decision = "ACCEPTED"
        self.explanations = lines
        self.shadow_order = f"would have submitted long {PROTOTYPE_SYMBOL} shadow bracket near {price}"

    def record_rejected_result(self, lines: tuple[str, ...]) -> None:
        """Record the rejected lookalike prototype setup result."""
        self.current_setup = "Rejected lookalike"
        self.decision = "REJECTED"
        self.explanations = lines
        self.shadow_order = ""

    def snapshot(self) -> PrototypeDashboardSnapshot:
        """Return the GUI-facing prototype snapshot."""
        return PrototypeDashboardSnapshot(
            banner="PROTOTYPE DATA - NOT REAL MARKET DATA",
            runtime_state=self.runtime_state,
            synthetic_status=self.synthetic_status,
            instrument=PROTOTYPE_SYMBOL,
            source_mode="PROTOTYPE",
            scenario=self.scenario,
            session=self.session,
            regime=self.regime,
            profile=self.profile,
            warmup=self.warmup,
            depth_events=self.depth_events,
            trade_events=self.trade_events,
            control_events=self.control_events,
            current_setup=self.current_setup,
            decision=self.decision,
            explanations=self.explanations,
            raw_features=self.raw_features,
            normalized_features=self.normalized_features,
            shadow_order="" if self.decision.casefold() == "rejected" else self.shadow_order,
            report_path=self.report_path,
            playback_speed=self.playback.speed,
            paused=self.playback.paused,
            last_trade_price=self._last_trade_price,
        )


class PrototypeInstanceLock:
    """Simple file lock preventing duplicate one-click prototype instances."""

    def __init__(self, path: Path) -> None:
        """Create a lock wrapper for the given path."""
        self.path = path
        self._handle: int | None = None

    def acquire(self) -> None:
        """Acquire the prototype lock or raise a startup error."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._handle, str(os.getpid()).encode("ascii"))
        except FileExistsError as error:
            raise AssistantStartupError(
                f"Prototype runtime already appears to be running. Lock file: {self.path}",
            ) from error

    def release(self) -> None:
        """Release the prototype lock."""
        if self._handle is not None:
            os.close(self._handle)
            self._handle = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "PrototypeInstanceLock":
        """Acquire the lock as a context manager."""
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Release the lock when leaving the context."""
        self.release()


async def run_headless_prototype(
    config: PrototypeRuntimeConfig,
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
    *,
    run_once: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the receiver, recorder, controller, and synthetic feed."""
    _reset_setup_tracking()
    controller.start()
    controller.source_mode = "prototype"
    controller.threshold_summary = "PROVISIONAL/SYNTHETIC dynamic thresholds from warm-up events"
    from app.market.feed_guard import FeedGuard, FeedGuardConfig

    feed_guard = FeedGuard(FeedGuardConfig(source_mode="replay"))
    server = await start_receiver_websocket_server(
        config.receiver_config,
        on_market_event=lambda event: _handle_market_event(event, controller, prototype_state),
        on_control_event=lambda event: _handle_control_event(event, controller, prototype_state),
        on_session_finalized=lambda recorder: _finalize_report(controller, prototype_state, recorder),
        recorder_factory=lambda: MarketSessionRecorder(root_dir=config.output_root),
        feed_guard=feed_guard,
    )
    actual_config = PrototypeRuntimeConfig(
        host=config.host,
        port=server.port,
        path=config.path,
        output_root=config.output_root,
        report_root=config.report_root,
        session_config=config.session_config,
        seed=config.seed,
        speed=config.speed,
        gui=config.gui,
        lock_path=config.lock_path,
        wall_clock=config.wall_clock,
        scenario=config.scenario,
    )
    url = actual_config.receiver_config.url
    print("MNQ Prototype running in SHADOW mode.", flush=True)
    print("PROTOTYPE DATA - NOT REAL MARKET DATA", flush=True)
    print(startup_message(actual_config.receiver_config), flush=True)
    feed = SyntheticBookmapFeed(
        SyntheticFeedConfig(
            url=url,
            seed=config.seed,
            speed=config.speed,
            wall_clock=config.wall_clock,
            scenario_path=config.scenario,
        ),
        controller=prototype_state.playback,
    )
    try:
        if run_once:
            await feed.run()
            return
        feed_task = asyncio.create_task(feed.run())
        while not feed_task.done():
            if stop_event is not None and stop_event.is_set():
                feed.stop()
                break
            await asyncio.sleep(0.1)
        await feed_task
    finally:
        feed.stop()
        controller.stop()
        await server.close()


def run_prototype(config: PrototypeRuntimeConfig) -> int:
    """Run the one-click prototype runtime."""
    repo_root = find_repo_root()
    validate_runtime_environment(repo_root, gui=config.gui)
    controller = AutomaticRuntimeController.from_config(
        config.session_config,
        report_root=config.report_root,
    )
    playback = PrototypePlaybackController(speed=config.speed)
    prototype_state = PrototypeRuntimeState(
        playback=playback,
        scenario_path=config.scenario,
        scenario_seed=config.seed,
    )
    if config.scenario is not None:
        prototype_state.scenario = f"scenario file: {config.scenario.stem}"
    with PrototypeInstanceLock(config.lock_path):
        if not config.gui:
            try:
                asyncio.run(run_headless_prototype(config, controller, prototype_state))
            except KeyboardInterrupt:
                controller.stop()
                return 0
            return 0

        stop_event = threading.Event()
        worker = threading.Thread(
            target=_run_prototype_worker,
            args=(config, controller, prototype_state, stop_event),
            name="mnq-prototype-runtime",
            daemon=True,
        )
        worker.start()
        return _run_gui(controller, prototype_state, stop_event, worker)


def parse_args(argv: Sequence[str] | None = None) -> PrototypeRuntimeConfig:
    """Parse command-line arguments for the one-click prototype runtime."""
    parser = argparse.ArgumentParser(description="Start the MNQ assistant in free synthetic PROTOTYPE mode.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/bookmap")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--session-config", type=Path, default=Path("config/session_profiles.yaml"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--speed", type=int, choices=SUPPORTED_SPEEDS, default=5)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--no-gui", action="store_true")
    parser.add_argument("--no-wall-clock", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scenario",
        type=Path,
        default=None,
        help="Optional scenario YAML from config/prototype_scenarios to play instead of the default demo.",
    )
    args = parser.parse_args(argv)
    if args.port <= 0:
        raise AssistantStartupError("--port must be greater than zero.")
    if not str(args.path).startswith("/"):
        raise AssistantStartupError("--path must start with /.")
    if args.scenario is not None and not Path(args.scenario).is_file():
        raise AssistantStartupError(f"--scenario file not found: {args.scenario}")
    return PrototypeRuntimeConfig(
        host=str(args.host),
        port=int(args.port),
        path=str(args.path),
        output_root=Path(args.output_root),
        report_root=Path(args.report_root),
        session_config=Path(args.session_config),
        seed=int(args.seed),
        speed=int(args.speed),
        gui=not bool(args.no_gui),
        lock_path=Path(args.lock_path),
        wall_clock=not bool(args.no_wall_clock),
        scenario=Path(args.scenario) if args.scenario is not None else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the one-click prototype runtime."""
    try:
        return run_prototype(parse_args(argv))
    except AssistantStartupError as error:
        print(f"MNQ Prototype could not start: {error}", file=sys.stderr)
        return 2


def _run_prototype_worker(
    config: PrototypeRuntimeConfig,
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
    stop_event: threading.Event,
) -> None:
    try:
        asyncio.run(run_headless_prototype(config, controller, prototype_state, stop_event=stop_event))
    except Exception as error:  # pragma: no cover - defensive thread boundary
        prototype_state.synthetic_status = f"failed: {error}"
        controller.health.record_event("prototype", "failed", str(error))


def _run_gui(
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
    stop_event: threading.Event,
    worker: threading.Thread,
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
        replay_data_root=DEFAULT_OUTPUT_ROOT,
        prototype_snapshot_provider=prototype_state.snapshot,
        prototype_control_handler=lambda command: _handle_playback_command(command, prototype_state.playback),
    )
    window.show()
    exit_code = int(app.exec())
    stop_event.set()
    worker.join(timeout=5)
    return exit_code


def _handle_control_event(
    event: dict[str, object],
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
) -> None:
    prototype_state.on_control_event(event)
    controller.handle_control_event(event)


def _handle_market_event(
    event: dict[str, object],
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
) -> None:
    controller.handle_market_event(event, current_timestamp_ns=_event_timestamp_ns(event))
    prototype_state.on_market_event(event, controller)
    _record_setup_if_window_complete(event, controller, prototype_state)


def _record_setup_if_window_complete(
    event: Mapping[str, object],
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
) -> None:
    if not is_market_event(event):
        return
    timestamp_ns = _event_timestamp_ns(event)
    scenario = getattr(_record_setup_if_window_complete, "_scenario", None)
    if scenario is None:
        from app.prototype.scenarios import build_default_prototype_scenario, evaluate_prototype_setups

        if prototype_state.scenario_path is not None:
            from app.prototype.scenario_library import load_scenario_yaml

            scenario = load_scenario_yaml(prototype_state.scenario_path)
        else:
            scenario = build_default_prototype_scenario(seed=prototype_state.scenario_seed)
        setattr(_record_setup_if_window_complete, "_scenario", scenario)
        setattr(_record_setup_if_window_complete, "_evaluations", evaluate_prototype_setups(scenario))
        setattr(_record_setup_if_window_complete, "_recorded", set())
    evaluations = getattr(_record_setup_if_window_complete, "_evaluations")
    recorded = getattr(_record_setup_if_window_complete, "_recorded")
    if timestamp_ns >= scenario.clean_window.end_timestamp_ns and "clean" not in recorded:
        result = evaluations["clean"]
        controller.record_setup_decision(
            "prototype-clean-long-absorption-reclaim",
            result,
            symbol=PROTOTYPE_SYMBOL,
            timestamp=_datetime_from_ns(timestamp_ns),
        )
        prototype_state.record_clean_result(result.render_lines(), price=str(event.get("price", "")))
        recorded.add("clean")
    if timestamp_ns >= scenario.rejected_window.end_timestamp_ns and "rejected" not in recorded:
        result = evaluations["rejected"]
        controller.record_setup_decision(
            "prototype-rejected-lookalike",
            result,
            symbol=PROTOTYPE_SYMBOL,
            timestamp=_datetime_from_ns(timestamp_ns),
        )
        prototype_state.record_rejected_result(result.render_lines())
        recorded.add("rejected")


def _reset_setup_tracking() -> None:
    for name in ("_scenario", "_evaluations", "_recorded"):
        if hasattr(_record_setup_if_window_complete, name):
            delattr(_record_setup_if_window_complete, name)


def _finalize_report(
    controller: AutomaticRuntimeController,
    prototype_state: PrototypeRuntimeState,
    recorder: MarketSessionRecorder,
) -> None:
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8")) if recorder.manifest_path.exists() else {}
    session_date = recorder.session_start_utc.astimezone(UTC).date()
    report_dir = controller.finalize_session_report(
        session_id=recorder.session_id,
        session_date=session_date,
        recorder_manifest=manifest,
    )
    prototype_state.report_path = str(report_dir)


def _handle_playback_command(command: str, playback: PrototypePlaybackController) -> None:
    if command == "pause":
        playback.pause()
    elif command == "resume":
        playback.resume()
    elif command == "restart":
        playback.restart()
    elif command == "jump_clean":
        playback.jump_to_clean_setup()
    elif command == "jump_rejected":
        playback.jump_to_rejected_setup()
    elif command.startswith("speed:"):
        playback.set_speed(int(command.split(":", maxsplit=1)[1]))


def _feature_text(controller: AutomaticRuntimeController) -> tuple[str, str]:
    window = tuple(controller._window)
    if not window:
        return "not available", "not available"
    features = compute_market_features(window, bid_reload_threshold=controller.market_state.bid_size_at_level(1) or 1)
    raw = (
        f"imbalance={features.book_imbalance:.4f}, added={features.liquidity_added}, "
        f"cancelled={features.liquidity_cancelled}, velocity={features.trade_velocity:.2f}, "
        f"vol={features.short_term_volatility:.4f}"
    )
    normalized = (
        f"reload_count={features.bid_reload_count}, session_high_dist={features.distance_from_session_high}, "
        f"session_low_dist={features.distance_from_session_low}"
    )
    return raw, normalized


def _setup_text(depth_events: int, trade_events: int) -> str:
    if trade_events == 0:
        return "warming book"
    if depth_events < 70:
        return "watching absorption context"
    return "watching prototype setup stream"


def _event_timestamp_ns(event: Mapping[str, object]) -> int:
    if "timestamp_ns" in event:
        return int(event["timestamp_ns"])
    return int(event["timestamp"])


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1000)


def default_snapshot() -> PrototypeDashboardSnapshot:
    """Return the default prototype dashboard snapshot."""
    return empty_prototype_dashboard_snapshot()


if __name__ == "__main__":
    raise SystemExit(main())
