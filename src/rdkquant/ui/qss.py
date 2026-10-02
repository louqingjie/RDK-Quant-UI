# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""从**活的** QPalette 生成 QSS。

QSS 里没法写 `palette(window)` 这样的引用（那是 GTK 的能力，Qt 没有），所以
唯一的办法是在运行时把 palette 里的颜色取出来、拼成字面量。这样得到的样式表
依然是"平台驱动"的：KDE 上就是 Breeze 的配色，用户换强调色、切高对比度，
我们全部跟着变。

**刻意不做的事**：不调 `app.setStyle("Fusion")`。强制 Fusion 会顶掉 KDE 的
Breeze 原生绘制，与"原生观感"的目标相反。代价是 QSS 会把 QPushButton 这类
控件的绘制接管过来（颜色仍来自平台），这是有意接受的取舍：得到"平台配色 +
现代扁平控件"。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor, QPalette

from .metrics import METRICS


# --------------------------------------------------------------------------
# 颜色工具
# --------------------------------------------------------------------------
def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """线性混合，t=0 取 a，t=1 取 b。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _rgba(color: QColor, alpha: float) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha:.3f})"


def _hex(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


_CHEVRON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" fill="none" '
    'stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    "{path}</svg>"
)


def _arrow_urls(color: QColor) -> tuple[str, str]:
    """把上下箭头写成 SVG 落到缓存目录，返回 (up, down) 的绝对路径。

    为什么必须落盘：QSS 的 `image:` 只认 url(路径)，不支持 data: URI；而用
    border 拼三角形的 CSS 技巧在 Qt 里不可靠（实测渲染成小方块）。一旦我们
    给 ::up-button 设了背景，Qt 就不再画原生箭头了，所以必须自己提供。

    文件名带上颜色，主题切换时生成新文件，互不覆盖。
    """
    cache_root = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    )
    directory = Path(cache_root or tempfile.gettempdir()) / "rdkquant" / "ui"
    directory.mkdir(parents=True, exist_ok=True)

    hexc = color.name(QColor.NameFormat.HexRgb)
    up = directory / f"chevron-up-{hexc}.svg"
    down = directory / f"chevron-down-{hexc}.svg"
    if not up.exists():
        up.write_text(
            _CHEVRON_SVG.format(color=hexc, path='<path d="M2.5 7.5L6 4l3.5 3.5"/>'),
            encoding="utf-8",
        )
    if not down.exists():
        down.write_text(
            _CHEVRON_SVG.format(color=hexc, path='<path d="M2.5 4.5L6 8l3.5-3.5"/>'),
            encoding="utf-8",
        )
    return up.as_posix(), down.as_posix()


# --------------------------------------------------------------------------
def build_qss(palette: QPalette, semantic: dict[str, QColor]) -> str:
    m = METRICS
    R = QPalette.ColorRole

    window = palette.color(R.Window)
    window_text = palette.color(R.WindowText)
    base = palette.color(R.Base)
    alt_base = palette.color(R.AlternateBase)
    text = palette.color(R.Text)
    button = palette.color(R.Button)
    button_text = palette.color(R.ButtonText)
    border = palette.color(R.Mid)
    border_strong = palette.color(R.Dark)
    accent = palette.color(R.Highlight)
    on_accent = palette.color(R.HighlightedText)
    tooltip_bg = palette.color(R.ToolTipBase)
    tooltip_text = palette.color(R.ToolTipText)
    disabled = palette.color(QPalette.ColorGroup.Disabled, R.WindowText)

    dark = window.lightness() < 128
    white = QColor(255, 255, 255)

    # 卡片用"比窗口略高一层"来暗示层级。深色下要往亮走，浅色下也要往亮走——
    # 否则深色主题里卡片会比背景更暗，层级感反了。
    card = _mix(window, white, 0.045) if dark else _mix(window, white, 0.72)
    hover = _mix(button, white, 0.10) if dark else _mix(button, QColor(0, 0, 0), 0.06)
    pressed = _mix(button, QColor(0, 0, 0), 0.16) if dark else _mix(button, QColor(0, 0, 0), 0.12)
    accent_hover = _mix(accent, white, 0.14) if dark else _mix(accent, QColor(0, 0, 0), 0.10)
    nav_hover = _rgba(window_text, 0.07)
    nav_selected = _rgba(accent, 0.18)
    scroll = _rgba(window_text, 0.22)

    muted = _hex(semantic["muted"])
    arrow_up, arrow_down = _arrow_urls(window_text)

    return f"""
