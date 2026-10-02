# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""主题与调色板。

**核心原则：这里没有一张颜色常量表。**

颜色一律来自平台调色板——KDE 上由 plasma-integration 提供 Breeze 配色与强调色，
GNOME 上由 Adwaita-Qt 提供，什么都没有的环境里落到 Qt 自己的默认 palette。
这样做的回报是白拿三件事：

  * 自动跟随系统明暗切换；
  * 自动跟随用户的高对比度主题（手写 QSS 的程序经常在这里翻车）；
  * 自动跟随系统强调色。

唯一的例外是 success / warning / danger 三个语义色：平台调色板里没有对应角色，
必须自己定。规则见 `semantic_colors()`——按当前底色的明暗自动加深或提亮，
并且**只用于小图标和小字**，不做大面积色块，避免和系统主题打架。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

ORG_NAME = "RDKQuant"
APP_NAME = "rdkquant"

# 只要系统装了其中任意一个，中文就不会变成豆腐块
_CJK_FAMILIES = (
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Source Han Sans CN",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei", "PingFang SC",
    "Hiragino Sans GB", "Sarasa Gothic SC", "HarmonyOS Sans SC",
)


class ThemeMode(str, Enum):
    """用 (str, Enum) 而不是 StrEnum —— 后者要 Python 3.11+，
    而我们的下限是 3.10（Ubuntu 22.04 的系统 Python）。"""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def is_dark(palette: QPalette) -> bool:
    """以窗口底色的明度判断当前是深色还是浅色。"""
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def detect_system_mode(app: QApplication) -> ThemeMode:
    """读取系统的明暗偏好。Qt 6.5+ 才有 colorScheme()，取不到就按浅色处理。"""
    try:
        scheme = app.styleHints().colorScheme()
    except (AttributeError, TypeError):
        return ThemeMode.LIGHT
    return ThemeMode.DARK if scheme == Qt.ColorScheme.Dark else ThemeMode.LIGHT


def cjk_available() -> bool:
    """系统是否存在可覆盖中文的字族。

    刻意**不**用这个结果去覆盖系统字体——KDE/GNOME 用户已经配置过自己的字体，
    我们改掉就破坏了"原生"。它只用来在完全缺失时给出提示（裸 WSL 的常见情况）。
    """
    installed = set(QFontDatabase.families())
    return any(family in installed for family in _CJK_FAMILIES)


# --------------------------------------------------------------------------
# 兜底调色板：仅在平台主题缺失、或用户手动指定了与平台相反的明暗时使用
# --------------------------------------------------------------------------
def _builtin_light() -> QPalette:
    p = QPalette()
    window = QColor(0xF5, 0xF6, 0xF8)
    base = QColor(0xFF, 0xFF, 0xFF)
    text = QColor(0x1B, 0x1D, 0x21)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(0xF0, 0xF2, 0xF5))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(0xE9, 0xEC, 0xF0))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(0x1B, 0x6E, 0xF0))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    p.setColor(QPalette.ColorRole.Mid, QColor(0xC9, 0xCE, 0xD6))
    p.setColor(QPalette.ColorRole.Dark, QColor(0xA8, 0xAF, 0xB9))
    p.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(0x9A, 0xA0, 0xA8)
    )
    p.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(0x9A, 0xA0, 0xA8)
    )
    return p


def _builtin_dark() -> QPalette:
    p = QPalette()
    window = QColor(0x1E, 0x20, 0x24)
    base = QColor(0x16, 0x18, 0x1B)
    text = QColor(0xE6, 0xE8, 0xEB)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(0x24, 0x27, 0x2C))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(0x2B, 0x2F, 0x35))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(0x2B, 0x2F, 0x35))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(0x4C, 0x9A, 0xFF))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(0x10, 0x12, 0x14))
    p.setColor(QPalette.ColorRole.Mid, QColor(0x3A, 0x3F, 0x46))
    p.setColor(QPalette.ColorRole.Dark, QColor(0x11, 0x13, 0x16))
    p.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(0x6B, 0x71, 0x7A)
    )
    p.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(0x6B, 0x71, 0x7A)
    )
    return p


def resolve_palette(mode: ThemeMode, platform_palette: QPalette) -> QPalette:
    """决定最终用哪张调色板。

    优先级刻意是"平台优先"：只有用户手动指定的明暗与平台**相反**时才覆盖，
    否则一律沿用平台调色板——那才是真正的原生观感。
    """
    if mode is ThemeMode.SYSTEM:
        return platform_palette
    want_dark = mode is ThemeMode.DARK
    if is_dark(platform_palette) == want_dark:
        return platform_palette
    return _builtin_dark() if want_dark else _builtin_light()


# --------------------------------------------------------------------------
# 语义色
# --------------------------------------------------------------------------
_LIGHT_SEMANTIC = {
    "success": "#1e7b3c",
    "warning": "#a35a00",
    "danger": "#b3261e",
    "muted": "#5f6672",
}
_DARK_SEMANTIC = {
    "success": "#4ade80",
    "warning": "#fbbf24",
    "danger": "#f87171",
    "muted": "#9aa3b2",
}


def semantic_colors(palette: QPalette) -> dict[str, QColor]:
    """平台调色板没有语义色角色，这里按底色明暗挑一组高对比度的。

    只用于小图标和小字；大面积色块会跟系统主题打架。
    """
    source = _DARK_SEMANTIC if is_dark(palette) else _LIGHT_SEMANTIC
    colors = {name: QColor(value) for name, value in source.items()}
    # 强调色直接用平台的 Highlight —— 用户在系统设置里换强调色我们会自动跟随
    colors["accent"] = palette.color(QPalette.ColorRole.Highlight)
    colors["on_accent"] = palette.color(QPalette.ColorRole.HighlightedText)
    return colors


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
def apply_theme(app: QApplication, mode: ThemeMode, *, platform_palette: QPalette | None = None) -> None:
    """应用主题。**不设置 QStyle** —— 强制 Fusion 会顶掉 KDE 的 Breeze。"""
    from .metrics import configure  # 局部导入，避免 qss <-> palette 循环引用
    from .qss import build_qss

    if platform_palette is None:
        platform_palette = app.palette()

    # 尺寸必须按当前字体重算，且一定要早于任何页面构造
    configure(app)

    palette = resolve_palette(mode, platform_palette)
    app.setPalette(palette)
    app.setStyleSheet(build_qss(palette, semantic_colors(palette)))
