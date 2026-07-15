"""GUI tests for the STAGE 3 pipeline panel and the profitability meter."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtWidgets import QLabel, QListWidget, QProgressBar, QPushButton, QWidget

from app.database.recorder import MarketSessionRecorder
from app.discovery.supervisor import ModeSupervisor
from app.gui.main_window import MainWindow


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


def _window(qtbot: object, tmp_path: Path) -> MainWindow:
    window = MainWindow(
        replay_data_root=tmp_path / "raw",
        processed_root=tmp_path / "processed",
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)
    return window


def test_pipeline_panel_renders_nine_honest_stages_on_cold_start(qtbot: object, tmp_path: Path) -> None:
    """With empty roots and no runtime, all nine stages render honestly."""
    window = _window(qtbot, tmp_path)
    window._refresh_pipeline_and_progress()

    panel = window.findChild(QListWidget, "pipeline_stage_list")
    assert panel is not None
    texts = [panel.item(i).text() for i in range(panel.count())]
    assert len(texts) == 9
    assert texts[0].startswith("[NOT_STARTED] 1. Receiver listening")
    # Demo and live gates are always locked - no fake demo/live availability.
    assert any("[LOCKED] 8. Tradovate DEMO gate" in t for t in texts)
    assert any("[LOCKED] 9. LIVE gate" in t for t in texts)


def test_profitability_meter_is_zero_and_honest_when_no_evidence(qtbot: object, tmp_path: Path) -> None:
    """No completed outcomes: meter reads 0% and claims nothing."""
    window = _window(qtbot, tmp_path)
    window._refresh_pipeline_and_progress()

    bar = window.findChild(QProgressBar, "profitability_meter_bar")
    gates = window.findChild(QListWidget, "profitability_gate_list")
    assert bar is not None and bar.value() == 0
    assert gates is not None
    gate_text = " ".join(gates.item(i).text() for i in range(gates.count()))
    assert "INSUFFICIENT_EVIDENCE" in gate_text  # performance gates cannot be judged yet
    assert "never live-decision" in gate_text.lower() or "never live decisions" in gate_text.lower()


def test_pipeline_reflects_built_sessions_with_zero_completed_setups(qtbot: object, tmp_path: Path) -> None:
    """A built session with zero accepted setups shows honest blocked completed-outcomes stage."""
    processed = tmp_path / "processed"
    processed.mkdir(parents=True)
    (processed / "s.build.json").write_text(json.dumps({
        "evaluations": 20, "accepted_candidates": 0, "completed": 0, "ledger_eligible": 0,
        "replay_quality": {"continuity_ok": False},
        "rejected_condition_tally": {"durable_defending_block": 20},
    }), encoding="utf-8")
    window = _window(qtbot, tmp_path)
    window._refresh_pipeline_and_progress()

    panel = window.findChild(QListWidget, "pipeline_stage_list")
    texts = [panel.item(i).text() for i in range(panel.count())]
    completed_line = next(t for t in texts if "6. Completed paper outcomes" in t)
    assert "[BLOCKED]" in completed_line
    assert "durable_defending_block" in completed_line


def test_analyze_button_builds_finalized_session_in_background(qtbot: object, tmp_path: Path) -> None:
    """The Analyze button runs an idempotent background build and refreshes honestly."""
    _finalized_session(tmp_path / "raw")
    window = _window(qtbot, tmp_path)
    button = window.findChild(QPushButton, "analyze_sessions_button")
    assert button is not None

    window._analyze_finalized_sessions()
    thread = window._build_thread
    thread.join(timeout=30)  # deterministic wait for the daemon worker
    assert not thread.is_alive()
    window._poll_build_thread(window._build_poll_timer)  # drive the completion path

    status = window.findChild(QLabel, "analyze_status_label")
    assert status is not None and "Build complete" in status.text()
    # A build summary was produced for the finalized session (idempotency artifact).
    assert list((tmp_path / "processed").glob("*.build.json"))


def test_auto_research_panel_reports_hardware_and_honest_zero(qtbot: object, tmp_path: Path) -> None:
    """The automated-research panel shows hardware, GPU honesty, and an honest empty state."""
    window = _window(qtbot, tmp_path)  # no service attached -> preview path
    for name in ("research_start_button", "research_pause_button", "research_resume_button",
                 "research_leaderboard_button", "research_high_perf", "research_gpu_enabled",
                 "research_worker_count"):
        assert window.findChild(QWidget, name) is not None, name
    window._run_auto_research_pass()

    panel = window.findChild(QListWidget, "auto_research_list")
    assert panel is not None
    text = " ".join(panel.item(i).text() for i in range(panel.count()))
    assert "CPU cores" in text and "Data capture always has priority" in text
    # GPU honesty: never claims GPU use from mere detection.
    assert "GPU" in text
    assert "nothing to research" in text.lower()
    assert "canonical" in text.lower()


def test_research_controls_drive_the_service(qtbot: object, tmp_path: Path) -> None:
    """Start/Pause/Resume and worker/High-Performance controls drive a real service."""
    from app.research.research_service import ResearchService

    service = ResearchService(tmp_path / "raw", tmp_path / "processed", state_dir=tmp_path / "state")
    window = MainWindow(
        replay_data_root=tmp_path / "raw", processed_root=tmp_path / "processed",
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
        research_service=service,
    )
    qtbot.addWidget(window)

    window.findChild(QWidget, "research_worker_count").setValue(3)
    window.findChild(QWidget, "research_high_perf").setChecked(True)
    window._research_apply_runtime_config()
    assert service.runtime_config.worker_count == 3
    assert service.runtime_config.high_performance is True

    window._research_pause()
    assert service.is_paused is True
    window._research_resume()
    assert service.is_paused is False
    service.stop()


def test_pipeline_receiver_stage_uses_actual_bound_state(qtbot: object, tmp_path: Path) -> None:
    """Receiver-listening reflects the real bound socket, not the controller's existence."""
    from app.runtime.server_state import ReceiverStatusHolder

    holder = ReceiverStatusHolder()
    window = MainWindow(
        replay_data_root=tmp_path / "raw", processed_root=tmp_path / "processed",
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
        receiver_status_provider=holder.snapshot,
    )
    qtbot.addWidget(window)

    window._refresh_pipeline_and_progress()
    panel = window.findChild(QListWidget, "pipeline_stage_list")
    receiver_line = next(panel.item(i).text() for i in range(panel.count()) if "1. Receiver listening" in panel.item(i).text())
    assert "[NOT_STARTED]" in receiver_line  # nothing bound yet

    holder.mark_bound("127.0.0.1", 8765)
    window._refresh_pipeline_and_progress()
    receiver_line = next(panel.item(i).text() for i in range(panel.count()) if "1. Receiver listening" in panel.item(i).text())
    assert "[READY]" in receiver_line  # now genuinely bound

