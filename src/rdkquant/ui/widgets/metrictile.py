# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""指标块：把结论用大字号摆出来，而不是塞进表格单元格。

延迟、帧率、余弦相似度这类"一眼要看懂"的数字，用 MetricTile；
逐层明细才用表格。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..metrics import METRICS, font_with_delta, line_height
from .statuspill import Tone, tone_color


class MetricTile(QFrame):
    def __init__(
        self,
        caption: str,
        value: str = "—",
        unit: str = "",
        hint: str = "",
        tone: Tone = Tone.NEUTRAL,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("MetricTile")
        self._tone = tone
        m = METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_3, m.space_3, m.space_3)
        layout.setSpacing(2)

        self._caption = QLabel(caption)
        self._caption.setObjectName("Muted")
        self._caption.setFont(font_with_delta(self.font(), m.font_delta_small))
        layout.addWidget(self._caption)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(m.space_1)
        self._value = QLabel(value)
        self._value.setFont(font_with_delta(self.font(), m.font_delta_metric, bold=True))
        # 必须给 stretch：数值标签的横向策略是 Ignored（最小宽度为 0），
        # 若后面再跟一个 addStretch，它会被分到 0 宽度而完全看不见。
        row.addWidget(self._value, 1)
        self._unit = QLabel(unit)
        self._unit.setObjectName("Muted")
        self._unit.setFont(font_with_delta(self.font(), m.font_delta_small))
        self._unit.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        row.addWidget(self._unit, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(row)

        self._hint = QLabel(hint)
        self._hint.setObjectName("Muted")
        self._hint.setFont(font_with_delta(self.font(), m.font_delta_small))
        self._hint.setWordWrap(True)
        self._hint.setVisible(bool(hint))
        layout.addWidget(self._hint)

        # 关键：文字的横向尺寸策略设为 Ignored。
        # 否则当四块指标挤在一行、单块宽度不够时，QLabel 会按自己的
        # minimumSizeHint 撑开；父框画不下，而 Qt **不裁剪子控件**，
        # 文字就会画到隔壁那块上去（150% 缩放下实测到的重叠）。
        # Ignored 让标签接受任何宽度、老老实实被裁，不会越界。
        # 提示文字带自动换行，它的高度依赖宽度，不能用 Ignored（会算错高度）。
        for label in (self._caption, self._value):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, label.sizePolicy().verticalPolicy())
            label.setMinimumWidth(0)
        self._hint.setMinimumWidth(0)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._apply_minimum_height(hint)
        self._apply_colors()

    def _apply_minimum_height(self, hint: str) -> None:
        """把"内容需要多高"算清楚并设成最小高度。

        不这么做的话，指标块会被上面的布局压扁（Maximum 策略允许压到
        minimumSizeHint，而标签横向策略是 Ignored，算出来的最小高度接近 0），
        字体放大时数值就会被裁掉。
        """
        m = METRICS
        caption_h = line_height(font_with_delta(self.font(), m.font_delta_small))
        value_h = line_height(font_with_delta(self.font(), m.font_delta_metric, bold=True))
        hint_h = line_height(font_with_delta(self.font(), m.font_delta_small)) if hint else 0
        self.setMinimumHeight(
            m.space_3 * 2 + caption_h + value_h + hint_h + m.space_1
        )

    # ------------------------------------------------------------------
    def set_value(self, value: str, unit: str | None = None, tone: Tone | None = None) -> None:
        self._value.setText(value)
        if unit is not None:
            self._unit.setText(unit)
        if tone is not None:
            self._tone = tone
        self._apply_colors()

    def set_hint(self, text: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    # ------------------------------------------------------------------
    def _apply_colors(self) -> None:
        """在**当前**调色板下解析颜色。"""
        from PySide6.QtGui import QPalette

        if self._tone is Tone.NEUTRAL:
            color = self.palette().color(QPalette.ColorRole.WindowText)
        else:
            color = tone_color(self, self._tone)
        self._value.setStyleSheet(f"color: {color.name()};")

    def changeEvent(self, event) -> None:  # noqa: N802
        # 调色板变化（主题切换 / 系统高对比度）时自动重新取色，
        # 不需要上层做任何"通知子树刷新"的连线。
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_colors()
        super().changeEvent(event)
