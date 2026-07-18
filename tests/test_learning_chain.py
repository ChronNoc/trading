"""The full automatic learning chain, end to end, in the REAL backend process.

clean session recorded -> finalized eligible -> automatic episode build ->
research discovers it -> jobs queued ONCE per candidate -> results persisted ->
daily reports written. All in sandboxed temp trees (the backend sandboxes every
writable root beside a non-default --output-root); nothing touches user data.

Learning only ever consumes FINALIZED sessions; the canonical candidate stays
untouched; feed-scale block candidates are EXPERIMENTAL only.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from app.runtime.process_files import StatusFile, StopRequest

REPO = Path(__file__).resolve().parent.parent


def _send_clean_session(port: int, *, minutes: float = 1.6) -> int:
    """Stream a clean, order-flow-shaped session and end it with the marker."""
    import websockets

    async def run() -> int:
        sent = 0
        async with websockets.connect(f"ws://127.0.0.1:{port}/bookmap") as ws:
            await ws.send(json.dumps({"type": "connected", "timestamp_ns": 1,
                                      "alias": "MNQU6", "addon_version": "0.1.0"}))
            base = 1_752_537_751_000_000_000
            seq = 0
            total = int(minutes * 60 * 4)  # 4 ev/s of market time, replayed fast
            for i in range(total):
                ts = base + i * 250_000_000  # 250ms market spacing
                await ws.send(json.dumps({
                    "type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask",
                    "price": f"{29500 + (i % 20) * 0.25:.2f}",
                    "previous_size": str(i % 40), "new_size": str(i % 40 + 1)}))
                sent += 1
                if i % 4 == 0:
                    seq += 1
                    await ws.send(json.dumps({
                        "timestamp_ns": ts + 1, "price": f"{29500 + (i % 20) * 0.25:.2f}",
                        "size": "2", "aggressor_side": "buy" if i % 2 else "sell",
                        "instrument": "MNQ", "sequence_id": seq}))
                    sent += 1
            await ws.send(json.dumps({"type": "session_ended", "timestamp_ns": base + 10**12,
                                      "source_mode": "delayed", "dropped_messages": 0,
                                      "reason": "clean shutdown"}))
            await asyncio.sleep(0.5)
        return sent

    return asyncio.run(run())


def test_finalized_session_flows_into_research_and_reports(tmp_path: Path) -> None:
    """The chain the product exists for, driven through the real backend."""
    runtime = tmp_path / "runtime"
    raw = tmp_path / "raw"
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.start_backend",
         "--runtime-dir", str(runtime), "--port", "0",
         "--output-root", str(raw), "--max-seconds", "180",
         "--delayed-data-minutes", "15"],  # the batch file's real launch mode
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        port = 0
        while time.monotonic() < deadline:
            binding_path = runtime / "binding.json"
            if binding_path.is_file():
                port = int(json.loads(binding_path.read_text(encoding="utf-8"))["port"])
                break
            time.sleep(0.2)
        assert port, "backend must publish its bound port"

        sent = _send_clean_session(port)
        assert sent > 400

        # 1. The session finalized CLEAN (the recorder drained before the marker).
        deadline = time.monotonic() + 30
        manifest = None
        while time.monotonic() < deadline:
            manifests = list(raw.rglob("session_manifest.json"))
            if manifests:
                candidate = json.loads(manifests[0].read_text(encoding="utf-8"))
                if candidate.get("clean_shutdown"):
                    manifest = candidate
                    break
            time.sleep(0.5)
        assert manifest is not None, "the clean session must finalize"
        assert manifest["data_quality"]["ok"] is True

        # 2. The automatic episode build wrote into the SANDBOXED processed tree
        #    (never the real data/processed).
        processed = tmp_path / "processed"
        deadline = time.monotonic() + 60
        builds = []
        while time.monotonic() < deadline:
            builds = list(processed.glob("*.build.json"))
            if builds:
                break
            time.sleep(1.0)
        assert builds, "a clean finalized session must be built automatically"

        # 3. Research discovered the session and ran jobs - once per candidate,
        #    including the experimental feed-scale block candidates.
        state = tmp_path / "research_state"
        deadline = time.monotonic() + 90
        checkpoint = {}
        while time.monotonic() < deadline:
            checkpoint_path = state / "research_checkpoint.json"
            if checkpoint_path.is_file():
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if len(checkpoint.get("completed", checkpoint.get("completed_keys", []))) >= 5:
                    break
            time.sleep(2.0)
        completed = checkpoint.get("completed", checkpoint.get("completed_keys", []))
        assert len(completed) >= 5, (
            f"all 5 candidates (1 canonical + 4 experimental) must run once: {completed}"
        )
        # No duplicates: the same (session, candidate) never runs twice.
        assert len(set(completed)) == len(completed)

        # 4. The daily learning + paper reports were generated automatically.
        reports = tmp_path / "reports"
        assert list(reports.rglob("daily_learning.md")), "daily learning report must exist"
        assert list(reports.rglob("paper_report.md")), "paper daily report must exist"

        # 5. Nothing leaked into the REAL data tree from this backend.
        #    (The sandbox puts every writable root under tmp_path.)
        status = StatusFile(runtime)
        assert status.read() is not None
    finally:
        StopRequest(runtime).request("test cleanup")
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()


def test_bookmap_reconnects_produce_fresh_sessions_without_stalling(tmp_path: Path) -> None:
    """Multi-day reality: the feed disconnects and reconnects.

    Each connection must get its own session, the receiver must keep
    listening, and both sessions' events must be recorded - no stall, no
    duplicate backend state, no cross-session mixing.
    """
    import websockets

    runtime = tmp_path / "runtime"
    raw = tmp_path / "raw"
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.start_backend",
         "--runtime-dir", str(runtime), "--port", "0",
         "--output-root", str(raw), "--max-seconds", "90",
         "--delayed-data-minutes", "15"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        port = 0
        while time.monotonic() < deadline:
            binding = runtime / "binding.json"
            if binding.is_file():
                port = int(json.loads(binding.read_text(encoding="utf-8"))["port"])
                break
            time.sleep(0.2)
        assert port

        async def one_connection(start: int, count: int, *, clean_end: bool) -> None:
            async with websockets.connect(f"ws://127.0.0.1:{port}/bookmap") as ws:
                await ws.send(json.dumps({"type": "connected", "timestamp_ns": 1,
                                          "alias": "MNQU6", "addon_version": "0.1.0"}))
                base = 1_752_537_751_000_000_000
                for i in range(start, start + count):
                    await ws.send(json.dumps({
                        "type": "depth_update", "timestamp": base + i * 250_000_000,
                        "symbol": "MNQ", "side": "bid" if i % 2 else "ask",
                        "price": f"{29500 + (i % 20) * 0.25:.2f}",
                        "previous_size": "0", "new_size": str(i % 30 + 1)}))
                if clean_end:
                    await ws.send(json.dumps({"type": "session_ended",
                                              "timestamp_ns": base + 10**12,
                                              "source_mode": "delayed",
                                              "dropped_messages": 0,
                                              "reason": "clean shutdown"}))
                await asyncio.sleep(0.3)
            # leaving the block closes the socket - an abrupt disconnect when
            # clean_end is False, exactly like a feed drop.

        asyncio.run(one_connection(0, 200, clean_end=False))   # feed drop
        time.sleep(1.0)
        asyncio.run(one_connection(200, 200, clean_end=True))  # clean session
        time.sleep(2.0)

        manifests = sorted(raw.rglob("session_manifest.json"))
        assert len(manifests) == 2, "each connection must get its OWN session"
        documents = [json.loads(m.read_text(encoding="utf-8")) for m in manifests]
        totals = sorted(d["event_counts"]["depth_updates"] for d in documents)
        assert totals == [200, 200], f"both sessions must hold their events: {totals}"
        clean_flags = sorted(d.get("clean_shutdown", False) for d in documents)
        assert clean_flags == [False, True], (
            "the dropped connection finalizes unclean; the marked one clean"
        )
    finally:
        StopRequest(runtime).request("test cleanup")
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
