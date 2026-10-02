# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""工具箱 —— 官方标注为"可选"的模型工具。

刻意不做成流程步骤：官方在 4.1.1.9 里明确把它们列为可选工具，
做成第 8 步会让人误以为必须走完。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...core.workflow import TOOLBOX_REF, TOOLBOX_TOOLS
from ..metrics import METRICS
from ..widgets import Card, EmptyState, StatusPill, Tone


class ToolboxPage(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        m = METRICS

        root = QVBoxLayout(self)
        root.setContentsMargins(m.page_margin, m.page_margin, m.page_margin, m.page_margin)
        root.setSpacing(m.gap)

        card = Card("工具箱", official_ref=TOOLBOX_REF)
        card.set_subtitle(
            "这些是官方提供的可选工具，不属于必经流程。对应产物存在时才用得上。"
        )

        row = QHBoxLayout()
        row.setSpacing(m.space_2)
        for tool in TOOLBOX_TOOLS:
            row.addWidget(StatusPill(tool, Tone.NEUTRAL))
        row.addStretch(1)
        card.add_layout(row)

        empty = EmptyState(
            "toolbox",
            "工具箱尚未实现",
            "计划在 M5 接通：hb_pack 打包、hb_model_info 查看模型信息、"
            "hb_model_modifier 增删 bin 模型首尾节点。",
        )
        empty.setMinimumHeight(220)
        card.add(empty)
        card.add_stretch(1)
        root.addWidget(card, 1)
