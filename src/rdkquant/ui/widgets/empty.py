# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""空状态：没有数据时给出下一步，而不是留一片空白。

这是"易用"里最廉价也最容易被忽略的一环——用户看到空白只会以为程序坏了。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..icons import glyph_pixmap
from ..metrics import METRICS, font_with_delta


class EmptyState(QWidget):
    actionTriggered = Signal()

    def __init__(
        self,
        icon_name: str,
        title: str,
        hint: str = "",
        action_text: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        m = METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_5, m.space_5, m.space_5)
        layout.setSpacing(m.space_2)
        layout.addStretch(1)

        self._icon_name = icon_name
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon)

        self._title = QLabel(title)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setFont(font_with_delta(self.font(), m.font_delta_title, bold=True))
        layout.addWidget(self._title)

        self._hint = QLabel(hint)
        self._hint.setObjectName("Muted")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setVisible(bool(hint))
        layout.addWidget(self._hint)

        self._button = QPushButton(action_text)
        self._button.setObjectName("Primary")
        self._button.setVisible(bool(action_text))
        self._button.clicked.connect(self.actionTriggered.emit)
        layout.addWidget(self._button, 0, Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        self._apply_icon()

    def set_text(self, title: str, hint: str = "") -> None:
        self._title.setText(title)
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))

    def set_action_text(self, text: str) -> None:
        self._button.setText(text)
        self._button.setVisible(bool(text))

    def _apply_icon(self) -> None:
        from PySide6.QtGui import QPalette

        color = self.palette().color(QPalette.ColorRole.WindowText)
        color.setAlpha(110)
        self._icon.setPixmap(glyph_pixmap(self._icon_name, color, 48))
        self._icon.setFixedHeight(56)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_icon()
        super().changeEvent(event)
