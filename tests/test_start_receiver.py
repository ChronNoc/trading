"""Tests for the local Bookmap receiver CLI server."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import websockets

from app.market.receiver import CurrentMarketState
from bookmap_addon.events import event_to_json, format_depth_update, format_trade
from tools.start_receiver import (
    ReceiverServerConfig,
    parse_args,
    start_receiver_websocket_server,
    startup_message,
)


def test_startup_message_matches_default_bookmap_endpoint() -> None:
    """The CLI startup message clearly names the endpoint and date partition target."""
    assert (
        startup_message(ReceiverServerConfig())
        == "listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/"
    )


def test_parse_args_builds_receiver_config() -> None:
    """CLI arguments map directly into receiver server configuration."""
    config = parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
            "--path",
            "/bookmap",
            "--output-root",
            "data/raw",
        ],
    )

    assert config == ReceiverServerConfig(
        host="127.0.0.1",
        port=9001,
        path="/bookmap",
        output_root=Path("data/raw"),
    )


def test_server_records_mock_bookmap_client_messages(tmp_path: Path) -> None:
    """A mock WebSocket client can connect and drive receiver state plus Parquet recording."""
    asyncio.run(_server_records_messages(tmp_path))


async def _server_records_messages(tmp_path: Path) -> None:
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    state_store = CurrentMarketState()
    server = await start_receiver_websocket_server(
        ReceiverServerConfig(port=0, output_root=tmp_path),
        state_store=state_store,
    )

    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.send(
                event_to_json(
                    format_depth_update(
                        timestamp=timestamp_ns,
                        symbol="MNQ",
                        side="bid",
                        price="100.00",
                        previous_size="0",
                        new_size="10",
                    ),
                ),
            )
            await websocket.send(
                event_to_json(
                    format_trade(
                        timestamp_ns=timestamp_ns + 1,
                        price="100.25",
                        size="3",
                        aggressor_side="buy",
                        instrument="MNQ",
                        sequence_id=1,
                    ),
                ),
            )
        await _wait_for_path(tmp_path / "2026-07-10" / "trades.parquet")
    finally:
        await server.close()

    current_state = state_store.get_state()
    assert current_state.best_bid == Decimal("100.00")
    assert current_state.executed_buy_volume == Decimal("3")
    depth_rows = pq.read_table(tmp_path / "2026-07-10" / "depth.parquet").to_pylist()
    trade_rows = pq.read_table(tmp_path / "2026-07-10" / "trades.parquet").to_pylist()
    assert depth_rows == [
        {
            "timestamp": timestamp_ns,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    ]
    assert trade_rows == [
        {
            "timestamp_ns": timestamp_ns + 1,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    ]


async def _wait_for_path(path: Path) -> None:
    for _ in range(20):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(timestamp.timestamp()) * 1_000_000_000
