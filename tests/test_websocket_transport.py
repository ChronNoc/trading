"""End-to-end WebSocket transport benchmark (correction #4).

Drives real events through the whole intake path - localhost WebSocket server,
JSON parse, schema validation, FeedGuard, receiver, MarketState, recorder
batching, closed Parquet parts, finalization - and proves the persisted row
count equals the accepted event count, with malformed messages counted, not
silently dropped.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from app.database.recorder import MarketSessionRecorder
from app.market.feed_guard import FeedGuard, FeedGuardConfig
from tools.start_receiver import ReceiverServerConfig, start_receiver_websocket_server


@dataclass
class TransportMetrics:
    sent: int
    accepted: int
    persisted: int
    malformed: int
    rejected: int
    elapsed_s: float
    bound_url: str

    @property
    def events_per_second(self) -> float:
        return self.sent / self.elapsed_s if self.elapsed_s else float("inf")


async def _run_transport(tmp_path: Path, pairs: int) -> TransportMetrics:
    from websockets.asyncio.client import connect

    guard = FeedGuard(FeedGuardConfig(source_mode="delayed"))
    recorders: list[MarketSessionRecorder] = []

    def factory() -> MarketSessionRecorder:
        rec = MarketSessionRecorder(root_dir=tmp_path / "raw")
        recorders.append(rec)
        return rec

    server = await start_receiver_websocket_server(
        ReceiverServerConfig(host="127.0.0.1", port=0, output_root=tmp_path / "raw"),
        recorder_factory=factory,
        feed_guard=guard,
        initial_control_events=[{"type": "delayed_mode", "delay_minutes": 15, "source_mode": "delayed"}],
    )
    bound_url = server.url
    base = 1_752_537_751_000_000_000
    sent = 0
    start = time.perf_counter()
    async with connect(bound_url) as ws:
        for i in range(pairs):
            await ws.send(json.dumps({
                "type": "depth_update", "timestamp": base + i * 1000, "symbol": "MNQ",
                "side": "bid" if i % 2 else "ask", "price": "29500.00",
                "previous_size": "0", "new_size": str(i % 40 + 1),
            }))
            await ws.send(json.dumps({
                "timestamp_ns": base + i * 1000 + 1, "price": "29500.00", "size": "1",
                "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1,
            }))
            sent += 2
        await ws.send("{ this is not valid json")  # one malformed message
        sent += 1
    elapsed = time.perf_counter() - start

    # Wait for the server handler to finalize the recorder after the client closes.
    for _ in range(200):
        if recorders and recorders[0].finalized:
            break
        await asyncio.sleep(0.02)
    await server.close()

    rec = recorders[0]
    persisted = _rows(rec.depth_path) + _rows(rec.trades_path)
    accepted = 2 * pairs - rec.rejected_event_count
    return TransportMetrics(
        sent=sent, accepted=accepted, persisted=persisted,
        malformed=rec.malformed_event_count, rejected=rec.rejected_event_count,
        elapsed_s=elapsed, bound_url=bound_url,
    )


def _rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows if path.is_file() else 0


def test_end_to_end_persisted_equals_accepted(tmp_path: Path) -> None:
    """Every accepted event through the real socket path is persisted; malformed is counted."""
    metrics = asyncio.run(_run_transport(tmp_path, pairs=2000))
    assert metrics.bound_url.startswith("ws://127.0.0.1:")  # server actually bound a port
    assert metrics.malformed == 1  # the bad frame was counted, not silently dropped
    assert metrics.rejected == 0  # in-order events are all accepted
    assert metrics.persisted == metrics.accepted == 4000
    assert metrics.events_per_second > 500  # end-to-end floor incl. socket + parse + guard
