"""Premium glass-morphism widgets using dark trading aesthetic.

Reusable components styled with design_tokens for visual consistency.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget

from app.gui import design_tokens as dt


class GlassCard(QFrame):
    """Glass morphism card with subtle depth and glow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("GlassCard")
        self.setStyleSheet(
            f"""
            #GlassCard {{
                background-color: {dt.COLOR_BASE_MID};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_LG}px;
                padding: {dt.SPACE_MD}px;
            }}
            """
        )


class PremiumStatTile(QFrame):
    """KPI display tile with value, label, and optional trend."""

    def __init__(
        self,
        label: str,
        value: str,
        parent: QWidget | None = None,
        *,
        trend: str | None = None,
        trend_positive: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PremiumStatTile")

        layout = QVBoxLayout(self)
        layout.setSpacing(dt.SPACE_XS)
        layout.setContentsMargins(dt.SPACE_MD, dt.SPACE_MD, dt.SPACE_MD, dt.SPACE_MD)

        # Value (large, prominent)
        self._value_label = QLabel(value)
        self._value_label.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_PRIMARY};
            font-size: {dt.FONT_SIZE_XXL}px;
            font-weight: {dt.FONT_WEIGHT_BOLD};
            """
        )
        layout.addWidget(self._value_label)

        # Trend (if provided)
        if trend:
            trend_color = dt.COLOR_SUCCESS if trend_positive else dt.COLOR_ERROR
            self._trend_label = QLabel(trend)
            self._trend_label.setStyleSheet(
                f"""
                color: {trend_color};
                font-size: {dt.FONT_SIZE_SM}px;
                font-weight: {dt.FONT_WEIGHT_MEDIUM};
                """
            )
            layout.addWidget(self._trend_label)

        # Label (secondary, small)
        self._label_widget = QLabel(label)
        self._label_widget.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_SECONDARY};
            font-size: {dt.FONT_SIZE_SM}px;
            font-weight: {dt.FONT_WEIGHT_NORMAL};
            """
        )
        layout.addWidget(self._label_widget)

        self.setStyleSheet(
            f"""
            #PremiumStatTile {{
                background-color: {dt.COLOR_BASE_MID};
                border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
                border-radius: {dt.RADIUS_LG}px;
            }}
            """
        )

    def update_value(self, value: str) -> None:
        """Update the displayed value."""
        self._value_label.setText(value)


