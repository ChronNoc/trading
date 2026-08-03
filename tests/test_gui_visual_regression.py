"""Blessed offscreen visual regression coverage for every GUI state and screen.

Regenerate intentionally with::

    .venv\\Scripts\\python.exe -m tools.bless_gui_baselines

Normal tests never rewrite a missing or changed fixture.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.gui.app_window import AppWindow
from app.gui.review_snapshot import (
    build_empty_snapshot,
    build_partial_snapshot,
    build_review_snapshot,
)
from app.gui.screens import SCREEN_ORDER
from app.gui.view_models import AppSnapshot
from app.gui.visual_regression import (
    build_diff_image,
    capture_window_image,
    compare_images,
    load_image_array,
    save_image_array,
    screen_slug,
)

BASELINE_DIR = Path(__file__).parent / "fixtures" / "gui_baselines"
CHANNEL_TOLERANCE = 8
MAX_MEAN_ABSOLUTE_ERROR = 0.35
MAX_CHANGED_PIXEL_FRACTION = 0.0025

SnapshotBuilder = Callable[[], AppSnapshot]
STATES: tuple[tuple[str, SnapshotBuilder], ...] = (
    ("empty", build_empty_snapshot),
    ("partial", build_partial_snapshot),
    ("full", build_review_snapshot),
)
CASES = tuple(
    (state_name, builder, screen_name)
    for state_name, builder in STATES
    for screen_name in SCREEN_ORDER
)
EXPECTED_FIXTURE_NAMES = {
    f"{state_name}__{screen_slug(screen_name)}.png"
    for state_name, _, screen_name in CASES
}


def test_gui_baseline_inventory_is_exact() -> None:
    """Require one and only one blessed PNG for each state/screen pair."""
    actual_names = {path.name for path in BASELINE_DIR.glob("*.png")}

    assert actual_names == EXPECTED_FIXTURE_NAMES
    assert len(actual_names) == 24


@pytest.mark.parametrize(
    ("state_name", "snapshot_builder", "screen_name"),
    CASES,
    ids=(f"{state}-{screen_slug(screen)}" for state, _, screen in CASES),
)
def test_blessed_gui_screen_matches_baseline(
    qtbot: object,
    tmp_path: Path,
    state_name: str,
    snapshot_builder: SnapshotBuilder,
    screen_name: str,
) -> None:
    """Keep all eight destinations stable in empty, partial, and full states."""
    window = AppWindow(snapshot_provider=snapshot_builder, start_timer=False)
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    actual = capture_window_image(window, screen_name)

    fixture_name = f"{state_name}__{screen_slug(screen_name)}.png"
    baseline_path = BASELINE_DIR / fixture_name
    assert baseline_path.is_file(), (
        f"missing blessed GUI fixture {baseline_path}; regenerate explicitly with "
        ".venv\\Scripts\\python.exe -m tools.bless_gui_baselines"
    )

    expected = load_image_array(baseline_path)
    visual_diff = compare_images(
        actual,
        expected,
        channel_tolerance=CHANNEL_TOLERANCE,
    )
    if not visual_diff.is_within(
        max_mean_absolute_error=MAX_MEAN_ABSOLUTE_ERROR,
        max_changed_pixel_fraction=MAX_CHANGED_PIXEL_FRACTION,
    ):
        artifact_dir = tmp_path / "gui_visual_diff"
        actual_path = artifact_dir / f"{fixture_name.removesuffix('.png')}__actual.png"
        diff_path = artifact_dir / f"{fixture_name.removesuffix('.png')}__diff.png"
        save_image_array(actual, actual_path)
        save_image_array(build_diff_image(actual, expected), diff_path)
        pytest.fail(
            f"GUI visual regression for {state_name}/{screen_name}: "
            f"{visual_diff.describe()}; actual={actual_path}; diff={diff_path}"
        )
