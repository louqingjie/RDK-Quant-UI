# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""日志视图：按等级着色、可搜索、可导出、块数有上限。

量化编译动辄几万行，所以必须限长（默认 20000 块），否则长时间跑会吃光内存。
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from ..metrics import mono_font
from ..palette import semantic_colors
from ...core.logparse import ansi_level, strip_ansi, strip_timestamp_prefix

DEFAULT_MAX_BLOCKS = 20000


class LogLevel(str, Enum):
    PLAIN = "plain"
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


_LEVEL_RE = re.compile(
    r"^\s*(?:\[|\b)(DEBUG|INFO|WARN|WARNING|ERROR|FAIL|FAILED|SUCCESS|OK)(?:\]|\b)",
    re.IGNORECASE,
)
_ALIASES = {
    "warning": LogLevel.WARN,
    "fail": LogLevel.ERROR,
    "failed": LogLevel.ERROR,
    "ok": LogLevel.SUCCESS,
}


def classify(line: str) -> LogLevel:
    """判断日志等级。

    **优先用工具自己标的 ANSI 颜色。** 颜色是工具给出的结论，比我们按文字猜可靠：
    hb_mapper 打的是 `\\x1b[92mINFO\\x1b[0m` / `\\x1b[91mERROR\\x1b[0m`。
    之前完全没用这个信息，还把颜色码当垃圾显示出来（用户看到的是 `[92mINFO[0m`）。

    退路才是行首标记，而且必须先剥掉时间戳前缀：真实输出形如
    `2026-10-02 20:57:55,451 INFO ...`，在行首直接匹配 `INFO` 永远匹配不上——
    实测的后果是所有行都被判成普通级，日志一片灰。

    认不出就当普通行——**绝不因为格式变了就报错**。
    """
    hint = ansi_level(line)
    if hint:
        try:
            return LogLevel(hint)
        except ValueError:
            pass

    text = strip_timestamp_prefix(line)
    match = _LEVEL_RE.match(text)
    if not match:
        return LogLevel.PLAIN
    token = match.group(1).lower()
    if token in _ALIASES:
        return _ALIASES[token]
    try:
        return LogLevel(token)
    except ValueError:
        return LogLevel.PLAIN


def clean_for_display(line: str) -> str:
    """把一行变成"终端上看起来的样子"。

    除了剥 ANSI，还要处理回车：容器里的进度条靠 `\\r` 反复覆盖同一行，
    原样插进文本框会在日志里堆出一长串中间态（`  0%|   ... 50%|   ... 100%|`）。
    终端上用户只看得到最后一段，这里也照做。
    """
    text = strip_ansi(str(line)).rstrip("\n")
    if "\r" in text:
        segments = [part for part in text.split("\r") if part.strip()]
        if segments:
            text = segments[-1]
    return text.rstrip()


class LogView(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None, max_blocks: int = DEFAULT_MAX_BLOCKS):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(mono_font(self.font()))
        self.setMaximumBlockCount(max_blocks)
        self.setPlaceholderText("这里是工具链的原始输出。")
        self._entries: list[tuple[LogLevel, str]] = []
        self._auto_scroll = True

    # ------------------------------------------------------------------
    @property
    def auto_scroll(self) -> bool:
        return self._auto_scroll

    def set_auto_scroll(self, enabled: bool) -> None:
        self._auto_scroll = bool(enabled)

    def append_line(self, line: str) -> None:
        # 等级用**原始行**判断（ANSI 颜色在里面），显示和存档用剥干净的文本。
        level = classify(line)
        text = clean_for_display(line)
        self._entries.append((level, text))
        self._write(level, text)
        if self._auto_scroll:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def append_lines(self, lines) -> None:
        for line in lines:
            self.append_line(str(line))

    def clear_log(self) -> None:
        self._entries.clear()
        self.clear()

    def text_all(self) -> str:
        return "\n".join(text for _level, text in self._entries)

    def save_to(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text_all(), encoding="utf-8")
        return target

    # ------------------------------------------------------------------
    def _format_for(self, level: LogLevel) -> QTextCharFormat:
        fmt = QTextCharFormat()
        palette = self.palette()
        colors = semantic_colors(palette)
        if level is LogLevel.ERROR:
            color = colors["danger"]
        elif level is LogLevel.WARN:
            color = colors["warning"]
        elif level is LogLevel.SUCCESS:
            color = colors["success"]
        elif level is LogLevel.DEBUG:
            color = colors["muted"]
        elif level is LogLevel.INFO:
            color = palette.color(QPalette.ColorRole.Text)
        else:
            color = palette.color(QPalette.ColorRole.Text)
        fmt.setForeground(QColor(color))
        return fmt

    def _write(self, level: LogLevel, line: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(line + "\n", self._format_for(level))

    def _rebuild(self) -> None:
        """调色板变了要整篇重画——已插入的字符格式是写死的，不会自己跟着变。"""
        scroll = self.verticalScrollBar().value()
        self.setUpdatesEnabled(False)
        try:
            super().clear()
            for level, line in self._entries:
                self._write(level, line)
        finally:
            self.setUpdatesEnabled(True)
        self.verticalScrollBar().setValue(scroll)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange and self._entries:
            self._rebuild()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Ctrl+C 在只读控件里默认不复制，这里补上——用户最想干的就是复制报错
        if event.matches(QPlainTextEdit.StandardKey.Copy) or (
            event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.copy()
            return
        super().keyPressEvent(event)
