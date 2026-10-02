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
    """从行首的 `[INFO]` / `[WARN]` 这类标记判断等级。

    hb_mapper 的日志格式在不同工具链版本之间会漂移，所以只认行首标记，
    认不出就当普通行——**绝不因为格式变了就报错**。
    """
    match = _LEVEL_RE.match(line)
    if not match:
        return LogLevel.PLAIN
    token = match.group(1).lower()
    if token in _ALIASES:
        return _ALIASES[token]
    try:
        return LogLevel(token)
    except ValueError:
        return LogLevel.PLAIN


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
        level = classify(line)
        self._entries.append((level, line))
        self._write(level, line)
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
