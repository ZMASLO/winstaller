"""Application icon drawn in code: a terminal prompt and the yellow block cursor."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_BG = QColor("#0D0D0D")
_FG = QColor("#F0F0EE")
_ACCENT = QColor("#E9C84A")


def _render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64.0
    p.fillRect(QRectF(0, 0, size, size), _BG)
    # ">" prompt
    pen = QPen(_FG, max(1.6, 6.0 * s))
    pen.setCapStyle(Qt.PenCapStyle.SquareCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(pen)
    path = QPainterPath(QPointF(12 * s, 18 * s))
    path.lineTo(QPointF(27 * s, 32 * s))
    path.lineTo(QPointF(12 * s, 46 * s))
    p.drawPath(path)
    # block cursor
    p.fillRect(QRectF(34 * s, 40 * s, 18 * s, 8 * s), _ACCENT)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_render(size))
    return icon
