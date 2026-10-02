# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""未实现步骤的占位页。

刻意不写成一个空白页：它把该步骤的**官方依据、要做什么、产出什么、用什么工具**
摆出来，这样即使用户现在点进来，也能看懂这一步在整条流程里的位置。
每个里程碑接通一步，就把对应页面换成真实实现。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...core.workflow import StageSpec
from ..metrics import METRICS, font_with_delta
from ..widgets import Card, EmptyState, StatusPill, Tone


class PlaceholderPage(QWidget):
    def __init__(
        self,
        spec: StageSpec,
        *,
        milestone: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        m = METRICS

        root = QVBoxLayout(self)
        root.setContentsMargins(m.page_margin, m.page_margin, m.page_margin, m.page_margin)
        root.setSpacing(m.gap)

        card = Card(spec.title, official_ref=spec.official_ref)
        card.set_subtitle(spec.goal)

        # 产出物与工具：让用户知道这一步会留下什么
        info = QHBoxLayout()
        info.setSpacing(m.space_2)
        info.addWidget(QLabel("工具"))
        info.addWidget(StatusPill(spec.tool or "—", Tone.NEUTRAL))
        info.addSpacing(m.space_3)
        info.addWidget(QLabel("产出"))
        for item in spec.produces:
            info.addWidget(StatusPill(item, Tone.NEUTRAL))
        info.addStretch(1)
        card.add_layout(info)

        note = f"（计划在 {milestone} 实现）" if milestone else ""
        empty = EmptyState(
            "clock",
            f"「{spec.title}」尚未实现",
            f"流程骨架与门禁逻辑已经就位{note}。"
            "在那之前，这一页只说明它在官方流程里的位置与产出。",
        )
        empty.setMinimumHeight(220)
        card.add(empty)
        card.add_stretch(1)
        root.addWidget(card, 1)
