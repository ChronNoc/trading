"""Small accessible charts that paint immutable snapshot series only."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
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
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(12, 20, -12, -20)
        muted = QColor("#718096")
        ink = QColor("#dbe7f5")
        line = QColor("#4aa8ff")
        painter.setPen(muted)
        painter.drawText(12, 15, self._title)
        values = [(point, value) for point in self._points if (value := self._value(point)) is not None]
        if len(values) < 2:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Awaiting observed samples")
            return
        minimum = min(value for _, value in values)
        maximum = max(value for _, value in values)
        span = maximum - minimum
        if span == 0:
            span = Decimal("1")
        painter.setPen(QPen(QColor("#2a3442"), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = rect.top() + rect.height() * fraction
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        path = QPainterPath()
        for index, (_, value) in enumerate(values):
            x = rect.left() + rect.width() * index / max(1, len(values) - 1)
            ratio = float((value - minimum) / span)
            y = rect.bottom() - rect.height() * ratio
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(line, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(path)
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
    """Part-to-whole aggressive-volume bar with labels and a legend."""

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self._buy: Decimal | None = None
        self._sell: Decimal | None = None
        self.setMinimumHeight(72)
        self.setAccessibleName("Aggressive buy and sell volume")

    def set_values(self, buy: Decimal | None, sell: Decimal | None) -> None:
        """Set observed values, retaining unavailable as unavailable."""
        self._buy = buy
        self._sell = sell
        if buy is None or sell is None:
            self.setAccessibleDescription("Aggressor-side volume is unavailable")
        else:
            self.setAccessibleDescription(f"Aggressive buys {buy}; aggressive sells {sell}")
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802
        """Paint a thin labelled split bar without status colours."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._buy is None or self._sell is None:
            painter.setPen(QColor("#718096"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Aggressor side unavailable")
            return
        total = self._buy + self._sell
        buy_fraction = float(self._buy / total) if total > 0 else 0.5
        bar = QRectF(self.rect()).adjusted(10, 26, -10, -22)
        buy_rect = QRectF(bar.left(), bar.top(), bar.width() * buy_fraction - 1, bar.height())
        sell_rect = QRectF(buy_rect.right() + 2, bar.top(), bar.width() - buy_rect.width() - 2, bar.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#4aa8ff"))
        painter.drawRoundedRect(buy_rect, 4, 4)
        painter.setBrush(QColor("#ff9f43"))
        painter.drawRoundedRect(sell_rect, 4, 4)
        painter.setPen(QColor("#dbe7f5"))
        painter.drawText(10, 17, f"BUY {self._buy:,.0f}")
        sell_label = f"SELL {self._sell:,.0f}"
        painter.drawText(self.width() - 10 - QFontMetrics(painter.font()).horizontalAdvance(sell_label), 17, sell_label)


class ChartPanel(QWidget):
    """Chart plus a visible text equivalent for copying and accessibility."""

    def __init__(self, chart: HistoryChart) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(chart, 1)
        self.chart = chart
        self.summary = QLabel(chart.table_text())
        self.summary.setProperty("role", "muted")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

    def set_history(self, points: tuple[MarketHistoryPoint, ...]) -> None:
        """Update visual and text forms together."""
        self.chart.set_history(points)
        self.summary.setText(self.chart.table_text())
