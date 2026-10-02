# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""缩略图网格：可勾选。

缩略图由**容器**生成（宿主不碰像素——宿主连图像库都没装，这是刻意的），
这里只负责把生成的 PNG 显示出来，并记录用户勾了哪些。

**为什么用勾选框而不是"选中"**：这个网格的用途是"决定哪些图片进入校准集"，
是一个**集合**语义。而 Qt 的 selection 是"当前操作焦点"，点一张会取消上一张，
两件事混在一起会让用户误操作。勾选状态是持久的、可累加的，也和最终产物一一对应。

（实测确认过：`QListView` 的 IconMode **会**渲染 `setCheckable(True)` 的复选框，
渲染差异 6155 像素。如果不成立，退路是把 selection 当纳入集合——
下面的 API 一个都不用改。）
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QListView

from ..metrics import METRICS

#: 最多显示多少张。校准集官方建议 20~100 张，超过这个数说明该先筛选了。
MAX_TILES = 200


class ThumbnailGrid(QListView):
    """图标模式、可勾选的文件网格。"""

    #: 勾选集合发生变化
    checkedChanged = Signal()

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
        # 勾选框负责"用不用"，selection 只用于键盘导航，所以保持单选语义但不禁用
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self._model.itemChanged.connect(self._on_item_changed)
        #: 批量改勾选状态时压住信号，避免 N 次重复通知
        self._suppress = False
        # 网格自身不滚动：外层页面已经是滚动区，嵌套滚动会很难用
        self.setMinimumHeight(160)

    # ------------------------------------------------------------------
    # 勾选状态
    # ------------------------------------------------------------------
    def checked_names(self) -> list[str]:
        """被勾选的文件名，**按网格顺序**。

        顺序很重要：它决定了生成时处理图片的顺序，也就决定了产物文件名的对照关系。
        """
        return [
            self._model.item(row).text()
            for row in range(self._model.rowCount())
            if self._model.item(row).checkState() == Qt.CheckState.Checked
        ]

    def all_names(self) -> list[str]:
        return [self._model.item(row).text() for row in range(self._model.rowCount())]

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        with self._bulk():
            for row in range(self._model.rowCount()):
                self._model.item(row).setCheckState(state)

    def invert_checked(self) -> None:
        with self._bulk():
            for row in range(self._model.rowCount()):
                item = self._model.item(row)
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )

    def uncheck(self, names: Iterable[str]) -> int:
        """取消勾选指定的名字，返回实际取消了几个。"""
        wanted = set(names)
        changed = 0
        with self._bulk() as counter:
            for row in range(self._model.rowCount()):
                item = self._model.item(row)
                if item.text() in wanted and item.checkState() == Qt.CheckState.Checked:
                    item.setCheckState(Qt.CheckState.Unchecked)
                    changed += 1
            counter["changed"] = changed
        return changed

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def clear(self) -> None:
        with self._bulk():
            self._model.clear()
        self._sync_height()

    def set_images(self, directory: Path, names: list[str] | None = None) -> int:
        """把目录（或指定文件名）下的图片装进网格，**默认全部勾选**。"""
        with self._bulk():
            self._model.clear()
            if directory.is_dir():
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
                # 曾经用 setItem(row, col) 铺了 6 列，结果只有每行第 0 个能被画出来：
                # 50 张只显示了 9 张（50÷6 向上取整），界面上就成了一排。
                # 换行交给视图自己按宽度做。
                for path in candidates:
                    item = QStandardItem(QIcon(str(path)), path.name)
                    item.setToolTip(str(path))
                    item.setEditable(False)
                    item.setCheckable(True)
                    item.setCheckState(Qt.CheckState.Checked)
                    self._model.appendRow(item)
        self._sync_height()
        return self._model.rowCount()

    # ------------------------------------------------------------------
    @contextmanager
    def _bulk(self):
        """批量改动时压住 `itemChanged`，只在最后发一次信号。

        不做这个的话，"全选 200 张"会触发 200 次信号，界面会重算 200 次计数。
        """
        self._suppress = True
        counter: dict = {}
        try:
            yield counter
        finally:
            self._suppress = False
            self.checkedChanged.emit()

    def _on_item_changed(self, _item: QStandardItem) -> None:
        if not self._suppress:
            self.checkedChanged.emit()

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
