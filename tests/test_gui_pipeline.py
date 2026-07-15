"""GUI tests for the STAGE 3 pipeline panel and the profitability meter."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtWidgets import QListWidget, QProgressBar

from app.discovery.supervisor import ModeSupervisor
from app.gui.main_window import MainWindow


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
