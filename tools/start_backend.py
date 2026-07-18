"""The authoritative capture backend - a standalone process, no GUI attached.

    .venv\\Scripts\\python.exe -m tools.start_backend [--runtime-dir runtime]

Owns EVERYTHING stateful: the WebSocket receiver, recording, session rotation,
the analysis feed, the paper engine and ledger, and automatic research. It
publishes an atomically-replaced ``runtime/status.json`` heartbeat (encoded
AppSnapshot + PID) about twice a second; the GUI process only ever reads that
file. Closing or restarting the GUI therefore cannot interrupt capture -
structurally, not by convention.

Lifecycle contract:

* a PID lock refuses duplicate backends (repeated batch-file launches attach
  instead of double-recording); a stale lock from a crash is reported and
  cleaned - the forced-shutdown marker
* ``runtime/stop.request`` triggers the normal clean drain (recorder finalize,
  analysis-feed drain, paper flatten) - the same tested shutdown path
* on exit the final status is written (STOPPED, clean or not) and the lock is
  released
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

STATUS_INTERVAL_SECONDS = 0.5
STOP_POLL_SECONDS = 1.0


def run_backend(
    runtime_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    output_root: Path | None = None,
    max_seconds: float | None = None,
    delayed_data_minutes: int = 0,
) -> int:
    """Run the backend until a stop request (or ``max_seconds``, for tests)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.gui.snapshot_codec import encode_snapshot
    from app.gui.snapshot_source import SnapshotSource
    from app.market.analysis_feed import AnalysisFeed
    from app.market.bounded_pipeline import PipelineStateHolder
    from app.market.receiver import get_current_market_state
    from app.paper.ledger import PaperLedger
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.runtime.controller import AutomaticRuntimeController
    from app.runtime.diagnostics import install_diagnostics
    from app.runtime.process_files import SingletonLock, StatusFile, StopRequest
    from app.runtime.server_state import ReceiverStatusHolder
    from app.runtime.shutdown import ShutdownSignal
    from tools.start_assistant import (
        AssistantConfig,
        _build_research_service,
        _run_receiver_thread,
        _shutdown_receiver,
    )

    lock = SingletonLock(runtime_dir)
    result = lock.acquire()
    if not result.acquired:
        print(f"REFUSED: {result.reason}", file=sys.stderr, flush=True)
        return 3
    if result.stale_lock_cleaned:
        print(f"NOTE: {result.reason}", flush=True)

    if output_root is not None:
        # A non-default output root sandboxes EVERY writable tree beside it,
        # so a test backend can never touch the real data directories.
        config = AssistantConfig.sandboxed(
            output_root, host=host, port=port, gui=False,
            delayed_data_minutes=delayed_data_minutes,
        )
    else:
        config = AssistantConfig(host=host, port=port, gui=False,
                                 delayed_data_minutes=delayed_data_minutes)
    logger = install_diagnostics(config.log_dir)
    logger.info("backend starting (pid=%s, runtime=%s)", __import__("os").getpid(), runtime_dir)

    controller = AutomaticRuntimeController.from_config(
        config.session_config, report_root=config.report_root,
    )
    status_holder = ReceiverStatusHolder()
    pipeline_holder = PipelineStateHolder()
    paper_engine = DelayedPaperEngine()
    paper_ledger = PaperLedger(config.paper_ledger_path)
    paper_engine.on_trade_closed(paper_ledger.append)
    research_service = _build_research_service(config, controller, pipeline_holder)
    shutdown = ShutdownSignal()
    feed = AnalysisFeed(
        pressure_check=lambda: pipeline_holder.worst_queue_occupancy_fraction() > 0.25,
    )
    receiver = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status_holder, research_service, pipeline_holder,
              paper_engine, shutdown, feed),
        name="mnq-backend-receiver", daemon=True,
    )
    receiver.start()

    # Publish the ACTUAL bound socket (port 0 resolves at bind time) so an
    # attaching GUI or harness knows where Bookmap should connect.
    import json as _json

    bind_deadline = time.monotonic() + 30
    while time.monotonic() < bind_deadline:
        binding = status_holder.snapshot()
        if binding.listening:
            (runtime_dir / "binding.json").write_text(
                _json.dumps({"host": host, "port": binding.port}), encoding="utf-8",
            )
            break
        time.sleep(0.1)

    source = SnapshotSource(
        controller=controller, pipeline_holder=pipeline_holder,
        research_service=research_service, receiver_status=status_holder.snapshot,
        market_state=get_current_market_state, paper_engine=paper_engine,
        analysis_feed=feed,
    )
    status = StatusFile(runtime_dir)
    stop = StopRequest(runtime_dir)
    started = time.monotonic()
    clean = True
    try:
        while True:
            try:
                status.write(encode_snapshot(source()))
            except Exception as error:  # noqa: BLE001 - a bad heartbeat must not kill capture
                logger.error("status write failed: %s", error)
            if stop.pending():
                logger.info("stop requested; draining")
                break
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                break
            time.sleep(STATUS_INTERVAL_SECONDS)
        clean = _shutdown_receiver(shutdown, receiver, timeout=20.0)
    finally:
        try:
            status.write(encode_snapshot(source()),
                         state="STOPPED_CLEAN" if clean else "STOPPED_UNCLEAN")
        except Exception:  # noqa: BLE001,S110
            pass
        stop.clear()
        lock.release()
        logger.info("backend stopped (clean=%s)", clean)
    return 0 if clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI wrapper."""
    parser = argparse.ArgumentParser(description="MNQ capture backend (no GUI).")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="exit after this long (integration tests only)")
    parser.add_argument("--delayed-data-minutes", type=int, default=0)
    args = parser.parse_args(argv)
    return run_backend(args.runtime_dir, host=args.host, port=args.port,
                       output_root=args.output_root, max_seconds=args.max_seconds,
                       delayed_data_minutes=args.delayed_data_minutes)


if __name__ == "__main__":
    raise SystemExit(main())
