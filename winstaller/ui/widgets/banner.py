"""Non-modal strip at the top of the content area (updates, winget)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtWidgets import QFrame, QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from winstaller.ui.theme import Role
from winstaller.ui.widgets.common import button, label, set_prop

BannerAction = tuple[str, str, Callable[[], object], str]  # text, variant, callback, objectName


class Banner(QFrame):
    def __init__(self, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self.setProperty("bannerId", object_name)
        self.tag = label("", Role.LABEL, tone="accent", object_name=f"{object_name}Tag")
        self.message = label("", Role.BODY, object_name=f"{object_name}Message")
        self.message.setWordWrap(True)
        self.detail = label("", Role.SMALL, tone="muted", object_name=f"{object_name}Detail")
        self.progress = QProgressBar()
        self.progress.setObjectName(f"{object_name}Progress")
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.progress.setFixedWidth(220)
        self._buttons: list[QPushButton] = []
        self._button_row = QHBoxLayout()
        self._button_row.setSpacing(8)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(self.message)
        text_col.addWidget(self.detail)

        row = QHBoxLayout(self)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(16)
        row.addWidget(self.tag)
        row.addLayout(text_col, 1)
        row.addWidget(self.progress)
        row.addLayout(self._button_row)
        self.setVisible(False)

    def configure(
        self,
        *,
        tag: str,
        message: str,
        detail: str = "",
        actions: Sequence[BannerAction] = (),
        tone: str = "info",
        progress: tuple[int, int] | None = None,
        busy: bool = False,
    ) -> None:
        """Replace the whole banner content and show it. ``tone``: "info" (accent: an
        offer or something in progress), "neutral" (a finished result), "error"."""
        set_prop(self, "tone", tone)
        set_prop(self.tag, "role", {"error": "error", "neutral": ""}.get(tone, "accent"))
        self.tag.setText(tag)
        self.message.setText(message)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.set_progress(progress, busy=busy)
        for btn in self._buttons:
            self._button_row.removeWidget(btn)
            btn.hide()
            btn.setParent(None)
            btn.deleteLater()
        self._buttons = []
        for text, variant, callback, name in actions:
            btn = button(text, variant=variant, on_click=callback, object_name=name)
            self._button_row.addWidget(btn)
            self._buttons.append(btn)
        self.setVisible(True)

    def set_progress(self, progress: tuple[int, int] | None, *, busy: bool = False) -> None:
        if busy:
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
        elif progress is None:
            self.progress.setVisible(False)
        else:
            value, total = progress
            if total <= 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(1000 * min(value, total) / total))
            self.progress.setVisible(True)

    def set_detail(self, text: str) -> None:
        self.detail.setText(text)
        self.detail.setVisible(bool(text))

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)
