"""Styled yes/no confirmation dialog (the only modal window in the app)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QScrollArea, QVBoxLayout, QWidget

from winstaller.ui.services import ConfirmRequest
from winstaller.ui.theme import Role
from winstaller.ui.widgets.common import button, hbox, hsep, label, section_title


class ConfirmDialog(QDialog):
    def __init__(self, request: ConfirmRequest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle(request.title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMaximumWidth(640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)
        lay.addWidget(section_title("POTWIERDZENIE"))
        title = label(request.title, Role.H2, wrap=True, object_name="confirmTitle")
        lay.addWidget(title)
        if request.message:
            lay.addWidget(
                label(
                    request.message,
                    Role.BODY,
                    tone="muted",
                    wrap=True,
                    object_name="confirmMessage",
                )
            )

        if request.details:
            box = QFrame()
            box.setObjectName("dialogList")
            inner = QVBoxLayout(box)
            inner.setContentsMargins(16, 12, 16, 12)
            inner.setSpacing(10)
            for n, (name, text) in enumerate(request.details):
                if n:
                    inner.addWidget(hsep())
                inner.addWidget(label(name, Role.BODY_STRONG, wrap=True))
                inner.addWidget(label(text, Role.SMALL, tone="muted", wrap=True))
            if len(request.details) > 3:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.Shape.NoFrame)
                scroll.setWidget(box)
                scroll.setMinimumHeight(220)
                lay.addWidget(scroll)
            else:
                lay.addWidget(box)

        self.reject_button = button(
            request.reject_text, on_click=self.reject, object_name="confirmReject"
        )
        self.accept_button = button(
            request.accept_text,
            variant="primary",
            on_click=self.accept,
            object_name="confirmAccept",
        )
        self.accept_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.reject_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        lay.addSpacing(6)
        lay.addLayout(hbox(None, self.reject_button, self.accept_button))
        # Safe default: Enter does not confirm a destructive operation by accident.
        self.reject_button.setDefault(True)
        self.reject_button.setFocus()


def ask(parent: QWidget, request: ConfirmRequest) -> bool:
    dialog = ConfirmDialog(request, parent)
    return dialog.exec() == QDialog.DialogCode.Accepted
