# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""可折叠分组。

自己写而不是引入 superqt：我们只需要一个能折叠的容器，
为此增加一个第三方依赖不划算（约 60 行就能写完，而且没有许可证顾虑）。
S4 的"专家参数"折叠区就是用这个载体。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

from ..icons import icon
from ..metrics import METRICS, font_with_delta


class CollapsibleSection(QWidget):
    toggled = Signal(bool)

    def __init__(self, title: str, *, expanded: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        m = METRICS

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(m.space_2)

        self._button = QToolButton()
        self._button.setObjectName("SectionHeader")
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(expanded)
        self._button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._button.setIconSize(QSize(16, 16))
        self._button.setFont(font_with_delta(self.font(), 0, bold=True))
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setSizePolicy(
            self._button.sizePolicy().horizontalPolicy(),
            self._button.sizePolicy().verticalPolicy(),
        )
        self._button.toggled.connect(self._on_toggled)
        outer.addWidget(self._button)

        self._content = QWidget()
        self.content = QVBoxLayout(self._content)
        self.content.setContentsMargins(m.space_3, 0, 0, 0)
        self.content.setSpacing(m.space_3)
        self._content.setVisible(expanded)
        outer.addWidget(self._content)

        self._sync_icon()

    # ------------------------------------------------------------------
    def _on_toggled(self, checked: bool) -> None:
        self._content.setVisible(checked)
        self._sync_icon()
        self.toggled.emit(checked)

    def _sync_icon(self) -> None:
        name = "chevron-down" if self._button.isChecked() else "chevron-right"
        self._button.setIcon(icon(name, size=16))

    def set_expanded(self, expanded: bool) -> None:
        self._button.setChecked(bool(expanded))

    def is_expanded(self) -> bool:
        return self._button.isChecked()

    def set_title(self, title: str) -> None:
        self._button.setText(title)

    def add(self, widget: QWidget) -> QWidget:
        self.content.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.content.addLayout(layout)

    def add_stretch(self, stretch: int = 1) -> None:
        self.content.addStretch(stretch)
