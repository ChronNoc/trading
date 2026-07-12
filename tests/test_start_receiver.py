"""Tests for the local Bookmap receiver CLI server."""

from __future__ import annotations

import asyncio
import json
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
        == "listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/session_<UTC timestamp>/"
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


def test_server_injects_initial_delayed_mode_control_event(tmp_path: Path) -> None:
    """The assistant can mark every Bookmap-free connection as delayed before data arrives."""
    asyncio.run(_server_injects_delayed_mode(tmp_path))


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
            await websocket.send(
                json.dumps(
                    {
                        "type": "session_ended",
                        "timestamp_ns": timestamp_ns + 2,
                    },
                ),
            )
        session_dir = await _wait_for_session_dir(tmp_path)
        await _wait_for_path(session_dir / "trades.parquet")
    finally:
        await server.close()

    current_state = state_store.get_state()
    assert current_state.best_bid == Decimal("100.00")
    assert current_state.executed_buy_volume == Decimal("3")
    depth_rows = pq.read_table(session_dir / "depth.parquet").to_pylist()
    trade_rows = pq.read_table(session_dir / "trades.parquet").to_pylist()
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
    manifest = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
    assert manifest["clean_shutdown"] is True
    assert manifest["event_counts"]["depth_updates"] == 1
    assert manifest["event_counts"]["trades"] == 1


async def _server_injects_delayed_mode(tmp_path: Path) -> None:
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    control_events: list[dict[str, object]] = []
    server = await start_receiver_websocket_server(
        ReceiverServerConfig(port=0, output_root=tmp_path),
        on_control_event=lambda event: control_events.append(dict(event)),
        initial_control_events=(
            {
                "type": "delayed_mode",
                "timestamp_ns": timestamp_ns,
                "source_mode": "delayed",
                "delay_minutes": 15,
                "reason": "Bookmap free delayed data feed",
            },
        ),
    )

    try:
        async with websockets.connect(server.url) as websocket:
            await websocket.send(json.dumps({"type": "session_ended", "timestamp_ns": timestamp_ns + 1}))
        session_dir = await _wait_for_session_dir(tmp_path)
    finally:
        await server.close()

    manifest = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
    assert control_events[0]["type"] == "delayed_mode"
    assert control_events[0]["delay_minutes"] == 15
    assert manifest["source_mode"] == "delayed"
    assert manifest["data_delay_minutes"] == 15
    assert manifest["valid_for_live_decisions"] is False


async def _wait_for_path(path: Path) -> None:
    for _ in range(20):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


async def _wait_for_session_dir(root_dir: Path) -> Path:
    for _ in range(20):
        for date_dir in root_dir.iterdir() if root_dir.exists() else ():
            if not date_dir.is_dir():
                continue
            sessions = [path for path in date_dir.iterdir() if path.is_dir()]
            if sessions:
                return sessions[0]
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for session under {root_dir}")


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(timestamp.timestamp()) * 1_000_000_000
