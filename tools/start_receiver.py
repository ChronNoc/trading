"""Start the local Bookmap WebSocket receiver and raw-event recorder."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.database.recorder import MarketSessionRecorder
from app.market.feed_guard import FeedGuard
from app.market.receiver import CURRENT_MARKET_STATE, CurrentMarketState, consume_market_stream
from app.market.state import MarketState

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/bookmap"
DEFAULT_OUTPUT_ROOT = Path("data/raw")


@dataclass(frozen=True, slots=True)
class ReceiverServerConfig:
    """Configuration for the local Bookmap WebSocket receiver server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    output_root: Path = DEFAULT_OUTPUT_ROOT

    @property
    def url(self) -> str:
        """Return the WebSocket URL for this receiver configuration."""
        return f"ws://{self.host}:{self.port}{self.path}"


@dataclass(frozen=True, slots=True)
class RunningReceiverServer:
    """Running WebSocket receiver server returned to tests and service wrappers."""

    server: Any
    host: str
    port: int
    path: str
    output_root: Path
    feed_guard: FeedGuard | None = None

    @property
    def url(self) -> str:
        """Return the actual WebSocket URL for the running receiver server."""
        return f"ws://{self.host}:{self.port}{self.path}"

    async def close(self) -> None:
        """Close the running server and wait until sockets are released."""
        self.server.close()
        await self.server.wait_closed()

    async def wait_closed(self) -> None:
        """Wait until the running server has closed."""
        await self.server.wait_closed()


def startup_message(config: ReceiverServerConfig) -> str:
    """Return the user-facing startup message for the receiver server."""
    return f"listening on {config.url}, writing to {_output_template(config.output_root)}"


class _PortProbeNoiseFilter(logging.Filter):
    """Drop handshake-failure logs caused by plain TCP port probes.

    Health checks like ``Test-NetConnection`` open a raw TCP connection and
    close it without an HTTP request; the websockets server would log a
    full traceback for each probe. Real protocol errors still log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "opening handshake failed" not in record.getMessage()


def _install_port_probe_noise_filter() -> None:
    logger = logging.getLogger("websockets.server")
    if not any(isinstance(existing, _PortProbeNoiseFilter) for existing in logger.filters):
        logger.addFilter(_PortProbeNoiseFilter())


async def start_receiver_websocket_server(
    config: ReceiverServerConfig,
    *,
    state_store: CurrentMarketState | None = CURRENT_MARKET_STATE,
    on_state: Callable[[MarketState], None] | None = None,
    on_market_event: Callable[[dict[str, object]], None] | None = None,
    on_control_event: Callable[[dict[str, object]], None] | None = None,
    recorder_factory: Callable[[], MarketSessionRecorder] | None = None,
    on_session_finalized: Callable[[MarketSessionRecorder], None] | None = None,
    initial_control_events: Sequence[dict[str, object]] = (),
    feed_guard: FeedGuard | None = None,
) -> RunningReceiverServer:
    """Start the local WebSocket server that records Bookmap market events.

    When ``feed_guard`` is provided, every message flows through it first:
    malformed messages are counted loudly instead of killing the session,
    rejected events (e.g. out-of-order depth) never reach state or disk,
    and the guard's dual connection/data-quality flags stay current.
    """
    from websockets.asyncio.server import ServerConnection, serve

    _install_port_probe_noise_filter()

    def _guarded_control_event(event: dict[str, object]) -> None:
        if feed_guard is not None:
            feed_guard.handle_control_event(event)
        if on_control_event is not None:
            on_control_event(event)

    async def handler(connection: ServerConnection) -> None:
        request_path = getattr(getattr(connection, "request", None), "path", "")
        if request_path != config.path:
            await connection.close(code=1008, reason=f"expected {config.path}")
            return
        recorder = recorder_factory() if recorder_factory is not None else MarketSessionRecorder(root_dir=config.output_root)
        try:
            for control_event in initial_control_events:
                event = _fresh_control_event(control_event)
                recorder.record_control_event(event)
                _guarded_control_event(event)
            await consume_market_stream(
                connection,
                recorder=recorder,
                state_store=state_store,
                on_state=on_state,
                on_market_event=on_market_event,
                on_control_event=_guarded_control_event,
                event_filter=(
                    (lambda event: feed_guard.ingest_market_event(event)[0])
                    if feed_guard is not None
                    else None
                ),
                on_schema_error=feed_guard.record_malformed if feed_guard is not None else None,
            )
        except Exception as error:
            detail = f"receiver_error: {type(error).__name__}: {error}"[:300]
            print(f"Receiver session ended with an error: {detail}", file=sys.stderr, flush=True)
            recorder.finalize(clean_shutdown=False, reason=detail)
            raise
        finally:
            recorder.finalize(clean_shutdown=recorder.clean_shutdown, reason="websocket_closed")
            if on_session_finalized is not None:
                on_session_finalized(recorder)

    server = await serve(handler, config.host, config.port)
    actual_port = _actual_server_port(server, config.port)
    return RunningReceiverServer(
        server=server,
        host=config.host,
        port=actual_port,
        path=config.path,
        output_root=config.output_root,
        feed_guard=feed_guard,
    )


async def run_receiver_server(config: ReceiverServerConfig) -> None:
    """Run the receiver server until interrupted by the caller or process."""
    server = await start_receiver_websocket_server(config)
    actual_config = ReceiverServerConfig(
        host=config.host,
        port=server.port,
        path=config.path,
        output_root=config.output_root,
    )
    print(startup_message(actual_config), flush=True)
    try:
        await asyncio.Future()
    finally:
        await server.close()


def parse_args(argv: Sequence[str] | None = None) -> ReceiverServerConfig:
    """Parse CLI arguments into a receiver server configuration."""
    parser = argparse.ArgumentParser(description="Start the local Bookmap WebSocket receiver.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)
    if args.port <= 0:
        raise ValueError("--port must be greater than zero for the CLI")
    if not str(args.path).startswith("/"):
        raise ValueError("--path must start with /")
    return ReceiverServerConfig(
        host=str(args.host),
        port=int(args.port),
        path=str(args.path),
        output_root=Path(args.output_root),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the receiver CLI entry point."""
    config = parse_args(argv)
    try:
        asyncio.run(run_receiver_server(config))
    except KeyboardInterrupt:
        return 0
    return 0


def _actual_server_port(server: Any, configured_port: int) -> int:
    if configured_port != 0:
        return configured_port
    sockets = getattr(server, "sockets", None)
    if not sockets:
        raise RuntimeError("receiver server did not expose a bound socket")
    return int(sockets[0].getsockname()[1])


def _output_template(output_root: Path) -> str:
    normalized = output_root.as_posix().rstrip("/")
    return f"{normalized}/{{date}}/session_<UTC timestamp>/"


def _fresh_control_event(event: dict[str, object]) -> dict[str, object]:
    current = dict(event)
    current["timestamp_ns"] = int(datetime.now(UTC).timestamp()) * 1_000_000_000
    return current


if __name__ == "__main__":
    raise SystemExit(main())