class PremiumStatusBadge(QLabel):
    """Compact status indicator with color-coded styling."""

    def __init__(
        self,
        text: str,
        variant: str = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("PremiumStatusBadge")

        # Select badge style
        badge_map = {
            "success": dt.BADGE_SUCCESS,
            "warning": dt.BADGE_WARNING,
            "error": dt.BADGE_ERROR,
            "info": dt.BADGE_INFO,
            "neutral": dt.BADGE_NEUTRAL,
        }
        style = badge_map.get(variant, dt.BADGE_NEUTRAL)

        glow = f"box-shadow: {style.glow};" if style.glow else ""
        border = f"border: {dt.BORDER_WIDTH_THIN}px solid {style.border};" if style.border else ""

        self.setStyleSheet(
            f"""
            #PremiumStatusBadge {{
                background-color: {style.background};
                color: {style.text};
                {border}
                border-radius: {style.border_radius}px;
                padding: {style.padding_y}px {style.padding_x}px;
                font-size: {style.font_size}px;
                font-weight: {style.font_weight};
                {glow}
            }}
            """
        )


class PremiumSection(QFrame):
    """Section container with optional header."""

    def __init__(
        self,
        title: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PremiumSection")

        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(dt.SPACE_MD)
        self._layout.setContentsMargins(0, 0, 0, 0)

        if title:
            header = QLabel(title)
            header.setStyleSheet(
                f"""
                color: {dt.COLOR_TEXT_PRIMARY};
                font-size: {dt.FONT_SIZE_LG}px;
                font-weight: {dt.FONT_WEIGHT_SEMIBOLD};
                """
            )
            self._layout.addWidget(header)

        self._content = GlassCard()
        self._content_layout = QVBoxLayout(self._content)
        self._layout.addWidget(self._content)

    def add_widget(self, widget: QWidget) -> None:
        """Add a widget to the section content."""
        self._content_layout.addWidget(widget)

    def content_layout(self) -> QVBoxLayout:
        """Access the content layout for direct manipulation."""
        return self._content_layout


def apply_dark_theme_stylesheet(widget: QWidget) -> None:
    """Apply global dark theme stylesheet to root widget."""
    widget.setStyleSheet(
        f"""
        QWidget {{
            background-color: {dt.COLOR_BASE_DARKEST};
            color: {dt.COLOR_TEXT_PRIMARY};
            font-family: {dt.FONT_FAMILY_SANS};
            font-size: {dt.FONT_SIZE_MD}px;
        }}

        QMainWindow {{
            background-color: {dt.COLOR_BASE_DARKEST};
        }}

        QPushButton {{
            background-color: {dt.COLOR_BASE_LIGHT};
            color: {dt.COLOR_TEXT_PRIMARY};
            border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
            border-radius: {dt.RADIUS_MD}px;
            padding: {dt.SPACE_SM}px {dt.SPACE_MD}px;
            font-weight: {dt.FONT_WEIGHT_MEDIUM};
        }}

        QPushButton:hover {{
            background-color: {dt.COLOR_GLASS_HOVER};
            border-color: {dt.COLOR_PRIMARY};
        }}

        QPushButton:pressed {{
            background-color: {dt.COLOR_BASE_DARKER};
        }}

        QLineEdit, QTextEdit, QComboBox {{
            background-color: {dt.COLOR_BASE_DARK};
            color: {dt.COLOR_TEXT_PRIMARY};
            border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
            border-radius: {dt.RADIUS_MD}px;
            padding: {dt.SPACE_SM}px;
        }}

        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
            border-color: {dt.COLOR_PRIMARY};
            box-shadow: 0 0 8px {dt.COLOR_PRIMARY_GLOW};
        }}

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

        QScrollBar:vertical {{
            background-color: {dt.COLOR_BASE_DARKER};
            width: 12px;
            border-radius: {dt.RADIUS_SM}px;
        }}

        QScrollBar::handle:vertical {{
            background-color: {dt.COLOR_BASE_LIGHT};
            border-radius: {dt.RADIUS_SM}px;
            min-height: 30px;
        }}

        QScrollBar::handle:vertical:hover {{
            background-color: {dt.COLOR_PRIMARY};
        }}

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}

        QTabWidget::pane {{
            border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
            border-radius: {dt.RADIUS_LG}px;
            background-color: {dt.COLOR_BASE_DARK};
        }}

        QTabBar::tab {{
            background-color: {dt.COLOR_BASE_DARKER};
            color: {dt.COLOR_TEXT_SECONDARY};
            border: {dt.BORDER_WIDTH_THIN}px solid {dt.COLOR_GLASS_BORDER};
            border-bottom: none;
            border-top-left-radius: {dt.RADIUS_MD}px;
            border-top-right-radius: {dt.RADIUS_MD}px;
            padding: {dt.SPACE_SM}px {dt.SPACE_MD}px;
            margin-right: {dt.SPACE_XXS}px;
        }}

        QTabBar::tab:selected {{
            background-color: {dt.COLOR_BASE_DARK};
            color: {dt.COLOR_TEXT_PRIMARY};
            border-bottom: {dt.BORDER_WIDTH_MEDIUM}px solid {dt.COLOR_PRIMARY};
        }}

        QTabBar::tab:hover {{
            background-color: {dt.COLOR_BASE_MID};
            color: {dt.COLOR_PRIMARY_LIGHT};
        }}
        """
    )
