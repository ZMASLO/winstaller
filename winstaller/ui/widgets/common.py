"""Small building blocks shared by pages."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from winstaller.ui.theme import Role, ThemeManager, fonts

PAGE_MARGIN_H = 40
PAGE_MARGIN_TOP = 32


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_prop(widget: QWidget, name: str, value: object) -> None:
    """Set a dynamic property used by QSS selectors and re-apply the style."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


def label(
    text: str = "",
    role: Role = Role.BODY,
    *,
    tone: str | None = None,
    wrap: bool = False,
    object_name: str | None = None,
    selectable: bool = False,
) -> QLabel:
    """QLabel with a font role and an optional colour tone (muted/faint/accent/error/badge)."""
    lab = QLabel(text)
    fonts.apply(lab, role)
    if tone:
        lab.setProperty("role", tone)
    if wrap:
        lab.setWordWrap(True)
    if object_name:
        lab.setObjectName(object_name)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def button(
    text: str,
    *,
    variant: str = "secondary",
    role: Role = Role.LABEL,
    on_click: Callable[[], object] | None = None,
    object_name: str | None = None,
    tooltip: str | None = None,
) -> QPushButton:
    """Push button; focus only via keyboard so the focus ring means "keyboard here"."""
    btn = QPushButton(text)
    btn.setProperty("variant", variant)
    fonts.apply(btn, role)
    btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        btn.clicked.connect(lambda _checked=False: on_click())
    if object_name:
        btn.setObjectName(object_name)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def hsep() -> QFrame:
    line = QFrame()
    line.setObjectName("hsep")
    line.setFixedHeight(1)
    return line


def vsep() -> QFrame:
    line = QFrame()
    line.setObjectName("vsep")
    line.setFixedWidth(1)
    return line


def hbox(*children: QWidget | QLayout | int | None, spacing: int = 8) -> QHBoxLayout:
    """Horizontal layout; ``None`` adds a stretch, an int adds fixed spacing."""
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    _fill(lay, children)
    return lay


def vbox(*children: QWidget | QLayout | int | None, spacing: int = 8) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    _fill(lay, children)
    return lay


def _fill(lay: QHBoxLayout | QVBoxLayout, children: tuple[QWidget | QLayout | int | None, ...]):
    for child in children:
        if child is None:
            lay.addStretch(1)
        elif isinstance(child, int):
            lay.addSpacing(child)
        elif isinstance(child, QLayout):
            lay.addLayout(child)
        else:
            lay.addWidget(child)


def section_title(text: str) -> QLabel:
    """``// SECTION`` header in mono caps, accent colour."""
    return label(f"// {text}", Role.LABEL, tone="accent")


class PageHeader(QWidget):
    """Page title (heavy grotesque) with an optional muted subtitle and right-side widgets."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = label(title, Role.H1, object_name="pageTitle")
        self.subtitle = label(subtitle, Role.BODY, tone="muted", wrap=True)
        self.subtitle.setVisible(bool(subtitle))
        self.right = QHBoxLayout()
        self.right.setSpacing(8)
        text_col = vbox(self.title, self.subtitle, spacing=4)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        row.addLayout(text_col, 1)
        row.addLayout(self.right)
        row.setAlignment(self.right, Qt.AlignmentFlag.AlignBottom)


class CursorBlock(QWidget):
    """The brand detail: a small accent rectangle; optionally blinking."""

    def __init__(
        self,
        theme: ThemeManager,
        size: QSize | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._visible = True
        self.setFixedSize(size or QSize(9, 16))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(530)
        self._timer.timeout.connect(self._tick)
        theme.changed.connect(lambda _p: self.update())

    def set_blinking(self, blinking: bool) -> None:
        if blinking:
            self._timer.start()
        else:
            self._timer.stop()
            self._visible = True
            self.update()

    @property
    def blinking(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        self._visible = not self._visible
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._visible:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self._theme.palette.accent_fg))
        p.end()


def page_layout(widget: QWidget, *, top: int = PAGE_MARGIN_TOP, bottom: int = 24) -> QVBoxLayout:
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(PAGE_MARGIN_H, top, PAGE_MARGIN_H, bottom)
    lay.setSpacing(16)
    return lay


def format_duration(seconds: float) -> str:
    """``0,8 s`` / ``12,4 s`` / ``1:05`` / ``1:02:03`` (Polish decimal comma)."""
    if seconds < 60:
        return f"{seconds:.1f}".replace(".", ",") + " s"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_clock(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Polish plural form: 1 linia, 2 linie, 5 linii (22 linie, 12 linii)."""
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def format_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}".replace(".", ",") + " MB"
