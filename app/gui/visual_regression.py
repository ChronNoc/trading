"""Deterministic Qt image capture and pixel-diff helpers.

The visual-regression suite uses Qt's offscreen platform.  It is intentionally
separate from native, human-review screenshots: offscreen rendering is stable
enough for blessed geometry/colour checks even when the platform has no fonts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

if TYPE_CHECKING:
    from app.gui.app_window import AppWindow


@dataclass(frozen=True, slots=True)
class VisualDiff:
    """Quantitative difference between two RGB32 captures."""

    actual_shape: tuple[int, ...]
    expected_shape: tuple[int, ...]
    max_channel_delta: int
    mean_absolute_error: float
    changed_pixel_fraction: float

    @property
    def shapes_match(self) -> bool:
        """Return whether both captures have identical dimensions."""
        return self.actual_shape == self.expected_shape

    def is_within(
        self,
        *,
        max_mean_absolute_error: float,
        max_changed_pixel_fraction: float,
    ) -> bool:
        """Return whether this diff remains inside both visual budgets."""
        return (
            self.shapes_match
            and self.mean_absolute_error <= max_mean_absolute_error
            and self.changed_pixel_fraction <= max_changed_pixel_fraction
        )

    def describe(self) -> str:
        """Return compact diagnostics suitable for an assertion message."""
        return (
            f"shape={self.actual_shape} expected={self.expected_shape}; "
            f"max_delta={self.max_channel_delta}; "
            f"mean_abs_error={self.mean_absolute_error:.6f}; "
            f"changed_pixels={self.changed_pixel_fraction:.6%}"
        )


def screen_slug(screen_name: str) -> str:
    """Return a stable filesystem slug for one navigation destination."""
    return re.sub(r"[^a-z0-9]+", "_", screen_name.lower()).strip("_")


def qimage_to_array(image: QImage) -> np.ndarray:
    """Copy a QImage into a tightly packed ``height x width x 4`` RGB32 array."""
    converted = image.convertToFormat(QImage.Format.Format_RGB32)
    width = converted.width()
    height = converted.height()
    bytes_per_line = converted.bytesPerLine()
    raw = np.frombuffer(bytes(converted.constBits()), dtype=np.uint8)
    rows = raw.reshape(height, bytes_per_line)
    return rows[:, : width * 4].reshape(height, width, 4).copy()


def capture_window_image(window: AppWindow, screen_name: str) -> np.ndarray:
    """Synchronously render and capture one screen from an immutable snapshot."""
    window.resize(1366, 768)
    window.navigate_to(screen_name)
    window.refresh_from_snapshot()
    window.show()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    return qimage_to_array(window.grab().toImage())


def load_image_array(path: Path) -> np.ndarray:
    """Load a PNG fixture as an RGB32 array, failing clearly for bad files."""
    image = QImage(str(path))
    if image.isNull():
        raise ValueError(f"unable to load GUI baseline: {path}")
    return qimage_to_array(image)


def save_image_array(array: np.ndarray, path: Path) -> None:
    """Save a tightly packed RGB32 array as a PNG."""
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 4:
        raise ValueError(f"expected uint8 HxWx4 image, got {array.dtype} {array.shape}")
    contiguous = np.ascontiguousarray(array)
    height, width, _ = contiguous.shape
    image = QImage(
        contiguous.data,
        width,
        height,
        contiguous.strides[0],
        QImage.Format.Format_RGB32,
    ).copy()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise OSError(f"failed to save GUI image: {path}")


def compare_images(
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    channel_tolerance: int,
) -> VisualDiff:
    """Measure RGB differences, ignoring deltas within ``channel_tolerance``."""
    actual_shape = tuple(actual.shape)
    expected_shape = tuple(expected.shape)
    if actual_shape != expected_shape:
        return VisualDiff(actual_shape, expected_shape, 255, float("inf"), 1.0)

    delta = np.abs(
        actual[..., :3].astype(np.int16) - expected[..., :3].astype(np.int16)
    )
    changed_pixels = np.any(delta > channel_tolerance, axis=2)
    return VisualDiff(
        actual_shape=actual_shape,
        expected_shape=expected_shape,
        max_channel_delta=int(delta.max(initial=0)),
        mean_absolute_error=float(delta.mean()),
        changed_pixel_fraction=float(changed_pixels.mean()),
    )


def build_diff_image(actual: np.ndarray, expected: np.ndarray) -> np.ndarray:
    """Build an amplified RGB32 diagnostic image for a failed comparison."""
    if actual.shape != expected.shape:
        return actual.copy()
    delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    diagnostic = np.clip(delta * 4, 0, 255).astype(np.uint8)
    diagnostic[..., 3] = 255
    return diagnostic
