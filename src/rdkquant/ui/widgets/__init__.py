# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""UI 组件库。

约定：**自定义绘制的组件在 paintEvent 里读 `self.palette()`，绝不缓存颜色。**
这样主题切换、系统高对比度设置变化时会自动跟上，不需要任何"主题变了请刷新"
的连线。只有需要重新排版的组件（如日志按等级着色）才实现 `refresh_theme()`，
由 `shell.refresh_theme_tree()` 统一调用。
"""

from .card import Card
from .collapsible import CollapsibleSection
from .empty import EmptyState
from .logview import LogView, LogLevel
from .logpanel import LogPanel
from .metrictile import MetricTile
from .statuspill import StatusPill, Tone
from .stepper import StepItem, Stepper, StepState
from .tablemodel import TableModel, make_table

__all__ = [
    "Card",
    "CollapsibleSection",
    "EmptyState",
    "LogLevel",
    "LogPanel",
    "LogView",
    "MetricTile",
    "StatusPill",
    "StepItem",
    "StepState",
    "Stepper",
    "TableModel",
    "Tone",
    "make_table",
]
