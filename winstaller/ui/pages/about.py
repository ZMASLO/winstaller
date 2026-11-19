"""O programie: version, architecture, mode, repo link, theme switch, update check."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QGridLayout, QPushButton, QWidget

from winstaller.core.updater import GITHUB_REPO
from winstaller.ui.theme import Role, ThemeManager, ThemeMode
from winstaller.ui.widgets.common import (
    CursorBlock,
    button,
    hbox,
    label,
    page_layout,
    section_title,
    set_prop,
)

REPO_URL = f"https://github.com/{GITHUB_REPO}"

THEME_LABELS: dict[ThemeMode, str] = {
    ThemeMode.SYSTEM: "Systemowy",
    ThemeMode.DARK: "Ciemny",
    ThemeMode.LIGHT: "Jasny",
}


class AboutPage(QWidget):
    """Public hooks: ``theme_buttons`` (ThemeMode -> button), ``check_button``,
    ``update_status``; signals ``themeRequested(ThemeMode)``, ``checkRequested``,
    ``openUrlRequested(str)``.
    """

    themeRequested = Signal(object)
    checkRequested = Signal()
    openUrlRequested = Signal(str)

    def __init__(
        self,
        *,
        version: str,
        arch: str,
        dry_run: bool,
        item_count: int,
        theme: ThemeManager,
        updates_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("page_about")
        lay = page_layout(self, top=40)
        lay.setSpacing(14)

        title = label("Winstaller", Role.DISPLAY, object_name="aboutTitle")
        cursor = CursorBlock(theme, QSize(14, 32))
        row = hbox(title, 8, cursor, None, spacing=0)
        row.setAlignment(cursor, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(row)
        lay.addWidget(
            label(
                "Instalacja programów przez winget i przygotowanie platformy testowej "
                "do benchmarków — szybko, na świeżym systemie.",
                Role.BODY,
                tone="muted",
                wrap=True,
            )
        )
        lay.addSpacing(12)

        info = QFrame()
        info.setObjectName("infoPanel")
        grid = QGridLayout(info)
        grid.setContentsMargins(24, 18, 24, 18)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(12)
        mode_text = "[DRY-RUN] symulacja — nic nie jest instalowane" if dry_run else "Normalny"
        rows: list[tuple[str, QWidget]] = [
            ("Wersja", label(version, Role.BODY_STRONG, object_name="aboutVersion")),
            ("Architektura", label(arch.upper(), Role.BODY_STRONG, object_name="aboutArch")),
            (
                "Tryb",
                label(
                    mode_text,
                    Role.LABEL if dry_run else Role.BODY_STRONG,
                    tone="accent" if dry_run else None,
                    object_name="aboutMode",
                ),
            ),
            ("Katalog", label(f"{item_count} pozycji", Role.BODY_STRONG)),
        ]
        repo = button(
            f"github.com/{GITHUB_REPO}  →",
            variant="link",
            role=Role.LABEL,
            on_click=lambda: self.openUrlRequested.emit(REPO_URL),
            object_name="repoLink",
            tooltip=REPO_URL,
        )
        rows.append(("Repozytorium", repo))
        for n, (name, value) in enumerate(rows):
            grid.addWidget(label(name, Role.LABEL_SM, tone="muted"), n, 0)
            grid.addWidget(value, n, 1, Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)
        lay.addWidget(info)
        lay.addSpacing(14)

        # -- theme
        lay.addWidget(section_title("MOTYW"))
        self.theme_buttons: dict[ThemeMode, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        seg = hbox(spacing=0)
        for mode, text in THEME_LABELS.items():
            btn = button(text, role=Role.LABEL, object_name=f"theme_{mode.value}")
            btn.setProperty("segment", True)
            btn.setCheckable(True)
            btn.setMinimumWidth(120)
            btn.clicked.connect(lambda _c=False, m=mode: self.themeRequested.emit(m))
            group.addButton(btn)
            seg.addWidget(btn)
            self.theme_buttons[mode] = btn
        seg.addStretch(1)
        lay.addLayout(seg)
        self.set_theme_mode(theme.mode)
        lay.addSpacing(14)

        # -- updates
        lay.addWidget(section_title("AKTUALIZACJE"))
        self.check_button = button(
            "Sprawdź aktualizacje",
            on_click=self.checkRequested.emit,
            object_name="checkUpdates",
        )
        self.update_status = label(
            "", Role.BODY, tone="muted", wrap=True, object_name="updateStatus"
        )
        if not updates_enabled:
            self.check_button.setEnabled(False)
            self.update_status.setText(
                "W trybie dry-run sprawdzanie aktualizacji jest wyłączone "
                "(WINSTALLER_FAKE_UPDATE=<wersja> włącza symulację)."
            )
        self.update_status.setTextFormat(Qt.TextFormat.PlainText)
        update_row = hbox(self.check_button, 16, spacing=0)
        update_row.addWidget(self.update_status, 1)
        lay.addLayout(update_row)
        lay.addStretch(1)

    def set_theme_mode(self, mode: ThemeMode) -> None:
        for m, btn in self.theme_buttons.items():
            btn.setChecked(m == mode)

    def set_update_status(self, text: str, *, tone: str = "muted", busy: bool = False) -> None:
        self.update_status.setText(text)
        set_prop(self.update_status, "role", tone)
        self.check_button.setEnabled(not busy)
