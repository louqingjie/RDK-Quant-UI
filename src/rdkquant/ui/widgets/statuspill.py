# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""语义色调。

`success / warning / danger` 在平台调色板里没有对应角色，必须自己定。
规则（见 palette.semantic_colors）：按当前底色明暗自动选一组高对比度的，
并且只用于小图标和小字，不做大面积色块，避免和系统主题打架。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from ..metrics import METRICS, font_with_delta
from ..palette import semantic_colors


class Tone(str, Enum):
    NEUTRAL = "neutral"
    ACCENT = "accent"
    OK = "success"
    WARN = "warning"
    DANGER = "danger"


def tone_color(widget: QWidget, tone: Tone) -> QColor:
    """**在绘制时**从当前调色板取色，所以主题切换会自动跟随。"""
    colors = semantic_colors(widget.palette())
    if tone is Tone.ACCENT:
        return colors["accent"]
    if tone is Tone.OK:
        return colors["success"]
    if tone is Tone.WARN:
        return colors["warning"]
    if tone is Tone.DANGER:
        return colors["danger"]
    return colors["muted"]


class StatusPill(QLabel):
    """带语义色的小圆角标签：BPU/CPU、通过/警告/失败、耗时/帧率……

    自绘而非 setStyleSheet：一来避免每个实例都生成一段样式表，
    二来能在 paintEvent 里直接读当前调色板，主题切换不需要任何通知。
    """

    def __init__(self, text: str = "", tone: Tone = Tone.NEUTRAL, parent: QWidget | None = None):
        super().__init__(text, parent)
        self._tone = tone
        self.setFont(font_with_delta(self.font(), METRICS.font_delta_small, bold=True))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_status(self, text: str, tone: Tone | None = None) -> None:
        self.setText(text)
        if tone is not None:
            self._tone = tone
        self.update()

    @property
    def tone(self) -> Tone:
        return self._tone

    def sizeHint(self):  # noqa: N802
        base = super().sizeHint()
        base.setWidth(base.width() + METRICS.space_3)
        base.setHeight(max(base.height() + METRICS.space_1, 20))
        return base

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802
        color = tone_color(self, self._tone)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        radius = self.height() / 2.0
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(color.red(), color.green(), color.blue(), 44))

        pen_color = QColor(color)
        pen = painter.pen()
        pen.setColor(QColor(color.red(), color.green(), color.blue(), 150))
        painter.setPen(pen)
        painter.drawPath(path)

        painter.setPen(color)
        painter.setFont(self.font())
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        painter.end()
