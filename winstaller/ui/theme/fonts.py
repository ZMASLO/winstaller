"""Font roles resolved against the fonts installed on the system (nothing is downloaded).

Stylesheets in Qt cannot set letter spacing, so fonts are never set from QSS: widgets get
their font from :func:`font` / :func:`apply` and QSS only carries colours and geometry.
"""

from __future__ import annotations

from enum import StrEnum
from functools import cache

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QWidget

HEADING_FAMILIES = ("Geist", "Segoe UI Variable Display", "Segoe UI", "Arial")
BODY_FAMILIES = ("Geist", "Segoe UI Variable Text", "Segoe UI", "Arial")
MONO_FAMILIES = ("Geist Mono", "Cascadia Mono", "Consolas", "Courier New")


class Role(StrEnum):
    DISPLAY = "display"  # big page / brand title
    H1 = "h1"  # page title
    H2 = "h2"  # card title, current task
    BODY = "body"
    BODY_STRONG = "body_strong"
    SMALL = "small"  # descriptions
    LABEL = "label"  # mono caps: section headers, tags, buttons, counters
    LABEL_SM = "label_sm"  # mono caps, small metadata
    MONO = "mono"  # log, paths (normal case)


@cache
def _families() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


def pick_family(candidates: tuple[str, ...]) -> str:
    """First installed family from ``candidates`` (last one as a blind fallback)."""
    available = _families()
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


# role -> (families, pixel size, weight, mono caps)
_SPEC: dict[Role, tuple[tuple[str, ...], int, QFont.Weight, bool]] = {
    Role.DISPLAY: (HEADING_FAMILIES, 34, QFont.Weight.Bold, False),
    Role.H1: (HEADING_FAMILIES, 26, QFont.Weight.Bold, False),
    Role.H2: (HEADING_FAMILIES, 17, QFont.Weight.Bold, False),
    Role.BODY: (BODY_FAMILIES, 14, QFont.Weight.Normal, False),
    Role.BODY_STRONG: (BODY_FAMILIES, 14, QFont.Weight.DemiBold, False),
    Role.SMALL: (BODY_FAMILIES, 12, QFont.Weight.Normal, False),
    Role.LABEL: (MONO_FAMILIES, 11, QFont.Weight.DemiBold, True),
    Role.LABEL_SM: (MONO_FAMILIES, 10, QFont.Weight.Normal, True),
    Role.MONO: (MONO_FAMILIES, 12, QFont.Weight.Normal, False),
}


def font(role: Role) -> QFont:
    """A fresh copy of the role's font (safe to modify)."""
    return QFont(_font(role))


@cache
def _font(role: Role) -> QFont:
    families, px, weight, caps = _SPEC[role]
    f = QFont(pick_family(families))
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6 if px >= 11 else 1.4)
    if families is MONO_FAMILIES:
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
    return f


def apply(widget: QWidget, role: Role) -> QWidget:
    widget.setFont(font(role))
    return widget
