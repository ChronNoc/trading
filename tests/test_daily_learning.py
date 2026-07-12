"""Tests for observe-only daily market learning reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.daily_learning import (
    analyze_and_write_daily_learning_report,
    analyze_recorded_day,
    render_daily_learning_markdown,
)
from tools.daily_learning_summary import parse_args


def test_daily_learning_report_summarizes_bookmap_recording(tmp_path: Path) -> None:
    """A raw Bookmap recording produces JSON and Markdown consistency reports."""
    raw_root = tmp_path / "raw"
    report_root = tmp_path / "reports"
    trading_date = datetime(2026, 7, 10, 14, 30, tzinfo=UTC).date()
    _record_bookmap_session(raw_root, delayed=False)

    summary = analyze_recorded_day(raw_root, trading_date)
    paths = analyze_and_write_daily_learning_report(raw_root, report_root, trading_date)
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    markdown = paths.markdown_path.read_text(encoding="utf-8")

    assert summary.total_depth_updates == 6
    assert summary.total_trades == 2
    assert summary.sessions[0].bid_reload_count == 1
    assert summary.sessions[0].possible_long_absorption is True
    assert payload["mode"] == "observe_only"
    assert payload["auto_retraining_enabled"] is False
    assert payload["live_decision_ready"] is False
    assert payload["sessions"][0]["possible_long_absorption"] is True
    assert "Observe-only learning report" in markdown
    assert "Possible long absorption" in markdown


def test_daily_learning_marks_delayed_data_review_only(tmp_path: Path) -> None:
    """Delayed/free Bookmap sessions are learned from for review but never live decisions."""
    raw_root = tmp_path / "raw"
    trading_date = datetime(2026, 7, 10, 14, 30, tzinfo=UTC).date()
    _record_bookmap_session(raw_root, delayed=True)

    summary = analyze_recorded_day(raw_root, trading_date)
    markdown = render_daily_learning_markdown(summary)

    assert summary.delayed_session_count == 1
    assert summary.sessions[0].source_mode == "delayed"
    assert summary.sessions[0].valid_for_live_decisions is False
    assert "delayed/free-data session" in summary.blockers[0]
    assert "delayed data: review only" in markdown


def test_daily_learning_cli_parses_date_and_roots(tmp_path: Path) -> None:
    """The manual report command accepts explicit roots and a UTC recording date."""
    config = parse_args(
        [
            "--raw-root",
            str(tmp_path / "raw"),
            "--report-root",
            str(tmp_path / "reports"),
            "--date",
            "2026-07-10",
        ],
    )

    assert config.raw_root == tmp_path / "raw"
    assert config.report_root == tmp_path / "reports"
    assert config.trading_date.isoformat() == "2026-07-10"


def _record_bookmap_session(raw_root: Path, *, delayed: bool) -> None:
    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=raw_root, session_start_utc=start)
    timestamp_ns = int(start.timestamp()) * 1_000_000_000
    if delayed:
        recorder.record_control_event(
            {
                "type": "delayed_mode",
                "timestamp_ns": timestamp_ns,
                "source_mode": "delayed",
                "delay_minutes": 15,
                "reason": "Bookmap free delayed data feed",
            },
        )
    else:
        recorder.record_control_event(
            {
                "type": "connected",
                "timestamp_ns": timestamp_ns,
                "source_mode": "live",
                "alias": "MNQU6",
                "symbol": "MNQU6",
                "dropped_message_count": 0,
            },
        )
        recorder.record_control_event({"type": "realtime_started", "timestamp_ns": timestamp_ns + 1})

    _record_book(recorder, timestamp_ns)
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": timestamp_ns + 8})


def _record_book(recorder: MarketSessionRecorder, timestamp_ns: int) -> None:
    events = (
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 1,
            "symbol": "MNQU6",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "120",
        },
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 2,
            "symbol": "MNQU6",
            "side": "ask",
            "price": "100.25",
            "previous_size": "0",
            "new_size": "80",
        },
        {
            "type": "trade",
            "timestamp_ns": timestamp_ns + 3,
            "price": "100.00",
            "size": "30",
            "aggressor_side": "sell",
            "instrument": "MNQU6",
            "sequence_id": 1,
        },
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 4,
            "symbol": "MNQU6",
            "side": "bid",
            "price": "100.00",
            "previous_size": "120",
            "new_size": "20",
        },
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 5,
            "symbol": "MNQU6",
            "side": "bid",
            "price": "100.00",
            "previous_size": "20",
            "new_size": "130",
        },
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 6,
            "symbol": "MNQU6",
            "side": "ask",
            "price": "100.50",
            "previous_size": "0",
            "new_size": "75",
        },
        {
            "type": "trade",
            "timestamp_ns": timestamp_ns + 7,
            "price": "100.25",
            "size": "5",
            "aggressor_side": "buy",
            "instrument": "MNQU6",
            "sequence_id": 2,
        },
        {
            "type": "depth_update",
            "timestamp": timestamp_ns + 8,
            "symbol": "MNQU6",
            "side": "bid",
            "price": "100.25",
            "previous_size": "0",
            "new_size": "95",
        },
    )
    for event in events:
        recorder.record(event)
