"""The strategy constants audit must stay complete, honest and current."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.research import constants_audit
from app.research.constants_audit import (
    KINDS,
    RATINGS,
    REPORT_RELATIVE_PATH,
    RISKS,
    discover_parameters,
    render_report,
    stale_ratings,
    unrated,
)


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def test_every_tunable_number_has_a_rating() -> None:
    """A new threshold or cost assumption cannot slip in without being rated."""
    assert unrated() == [], "add each to RATINGS in app/research/constants_audit.py, then run python -m tools.constants_audit"


def test_no_rating_outlives_its_parameter() -> None:
    assert stale_ratings() == []


def test_ratings_use_only_the_documented_vocabulary() -> None:
    for owner, fields in RATINGS.items():
        for name, rating in fields.items():
            assert rating.kind in KINDS, f"{owner}.{name}"
            assert rating.risk in RISKS, f"{owner}.{name}"
            assert rating.role.strip() and rating.note.strip(), f"{owner}.{name}"


def test_the_committed_report_is_current() -> None:
    path = Path(REPORT_RELATIVE_PATH)

    assert path.is_file(), "run python -m tools.constants_audit"
    assert _normalise(path.read_text(encoding="utf-8")) == _normalise(render_report()), (
        "the audit report is out of date; run python -m tools.constants_audit"
    )


def test_a_parameter_added_without_a_rating_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    trimmed = {owner: dict(fields) for owner, fields in RATINGS.items()}
    del trimmed["OrderFlowThresholds"]["max_dol_distance_ticks"]
    monkeypatch.setattr(constants_audit, "RATINGS", trimmed)

    assert unrated() == ["OrderFlowThresholds.max_dol_distance_ticks"]


def test_a_rating_for_a_removed_parameter_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    extended = {owner: dict(fields) for owner, fields in RATINGS.items()}
    extended["OrderFlowThresholds"]["a_threshold_nobody_defined"] = RATINGS["OrderFlowThresholds"]["tick_size"]
    monkeypatch.setattr(constants_audit, "RATINGS", extended)

    assert stale_ratings() == ["OrderFlowThresholds.a_threshold_nobody_defined"]


def test_the_scope_covers_the_decisions_that_matter() -> None:
    owners = {item.owner for item in discover_parameters()}

    assert {"OrderFlowThresholds", "MomentumThresholds", "RegimeClassifier", "SessionTrainingConfig",
            "EpisodeConfig", "ExecutionConfig", "ProgressConfig", "ActivationSpec"} <= owners


def test_the_report_names_the_fixed_point_thresholds_that_most_need_checking() -> None:
    report = render_report()
    high_section = report.split("## Validate or scale these first (HIGH)", 1)[1].split("## Same name in several places", 1)[0]

    for key in ("OrderFlowThresholds.max_dol_distance_ticks", "OrderFlowThresholds.large_block_minimum",
                "SessionTrainingConfig.target_ticks", "RegimeClassifier.low_volatility",
                "EpisodeConfig.stop_buffer_points"):
        assert f"`{key}`" in high_section
    # Disabled placeholders and safety limits are not fragile, so they stay out of the HIGH list.
    assert "`ExecutionConfig.max_entries_per_day`" not in high_section
    assert "`TrailConfig.distance_ticks`" not in high_section


def test_the_report_surfaces_duplicates_whose_values_differ() -> None:
    report = render_report()
    duplicates = report.split("## Same name in several places", 1)[1].split("## Every parameter", 1)[0]
    row = next(line for line in duplicates.splitlines() if line.startswith("| `commission_per_contract`"))

    assert "**differ**" in row  # the Lab charges 0.62 per side; the paper engine 1.24
    tick_row = next(line for line in duplicates.splitlines() if line.startswith("| `tick_size`"))
    assert tick_row.endswith("agree |")
