"""Render the original MNQ Intelligence glass app icon and assemble a .ico.

Uses only PySide6 (already a GUI dependency) rendered offscreen - no Pillow, no
new packages. Produces a multi-resolution Windows .ico (16/32/48/128/256) plus
a 256px PNG source, under assets/branding/. The mark is original: a deep-navy
rounded glass tile with a thin cyan luminous border and an ascending
cyan->violet "intelligence" motif (rising bars + a rising line and node).

    QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python.exe -m tools.generate_app_icon
"""

from __future__ import annotations

import struct
from pathlib import Path

ICON_SIZES = (256, 128, 48, 32, 16)
OUTPUT_DIR = Path("assets/branding")
ICO_NAME = "mnq_intelligence.ico"
PNG_NAME = "mnq_intelligence_256.png"

# Restrained navy / cyan / blue / violet identity.
NAVY_TOP = (14, 20, 36)
NAVY_BOTTOM = (8, 11, 20)
CYAN = (86, 200, 255)
VIOLET = (150, 130, 255)
EMERALD = (72, 210, 160)


def _render_master() -> "object":
    """Render the 256px master icon as a QImage."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QColor,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QRadialGradient,
    )

    size = 256
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)

    # Rounded glass tile with a vertical navy gradient.
    margin = 18
    radius = 52
    tile = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    body = QPainterPath()
    body.addRoundedRect(tile, radius, radius)
    grad = QLinearGradient(QPointF(0, margin), QPointF(0, size - margin))
    grad.setColorAt(0.0, QColor(*NAVY_TOP))
    grad.setColorAt(1.0, QColor(*NAVY_BOTTOM))
    painter.fillPath(body, grad)

    # Ambient corner glow (very subtle) for depth.
    glow = QRadialGradient(QPointF(size * 0.32, size * 0.30), size * 0.55)
    glow.setColorAt(0.0, QColor(CYAN[0], CYAN[1], CYAN[2], 46))
    glow.setColorAt(1.0, QColor(CYAN[0], CYAN[1], CYAN[2], 0))
    painter.fillPath(body, glow)

    # Inner top highlight (glass sheen).
    sheen = QLinearGradient(QPointF(0, margin), QPointF(0, size * 0.5))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillPath(body, sheen)

    # Ascending bars (chart / intelligence rising).
    bar_color = QLinearGradient(QPointF(0, size * 0.75), QPointF(0, size * 0.30))
    bar_color.setColorAt(0.0, QColor(*CYAN))
    bar_color.setColorAt(1.0, QColor(*VIOLET))
    painter.setPen(Qt.NoPen)
    base_y = size * 0.72
    heights = (0.16, 0.28, 0.42)
    bar_w = 26
    xs = (size * 0.34, size * 0.50, size * 0.66)
    for x_center, h in zip(xs, heights):
        top = base_y - size * h
        rect = QRectF(x_center - bar_w / 2, top, bar_w, base_y - top)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.fillPath(path, bar_color)

    # Rising line with a glowing node above the bars.
    node = QPointF(size * 0.66, base_y - size * 0.42 - 8)
    node_glow = QRadialGradient(node, 34)
    node_glow.setColorAt(0.0, QColor(EMERALD[0], EMERALD[1], EMERALD[2], 210))
    node_glow.setColorAt(1.0, QColor(EMERALD[0], EMERALD[1], EMERALD[2], 0))
    painter.fillPath(_dot(node, 34), node_glow)
    painter.setBrush(QColor(230, 255, 245))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(node, 7, 7)

    line = QPen(QColor(EMERALD[0], EMERALD[1], EMERALD[2], 220), 6)
    line.setCapStyle(Qt.RoundCap)
    line.setJoinStyle(Qt.RoundJoin)
    painter.setPen(line)
    painter.setBrush(Qt.NoBrush)
    poly = [
        QPointF(size * 0.30, base_y - size * 0.10),
        QPointF(size * 0.46, base_y - size * 0.20),
        QPointF(size * 0.56, base_y - size * 0.16),
        node,
    ]
    painter.drawPolyline(poly)

    # Thin luminous border.
    border = QPen(QColor(CYAN[0], CYAN[1], CYAN[2], 150), 3)
    painter.setPen(border)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(tile, radius, radius)

    painter.end()
    return image


def _dot(center: "object", r: float) -> "object":
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    path.addEllipse(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r))
    return path


def _png_bytes(image: "object", size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, Qt

    scaled = image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    store = QByteArray()
    buffer = QBuffer(store)
    buffer.open(QBuffer.WriteOnly)
    if not scaled.save(buffer, "PNG"):
        raise RuntimeError(f"failed to encode {size}px PNG")
    return bytes(store)


def _assemble_ico(pngs: dict[int, bytes]) -> bytes:
    """Pack PNG frames into a Vista+ .ico container (PNG-in-ICO is valid)."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    entries = bytearray()
    data = bytearray()
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        png = pngs[size]
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    return bytes(header) + bytes(entries) + bytes(data)


def generate(output_dir: Path = OUTPUT_DIR) -> dict[str, Path]:
    """Render the icon and write the .ico and PNG source; returns their paths."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    master = _render_master()
    pngs = {size: _png_bytes(master, size) for size in ICON_SIZES}

    output_dir.mkdir(parents=True, exist_ok=True)
    ico_path = output_dir / ICO_NAME
    png_path = output_dir / PNG_NAME
    ico_path.write_bytes(_assemble_ico(pngs))
    png_path.write_bytes(pngs[256])
    del app
    return {"ico": ico_path, "png": png_path}


def main() -> int:
    """Generate the icon assets and print their paths."""
    paths = generate()
    for label, path in paths.items():
        print(f"{label}: {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
