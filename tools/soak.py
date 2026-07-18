"""Long-soak the full production pipeline and fail loudly on any limit breach.

    .venv\\Scripts\\python.exe -m tools.soak --minutes 30 --rate 1500

Runs the REAL launcher pipeline (websocket framing, schema parse, feed guard,
market state, recorder batching + Parquet, analysis feed, controller + paper
engine, session rotation) in temp directories for the stated duration, sampling
metrics every few seconds and writing them as JSON lines. The exit code is
nonzero when any limit fails:

* event conservation: persisted == accepted, zero intake/recorder loss
* bounded queues: no queue high-water beyond its capacity fraction limit
* bounded lag: analysis pipeline lag stays under the limit
* session validity: zero bridge drops / unclean markers during the soak
* paper continuity: the engine keeps evaluating (no silent stall)

A soak that did not run for its full stated duration reports that and fails.
Nothing here touches user data.
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

LAG_LIMIT_MS = 2_000.0
QUEUE_FRACTION_LIMIT = 0.8


def _memory_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - memory sampling is best-effort
        return None


async def _drive_forever(port: int, rate: float, stop: threading.Event, sent_box: list[int]) -> None:
    import websockets

    from tools.pipeline_loadtest import _build_events

    async with websockets.connect(f"ws://127.0.0.1:{port}/bookmap", max_queue=None) as ws:
        await ws.send(json.dumps({"type": "connected", "timestamp_ns": 1, "alias": "MNQU6",
                                  "addon_version": "0.1.0", "protocol_version": "1.0"}))
        start = time.perf_counter()
        chunk_seconds = 5.0
        while not stop.is_set():
            events = _build_events(int(rate * chunk_seconds), start_ns=time.time_ns(), rate=rate)
            chunk_start = time.perf_counter()
            per_tick = max(1, int(rate / 100))
            index = 0
            while index < len(events) and not stop.is_set():
                for payload in events[index:index + per_tick]:
                    await ws.send(payload)
                sent_box[0] += min(per_tick, len(events) - index)
                index += per_tick
                target = chunk_start + index / rate
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
        await ws.send(json.dumps({"type": "session_ended", "timestamp_ns": int(time.time_ns()),
                                  "source_mode": "delayed", "dropped_messages": 0,
                                  "reason": "clean shutdown"}))
        await asyncio.sleep(0.5)
        _ = start


def main(argv: Sequence[str] | None = None) -> int:
    """Run the soak; print a verdict; return nonzero on any limit breach."""
    parser = argparse.ArgumentParser(description="Production-pipeline long soak.")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--rate", type=float, default=1500.0)
    parser.add_argument("--metrics-out", type=Path, default=None,
                        help="JSONL metrics file (default: <tempdir>/soak_metrics.jsonl)")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.market.analysis_feed import AnalysisFeed
    from app.market.bounded_pipeline import PipelineStateHolder
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.runtime.controller import AutomaticRuntimeController
    from app.runtime.server_state import ReceiverStatusHolder
    from app.runtime.shutdown import ShutdownSignal
    from tools.start_assistant import AssistantConfig, _run_receiver_thread, _shutdown_receiver

    tmp = Path(tempfile.mkdtemp(prefix="soak_"))
    metrics_path = args.metrics_out or (tmp / "soak_metrics.jsonl")
    config = AssistantConfig.sandboxed(tmp / "raw", port=0, gui=False)
    controller = AutomaticRuntimeController.from_config(config.session_config,
                                                        report_root=config.report_root)
    status_holder = ReceiverStatusHolder()
    pipeline_holder = PipelineStateHolder()
    engine = DelayedPaperEngine()
    shutdown = ShutdownSignal()
    feed = AnalysisFeed(
        pressure_check=lambda: pipeline_holder.worst_queue_occupancy_fraction() > 0.25,
    )
    receiver = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, None, pipeline_holder, engine, shutdown),
        kwargs={"analysis_feed": feed}, name="soak-receiver", daemon=True,
    )
    receiver.start()
    for _ in range(500):
        if status_holder.snapshot().listening:
            break
        time.sleep(0.02)
    snap = status_holder.snapshot()
    if not snap.listening:
        print("SOAK FAIL: receiver did not bind", file=sys.stderr)
        return 2

    stop = threading.Event()
    sent_box = [0]
    driver = threading.Thread(
        target=lambda: asyncio.run(_drive_forever(snap.port, args.rate, stop, sent_box)),
        name="soak-driver", daemon=True,
    )
    started = time.monotonic()
    driver.start()

    failures: list[str] = []
    samples = 0
    last_evaluations = -1
    stalled_checks = 0
    duration = args.minutes * 60.0
    with metrics_path.open("w", encoding="utf-8") as sink:
        while time.monotonic() - started < duration:
            time.sleep(5.0)
            samples += 1
            feed_metrics = feed.metrics()
            pipe = pipeline_holder.snapshot()
            engine_status = engine.status()
            row = {
                "t": round(time.monotonic() - started, 1),
                "sent": sent_box[0],
                "feed_offered": feed_metrics.offered,
                "feed_processed": feed_metrics.processed,
                "feed_skipped": feed_metrics.skipped,
                "feed_depth": feed_metrics.depth,
                "feed_high_water": feed_metrics.high_water,
                "lag_ms": feed_metrics.lag_ms,
                "intake": pipe.intake, "recorder": pipe.recorder,
                "evaluations": engine_status.evaluations,
                "causality_breaks": engine_status.causality_breaks,
                "memory_mb": _memory_mb(),
            }
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            if feed_metrics.lag_ms is not None and feed_metrics.lag_ms > LAG_LIMIT_MS:
                failures.append(f"lag {feed_metrics.lag_ms:.0f} ms > {LAG_LIMIT_MS:.0f} at t={row['t']}")
            for stage_name, stage in (("intake", pipe.intake), ("recorder", pipe.recorder)):
                capacity = int(stage.get("capacity", 0) or 0)
                depth = int(stage.get("occupancy", 0) or 0)
                if capacity and depth / capacity > QUEUE_FRACTION_LIMIT:
                    failures.append(f"{stage_name} occupancy {depth}/{capacity} at t={row['t']}")
                if int(stage.get("overflow", 0) or 0):
                    failures.append(f"{stage_name} overflow at t={row['t']}")
            # Paper continuity: evaluations must advance once warmed up.
            if engine_status.state == "EVALUATING":
                if engine_status.evaluations == last_evaluations:
                    stalled_checks += 1
                    if stalled_checks >= 6:  # 30 s with zero new evaluations
                        failures.append(f"paper evaluation stalled at t={row['t']}")
                        stalled_checks = 0
                else:
                    stalled_checks = 0
                last_evaluations = engine_status.evaluations
    ran_seconds = time.monotonic() - started

    stop.set()
    driver.join(timeout=30)
    time.sleep(1.0)
    feed_final = feed.metrics()
    deadline = time.monotonic() + 30
    while feed_final.depth and time.monotonic() < deadline:
        time.sleep(0.2)
        feed_final = feed.metrics()
    _shutdown_receiver(shutdown, receiver, timeout=20.0)

    manifests = list((tmp / "raw").rglob("session_manifest.json"))
    persisted = 0
    bridge_drops = 0
    rejected = 0
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        counts = manifest.get("event_counts", {})
        persisted += int(counts.get("depth_updates", 0)) + int(counts.get("trades", 0))
        quality = manifest.get("data_quality", {})
        bridge_drops += int(quality.get("bridge_dropped_messages", 0))
        rejected += int(quality.get("rejected_events", 0))
    accepted = sent_box[0] - rejected
    if persisted != accepted:
        failures.append(f"conservation: persisted {persisted:,} != accepted {accepted:,}")
    if bridge_drops:
        failures.append(f"bridge drops during soak: {bridge_drops}")
    if feed_final.skipped:
        failures.append(f"analysis skipped {feed_final.skipped} (paper-only, but a soak must keep up)")
    if ran_seconds < duration - 1:
        failures.append(f"soak ran {ran_seconds:.0f}s of the stated {duration:.0f}s")

    print("=" * 70)
    print(f"  SOAK  {args.rate:,.0f} ev/s for {ran_seconds/60:.1f} min "
          f"({samples} samples)  ->  {'PASS' if not failures else 'FAIL'}")
    print("=" * 70)
    print(f"sent {sent_box[0]:,}  accepted {accepted:,}  persisted {persisted:,}  "
          f"analysis {feed_final.processed:,} (skipped {feed_final.skipped})")
    print(f"evaluations {engine.status().evaluations:,}  "
          f"feed high-water {feed_final.high_water:,}  metrics: {metrics_path}")
    for failure in failures:
        print(f"LIMIT FAILED: {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
