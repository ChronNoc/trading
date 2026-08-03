"""Small accessible charts that paint immutable snapshot series only."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.gui.view_models import MarketHistoryPoint


class HistoryChart(QWidget):
    """Single-series line chart with direct labels and a textual table twin."""

    def __init__(self, title: str, object_name: str, series: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self._title = title
        self._series = series
        self._points: tuple[MarketHistoryPoint, ...] = ()
        self.setMinimumHeight(112)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)

    def set_history(self, points: tuple[MarketHistoryPoint, ...]) -> None:
        """Replace the entire bounded history; the widget owns no market state."""
        self._points = points
        self.setAccessibleDescription(self.table_text())
        self.update()

    def _value(self, point: MarketHistoryPoint) -> Decimal | None:
        value = getattr(point, self._series)
        return value if isinstance(value, Decimal) else None

    def table_text(self) -> str:
        """Return a non-visual summary equivalent to the chart."""
        values = [value for point in self._points if (value := self._value(point)) is not None]
        if not values:
            return f"{self._title}: no observed samples"
        return (
            f"{self._title}: {len(values)} samples; first {values[0]}; "
            f"low {min(values)}; high {max(values)}; latest {values[-1]}"
        )

    def paintEvent(self, event: object) -> None:  # noqa: N802
        """Paint a restrained line chart; labels carry exact values."""
        from app.gui import design_tokens as dt
        
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(12, 20, -12, -20)
        
        # Premium color palette
        muted = QColor(dt.COLOR_TEXT_TERTIARY)
        ink = QColor(dt.COLOR_TEXT_PRIMARY)
        line = QColor(dt.COLOR_PRIMARY)
        grid = QColor(dt.COLOR_GLASS_BORDER)
        
        painter.setPen(muted)
        painter.drawText(12, 15, self._title)
        values = [(point, value) for point in self._points if (value := self._value(point)) is not None]
        if len(values) < 2:
            painter.setPen(QColor(dt.COLOR_TEXT_SECONDARY))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Awaiting observed samples")
            return
        minimum = min(value for _, value in values)
        maximum = max(value for _, value in values)
        span = maximum - minimum
        if span == 0:
            span = Decimal("1")
        
        # Premium grid lines
        painter.setPen(QPen(grid, 1))
        for fraction in (0.0, 0.5, 1.0):
            y = rect.top() + rect.height() * fraction
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        
        # Chart line
        path = QPainterPath()
        for index, (_, value) in enumerate(values):
            x = rect.left() + rect.width() * index / max(1, len(values) - 1)
            ratio = float((value - minimum) / span)
            y = rect.bottom() - rect.height() * ratio
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        # Static area-gradient fill under the line, fading to transparent
        area = QPainterPath(path)
        last_x = rect.left() + rect.width() * (len(values) - 1) / max(1, len(values) - 1)
        area.lineTo(last_x, rect.bottom())
        area.lineTo(rect.left(), rect.bottom())
        area.closeSubpath()
        gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
        fill_color = QColor(dt.COLOR_PRIMARY)
        fill_color.setAlpha(70)
        gradient.setColorAt(0.0, fill_color)
        transparent = QColor(dt.COLOR_PRIMARY)
        transparent.setAlpha(0)
        gradient.setColorAt(1.0, transparent)
        painter.fillPath(area, gradient)

        painter.setPen(QPen(line, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(path)
        
        # Latest value label
        latest = values[-1][1]
        label = f"{latest:,.2f}"
        painter.setPen(ink)
        metrics = QFontMetrics(painter.font())
        painter.drawText(
            int(rect.right() - metrics.horizontalAdvance(label)),
            int(rect.top() - 4),
            label,
        )


class AggressorBar(QWidget):
    """Horizontal split bar showing bid vs ask aggression ratio."""

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self._bid_fraction = 0.5
        self.setMinimumHeight(20)
        self.setMaximumHeight(20)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Aggressor split")

    def set_split(self, bid_fraction: float) -> None:
        """Set the bid aggressor fraction; 0.5 means balanced."""
        self._bid_fraction = max(0.0, min(1.0, bid_fraction))
        self.setAccessibleDescription(f"Bid {self._bid_fraction:.1%}, Ask {1 - self._bid_fraction:.1%}")
        self.update()

    def set_values(
        self,
        bid_volume: Decimal | None,
        ask_volume: Decimal | None,
    ) -> None:
        """Set aggressor volumes and use an even split before data arrives."""
        if bid_volume is None or ask_volume is None:
            self.set_split(0.5)
            return
        total = bid_volume + ask_volume
        if total > 0:
            self.set_split(float(bid_volume / total))
        else:
            self.set_split(0.5)

    def paintEvent(self, event: object) -> None:  # noqa: N802
        """Paint a thin labelled split bar without status colours."""
        from app.gui import design_tokens as dt
        
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.rect()
        split_x = int(rect.width() * self._bid_fraction)
        
        # Premium colors
        bid_color = QColor(dt.COLOR_SUCCESS)
        ask_color = QColor(dt.COLOR_ERROR)
        border_color = QColor(dt.COLOR_GLASS_BORDER)
        
        # Draw bid side
        bid_rect = QRectF(0, 0, split_x, rect.height())
        painter.fillRect(bid_rect, bid_color)
        
        # Draw ask side
        ask_rect = QRectF(split_x, 0, rect.width() - split_x, rect.height())
        painter.fillRect(ask_rect, ask_color)
        
        # Draw border
        painter.setPen(QPen(border_color, 1))
        painter.drawRect(QRectF(rect).adjusted(0, 0, -1, -1))
        
        # Draw center split line
        painter.setPen(QPen(QColor(dt.COLOR_BASE_DARKEST), 2))
        painter.drawLine(split_x, 0, split_x, rect.height())


class ChartPanel(QWidget):
    """Chart plus a visible text equivalent for copying and accessibility."""

    def __init__(self, chart: HistoryChart) -> None:
        from app.gui import design_tokens as dt
        
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(chart, 1)
        self.chart = chart
        self.summary = QLabel(chart.table_text())
        self.summary.setProperty("role", "muted")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setStyleSheet(
            f"""
            color: {dt.COLOR_TEXT_TERTIARY};
            font-size: {dt.FONT_SIZE_SM}px;
            """
        )
        layout.addWidget(self.summary)

    def set_history(self, points: tuple[MarketHistoryPoint, ...]) -> None:
        """Update visual and text forms together."""
        self.chart.set_history(points)
        self.summary.setText(self.chart.table_text())