/* ===================== 基础 ===================== */
QWidget {{
    color: {_hex(window_text)};
}}
QMainWindow, QDialog {{
    background: {_hex(window)};
}}
QLabel#Muted, QLabel[muted="true"] {{
    color: {muted};
}}
QLabel#CardTitle {{
    font-weight: 600;
}}
QLabel#SectionHint {{
    color: {muted};
}}

/* ===================== 卡片 ===================== */
QFrame#Card {{
    background: {_hex(card)};
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_md}px;
}}
QFrame#CardHeader {{
    background: transparent;
    border: none;
}}
QFrame#MetricTile {{
    background: {_hex(_mix(card, window_text, 0.05))};
    border: 1px solid {_hex(_mix(border, card, 0.4))};
    border-radius: {m.radius_md}px;
}}
QFrame#HLine {{
    background: {_hex(border)};
    max-height: 1px;
    border: none;
}}

/* ===================== 按钮 ===================== */
QPushButton {{
    background: {_hex(button)};
    color: {_hex(button_text)};
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_sm}px;
    padding: 0 {m.space_3}px;
    min-height: {m.control_height}px;
}}
QPushButton:hover {{
    background: {_hex(hover)};
}}
QPushButton:pressed {{
    background: {_hex(pressed)};
}}
QPushButton:disabled {{
    color: {_hex(disabled)};
    border-color: {_hex(_mix(border, window, 0.4))};
}}
QPushButton:focus {{
    border: 1px solid {_hex(accent)};
}}
QPushButton#Primary {{
    background: {_hex(accent)};
    color: {_hex(on_accent)};
    border: 1px solid {_hex(accent)};
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: {_hex(accent_hover)};
    border-color: {_hex(accent_hover)};
}}
QPushButton#Primary:disabled {{
    background: {_mix(accent, window, 0.55).name()};
    border-color: {_mix(accent, window, 0.55).name()};
    color: {_hex(disabled)};
}}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid transparent;
}}
QPushButton#Ghost:hover {{
    background: {nav_hover};
}}

/* ===================== 输入控件 ===================== */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {_hex(base)};
    color: {_hex(text)};
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_sm}px;
    padding: 0 {m.space_2}px;
    min-height: {m.control_height}px;
    selection-background-color: {_hex(accent)};
    selection-color: {_hex(on_accent)};
}}
QPlainTextEdit, QTextEdit {{
    padding: {m.space_2}px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {_hex(accent)};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {_hex(disabled)};
    background: {_hex(_mix(base, window, 0.5))};
}}
QComboBox QAbstractItemView {{
    background: {_hex(base)};
    color: {_hex(text)};
    border: 1px solid {_hex(border)};
    selection-background-color: {_hex(accent)};
    selection-color: {_hex(on_accent)};
    outline: none;
}}
/* 只给步进按钮上底色，**不碰** up-arrow/down-arrow：
   不设底色的话 Fusion 会用浅色按钮色画它，在深色主题里就是一个白方块。
   箭头留给基础样式自己画，形状才是对的。 */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 18px;
    background: {_hex(button)};
    border: none;
    border-left: 1px solid {_hex(border)};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {_hex(hover)};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{arrow_up}");
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{arrow_down}");
    width: 10px;
    height: 10px;
}}
/* QComboBox 的箭头不覆盖，交给基础样式画——它自带的三角形本来就是对的。 */

