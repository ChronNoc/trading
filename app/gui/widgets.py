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

from app.gui import design_tokens as dt


class Card(QFrame):
    """Elevated section surface with a title and retained body layout."""

    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "card")
        # Apply premium glass morphism styling
        self.setStyleSheet(
            f"""
            #{object_name} {{
                background-color: {dt.COLOR_BASE_MID};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_LG}px;
            }}
            """
        )
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(dt.SPACE_MD, dt.SPACE_SM, dt.SPACE_MD, dt.SPACE_SM)
        self.root.setSpacing(dt.SPACE_SM)
        heading = QLabel(title)
        heading.setProperty("role", "section_title")
        heading.setAccessibleName(title)
        heading.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_PRIMARY};
            font-size: {dt.FONT_SIZE_LG}px;
            font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
            """
        )
        self.root.addWidget(heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(dt.SPACE_XS)
        self.root.addLayout(self.body, 1)


class StatTile(QFrame):
    """Compact headline metric with an explicit label and optional detail."""

    def __init__(self, label: str, object_name: str, initial: str = "—") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "stat_tile")
        self.setMinimumHeight(82)
        # Apply premium stat tile styling
        self.setStyleSheet(
            f"""
            #{object_name} {{
                background-color: {dt.COLOR_BASE_DARK};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_MD}px;
            }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(dt.SPACE_SM, dt.SPACE_XS, dt.SPACE_SM, dt.SPACE_XS)
        layout.setSpacing(dt.SPACE_XXS)
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.caption.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_SECONDARY};
            font-size: {dt.FONT_SIZE_XS}px;
            font-weight: {dt.FONT_WEIGHT_MEDIUM};
            """
        )
        self.value = QLabel(initial)
        self.value.setProperty("role", "metric")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_PRIMARY};
            font-size: {dt.FONT_SIZE_XL}px;
            font-weight: {dt.FONT_WEIGHT_BOLD};
            """
        )
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_TERTIARY};
            font-size: {dt.FONT_SIZE_XS}px;
            """
        )
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
        # Store state for dynamic styling
        self._state = state
        self.set_status(text, state)

    def set_status(self, text: str, state: str = "neutral") -> None:
        """Set a labelled status and styling state."""
        self.setText(text)
        self.setProperty("state", state)
        self._state = state
        # Map states to premium badge styles
        style_map = {
            "ok": (dt.COLOR_SUCCESS_DARK, dt.COLOR_SUCCESS_LIGHT),
            "warn": (dt.COLOR_WARNING_DARK, dt.COLOR_WARNING_LIGHT),
            "fail": (dt.COLOR_ERROR_DARK, dt.COLOR_ERROR_LIGHT),
            "locked": (dt.COLOR_PRIMARY_DARK, dt.COLOR_PRIMARY_LIGHT),
            "neutral": (dt.COLOR_BASE_LIGHT, dt.COLOR_TEXT_SECONDARY),
        }
        bg, fg = style_map.get(state, style_map["neutral"])

        self.setStyleSheet(
            f"""
            #{self.objectName()} {{
                background-color: {bg};
                color: {fg};
                border: {dt.BORDER_WIDTH_MEDIUM}px solid {bg};
                border-radius: {dt.RADIUS_SM}px;
                padding: {dt.SPACE_XXS}px {dt.SPACE_SM}px;
                font-size: {dt.FONT_SIZE_XS}px;
                font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
            }}
            """
        )
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
        layout.setSpacing(dt.SPACE_XXS)
        
        row = QHBoxLayout()
        self.caption = QLabel(label)
        self.caption.setProperty("role", "caption")
        self.caption.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_SECONDARY};
            font-size: {dt.FONT_SIZE_SM}px;
            font-weight: {dt.FONT_WEIGHT_MEDIUM};
            """
        )
        
        self.value = QLabel("—")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_PRIMARY};
            font-size: {dt.FONT_SIZE_SM}px;
            font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
            """
        )
        
        row.addWidget(self.caption)
        row.addStretch(1)
        row.addWidget(self.value)
        
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: {dt.COLOR_BASE_DARK};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_SM}px;
            }}
            QProgressBar::chunk {{
                background-color: {dt.COLOR_PRIMARY};
                border-radius: {dt.RADIUS_SM}px;
            }}
            """
        )
        
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
        from app.gui import design_tokens as dt
        
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "empty_state")
        self.setStyleSheet(
            f"""
            QFrame[role="empty_state"] {{
                background-color: {dt.COLOR_BASE_DARK};
                border: {dt.BORDER_WIDTH_THIN}px dashed {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_LG}px;
                padding: {dt.SPACE_LG}px;
            }}
            """
        )
        layout = QVBoxLayout(self)
        self.title = QLabel("Measurement unavailable")
        self.title.setProperty("role", "section_title")
        self.title.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_PRIMARY};
            font-size: {dt.FONT_SIZE_LG}px;
            font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
            """
        )
        self.reason = QLabel("")
        self.reason.setWordWrap(True)
        self.reason.setProperty("role", "muted")
        self.reason.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_TERTIARY};
            font-size: {dt.FONT_SIZE_SM}px;
            """
        )
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
        
        self.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {dt.COLOR_BASE_DARK};
                alternate-background-color: {dt.COLOR_BASE_MID};
                color: {dt.COLOR_TEXT_PRIMARY};
                gridline-color: {dt.COLOR_GLASS_BORDER};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_LG}px;
            }}
            QTableWidget::item {{
                padding: {dt.SPACE_SM}px;
            }}
            QTableWidget::item:selected {{
                background-color: {dt.COLOR_PRIMARY_DARK};
            }}
            QHeaderView::section {{
                background-color: {dt.COLOR_BASE_MID};
                color: {dt.COLOR_TEXT_SECONDARY};
                padding: {dt.SPACE_SM}px;
                border: none;
                border-bottom: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
            }}
            """
        )

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
