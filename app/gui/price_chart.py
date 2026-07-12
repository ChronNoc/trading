"""Dependency-free price-path chart widget for the prototype dashboard.

Draws the synthetic last-trade price as a polyline with green/red decision
markers where setups were accepted or rejected. Pure QWidget painting -
no charting library, no network, no market interpretation of its own.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

_LINE_COLOR = QColor(96, 165, 250)
_ACCEPTED_COLOR = QColor(74, 222, 128)
_REJECTED_COLOR = QColor(248, 113, 113)
_GRID_COLOR = QColor(51, 65, 85)


class PriceChartWidget(QWidget):
    """Rolling price polyline with decision markers."""

    def __init__(self, parent: QWidget | None = None, *, max_points: int = 600) -> None:
        """Create an empty chart keeping at most ``max_points`` prices."""
        super().__init__(parent)
        self._max_points = max_points
        self._points: list[Decimal] = []
        self._markers: dict[int, bool] = {}
        self.setMinimumHeight(120)

    def point_count(self) -> int:
        """Return the number of plotted prices."""
        return len(self._points)

    def marker_count(self) -> int:
        """Return the number of decision markers."""
        return len(self._markers)

    def add_price(self, price_text: str) -> bool:
        """Append a price when it parses and differs from the last point."""
        try:
            price = Decimal(price_text)
        except (InvalidOperation, ValueError, TypeError):
            return False
        if self._points and self._points[-1] == price:
            return False
        self._points.append(price)
        if len(self._points) > self._max_points:
            overflow = len(self._points) - self._max_points
            self._points = self._points[overflow:]
            self._markers = {
                index - overflow: accepted
                for index, accepted in self._markers.items()
                if index - overflow >= 0
            }
        self.update()
        return True

    def mark_decision(self, *, accepted: bool) -> None:
        """Tag the most recent price point with a decision marker."""
        if not self._points:
            return
        self._markers[len(self._points) - 1] = accepted
        self.update()

    def clear(self) -> None:
        """Remove all points and markers."""
        self._points = []
        self._markers = {}
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Paint the grid, the price polyline, and the decision markers."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()

        painter.setPen(QPen(_GRID_COLOR, 1))
        for fraction in (0.25, 0.5, 0.75):
            y = height * fraction
            painter.drawLine(QPointF(0, y), QPointF(width, y))

        if len(self._points) < 2:
            painter.setPen(QPen(QColor(148, 163, 184), 1))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Waiting for synthetic trades...",
            )
            painter.end()
            return

        low = min(self._points)
        high = max(self._points)
        price_span = high - low
        if price_span == 0:
            price_span = Decimal("1")
        margin = 8.0
        usable_height = height - 2 * margin
        step = (width - 2 * margin) / (len(self._points) - 1)

        def position(index: int) -> QPointF:
            ratio = float((self._points[index] - low) / price_span)
            return QPointF(margin + index * step, margin + (1.0 - ratio) * usable_height)

        painter.setPen(QPen(_LINE_COLOR, 2))
        for index in range(1, len(self._points)):
            painter.drawLine(position(index - 1), position(index))

        for index, accepted in self._markers.items():
            color = _ACCEPTED_COLOR if accepted else _REJECTED_COLOR
            painter.setPen(QPen(color, 2))
            painter.setBrush(color)
            painter.drawEllipse(position(index), 5.0, 5.0)
        painter.end()
