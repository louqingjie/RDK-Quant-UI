# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""页面。

每个页面处理主流程里的一步（或工具箱），页面本身只负责"展示 + 收集输入"，
判断能不能跑、缺什么，一律交给 core/workflow.py。
"""

from .env import EnvPage
from .placeholder import PlaceholderPage
from .toolbox import ToolboxPage

__all__ = ["EnvPage", "PlaceholderPage", "ToolboxPage"]
