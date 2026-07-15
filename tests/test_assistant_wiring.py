"""Tests for the wired assistant: bound-state publish, auto-research, auto-build."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.runtime.controller import AutomaticRuntimeController
from app.runtime.server_state import ReceiverStatusHolder
from tools.start_assistant import AssistantConfig, _schedule_auto_build, run_headless_assistant


def _finalized_session(raw_root: Path) -> None:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")
    for i in range(200):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 1_000_000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 40 + 1)})
        rec.record({"timestamp_ns": base + i * 1_000_000 + 1, "price": f"{price:.2f}", "size": "1",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)


def test_schedule_auto_build_builds_finalized_session(tmp_path: Path) -> None:
    """A finalized session is auto-built idempotently in the background."""
    _finalized_session(tmp_path / "raw")
    config = AssistantConfig(output_root=tmp_path / "raw")
    # Redirect processed/labels to the tmp tree by monkeypatching cwd-relative writes.
    import app.research.build_orchestrator as orch

    captured = {}
    real = orch.schedule_pending_builds

    def fake(raw, processed, labels, **kw):
        captured["raw"] = raw
        thread = real(raw, tmp_path / "processed", tmp_path / "labels", **kw)
        return thread

    orch.schedule_pending_builds = fake  # type: ignore[assignment]
    try:
        _schedule_auto_build(config)
    finally:
        orch.schedule_pending_builds = real  # type: ignore[assignment]
    # Give the daemon thread a moment.
    import time

    for _ in range(100):
        if list((tmp_path / "processed").glob("*.build.json")):
            break
        time.sleep(0.05)
    assert list((tmp_path / "processed").glob("*.build.json")), "auto-build must produce a build summary"


def test_headless_assistant_publishes_bound_state_and_starts_research(tmp_path: Path) -> None:
    """Binding the socket publishes real listening state and auto-starts research."""
    holder = ReceiverStatusHolder()
    started = {"start": 0, "stop": 0}

    class FakeService:
        def start(self, **_kw: object) -> None:
            started["start"] += 1

        def stop(self) -> None:
            started["stop"] += 1

    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    config = AssistantConfig(host="127.0.0.1", port=0, output_root=tmp_path / "raw",
                             report_root=tmp_path / "reports", gui=False, delayed_data_minutes=15)

    async def _drive() -> tuple[bool, int]:
        task = asyncio.create_task(
            run_headless_assistant(config, controller, status_holder=holder, research_service=FakeService()),
        )
        listening = False
        for _ in range(200):
            if holder.snapshot().listening:
                listening = True
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return listening, started["start"]

    listening, start_calls = asyncio.run(_drive())
    assert listening is True  # actual bound socket state was published
    assert start_calls == 1  # automatic research resumed without a button press
    assert holder.snapshot().listening is False  # unbound on shutdown