/* ===================== 表格 ===================== */
QTableView, QTreeView {{
    background: {_hex(base)};
    alternate-background-color: {_hex(alt_base)};
    color: {_hex(text)};
    gridline-color: {_hex(_mix(border, base, 0.5))};
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_sm}px;
    selection-background-color: {_hex(accent)};
    selection-color: {_hex(on_accent)};
    outline: none;
}}
QTableView::item, QTreeView::item {{
    padding: 0 {m.space_2}px;
}}
QHeaderView {{
    background: transparent;
}}
QHeaderView::section {{
    background: {_hex(_mix(button, window, 0.25))};
    color: {_hex(window_text)};
    padding: {m.space_1}px {m.space_2}px;
    border: none;
    border-right: 1px solid {_hex(border)};
    border-bottom: 1px solid {_hex(border)};
    font-weight: 600;
}}
QHeaderView::section:last {{
    border-right: none;
}}
QTableCornerButton::section {{
    background: {_hex(_mix(button, window, 0.25))};
    border: none;
}}

/* ===================== 进度条 ===================== */
/* 刻意不给 QProgressBar 本身设 background：设了之后 Fusion 会再画一遍自己的
   凹槽，实测渲染成一条深蓝色底。只描述 chunk，凹槽交回基础样式。 */
QProgressBar {{
    color: {_hex(text)};
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_sm}px;
    min-height: 18px;
    max-height: 18px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {_hex(accent)};
    border-radius: {m.radius_sm - 1}px;
}}

/* ===================== 导航栏 ===================== */
QListWidget#Nav {{
    background: transparent;
    border: none;
    outline: none;
    padding: {m.space_2}px;
}}
QListWidget#Nav::item {{
    padding: {m.space_2}px {m.space_3}px;
    margin: 1px 0;
    border-radius: {m.radius_sm}px;
    color: {_hex(window_text)};
}}
QListWidget#Nav::item:hover {{
    background: {nav_hover};
}}
QListWidget#Nav::item:selected {{
    background: {nav_selected};
    color: {_hex(window_text)};
    font-weight: 600;
}}

/* ===================== 折叠区 ===================== */
QToolButton#SectionHeader {{
    background: transparent;
    border: none;
    color: {_hex(window_text)};
    padding: {m.space_1}px 0;
    text-align: left;
    font-weight: 600;
}}
QToolButton#SectionHeader:hover {{
    color: {_hex(accent)};
}}

/* ===================== 滚动条 ===================== */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {scroll}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:horizontal {{
    background: {scroll}; border-radius: 5px; min-width: 28px;
}}
QScrollBar::handle:hover {{
    background: {_rgba(window_text, 0.36)};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0; background: none; border: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ===================== 停靠面板 ===================== */
QDockWidget {{
    border-top: 1px solid {_hex(border)};
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}}
QDockWidget > QWidget {{
    background: {_hex(window)};
}}

/* ===================== 状态栏 / 工具栏 / 提示 ===================== */
QStatusBar {{
    background: {_hex(window)};
    color: {muted};
    border-top: 1px solid {_hex(border)};
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    color: {muted};
}}
QToolBar {{
    background: {_hex(window)};
    border: none;
    border-bottom: 1px solid {_hex(border)};
    padding: {m.space_1}px;
    spacing: {m.space_1}px;
}}
QToolTip {{
    background: {_hex(tooltip_bg)};
    color: {_hex(tooltip_text)};
    border: 1px solid {_hex(border_strong)};
    padding: {m.space_1}px {m.space_2}px;
}}

/* ===================== 分割器 / 分组框 / 页签 ===================== */
QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:hover {{
    background: {_hex(border)};
}}
QGroupBox {{
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_md}px;
    margin-top: {m.space_3}px;
    padding: {m.card_padding}px;
    padding-top: {m.space_4}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: {m.space_3}px;
    padding: 0 {m.space_1}px;
    color: {muted};
}}
QTabWidget::pane {{
    border: 1px solid {_hex(border)};
    border-radius: {m.radius_md}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {muted};
    padding: {m.space_2}px {m.space_3}px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {_hex(window_text)};
}}
QTabBar::tab:selected {{
    color: {_hex(window_text)};
    border-bottom: 2px solid {_hex(accent)};
}}
"""
