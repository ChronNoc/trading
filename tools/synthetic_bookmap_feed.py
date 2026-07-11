"""Synthetic Bookmap-compatible WebSocket feed for prototype mode."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.prototype.scenarios import (
    DEFAULT_SEED,
    DEFAULT_START_TIMESTAMP_NS,
    PROTOTYPE_SYMBOL,
    PrototypeScenario,
    PrototypeScheduledEvent,
    build_default_prototype_scenario,
    scenario_with_controls,
)
from bookmap_addon.events import event_to_json

DEFAULT_URL = "ws://127.0.0.1:8765/bookmap"
SUPPORTED_SPEEDS = (1, 2, 5, 10)


@dataclass(frozen=True, slots=True)
class SyntheticFeedConfig:
    """Configuration for the synthetic Bookmap-compatible feed."""

    url: str = DEFAULT_URL
    seed: int = DEFAULT_SEED
    speed: int = 5
    start_timestamp_ns: int = DEFAULT_START_TIMESTAMP_NS
    reconnect_delay_seconds: float = 0.25
    wall_clock: bool = True
    max_connect_attempts: int | None = None


@dataclass(frozen=True, slots=True)
class SyntheticFeedStats:
    """Summary of a completed synthetic feed run."""

    market_events_sent: int
    control_events_sent: int
    reconnects: int
    seed: int
    speed: int
    symbol: str = PROTOTYPE_SYMBOL


@dataclass(slots=True)
class PrototypePlaybackController:
    """Thread-safe-ish playback controls used by the prototype GUI."""

    paused: bool = False
    speed: int = 5
    restart_requested: bool = False
    jump_target: str | None = None

    def pause(self) -> None:
        """Pause synthetic playback."""
        self.paused = True

    def resume(self) -> None:
        """Resume synthetic playback."""
        self.paused = False

    def restart(self) -> None:
        """Request scenario restart."""
        self.restart_requested = True

    def set_speed(self, speed: int) -> None:
        """Set playback speed."""
        if speed not in SUPPORTED_SPEEDS:
            raise ValueError(f"speed must be one of {SUPPORTED_SPEEDS}")
        self.speed = speed

    def jump_to_clean_setup(self) -> None:
        """Request a jump to the clean setup."""
        self.jump_target = "clean"

    def jump_to_rejected_setup(self) -> None:
        """Request a jump to the rejected setup."""
        self.jump_target = "rejected"


class SyntheticBookmapFeed:
    """Emit deterministic prototype events to the real receiver WebSocket."""

    def __init__(
        self,
        config: SyntheticFeedConfig,
        *,
        controller: PrototypePlaybackController | None = None,
    ) -> None:
        """Create a synthetic feed."""
        if config.speed not in SUPPORTED_SPEEDS:
            raise ValueError(f"speed must be one of {SUPPORTED_SPEEDS}")
        self.config = config
        self.controller = controller or PrototypePlaybackController(speed=config.speed)
        self._stop_requested = asyncio.Event()
        self._market_events_sent = 0
        self._control_events_sent = 0
        self._reconnects = 0

    def stop(self) -> None:
        """Request clean feed shutdown."""
        self._stop_requested.set()

    async def run(self) -> SyntheticFeedStats:
        """Run the synthetic feed until the scenario completes or shutdown is requested."""
        scenario = build_default_prototype_scenario(
            seed=self.config.seed,
            start_timestamp_ns=self.config.start_timestamp_ns,
        )
        await self._play_scenario(scenario)
        return SyntheticFeedStats(
            market_events_sent=self._market_events_sent,
            control_events_sent=self._control_events_sent,
            reconnects=self._reconnects,
            seed=self.config.seed,
            speed=self.controller.speed,
        )

    async def _play_scenario(self, scenario: PrototypeScenario) -> None:
        events = list(scenario_with_controls(scenario, playback_speed=self.controller.speed))
        index = 0
        previous_timestamp = events[0].timestamp_ns if events else self.config.start_timestamp_ns
        websocket = None
        try:
            while index < len(events) and not self._stop_requested.is_set():
                if self.controller.restart_requested:
                    self.controller.restart_requested = False
                    index = 0
                    previous_timestamp = events[0].timestamp_ns
                    continue
                if self.controller.jump_target == "clean":
                    self.controller.jump_target = None
                    index = _first_index_at_or_after(events, scenario.clean_window.start_timestamp_ns)
                    previous_timestamp = events[index].timestamp_ns
                elif self.controller.jump_target == "rejected":
                    self.controller.jump_target = None
                    index = _first_index_at_or_after(events, scenario.rejected_window.start_timestamp_ns)
                    previous_timestamp = events[index].timestamp_ns

                while self.controller.paused and not self._stop_requested.is_set():
                    await asyncio.sleep(0.05)

                scheduled = events[index]
                await self._sleep_between(previous_timestamp, scheduled.timestamp_ns)
                websocket = await self._ensure_connection(websocket)
                await websocket.send(_event_payload(scheduled.event))
                if _is_control_event(scheduled.event):
                    self._control_events_sent += 1
                else:
                    self._market_events_sent += 1
                previous_timestamp = scheduled.timestamp_ns
                index += 1
                if scheduled.event.get("type") == "disconnected":
                    await websocket.close()
                    websocket = None
        finally:
            if websocket is not None:
                await websocket.close()

    async def _ensure_connection(self, websocket: object | None) -> object:
        if websocket is not None:
            return websocket
        import websockets

        attempts = 0
        while not self._stop_requested.is_set():
            try:
                connection = await websockets.connect(self.config.url)
                self._reconnects += 1
                return connection
            except OSError:
                attempts += 1
                if self.config.max_connect_attempts is not None and attempts >= self.config.max_connect_attempts:
                    raise
                await asyncio.sleep(self.config.reconnect_delay_seconds)
        raise asyncio.CancelledError

    async def _sleep_between(self, previous_timestamp_ns: int, timestamp_ns: int) -> None:
        if not self.config.wall_clock:
            return
        delta_seconds = max(timestamp_ns - previous_timestamp_ns, 0) / 1_000_000_000
        sleep_seconds = min(delta_seconds / max(self.controller.speed, 1), 1.0)
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)


def parse_args(argv: Sequence[str] | None = None) -> SyntheticFeedConfig:
    """Parse CLI arguments for the synthetic feed."""
    parser = argparse.ArgumentParser(description="Run the synthetic Bookmap-compatible prototype feed.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--speed", type=int, choices=SUPPORTED_SPEEDS, default=5)
    parser.add_argument("--start-timestamp-ns", type=int, default=DEFAULT_START_TIMESTAMP_NS)
    args = parser.parse_args(argv)
    return SyntheticFeedConfig(
        url=str(args.url),
        seed=int(args.seed),
        speed=int(args.speed),
        start_timestamp_ns=int(args.start_timestamp_ns),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the synthetic feed from the command line."""
    config = parse_args(argv)
    try:
        stats = asyncio.run(SyntheticBookmapFeed(config).run())
    except KeyboardInterrupt:
        return 0
    print(
        f"synthetic prototype feed complete: market={stats.market_events_sent} "
        f"control={stats.control_events_sent} reconnects={stats.reconnects}",
        flush=True,
    )
    return 0


def _event_payload(event: dict[str, object]) -> str:
    if _is_control_event(event):
        return json.dumps(event, separators=(",", ":"), sort_keys=True)
    return event_to_json(event)


def _is_control_event(event: dict[str, object]) -> bool:
    return str(event.get("type", "")) in {
        "connected",
        "disconnected",
        "heartbeat",
        "replay_started",
        "historical_mode",
        "prototype_mode",
        "realtime_started",
        "session_ended",
        "data_gap",
    }


def _first_index_at_or_after(events: list[PrototypeScheduledEvent], timestamp_ns: int) -> int:
    for index, event in enumerate(events):
        if event.timestamp_ns >= timestamp_ns:
            return index
    return max(len(events) - 1, 0)


if __name__ == "__main__":
    raise SystemExit(main())

