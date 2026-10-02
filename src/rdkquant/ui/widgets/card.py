# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""卡片容器：所有页面内容的统一载体。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..metrics import METRICS, font_with_delta


class Card(QFrame):
    """圆角分组容器，带标题行与右上角动作槽。

    层级感靠 `QFrame#Card` 的边框与比窗口略亮的底色表达（见 qss.py），
    不用阴影——阴影在不同平台样式下表现差异太大。
    """

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        *,
        official_ref: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("Card")
        m = METRICS

        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.card_padding, m.card_padding, m.card_padding, m.card_padding)
        outer.setSpacing(m.space_3)

        # ---- 标题行 ----
        self._header = QFrame()
        self._header.setObjectName("CardHeader")
        head = QHBoxLayout(self._header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(m.space_2)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)

        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        self._title.setFont(font_with_delta(self.font(), m.font_delta_title, bold=True))
        titles.addWidget(self._title)

        # 官方依据这个小标签是"严谨符合官方流程"在界面上的可见证据
        self._ref = QLabel(f"官方依据：{official_ref}" if official_ref else "")
        self._ref.setObjectName("Muted")
        self._ref.setFont(font_with_delta(self.font(), m.font_delta_small))
        self._ref.setVisible(bool(official_ref))
        titles.addWidget(self._ref)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setObjectName("SectionHint")
        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
        titles.addWidget(self._subtitle)

        # 与 MetricTile 同样的理由：文字标签接受任意宽度并被裁剪，
        # 而不是按 minimumSizeHint 撑开父容器（Qt 不裁剪子控件，会越界画到邻居上）。
        for label in (self._title, self._ref, self._subtitle):
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, label.sizePolicy().verticalPolicy()
            )
            label.setMinimumWidth(0)

        head.addLayout(titles, 1)

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(m.space_2)
        head.addLayout(self._actions, 0)

        outer.addWidget(self._header)

        # ---- 主体 ----
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(m.gap)
        outer.addLayout(self.body, 1)

        self.setFrameShape(QFrame.Shape.NoFrame)

    # ------------------------------------------------------------------
    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)

    def add_stretch(self, stretch: int = 1) -> None:
        self.body.addStretch(stretch)

    def add_action(self, widget: QWidget) -> QWidget:
        self._actions.addWidget(widget)
        return widget

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))

    def set_official_ref(self, text: str) -> None:
        self._ref.setText(f"官方依据：{text}" if text else "")
        self._ref.setVisible(bool(text))

    def hide_header(self) -> None:
        self._header.setVisible(False)


class HLine(QFrame):
    """一行分隔线。用 QFrame 而不是 QSS 的 border，避免影响布局计算。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HLine")
        self.setFixedHeight(1)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
