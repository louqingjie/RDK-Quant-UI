# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""图标：**优先用系统主题图标，缺失时回退到内置 SVG**。

优先 `QIcon.fromTheme()` 的理由和调色板一样——KDE 上拿到的是 Breeze 图标，
GNOME 上是 Adwaita，那才是原生。但 Windows 和裸 WSL 上没有任何图标主题，
`fromTheme()` 会返回空图标，所以必须自带一套兜底。

内置图标是 24×24 的描边风格（stroke=currentColor），用 QSvgRenderer 渲染
（`PySide6.QtSvg` 属于 PySide6-Essentials，已确认可用），不引入图标字体依赖。

内置的 SVG 全部只用圆、矩形、直线、折线这类基本图形手写——不抄任何图标库的
路径数据，避免许可证纠缠。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_WRAP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">{}</svg>'
)

# name -> 内部路径片段
_PATHS: dict[str, str] = {
    # ---- 流程步骤 ----
    "env": '<path d="M3 8l9-5 9 5v8l-9 5-9-5z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v8"/>',
    "model": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
             '<path d="M14 3v5h5"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/>'
           '<path d="M10 2v3M14 2v3M10 19v3M14 19v3M2 10h3M2 14h3M19 10h3M19 14h3"/>',
    "gauge": '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l5-6"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/>'
              '<circle cx="12" cy="12" r="1"/>',
    "toolbox": '<rect x="3" y="8" width="18" height="12" rx="2"/>'
               '<path d="M9 8V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><path d="M3 13h18"/>',
    # ---- 状态 ----
    "check": '<path d="M4 12.5l5 5L20 6.5"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
    "warning": '<path d="M12 3.5L22 20H2z"/><path d="M12 9.5v4.5"/>'
               '<path d="M12 17.5h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5h.01"/>',
    "dot": '<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/>'
            '<path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "skip": '<path d="M5 12h11"/><path d="M13 8l4 4-4 4"/>',
    # ---- 动作 ----
    "play": '<path d="M7 4.5l12 7.5-12 7.5z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "download": '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v5h-5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5'
              'a2 2 0 0 1-2-2z"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
                '<path d="M7 10l3 3-3 3"/><path d="M13 16h4"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18"/>'
             '<path d="M9 10v10"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/>'
            '<path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4l-8 8"/>'
                '<path d="M19 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
    "doc": '<path d="M5 4a1 1 0 0 1 1-1h12v18H6a1 1 0 0 1-1-1z"/><path d="M6 17h12"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    # ---- 主题 ----
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/>'
           '<path d="M5 5l1.4 1.4M17.6 17.6L19 19M19 5l-1.4 1.4M6.4 17.6L5 19"/>',
    "moon": '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
    "auto": '<circle cx="12" cy="12" r="9"/>'
            '<path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>',
}

# name -> 系统图标主题里的名字（取 freedesktop 标准名，KDE/GNOME 都认）
_THEME_NAMES: dict[str, str] = {
    "env": "preferences-system",
    "model": "document-open",
    "search": "edit-find",
    "layers": "view-list-details",
    "cpu": "application-x-executable",
    "gauge": "utilities-system-monitor",
    "target": "media-record",
    "toolbox": "applications-utilities",
    "check": "dialog-ok-apply",
    "cross": "dialog-error",
    "warning": "dialog-warning",
    "info": "dialog-information",
    "lock": "object-locked",
    "clock": "appointment-soon",
    "play": "media-playback-start",
    "stop": "media-playback-stop",
    "download": "download",
    "refresh": "view-refresh",
    "folder": "folder-open",
    "terminal": "utilities-terminal",
    "table": "view-list-details",
    "copy": "edit-copy",
    "external": "document-open-remote",
    "doc": "text-x-generic",
    "chevron-down": "go-down",
    "chevron-right": "go-next",
}

_cache: dict[tuple[str, str, int], QIcon] = {}


def clear_cache() -> None:
    """QPixmap 依附于 QApplication，测试里重建 app 前要清一次。"""
    _cache.clear()


def _svg(name: str) -> str | None:
    body = _PATHS.get(name)
    return _WRAP.format(body) if body else None


def glyph_pixmap(name: str, color: QColor, size: int = 16, *, dpr: float = 2.0) -> QPixmap:
    """直接渲染成 QPixmap，供自绘控件（如 Stepper）使用。"""
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    svg = _svg(name)
    if svg:
        renderer = QSvgRenderer(QByteArray(svg.replace("currentColor", color.name()).encode()))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter)
        painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: QColor | None = None, size: int = 20) -> QIcon:
    """取图标。

    `color is None` 时先试系统图标主题；指定了颜色则说明调用方需要重新着色，
    系统图标大多是固定色，这时直接用内置 SVG。
    """
    key = (name, color.name() if color else "-", size)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    if color is None:
        theme_name = _THEME_NAMES.get(name)
        if theme_name:
            themed = QIcon.fromTheme(theme_name)
            if not themed.isNull():
                _cache[key] = themed
                return themed

    # 回退：内置 SVG。颜色取窗口文字色（调用方通常不传）
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    fallback = color
    if fallback is None:
        fallback = app.palette().color(app.palette().ColorRole.WindowText) if app else QColor("#333333")
    result = QIcon(glyph_pixmap(name, fallback, size))
    _cache[key] = result
    return result


def available(names: list[str] | None = None) -> list[str]:
    """列出内置图标名（测试用）。"""
    return sorted(_PATHS) if names is None else [n for n in names if n in _PATHS]
