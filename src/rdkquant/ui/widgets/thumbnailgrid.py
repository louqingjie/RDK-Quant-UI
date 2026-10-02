# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""缩略图网格。

缩略图由**容器**生成（宿主不碰像素——宿主连图像库都没装，这是刻意的），
这里只负责把生成的 PNG 显示出来。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QListView

from ..metrics import METRICS

#: 最多显示多少张。校准集是 20~100 张，超过这个数说明用户指错了目录。
MAX_TILES = 200


class ThumbnailGrid(QListView):
    """图标模式的文件网格。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(96, 96))
        self.setGridSize(QSize(116, 132))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSpacing(METRICS.space_1)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setFocusPolicy(self.focusPolicy())
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        # 网格自身不滚动：外层页面已经是滚动区，嵌套滚动会很难用
        self.setVerticalScrollBarPolicy(self.verticalScrollBarPolicy())
        self.setMinimumHeight(160)

    def clear(self) -> None:
        self._model.clear()
        self._sync_height()

    def set_images(self, directory: Path, names: list[str] | None = None) -> int:
        """把目录（或指定文件名）下的图片装进网格，返回实际装了几个。"""
        self.clear()
        if not directory.is_dir():
            return 0
        candidates = sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")
        )
        if names:
            wanted = set(names)
            candidates = [p for p in candidates if p.name in wanted]
        candidates = candidates[:MAX_TILES]

        # ⚠️ **全部放在第 0 列。**
        # QListView 在 IconMode 下是"一维流式"布局，**只读模型的第 0 列** ——
        # 之前用 setItem(row, col) 铺了 6 列，结果只有每行第 0 个能被画出来：
        # 50 张只显示了 9 张（50÷6 向上取整），界面上就成了一排。
        # 换行交给视图自己按宽度做。
        for path in candidates:
            item = QStandardItem(QIcon(str(path)), path.name)
            item.setToolTip(str(path))
            item.setEditable(False)
            self._model.appendRow(item)
        self._sync_height()
        return len(candidates)

    def _sync_height(self) -> None:
        """按"每行能放几个"算出需要多高。

        高度必须自己算：外层是滚动区，视图给不出合适的 sizeHint，
        不算的话要么只露一行、要么留一大片空白。
        """
        grid = self.gridSize()
        per_row = max(1, self.viewport().width() // max(1, grid.width()))
        count = self._model.rowCount()
        rows = max(1, (count + per_row - 1) // per_row)
        wanted = rows * grid.height() + 12
        if self.minimumHeight() != wanted:
            self.setMinimumHeight(wanted)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_height()
