"""Start the local Bookmap WebSocket receiver and raw-event recorder."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
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
    on_event_state: Callable[[dict[str, object], object], None] | None = None,
    on_control_event: Callable[[dict[str, object]], None] | None = None,
    recorder_factory: Callable[[], MarketSessionRecorder] | None = None,
    on_session_finalized: Callable[[MarketSessionRecorder], None] | None = None,
    initial_control_events: Sequence[dict[str, object]] = (),
    feed_guard: FeedGuard | None = None,
    intake_capacity: int = 10_000,
    on_connection_started: Callable[[object, object], None] | None = None,
    require_protocol_handshake: bool = False,
) -> RunningReceiverServer:
    """Start the local WebSocket server that records Bookmap market events.

    When ``feed_guard`` is provided, every message flows through it first:
    malformed messages are counted loudly instead of killing the session,
    rejected events (e.g. out-of-order depth) never reach state or disk,
    and the guard's dual connection/data-quality flags stay current.
    """
    from websockets.asyncio.server import ServerConnection, serve

    _install_port_probe_noise_filter()

    from app.market.protocol import ConnectionTracker, parse_handshake

    connection_tracker = ConnectionTracker()

    async def handler(connection: ServerConnection) -> None:
        request_path = getattr(getattr(connection, "request", None), "path", "")
        if request_path != config.path:
            await connection.close(code=1008, reason=f"expected {config.path}")
            return
        recorder = recorder_factory() if recorder_factory is not None else MarketSessionRecorder(root_dir=config.output_root)
        quality_baseline = feed_guard.status() if feed_guard is not None else None
        handshake_accepted = False

        def _guarded_control_event(event: dict[str, object]) -> None:
            nonlocal handshake_accepted
            if str(event.get("type", "")) == "connected":
                handshake = parse_handshake(event)
                if require_protocol_handshake and not handshake.compatible:
                    recorder.note_rejected_event(handshake.reason)
                    raise ValueError(f"incompatible Bookmap bridge: {handshake.reason}")
                if handshake.compatible:
                    boundary = connection_tracker.observe(handshake)
                    event["connection_boundary"] = boundary
                    event["handshake_accepted"] = True
                    handshake_accepted = True
            if str(event.get("type", "")) == "data_gap" and "receiver_intake_lost" in event:
                recorder.receiver_intake_lost_count = max(
                    recorder.receiver_intake_lost_count,
                    int(str(event["receiver_intake_lost"])),
                )
            if feed_guard is not None:
                feed_guard.handle_control_event(event)
            if on_control_event is not None:
                on_control_event(event)

        def _filter_market_event(event: Mapping[str, object]) -> bool:
            if require_protocol_handshake and not handshake_accepted:
                recorder.note_rejected_event("market event arrived before a compatible handshake")
                return False
            if feed_guard is None:
                return True
            accepted, reason = feed_guard.ingest_market_event(event)
            if not accepted:
                recorder.note_rejected_event(reason or "feed guard rejected event")
            return accepted

        def _record_schema_error(reason: str) -> None:
            recorder.note_malformed_event(reason)
            if feed_guard is not None:
                feed_guard.record_malformed(reason)

        from app.market.bounded_pipeline import BoundedIntakeBuffer

        intake = BoundedIntakeBuffer(connection, capacity=intake_capacity)
        pump_task = asyncio.get_running_loop().create_task(intake.pump())
        if on_connection_started is not None:
            on_connection_started(intake, recorder)
        try:
            for control_event in initial_control_events:
                event = _fresh_control_event(control_event)
                # Enrich and validate connected handshakes before persistence so
                # the immutable manifest records the accepted provenance.
                _guarded_control_event(event)
                recorder.record_control_event(event)
            await consume_market_stream(
                intake,
                recorder=recorder,
                state_store=state_store,
                on_state=on_state,
                on_market_event=on_market_event,
                on_event_state=on_event_state,
                on_control_event=_guarded_control_event,
                event_filter=_filter_market_event
                if feed_guard is not None or require_protocol_handshake else None,
                on_schema_error=_record_schema_error,
            )
        except Exception as error:
            detail = f"receiver_error: {type(error).__name__}: {error}"[:300]
            print(f"Receiver session ended with an error: {detail}", file=sys.stderr, flush=True)
            recorder.finalize(clean_shutdown=False, reason=detail)
            raise
        finally:
            pump_task.cancel()
            # The pump reads the socket; its outcome is the ONLY signal that
            # distinguishes a graceful end from a lossy one. A transport error
            # (abnormal close) means the tail may be truncated - an unclean
            # session. A clean end (normal close frame) means the producer
            # finished and every frame was drained. Swallowing this (as before)
            # forced EVERY socket close to look identical and unclean.
            pump_transport_failed = False
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - abnormal transport close = unclean
                pump_transport_failed = True
            if feed_guard is not None and quality_baseline is not None:
                quality = feed_guard.status()
                recorder.update_feed_quality(
                    sequence_gaps=max(0, quality.sequence_gaps - quality_baseline.sequence_gaps),
                    missed_events=max(0, quality.missed_events - quality_baseline.missed_events),
                    malformed_events=max(0, quality.malformed_events - quality_baseline.malformed_events),
                    out_of_order_events=max(
                        0,
                        quality.out_of_order_events - quality_baseline.out_of_order_events,
                    ),
                    duplicate_events=max(
                        0,
                        quality.duplicate_events - quality_baseline.duplicate_events,
                    ),
                    clock_drift_alerts=max(
                        0,
                        quality.clock_drift_alerts - quality_baseline.clock_drift_alerts,
                    ),
                )
            # A market-data feed legitimately ends when the producer closes the
            # socket, even without an explicit session_ended marker. A NORMAL close
            # (no transport error) means the peer finished and every buffered frame
            # was drained - a clean, COMPLETE session. Only a transport ERROR leaves
            # a possibly-truncated tail and stays unclean. Data LOSS (drops/overflow)
            # is tracked separately and still blocks order-flow replay, so a clean
            # flag never hides loss. (An explicit session_ended already finalized.)
            graceful_close = not pump_transport_failed
            recorder.finalize(
                clean_shutdown=recorder.clean_shutdown or graceful_close,
                reason="websocket_closed" if graceful_close else "transport_closed_uncleanly")
            if on_session_finalized is not None:
                on_session_finalized(recorder)

    # Protocol-level keepalive pings are disabled: liveness is monitored by
    # the add-on's application heartbeats plus the feed guard's staleness
    # window. Requiring pongs killed real sessions at exactly ping-timeout
    # ("sent 1011 keepalive ping timeout") when the client starved inbound
    # demand, and a one-way data feed must never die for a missing pong.
    server = await serve(handler, config.host, config.port, ping_interval=None)
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
