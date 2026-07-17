"""The profitability meter is computed off-thread and only READ by the GUI.

``load_progress`` builds the session catalog and reads processed episodes. Doing
that on the Qt timer is precisely what starved the receiver of the GIL and froze
the window (low CPU, unresponsive UI). These tests pin the split: the research
thread computes, the GUI reads a cached value and never touches disk.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.research.profitability_progress import ProgressCache


def test_snapshot_is_none_before_any_refresh_and_never_computes(tmp_path: Path) -> None:
    """Reading must never trigger the expensive computation."""
    cache = ProgressCache(tmp_path / "raw", tmp_path / "processed")
    assert cache.snapshot() is None
    assert cache.computed_at == 0.0


def test_refresh_populates_the_cache_and_snapshot_then_serves_it(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    (tmp_path / "processed").mkdir()
    cache = ProgressCache(tmp_path / "raw", tmp_path / "processed")
    assert cache.refresh_if_due() is True
    progress = cache.snapshot()
    assert progress is not None, cache.error
    assert 0.0 <= progress.fraction <= 1.0
    # An empty tree can never support a profitability claim.
    assert progress.profitable_claim_supported is False


def test_refresh_is_rate_limited_so_it_cannot_starve_capture(tmp_path: Path) -> None:
    """A second refresh inside the interval must not redo the disk work."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "processed").mkdir()
    cache = ProgressCache(tmp_path / "raw", tmp_path / "processed", min_interval_seconds=300.0)
    assert cache.refresh_if_due() is True
    assert cache.is_due() is False
    assert cache.refresh_if_due() is False, "the meter must not recompute on every cycle"


def test_refresh_becomes_due_again_after_the_interval(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    (tmp_path / "processed").mkdir()
    cache = ProgressCache(tmp_path / "raw", tmp_path / "processed", min_interval_seconds=60.0)
    cache.refresh_if_due(now=1000.0)
    assert cache.is_due(now=1030.0) is False
    assert cache.is_due(now=1061.0) is True


def test_a_failing_computation_is_recorded_and_never_kills_research(tmp_path: Path) -> None:
    """A broken meter must not take the research loop down with it."""
    cache = ProgressCache(tmp_path / "missing_raw", tmp_path / "missing_processed")
    assert cache.refresh_if_due() is True  # must not raise
    if cache.snapshot() is None:
        assert cache.error, "a failure must be reported, not silent"


def test_gui_snapshot_source_reads_the_cache_without_computing(tmp_path: Path) -> None:
    """The GUI path must serve whatever the research thread last computed."""
    from app.gui.snapshot_source import SnapshotSource

    (tmp_path / "raw").mkdir()
    (tmp_path / "processed").mkdir()
    cache = ProgressCache(tmp_path / "raw", tmp_path / "processed")

    class _Research:
        progress_cache = cache

        def status(self):  # noqa: ANN202 - test stub
            from app.research.research_service import ServiceStatus

            return ServiceStatus()

    source = SnapshotSource(research_service=_Research())
    before = source._profitability_snapshot()  # noqa: SLF001 - verifying the read path
    assert before.computed is False, "the GUI must not compute the meter itself"

    cache.refresh_if_due()  # the research thread does the work
    after = source._profitability_snapshot()  # noqa: SLF001
    assert after.computed is True
    assert after.claim_supported is False


def test_snapshot_source_survives_a_service_without_a_cache() -> None:
    """An older/absent research service must not break the GUI."""
    from app.gui.snapshot_source import SnapshotSource

    source = SnapshotSource(research_service=None)
    snapshot = source._profitability_snapshot()  # noqa: SLF001
    assert snapshot.computed is False


def test_summary_never_claims_profitability_without_evidence() -> None:
    from app.gui.view_models import ProfitabilitySnapshot

    unproven = ProfitabilitySnapshot(fraction=0.067, computed=True, claim_supported=False)
    assert "NOT claimed" in unproven.summary
    assert "7%" in unproven.summary
    pending = ProfitabilitySnapshot()
    assert "computing" in pending.summary


def test_research_service_refreshes_the_meter_on_its_own_thread() -> None:
    """Regression guard: the meter must be computed by research, not by Qt."""
    source = Path("app/research/research_service.py").read_text(encoding="utf-8")
    assert "self.progress_cache.refresh_if_due()" in source
    gui = Path("app/gui/snapshot_source.py").read_text(encoding="utf-8")
    assert "load_progress" not in gui, "the GUI must never call the disk-reading loader"
