"""Production-path load test: the REAL launcher pipeline over a REAL WebSocket.

    .venv\\Scripts\\python.exe -m tools.pipeline_loadtest --rate 1500 --seconds 20

Why this exists: a synthetic recorder benchmark (26k ev/s) completely missed the
real failure, because the real cost lives elsewhere - fat depth books (hundreds
of price levels per MarketState copy) and strategy evaluation running inline on
the receiver loop. This harness exercises the exact production path
(``run_headless_assistant``: websocket framing -> schema parse -> feed guard ->
market state -> recorder pipeline -> analysis feed -> controller + paper engine)
with a realistic ~150-level book and the real depth:trade mix, then prints the
end-to-end conservation equation.

``--inline`` reproduces the OLD architecture (analysis sinks executed
synchronously on the capture loop) so before/after numbers come from the same
instrument. Everything runs in temp directories - no user data is touched.

Exit code is nonzero when conservation fails (an accepted event vanished).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Sequence

BASE_PRICE = 29500.00
BOOK_LEVELS = 150  # realistic MNQ visible-depth breadth per side


def _build_events(count: int, *, start_ns: int, rate: float) -> list[str]:
    """Pre-serialize a realistic stream: 3:1 depth:trade, book churn across levels."""
    events: list[str] = []
    interval_ns = int(1e9 / rate)
    seq = 0
    price = BASE_PRICE
    for i in range(count):
        ts = start_ns + i * interval_ns
        if i % 4 == 3:
            seq += 1
            price += 0.25 if i % 8 == 3 else -0.25
            events.append(json.dumps({
                "timestamp_ns": ts, "price": f"{price:.2f}", "size": str(i % 9 + 1),
                "aggressor_side": "buy" if i % 2 else "sell",
                "instrument": "MNQ", "sequence_id": seq,
            }))
        else:
            # Churn across the whole book so MarketState carries ~BOOK_LEVELS
            # levels per side - the realistic per-copy cost.
            level = i % BOOK_LEVELS
            side = "bid" if i % 2 else "ask"
            level_price = BASE_PRICE - level * 0.25 if side == "bid" else BASE_PRICE + level * 0.25
            events.append(json.dumps({
                "type": "depth_update", "timestamp": ts, "symbol": "MNQ", "side": side,
                "price": f"{level_price:.2f}", "previous_size": str((i // BOOK_LEVELS) % 40),
                "new_size": str((i // BOOK_LEVELS) % 40 + 1),
            }))
    return events


async def _drive(port: int, events: list[str], rate: float, *, burst: int = 0) -> dict[str, float]:
    """Send events at ``rate`` (batched at 10ms ticks); optional final burst."""
    import websockets

    sent = 0
    async with websockets.connect(f"ws://127.0.0.1:{port}/bookmap", max_queue=None) as ws:
        await ws.send(json.dumps({"type": "connected", "timestamp_ns": 1, "alias": "MNQU6",
                                  "addon_version": "0.1.0", "protocol_version": "1.0",
                                  "capabilities": "aggregated_depth,trades,aggressor_side"}))
        start = time.perf_counter()
        per_tick = max(1, int(rate / 100))
        index = 0
        while index < len(events):
            batch = events[index:index + per_tick]
            for payload in batch:
                await ws.send(payload)
            sent += len(batch)
            index += per_tick
            target = start + index / rate
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        if burst:
            # A realistic burst moves FORWARD in time (fresh timestamps after
            # the paced stream); replaying old events would be rejected by the
            # guard as out-of-order and pollute the conservation numbers.
            burst_events = [e for e in _build_events(int(burst * 1.4), start_ns=time.time_ns(), rate=50_000)
                            if '"depth_update"' in e][:burst]  # depth-only: fresh trades would rewind sequence_id
            for payload in burst_events:
                await ws.send(payload)
            sent += burst
        elapsed = time.perf_counter() - start
        await ws.send(json.dumps({"type": "session_ended", "timestamp_ns": 2,
                                  "source_mode": "delayed", "dropped_messages": 0,
                                  "reason": "clean shutdown"}))
        await asyncio.sleep(0.5)
    return {"sent": sent, "seconds": elapsed, "actual_rate": sent / elapsed if elapsed else 0.0}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the load test and print the conservation table."""
    parser = argparse.ArgumentParser(description="Production-path pipeline load test.")
    parser.add_argument("--rate", type=float, default=1500.0, help="events/sec to sustain")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--burst", type=int, default=0, help="extra unpaced events at the end")
    parser.add_argument("--inline", action="store_true",
                        help="reproduce the OLD architecture: analysis inline on the capture loop")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.market.analysis_feed import AnalysisFeed
    from app.market.bounded_pipeline import PipelineStateHolder
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.runtime.controller import AutomaticRuntimeController
    from app.runtime.server_state import ReceiverStatusHolder
    from app.runtime.shutdown import ShutdownSignal
    from tools.start_assistant import AssistantConfig, _run_receiver_thread, _shutdown_receiver

    tmp = Path(tempfile.mkdtemp(prefix="loadtest_"))
    config = AssistantConfig(port=0, output_root=tmp / "raw", report_root=tmp / "reports",
                             gui=False, log_dir=tmp / "logs",
                             paper_ledger_path=tmp / "paper/ledger.jsonl")
    controller = AutomaticRuntimeController.from_config(config.session_config,
                                                        report_root=config.report_root)
    status_holder = ReceiverStatusHolder()
    pipeline_holder = PipelineStateHolder()
    engine = DelayedPaperEngine()
    shutdown = ShutdownSignal()

    # Mirror the production wiring exactly, including capture-priority
    # backpressure (analysis yields the GIL while recorder queues fill).
    feed = AnalysisFeed(
        pressure_check=lambda: pipeline_holder.worst_queue_occupancy_fraction() > 0.25,
    )
    if args.inline:
        # The OLD wiring, faithfully: sinks run synchronously inside offer(),
        # i.e. on the capture loop. This is the architecture that dropped real
        # Bookmap events; keep it only as the measurement baseline.
        class _InlineFeed(AnalysisFeed):
            def offer(self, event, state):  # noqa: ANN001, ANN202
                self._offered += 1  # noqa: SLF001
                for sink in self._sinks:  # noqa: SLF001
                    try:
                        sink(event, state)
                    except Exception:  # noqa: BLE001,S110
                        pass
                self._processed += 1  # noqa: SLF001
                return True

            def start(self) -> None:  # no thread in inline mode
                return

            def stop(self, *, drain_seconds: float = 5.0) -> bool:
                return True

        feed = _InlineFeed()

    thread = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, None, pipeline_holder, engine, shutdown),
        kwargs={"analysis_feed": feed},
        name="loadtest-receiver", daemon=True,
    )
    thread.start()
    for _ in range(500):
        if status_holder.snapshot().listening:
            break
        time.sleep(0.02)
    snap = status_holder.snapshot()
    if not snap.listening:
        print("FAIL: receiver did not bind", file=sys.stderr)
        return 2

    total = int(args.rate * args.seconds)
    events = _build_events(total, start_ns=time.time_ns(), rate=args.rate)
    drive = asyncio.run(_drive(snap.port, events, args.rate, burst=args.burst))

    # Let the analysis feed catch up, then drain everything.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        metrics = feed.metrics()
        if metrics.depth == 0:
            break
        time.sleep(0.1)
    _shutdown_receiver(shutdown, thread, timeout=15.0)

    metrics = feed.metrics()
    engine_status = engine.status()
    pipe = pipeline_holder.snapshot()
    intake_overflow = int(pipe.intake.get("overflow", 0)) if pipe.intake else 0
    recorder_overflow = int(pipe.recorder.get("overflow", 0)) if pipe.recorder else 0
    recorder_high_water = int(pipe.recorder.get("high_water", 0)) if pipe.recorder else 0
    intake_high_water = int(pipe.intake.get("high_water", 0)) if pipe.intake else 0
    manifests = list((tmp / "raw").rglob("session_manifest.json"))
    persisted = {"depth_updates": 0, "trades": 0}
    quality = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in persisted:
            persisted[key] += int(manifest.get("event_counts", {}).get(key, 0))
        quality = manifest.get("data_quality", quality)

    persisted_total = persisted["depth_updates"] + persisted["trades"]
    guard_rejected = int(quality.get("rejected_events", 0))
    malformed = int(quality.get("malformed_events", 0))
    accepted = drive["sent"] - guard_rejected - malformed
    # Unintended loss (intake eviction / recorder overflow) must be zero for a
    # PASS; every other difference must be a named, counted rejection.
    named = persisted_total + intake_overflow + recorder_overflow
    conserved = (named == accepted and intake_overflow == 0
                 and recorder_overflow == 0)

    print("=" * 74)
    print(f"  PRODUCTION-PATH LOAD TEST  ({'INLINE (old architecture)' if args.inline else 'analysis feed (new)'})")
    print("=" * 74)
    print(f"target rate:          {args.rate:,.0f} ev/s for {args.seconds:.0f}s"
          + (f" + burst {args.burst:,}" if args.burst else ""))
    print(f"actually sent:        {drive['sent']:,} at {drive['actual_rate']:,.0f} ev/s")
    print()
    print("CONSERVATION  (every event accounted, none vanishing):")
    print(f"  sent                {drive['sent']:>10,}")
    print(f"  - guard rejected    {guard_rejected:>10,}")
    print(f"  - malformed         {malformed:>10,}")
    print(f"  = accepted          {accepted:>10,}")
    print(f"  persisted           {persisted_total:>10,}   (depth {persisted['depth_updates']:,} + trades {persisted['trades']:,})")
    print(f"  offered to analysis {metrics.offered:>10,}")
    print(f"  analysis processed  {metrics.processed:>10,}")
    print(f"  analysis skipped    {metrics.skipped:>10,}   (paper-only, counted, gap-notified)")
    print(f"  intake evicted      {intake_overflow:>10,}   (UNINTENDED loss if nonzero)")
    print(f"  recorder overflow   {recorder_overflow:>10,}   (UNINTENDED loss if nonzero)")
    print()
    print(f"analysis queue:       high-water {metrics.high_water:,} / {metrics.capacity:,}   "
          f"final depth {metrics.depth:,}")
    print(f"intake high-water:    {intake_high_water:,}   recorder high-water: {recorder_high_water:,}")
    print(f"analysis lag:         {metrics.lag_ms if metrics.lag_ms is not None else 'n/a'} ms (pipeline only)")
    print(f"paper evaluations:    {engine_status.evaluations:,}   causality breaks: {engine_status.causality_breaks}")
    print(f"bridge drops:         {int(quality.get('bridge_dropped_messages', 0)):,}")
    print(f"quality breakdown:    {quality}")
    print()
    verdict = "PASS" if conserved else "FAIL"
    print(f"conservation verdict: {verdict}  "
          f"(persisted {persisted_total:,} + named losses == accepted {accepted:,}; "
          f"unintended loss == 0: {conserved})")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "mode": "inline" if args.inline else "feed",
            "sent": drive["sent"], "actual_rate": drive["actual_rate"],
            "accepted": accepted, "persisted": persisted_total,
            "guard_rejected": guard_rejected, "malformed": malformed,
            "offered": metrics.offered, "processed": metrics.processed,
            "skipped": metrics.skipped, "high_water": metrics.high_water,
            "intake_overflow": intake_overflow, "recorder_overflow": recorder_overflow,
            "intake_high_water": intake_high_water, "recorder_high_water": recorder_high_water,
            "lag_ms": metrics.lag_ms, "evaluations": engine_status.evaluations,
            "conserved": conserved,
        }, indent=2), encoding="utf-8")
    return 0 if conserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
