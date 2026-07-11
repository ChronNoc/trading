"""Local WebSocket receiver for Bookmap market-event streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias

from app.database.recorder import MarketEventRecorder
from app.market.state import MarketState
from bookmap_addon.events import RawMarketEvent, parse_event_message

DEFAULT_RECEIVER_URL = "ws://127.0.0.1:8765/bookmap"
WebSocketMessage: TypeAlias = str | bytes


class AsyncMessageStream(Protocol):
    """Protocol for a WebSocket-like async message stream."""

    def __aiter__(self) -> AsyncIterator[WebSocketMessage]:
        """Return an async iterator of raw WebSocket messages."""


class RawEventRecorder(Protocol):
    """Protocol for recorders that persist normalized raw market events."""

    def record(self, event: Mapping[str, object]) -> Path:
        """Persist one raw market event and return the destination path."""


@dataclass(slots=True)
class CurrentMarketState:
    """Mutable holder for the latest market state produced by the receiver."""

    _state: MarketState = field(default_factory=MarketState)

    def get_state(self) -> MarketState:
        """Return the latest receiver market state."""
        return self._state

    def set_state(self, state: MarketState) -> None:
        """Replace the latest receiver market state."""
        self._state = state

    def apply_event(self, event: Mapping[str, object]) -> MarketState:
        """Apply one raw market event and return the updated state."""
        self._state = self._state.update(event)
        return self._state


CURRENT_MARKET_STATE = CurrentMarketState()


@dataclass(frozen=True, slots=True)
class MarketReceiverResult:
    """Summary returned after a WebSocket stream ends or a max-message limit is reached."""

    final_state: MarketState
    events_processed: int


def apply_market_event(state: MarketState, event: Mapping[str, object]) -> MarketState:
    """Return a new market state after applying one validated raw market event."""
    return state.update(event)


def get_current_market_state() -> MarketState:
    """Return the current global market state maintained by the receiver."""
    return CURRENT_MARKET_STATE.get_state()


async def consume_market_stream(
    stream: AsyncMessageStream,
    *,
    initial_state: MarketState | None = None,
    recorder: RawEventRecorder | None = None,
    state_store: CurrentMarketState | None = CURRENT_MARKET_STATE,
    on_state: Callable[[MarketState], None] | None = None,
    max_messages: int | None = None,
) -> MarketReceiverResult:
    """Consume a WebSocket-like stream, update ``MarketState``, and optionally record events."""
    if max_messages is not None and max_messages <= 0:
        raise ValueError("max_messages must be greater than zero when provided")

    state = initial_state or MarketState()
    if state_store is not None:
        state_store.set_state(state)
    events_processed = 0
    async for message in stream:
        event = parse_event_message(message)
        state = apply_market_event(state, event)
        if state_store is not None:
            state_store.set_state(state)
        if recorder is not None:
            recorder.record(event)
        if on_state is not None:
            on_state(state)
        events_processed += 1
        if max_messages is not None and events_processed >= max_messages:
            break

    return MarketReceiverResult(final_state=state, events_processed=events_processed)


async def listen_for_market_events(
    url: str = DEFAULT_RECEIVER_URL,
    *,
    initial_state: MarketState | None = None,
    recorder: RawEventRecorder | None = None,
    state_store: CurrentMarketState | None = CURRENT_MARKET_STATE,
    on_state: Callable[[MarketState], None] | None = None,
    max_messages: int | None = None,
) -> MarketReceiverResult:
    """Connect to a local WebSocket URL and consume Task 5 market-event messages."""
    import websockets

    async with websockets.connect(url) as websocket:
        return await consume_market_stream(
            websocket,
            initial_state=initial_state,
            recorder=recorder,
            state_store=state_store,
            on_state=on_state,
            max_messages=max_messages,
        )


def run_market_receiver(
    url: str = DEFAULT_RECEIVER_URL,
    *,
    output_root: str | Path = "data/raw",
    initial_state: MarketState | None = None,
) -> MarketReceiverResult:
    """Run the local receiver until the WebSocket stream closes."""
    recorder = MarketEventRecorder(root_dir=Path(output_root))
    return asyncio.run(
        listen_for_market_events(
            url,
            initial_state=initial_state,
            recorder=recorder,
        ),
    )


def decode_market_message(message: WebSocketMessage) -> RawMarketEvent:
    """Decode and validate one Task 5 WebSocket market-event message."""
    return parse_event_message(message)
