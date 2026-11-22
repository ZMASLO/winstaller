"""Left navigation rail (custom painted buttons with a counter badge)."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPaintEvent
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QVBoxLayout, QWidget

from winstaller.ui.theme import Role, ThemeManager, font
from winstaller.ui.widgets.common import CursorBlock, hbox, label


class NavButton(QAbstractButton):
    def __init__(
        self,
        page_id: str,
        number: int,
        text: str,
        theme: ThemeManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.page_id = page_id
        self.number = number
        self._theme = theme
        self._badge = ""
        self._badge_tone = "muted"
        self._extra = ""  # second badge, left of the first (e.g. "WU 2/5" on Przebieg)
        self._extra_tone = "muted"
        self._hover = False
        self.setText(text)
        self.setObjectName(f"nav_{page_id}")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        theme.changed.connect(lambda _p: self.update())

    @property
    def badge(self) -> str:
        return self._badge

    def set_badge(self, text: str, tone: str = "muted") -> None:
        if (text, tone) != (self._badge, self._badge_tone):
            self._badge, self._badge_tone = text, tone
            self.update()

    @property
    def extra_badge(self) -> str:
        return self._extra

    @property
    def extra_badge_tone(self) -> str:
        return self._extra_tone

    @property
    def badge_tone(self) -> str:
        return self._badge_tone

    def set_extra_badge(self, text: str, tone: str = "muted") -> None:
        if (text, tone) != (self._extra, self._extra_tone):
            self._extra, self._extra_tone = text, tone
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(220, 42)

    def enterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        pal = self._theme.palette
        p = QPainter(self)
        r = self.rect()
        active = self.isChecked()
        if active or self._hover or self.hasFocus():
            p.fillRect(r, QColor(pal.surface2))
        if active:
            p.fillRect(QRect(0, 0, 3, r.height()), QColor(pal.accent_fg))
        if self.hasFocus():
            p.setPen(QColor(pal.text))
            p.drawRect(r.adjusted(1, 1, -2, -2))
        mid = Qt.AlignmentFlag.AlignVCenter
        num_font = font(Role.LABEL_SM)
        p.setFont(num_font)
        p.setPen(QColor(pal.accent_fg if active else pal.faint))
        p.drawText(QRect(24, 0, 28, r.height()), mid, f"{self.number:02d}")
        text_font = font(Role.LABEL)
        p.setFont(text_font)
        p.setPen(QColor(pal.text if active or self._hover else pal.muted))
        right = r.width() - 18
        for text, tone in ((self._badge, self._badge_tone), (self._extra, self._extra_tone)):
            if not text:
                continue
            fm = QFontMetrics(num_font)
            w = fm.horizontalAdvance(text) + 4
            color = {"accent": pal.accent_fg, "error": pal.error_fg, "text": pal.text}.get(
                tone, pal.muted
            )
            p.setFont(num_font)
            p.setPen(QColor(color))
            p.drawText(QRect(right - w, 0, w, r.height()), mid | Qt.AlignmentFlag.AlignRight, text)
            right -= w + 6
        if self._badge or self._extra:
            p.setFont(text_font)
            p.setPen(QColor(pal.text if active or self._hover else pal.muted))
        p.drawText(QRect(56, 0, right - 56, r.height()), mid, self.text())
        p.end()


class NavRail(QWidget):
    """Brand header, page buttons, footer. ``pageRequested(page_id)``."""

    pageRequested = Signal(str)

    def __init__(
        self,
        pages: list[tuple[str, str]],
        theme: ThemeManager,
        *,
        version: str,
        arch: str,
        dry_run: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(224)
        self.buttons: dict[str, NavButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 26, 0, 18)
        lay.setSpacing(0)

        brand = label("Winstaller", Role.H2, object_name="brandTitle")
        cursor = CursorBlock(theme)
        head = hbox(brand, 4, cursor, None, spacing=0)
        head.setContentsMargins(24, 0, 16, 0)
        head.setAlignment(cursor, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(head)
        meta = label(f"v{version} · {arch}", Role.LABEL_SM, tone="muted", object_name="brandMeta")
        meta.setContentsMargins(24, 4, 16, 0)
        lay.addWidget(meta)
        if dry_run:
            badge = label("[DRY-RUN]", Role.LABEL_SM, tone="badge", object_name="dryRunBadge")
            badge.setToolTip("Tryb symulacji: nic nie jest instalowane ani zmieniane w systemie.")
            row = hbox(badge, None)
            row.setContentsMargins(24, 12, 16, 0)
            lay.addLayout(row)
        lay.addSpacing(28)

        for n, (page_id, text) in enumerate(pages, start=1):
            btn = NavButton(page_id, n, text, theme)
            btn.clicked.connect(lambda _c=False, pid=page_id: self.pageRequested.emit(pid))
            self._group.addButton(btn)
            self.buttons[page_id] = btn
            lay.addWidget(btn)
        lay.addStretch(1)
        footer = label("// by ZMASLO", Role.LABEL_SM, tone="faint", object_name="navFooter")
        footer.setContentsMargins(24, 0, 16, 0)
        lay.addWidget(footer)

    def set_current(self, page_id: str) -> None:
        btn = self.buttons.get(page_id)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def set_badge(self, page_id: str, text: str, tone: str = "muted") -> None:
        btn = self.buttons.get(page_id)
        if btn is not None:
            btn.set_badge(text, tone)

    def set_extra_badge(self, page_id: str, text: str, tone: str = "muted") -> None:
        btn = self.buttons.get(page_id)
        if btn is not None:
            btn.set_extra_badge(text, tone)
