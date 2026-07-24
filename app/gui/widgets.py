"""Reusable, snapshot-only widgets for the operational dashboard."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    """Elevated section surface with a title and retained body layout."""

    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "card")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(14, 12, 14, 12)
        self.root.setSpacing(10)
        heading = QLabel(title)
        heading.setProperty("role", "section_title")
        heading.setAccessibleName(title)
        self.root.addWidget(heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.root.addLayout(self.body, 1)


class StatTile(QFrame):
    """Compact headline metric with an explicit label and optional detail."""

    def __init__(self, label: str, object_name: str, initial: str = "—") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "stat_tile")
        self.setMinimumHeight(82)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.value = QLabel(initial)
        self.value.setProperty("role", "metric")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)
        self.setAccessibleName(label)

    def set_value(self, value: str, detail: str = "") -> None:
        """Update the displayed metric without changing its semantics."""
        self.value.setText(value)
        self.detail.setText(detail)
        self.setAccessibleDescription(f"{self.caption.text()}: {value}. {detail}".strip())


class StatusBadge(QLabel):
    """Text-and-shape status badge; status is never communicated by colour alone."""

    def __init__(self, text: str, object_name: str, state: str = "neutral") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(text, state)

    def set_status(self, text: str, state: str = "neutral") -> None:
        """Set a labelled status and styling state."""
        self.setText(text)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setAccessibleName(text)


class MetricMeter(QWidget):
    """Labelled meter with observed text and a bounded 0..1 fraction."""

    def __init__(self, label: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.value = QLabel("—")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.caption)
        row.addStretch(1)
        row.addWidget(self.value)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
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


def stat_grid(tiles: tuple[StatTile, ...], columns: int = 3) -> QGridLayout:
    """Lay out statistic tiles in a consistent grid."""
    grid = QGridLayout()
    grid.setSpacing(8)
    for index, tile in enumerate(tiles):
        grid.addWidget(tile, index // columns, index % columns)
    return grid
