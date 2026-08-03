"""Reusable, snapshot-only widgets for the operational dashboard."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui import design_tokens as dt


class Card(QFrame):
    """Elevated section surface with a title and retained body layout."""

    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "card")

        # Background, border, and title colors remain in the global theme so
        # both dark and light palettes stay authoritative. Qt has no QSS
        # backdrop blur; this theme-neutral shadow supplies restrained depth.
        card_style = dt.CardStyle()
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(card_style.shadow_blur_radius)
        shadow.setColor(QColor(0, 0, 0, card_style.shadow_alpha))
        shadow.setOffset(0, card_style.shadow_offset_y)
        self.setGraphicsEffect(shadow)

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(
            card_style.padding,
            dt.SPACE_SM,
            card_style.padding,
            dt.SPACE_SM,
        )
        self.root.setSpacing(dt.SPACE_SM)
        self.heading = QLabel(title)
        self.heading.setProperty("role", "section_title")
        self.heading.setAccessibleName(title)
        self.root.addWidget(self.heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(dt.SPACE_XS)
        self.root.addLayout(self.body, 1)


class StatTile(QFrame):
    """Compact headline metric with an explicit label and optional detail."""

    def __init__(self, label: str, object_name: str, initial: str = "—") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "stat_tile")
        self.setMinimumHeight(78)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(dt.SPACE_SM, dt.SPACE_XS, dt.SPACE_SM, dt.SPACE_XS)
        layout.setSpacing(dt.SPACE_XXS)
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.value = QLabel(initial)
        self.value.setProperty("role", "metric")
        self.value.setWordWrap(True)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        self.detail.hide()
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)
        self.setAccessibleName(label)

    def set_value(self, value: str, detail: str = "") -> None:
        """Update the displayed metric without changing its semantics."""
        self.value.setText(value)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.setAccessibleDescription(f"{self.caption.text()}: {value}. {detail}".strip())


class StatusBadge(QLabel):
    """Text-and-shape status badge; status is never communicated by colour alone."""

    _VALID_STATES = frozenset({"ok", "warn", "fail", "locked", "neutral"})
    _GLOW_COLORS = {
        "locked": QColor(dt.COLOR_LOCKED_LIGHT),
        "fail": QColor(dt.COLOR_ERROR_LIGHT),
    }

    def __init__(self, text: str, object_name: str, state: str = "neutral") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state = "neutral"
        self.set_status(text, state)

    def set_status(self, text: str, state: str = "neutral") -> None:
        """Set a labelled status and a theme-owned semantic styling state."""
        effective_state = state if state in self._VALID_STATES else "neutral"
        self.setText(text)
        self.setProperty("state", effective_state)
        self._state = effective_state
        self.style().unpolish(self)
        self.style().polish(self)
        self.setAccessibleName(text)
        glow_color = self._GLOW_COLORS.get(effective_state)
        if glow_color is None:
            self.setGraphicsEffect(None)
            return
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(16)
        glow.setColor(glow_color)
        glow.setOffset(0, 0)
        self.setGraphicsEffect(glow)


class MetricMeter(QWidget):
    """Labelled meter with observed text and a bounded 0..1 fraction."""

    def __init__(self, label: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(dt.SPACE_XXS)

        row = QHBoxLayout()
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.value = QLabel("—")
        self.value.setProperty("role", "meter_value")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.caption)
        row.addStretch(1)
        row.addWidget(self.value)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)

        layout.addLayout(row)
        layout.addWidget(self.bar)
        self.setAccessibleName(label)

    def set_fraction(self, fraction: float, text: str) -> None:
        """Update the meter, clamping only its visual fraction."""
        self.bar.setValue(round(max(0.0, min(1.0, fraction)) * 1000))
        self.value.setText(text)
        self.setAccessibleDescription(f"{self.caption.text()}: {text}")


class CapabilityEmptyState(QFrame):
    """Explain why a view is unavailable instead of drawing misleading zeros."""

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "empty_state")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(dt.SPACE_LG, dt.SPACE_LG, dt.SPACE_LG, dt.SPACE_LG)
        self.title = QLabel("Measurement unavailable")
        self.title.setProperty("role", "section_title")
        self.reason = QLabel("")
        self.reason.setWordWrap(True)
        self.reason.setProperty("role", "muted")
        layout.addWidget(self.title)
        layout.addWidget(self.reason)

    def set_reason(self, title: str, reason: str) -> None:
        """Set a human-readable capability explanation."""
        self.title.setText(title)
        self.reason.setText(reason)
        self.setAccessibleName(title)
        self.setAccessibleDescription(reason)


class EvidenceTable(QTableWidget):
    """Accessible compact table with deterministic retained columns."""

    def __init__(self, headers: tuple[str, ...], object_name: str) -> None:
        super().__init__(0, len(headers))
        self.setObjectName(object_name)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.horizontalHeader().setStretchLastSection(True)
        self.setAccessibleName("; ".join(headers))

    def set_rows(self, rows: tuple[tuple[str, ...], ...]) -> None:
        """Replace all table rows from immutable display values."""
        self.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.setItem(row_index, column_index, item)
        self.resizeRowsToContents()
        for column_index in range(max(0, self.columnCount() - 1)):
            self.resizeColumnToContents(column_index)


def stat_grid(tiles: tuple[StatTile, ...], columns: int = 3) -> QGridLayout:
    """Lay out statistic tiles in a consistent grid."""
    grid = QGridLayout()
    grid.setSpacing(8)
    for index, tile in enumerate(tiles):
        grid.addWidget(tile, index // columns, index % columns)
    return grid
