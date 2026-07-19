"""Disk/throughput guards, self-test, and redacted diagnostic export."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from app.runtime.disk_guard import (
    DISK_CRITICAL,
    DISK_HEALTHY,
    DISK_WARNING,
    DiskGuard,
    ThroughputGuard,
)

GB = 1024 ** 3


def _guard(tmp_path: Path, free_gb: float) -> DiskGuard:
    return DiskGuard(tmp_path, warning_free_gb=5.0, critical_free_gb=1.0,
                     probe=lambda _p: (100 * GB, 0, int(free_gb * GB)))


def test_disk_levels_classify_correctly(tmp_path: Path) -> None:
    assert _guard(tmp_path, 50).check().level == DISK_HEALTHY
    warning = _guard(tmp_path, 3).check()
    assert warning.level == DISK_WARNING and "research paused" in warning.detail
    critical = _guard(tmp_path, 0.5).check()
    assert critical.level == DISK_CRITICAL
    assert "cannot be guaranteed" in critical.detail


def test_disk_probe_failure_is_critical_not_silent(tmp_path: Path) -> None:
    def broken(_p: Path):  # noqa: ANN202
        raise OSError("device gone")

    state = DiskGuard(tmp_path, probe=broken).check()
    assert state.level == DISK_CRITICAL
    assert "probe failed" in state.detail


def test_throughput_guard_needs_sustained_deficit() -> None:
    guard = ThroughputGuard(consecutive_checks=3)
    guard.observe(0, 0)  # baseline
    assert guard.observe(1000, 900) is False   # one behind sample
    assert guard.observe(2000, 1000) is False  # two
    assert guard.observe(3000, 1100) is True   # three consecutive -> pressure
    # Catching up clears it.
    assert guard.observe(3100, 3100) is False
    assert guard.under_pressure is False


def test_throughput_guard_ignores_idle_periods() -> None:
    guard = ThroughputGuard(consecutive_checks=2)
    guard.observe(0, 0)
    assert guard.observe(0, 0) is False  # nothing arriving is not pressure
    assert guard.observe(0, 0) is False
    assert guard.under_pressure is False


def test_backend_wires_guards_with_the_documented_action_order() -> None:
    source = Path("tools/start_backend.py").read_text(encoding="utf-8")
    assert "DiskGuard(config.output_root)" in source
    assert "ThroughputGuard()" in source
    assert "request_pause()" in source, "research pauses FIRST under pressure"
    assert "resume()" in source, "research resumes when health returns"


def test_selftest_reports_named_checks(tmp_path: Path) -> None:
    from tools.selftest import run_selftest

    rows = run_selftest(tmp_path / "runtime", tmp_path / "raw")
    names = {name for name, _, _ in rows}
    assert {"backend_heartbeat", "receiver_port", "output_writable",
            "disk_space", "tradovate_demo_credentials", "live_locked"} <= names
    verdicts = {name: verdict for name, verdict, _ in rows}
    assert verdicts["live_locked"] == "PASS", "LIVE must be locked"
    assert verdicts["output_writable"] == "PASS"
    assert verdicts["backend_heartbeat"] == "FAIL", "no backend in this sandbox - honest FAIL"


def test_diagnostic_export_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.diagnostic_export import build_export

    secret = "super-secret-value-123"  # noqa: S105
    monkeypatch.setenv("TRADOVATE_DEMO_PASSWORD", secret)
    runtime = tmp_path / "runtime"
    logs = tmp_path / "logs"
    raw = tmp_path / "raw"
    runtime.mkdir(); logs.mkdir(); raw.mkdir()
    (runtime / "backend.out").write_text(f"connected with {secret} token", encoding="utf-8")
    (logs / "assistant.log").write_text("x" * 500_000, encoding="utf-8")  # oversized
    session = raw / "2026-07-19" / "session_x"
    session.mkdir(parents=True)
    (session / "session_manifest.json").write_text('{"session_id": "x"}', encoding="utf-8")

    out = build_export(tmp_path / "diag.zip", runtime_dir=runtime, log_dir=logs, raw_root=raw)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert "runtime/backend.out" in names
        assert "logs/assistant.log" in names
        assert "latest_session_manifest.json" in names
        assert "selftest.txt" in names and "versions.json" in names
        backend_text = archive.read("runtime/backend.out").decode("utf-8")
        assert secret not in backend_text, "credential values must be scrubbed"
        assert "<TRADOVATE_DEMO_PASSWORD>" in backend_text
        assert len(archive.read("logs/assistant.log")) <= 200_000, "tail-bounded"
        # No raw recordings, no env dump, no ledger files.
        assert not any(name.endswith(".parquet") for name in names)
        assert not any("ledger" in name for name in names)
    assert os.path.getsize(out) < 5_000_000
