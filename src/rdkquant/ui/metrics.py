# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""布局度量常量。

与颜色**完全解耦**：颜色一律来自平台调色板（见 palette.py），这里只管尺寸。

为什么把尺寸单独抽出来：Qt 默认控件的内边距和高度偏小，是"看起来像十年前的
程序"的主要原因。把这些数字集中成 8px 网格，是"现代化"里最不依赖皮肤、
也最不会跟系统主题打架的那一半。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QFont, QFontMetrics


@dataclass
class Metrics:
    """尺寸令牌。

    **刻意是可变对象**：`configure()` 会按当前字体就地改写它的字段。
    这一点很重要——widgets 里写的是 `from ..metrics import METRICS`，绑定的是
    对象引用；如果 `configure()` 改成重新赋值模块级变量，所有已经 import 过的
    模块仍指向旧对象，改了等于没改。就地改字段才生效。

    为什么必须按字体算：这些都是"逻辑像素"。用户把系统字体调大（或
    QT_SCALE_FACTOR=1.5）时，字体会占据更多逻辑像素，而写死的 26px 行高不会变，
    于是文字撑破容器。Qt 默认**不裁剪子控件**，所以撑破的文字会画到相邻控件上，
    看起来像"重叠"。150% 缩放截图就是专门用来抓这个的。
    """

    # 间距阶梯：与文字无关，保持固定，作为版式节奏
    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 24
    space_6: int = 32

    # 圆角
    radius_sm: int = 4
    radius_md: int = 8
    radius_lg: int = 12

    # 以下与文字高度相关，由 configure() 按字体重算
    control_height: int = 30
    row_height: int = 26
    header_height: int = 72
    nav_width: int = 176

    # 容器内边距
    page_margin: int = 16
    card_padding: int = 16
    gap: int = 12

    # 字号偏移（相对系统字体，单位 pt）
    font_delta_small: int = -1
    font_delta_title: int = 1
    font_delta_metric: int = 5       # MetricTile 的大数字


METRICS = Metrics()


def configure(app) -> None:
    """按当前字体重新计算与文字相关的尺寸。

    在 `apply_theme()` 里调用——那时 QApplication 已经存在，且一定早于任何
    页面构造，所以控件读到的是算好的值。
    """
    # QApplication.fontMetrics() 在 Qt 6.11 已标记弃用，用 QFontMetrics 自己算
    metrics = QFontMetrics(app.font())
    line = max(12, metrics.height())
    METRICS.control_height = max(26, line + 12)
    METRICS.row_height = max(22, line + 8)
    # Stepper 要放下"圆 + 标题"两行，再加一点呼吸空间
    METRICS.header_height = max(64, line * 2 + 40)
    # 导航栏宽度按最长条目文本算，避免长标题被省略号截断
    METRICS.nav_width = max(160, metrics.horizontalAdvance("模型量化与编译") + 76)


def line_height(font: QFont) -> int:
    return max(12, QFontMetrics(font).height())


def font_with_delta(base: QFont, delta: int = 0, *, bold: bool = False) -> QFont:
    """基于系统字体生成变体，而不是硬编码字号。

    这样用户把系统字体调大时，标题和数值会跟着一起变大，不会出现
    "正文放大了、标题还是原来大小"的错位。
    """
    font = QFont(base)
    size = base.pointSizeF()
    if size > 0:
        font.setPointSizeF(max(6.0, size + delta))
    font.setBold(bold)
    return font


def mono_font(base: QFont) -> QFont:
    """等宽字体，用于日志与逐层误差表。

    优先用系统上真实存在的等宽族；Qt 的 StyleHint 会在都没有时给出兜底。
    """
    font = QFont(base)
    for family in ("JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono",
                   "Noto Sans Mono", "Liberation Mono", "Menlo", "monospace"):
        font.setFamily(family)
        if font.exactMatch():
            break
    else:
        font.setFamily(base.family())
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    size = base.pointSizeF()
    if size > 0:
        font.setPointSizeF(max(6.0, size - 0.5))
    return font
