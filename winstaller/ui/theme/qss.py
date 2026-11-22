"""Stylesheet generated from a :class:`Palette`. Colours and geometry only (no fonts)."""

from __future__ import annotations

from string import Template

from winstaller.ui.theme.tokens import Palette

_QSS = Template(
    """
QMainWindow, QDialog, QWidget#page, QWidget#pageBody { background: $bg; }
QWidget { color: $text; }
QToolTip {
    background: $surface2; color: $text; border: 1px solid $border_strong; padding: 6px 8px;
}

/* ---------------------------------------------------------------- labels */
QLabel { background: transparent; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="faint"] { color: $faint; }
QLabel[role="accent"] { color: $accent_fg; }
QLabel[role="error"] { color: $error_fg; }
QLabel[role="badge"] {
    color: $accent_fg; border: 1px solid $accent_fg; padding: 2px 6px; border-radius: 1px;
}
QLabel[role="link"] { color: $accent_fg; }

QFrame#hsep { background: $border; border: none; min-height: 1px; max-height: 1px; }
QFrame#vsep { background: $border; border: none; min-width: 1px; max-width: 1px; }

/* ------------------------------------------------------------ navigation */
QWidget#navRail { background: $surface; border-right: 1px solid $border; }
QPushButton#navButton {
    background: transparent; color: $muted; border: none; border-left: 2px solid transparent;
    text-align: left; padding: 0 0 0 22px; min-height: 40px;
}
QPushButton#navButton:hover { color: $text; background: $surface2; }
QPushButton#navButton:checked {
    color: $text; background: $surface2; border-left: 2px solid $accent_fg;
}
QPushButton#navButton:focus { background: $surface2; border-left: 2px solid $text; }

/* --------------------------------------------------------------- buttons */
QPushButton {
    background: transparent; color: $text; border: 1px solid $border_strong;
    border-radius: 2px; padding: 0 16px; min-height: 36px;
}
QPushButton:hover { background: $surface2; border-color: $muted; }
QPushButton:pressed { background: $border; }
QPushButton:focus { border: 1px solid $accent_fg; }
QPushButton:disabled { color: $faint; border-color: $border; background: transparent; }

QPushButton[variant="primary"] {
    background: $accent; color: $on_accent; border: 1px solid $accent; padding: 0 20px;
    min-height: 36px;
}
QPushButton[variant="primary"]:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton[variant="primary"]:pressed { background: $accent_fg; }
QPushButton[variant="primary"]:focus { border: 2px solid $text; }
QPushButton[variant="primary"]:disabled {
    background: $surface2; color: $faint; border: 1px solid $border;
}

QPushButton[variant="ghost"] {
    background: transparent; color: $muted; border: 1px solid transparent; padding: 0 6px;
    min-height: 26px;
}
QPushButton[variant="ghost"]:hover { color: $text; }
QPushButton[variant="ghost"]:focus { border: 1px solid $accent_fg; color: $text; }
QPushButton[variant="ghost"]:disabled { color: $faint; }

QPushButton[variant="link"] {
    background: transparent; color: $accent_fg; border: 1px solid transparent; padding: 0 2px;
    min-height: 22px; text-align: left;
}
QPushButton[variant="link"]:hover { color: $text; }
QPushButton[variant="link"]:focus { border: 1px solid $accent_fg; }

QPushButton#profileChip {
    background: transparent; color: $text; border: 1px solid $border_strong;
    padding: 0 14px; min-height: 32px;
}
QPushButton#profileChip:hover { border-color: $accent_fg; }
QPushButton#profileChip:checked {
    background: $accent; color: $on_accent; border-color: $accent;
}
QPushButton#profileChip:checked:focus { border: 2px solid $text; }
QPushButton#profileChip:disabled { color: $faint; border-color: $border; }

QPushButton[segment="true"] {
    background: transparent; color: $muted; border: 1px solid $border_strong; border-radius: 0;
    min-height: 32px; padding: 0 14px;
}
QPushButton[segment="true"]:hover { color: $text; }
QPushButton[segment="true"]:checked {
    color: $on_accent; background: $accent; border-color: $accent;
}
QPushButton[segment="true"]:focus { border: 1px solid $accent_fg; }
QPushButton[segment="true"]:checked:focus { border: 2px solid $text; }

/* ---------------------------------------------------------------- inputs */
QLineEdit {
    background: $surface; color: $text; border: 1px solid $border; border-radius: 2px;
    padding: 0 12px; min-height: 36px; selection-background-color: $selection;
    selection-color: $text;
}
QLineEdit:hover { border-color: $border_strong; }
QLineEdit:focus { border-color: $accent_fg; }

QPlainTextEdit[log="true"] {
    background: $surface; color: $text; border: 1px solid $border; border-radius: 0;
    padding: 6px 4px; selection-background-color: $selection; selection-color: $text;
}

QProgressBar {
    background: $border; border: none; border-radius: 0; min-height: 4px; max-height: 4px;
}
QProgressBar::chunk { background: $accent_fg; }

QMenu { background: $surface; color: $text; border: 1px solid $border_strong; padding: 4px; }
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: $surface2; }
QMenu::item:disabled { color: $faint; }

/* ------------------------------------------------------------- scrolling */
QScrollArea, QScrollArea > QWidget > QWidget#scrollBody { background: transparent; border: none; }
QListView { background: transparent; border: none; outline: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $border_strong; min-height: 32px; margin: 2px 3px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: $border_strong; min-width: 32px; margin: 3px 2px; }
QScrollBar::handle:horizontal:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ------------------------------------------------------------ containers */
QWidget#actionBar { background: $surface; border-top: 1px solid $border; }
QFrame#banner {
    background: $surface; border: none; border-bottom: 1px solid $border;
    border-left: 3px solid $accent_fg;
}
QFrame#banner[tone="error"] { border-left: 3px solid $error; }
QFrame#banner[tone="neutral"] { border-left: 3px solid $border_strong; }
QFrame#toolCard, QFrame#summaryPanel, QFrame#infoPanel {
    background: $surface; border: 1px solid $border; border-radius: 0;
}
QFrame#wuPanelRestart { background: transparent; border: 1px solid $border_strong; }
QProgressBar[tone="muted"]::chunk { background: $muted; }
QFrame#toolResult { background: transparent; border: none; border-top: 1px solid $border; }
QDialog#confirmDialog { background: $bg; border: 1px solid $border_strong; }
QFrame#dialogList { background: $surface; border: 1px solid $border; }
"""
)


def build_qss(p: Palette) -> str:
    return _QSS.substitute(
        bg=p.bg,
        surface=p.surface,
        surface2=p.surface2,
        border=p.border,
        border_strong=p.border_strong,
        text=p.text,
        muted=p.muted,
        faint=p.faint,
        accent=p.accent,
        accent_hover=p.accent_hover,
        accent_fg=p.accent_fg,
        on_accent=p.on_accent,
        error=p.error,
        error_fg=p.error_fg,
        selection=p.selection,
    )
