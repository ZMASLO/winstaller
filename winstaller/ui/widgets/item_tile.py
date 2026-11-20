"""Clickable toggle tile for one catalog item (custom painted for speed and control)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from winstaller.core.catalog import Availability, Item, ItemKind
from winstaller.ui.theme import Role, ThemeManager, font

TILE_HEIGHT = 66
_PAD = 16
_BOX = 16

KIND_TAGS: dict[ItemKind, str] = {
    ItemKind.WINGET: "[WINGET]",
    ItemKind.WINGET_UNINSTALL: "[WINGET · USUŃ]",
    ItemKind.LOCAL_EXE: "[LOKALNY]",
    ItemKind.ACTION: "[AKCJA]",
    ItemKind.DRIVER: "[STEROWNIK]",
}


def kind_tag(item: Item) -> str:
    return KIND_TAGS.get(item.kind, f"[{item.kind.value.upper()}]")


class ItemTile(QWidget):
    """Shows name, description and kind tag; Space/Enter/click toggles.

    Emits ``toggleRequested(item_id)``; the checked state is pushed in by the
    page from :class:`SelectionModel` (the tile owns no selection state).
    """

    toggleRequested = Signal(str)
    navigate = Signal(object, int)  # (tile, Qt.Key)

    def __init__(
        self,
        item: Item,
        availability: Availability,
        theme: ThemeManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.availability = availability
        self._theme = theme
        self._checked = False
        self._locked = False
        self._hover = False
        self._pressed = False
        self.setObjectName(f"tile_{item.id}")
        self.setFixedHeight(TILE_HEIGHT)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(
            Qt.FocusPolicy.TabFocus if availability.available else Qt.FocusPolicy.NoFocus
        )
        self.setAccessibleName(item.name)
        self.setAccessibleDescription(self._tooltip())
        self.setToolTip(self._tooltip())
        self._update_cursor()
        theme.changed.connect(lambda _p: self.update())

    # -- state
    @property
    def available(self) -> bool:
        return self.availability.available

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if checked != self._checked:
            self._checked = checked
            self.update()

    def set_locked(self, locked: bool) -> None:
        if locked != self._locked:
            self._locked = locked
            self._update_cursor()
            self.update()

    @property
    def interactive(self) -> bool:
        return self.available and not self._locked

    def matches(self, needle: str) -> bool:
        """Case-insensitive match against name, description and winget id."""
        if not needle:
            return True
        hay = " ".join(
            filter(None, (self.item.name, self.item.description, self.item.winget_id))
        ).casefold()
        return all(part in hay for part in needle.casefold().split())

    def _update_cursor(self) -> None:
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self.interactive else Qt.CursorShape.ArrowCursor
        )

    def _tooltip(self) -> str:
        lines = [self.item.name]
        if self.item.description:
            lines.append(self.item.description)
        if self.item.winget_id:
            lines.append(f"winget: {self.item.winget_id}")
        if self.item.confirm:
            lines.append("Wymaga potwierdzenia przed startem.")
        if not self.availability.available:
            lines.append(f"Niedostępne: {self.availability.reason}")
        return "\n".join(lines)

    def sizeHint(self) -> QSize:
        return QSize(380, TILE_HEIGHT)

    # -- input
    def _request_toggle(self) -> None:
        if self.interactive:
            self.toggleRequested.emit(self.item.id)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.interactive:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self._request_toggle()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._request_toggle()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.navigate.emit(self, key)
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.HoverEnter:
            self._hover = True
            self.update()
        elif event.type() == QEvent.Type.HoverLeave:
            self._hover = False
            self._pressed = False
            self.update()
        return super().event(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.update()

    # -- painting
    def paintEvent(self, event: QPaintEvent) -> None:
        pal = self._theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        r = self.rect()
        available = self.available
        enabled = self.interactive

        if not available:
            bg = pal.bg
        elif self._pressed:
            bg = pal.border
        elif self._hover and enabled:
            bg = pal.surface2
        else:
            bg = pal.surface
        p.fillRect(r, QColor(bg))

        if self.hasFocus():
            border, width = pal.text, 2
        elif self._checked:
            border, width = pal.accent_fg, 1
        else:
            border, width = pal.border, 1
        pen = QPen(QColor(border), width)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        if not available:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRect(r.adjusted(1, 1, -1, -1) if width == 2 else r.adjusted(0, 0, -1, -1))
        if self._checked:
            # accent bar on the left edge: selection is recognisable at a glance
            p.fillRect(QRect(0, 0, 3, r.height()), QColor(pal.accent_fg))

        # checkbox
        box = QRect(_PAD, (r.height() - _BOX) // 2, _BOX, _BOX)
        if self._checked:
            p.fillRect(box, QColor(pal.accent))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            check = QPen(QColor(pal.on_accent), 2.0)
            check.setCapStyle(Qt.PenCapStyle.SquareCap)
            p.setPen(check)
            x, y = box.x(), box.y()
            p.drawPolyline(
                [QPointF(x + 3.5, y + 8.5), QPointF(x + 6.5, y + 11.5), QPointF(x + 12.5, y + 4.5)]
            )
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        else:
            p.setPen(QPen(QColor(pal.border_strong if available else pal.faint), 1))
            p.drawRect(box.adjusted(0, 0, -1, -1))
            if not available:
                p.drawLine(box.left() + 4, box.center().y(), box.right() - 4, box.center().y())

        text_x = _PAD + _BOX + 14
        right = r.width() - _PAD

        # tag (top right)
        tag_font = font(Role.LABEL_SM)
        tag = kind_tag(self.item) if available else "[NIEDOSTĘPNE]"
        tag_fm = QFontMetrics(tag_font)
        tag_w = tag_fm.horizontalAdvance(tag) + 4
        p.setFont(tag_font)
        p.setPen(QColor(pal.accent_fg if self._checked else pal.muted if available else pal.faint))
        tag_rect = QRect(right - tag_w, 13, tag_w, 18)
        p.drawText(tag_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tag)

        # name
        name_font = font(Role.BODY_STRONG)
        p.setFont(name_font)
        p.setPen(QColor(pal.text if available else pal.faint))
        name_w = max(40, tag_rect.left() - 12 - text_x)
        name = QFontMetrics(name_font).elidedText(
            self.item.name, Qt.TextElideMode.ElideRight, name_w
        )
        p.drawText(
            QRect(text_x, 12, name_w, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )

        # description or reason
        small = font(Role.SMALL)
        p.setFont(small)
        if available:
            second = self.item.description
            p.setPen(QColor(pal.muted))
        else:
            second = self.availability.reason or "Niedostępne."
            p.setPen(QColor(pal.muted))
        second_w = right - text_x
        second = QFontMetrics(small).elidedText(second, Qt.TextElideMode.ElideRight, second_w)
        p.drawText(
            QRect(text_x, 35, second_w, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            second,
        )
        p.end()
