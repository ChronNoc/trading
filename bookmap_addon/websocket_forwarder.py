"""Local WebSocket transport for Bookmap market-event forwarding."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Mapping

from bookmap_addon.events import event_to_json

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:8765/bookmap"
LOGGER = logging.getLogger(__name__)


class LocalWebSocketForwarder:
    """Background WebSocket sender for local append-only market-event forwarding."""

    def __init__(
        self,
        url: str = DEFAULT_WEBSOCKET_URL,
        *,
        reconnect_seconds: float = 1.0,
        max_queue_size: int = 10_000,
    ) -> None:
        """Create a forwarder that connects to a local WebSocket endpoint."""
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be greater than zero")
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero")

        self.url = url
        self.reconnect_seconds = reconnect_seconds
        self._outbox: queue.Queue[str] = queue.Queue(maxsize=max_queue_size)
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background WebSocket sender thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="bookmap-ws-forwarder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the background sender to stop and wait briefly for it to exit."""
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def publish(self, event: Mapping[str, object]) -> bool:
        """Queue one market event for WebSocket delivery without blocking Bookmap."""
        payload = event_to_json(dict(event))
        try:
            self._outbox.put_nowait(payload)
        except queue.Full:
            LOGGER.warning("Bookmap WebSocket outbox is full; dropping market event")
            return False
        return True

    def _run(self) -> None:
        asyncio.run(self._run_until_stopped())

    async def _run_until_stopped(self) -> None:
        import websockets

        while not self._stop_requested.is_set():
            try:
                async with websockets.connect(self.url) as websocket:
                    await self._send_until_stopped(websocket)
            except Exception as error:  # pragma: no cover - runtime transport path
                LOGGER.warning("Bookmap WebSocket connection failed: %s", error)
                await asyncio.sleep(self.reconnect_seconds)

    async def _send_until_stopped(self, websocket: object) -> None:
        while not self._stop_requested.is_set():
            try:
                payload = await asyncio.to_thread(self._outbox.get, True, 0.1)
            except queue.Empty:
                continue
            await websocket.send(payload)
            self._outbox.task_done()
