# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""通用只读表格模型。

findings、镜像清单、逐层量化误差、子图指标……全是"列 + 行"的形状，
一个模型就够。用 QTableWidget 逐格塞数据会让刷新逻辑变得很脏。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from ..metrics import METRICS


def make_table(
    headers: list[str],
    *,
    stretch_last: bool = True,
    fit_columns: bool = False,
) -> QTableView:
    """统一风格的只读表格。

    行高与表头高度一起提到现代密度（Qt 默认 20~24px，表头文字会被挤到贴边）。
    `fit_columns=True` 会按内容自适应列宽——适合列数少、内容短的结论型表格；
    列多且行多的明细表应保持默认，让用户自己拖。
    """
    view = QTableView()
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setAlternatingRowColors(True)
    view.setWordWrap(False)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(METRICS.row_height)

    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setFixedHeight(METRICS.control_height)
    if not fit_columns:
        header.setStretchLastSection(stretch_last)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return view


def fit_columns_to_content(view: QTableView, *, stretch_last: bool = True) -> None:
    """按内容自适应列宽，最后一列吃掉剩余宽度。

    **必须在 `setModel()` 之后调用。** 列数来自 model，模型还没挂上时
    `setSectionResizeMode(i, ...)` 对不存在的列号是无效的——之前把这段逻辑
    放在 make_table() 里（先建视图、后挂模型），结果就是各列按内容排完之后
    右侧留出一条没有表头覆盖的空白（实测到的现象）。

    另外注意 `setStretchLastSection` 会盖掉逐列的 ResizeToContents，
    所以这两个模式必须二选一，不能叠加。
    """
    model = view.model()
    count = model.columnCount() if model is not None else 0
    if count <= 0:
        return
    header = view.horizontalHeader()
    header.setStretchLastSection(False)
    for index in range(count):
        if stretch_last and index == count - 1:
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)


class TableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], rows=None, parent=None):
        super().__init__(parent)
        self._headers = list(headers)
        self._rows: list[list[str]] = []
        self._colors: dict[tuple[int, int], QColor] = {}
        self._tooltips: dict[tuple[int, int], str] = {}
        if rows:
            self.set_rows(rows)

    # ------------------------------------------------------------------
    def set_rows(
        self,
        rows,
        colors: dict[tuple[int, int], QColor] | None = None,
        tooltips: dict[tuple[int, int], str] | None = None,
    ) -> None:
        self.beginResetModel()
        self._rows = [list(map(str, row)) for row in rows]
        self._colors = dict(colors or {})
        self._tooltips = dict(tooltips or {})
        self.endResetModel()

    def row_text(self, row: int) -> list[str]:
        return list(self._rows[row]) if 0 <= row < len(self._rows) else []

    def rows_text(self) -> list[list[str]]:
        return [list(row) for row in self._rows]

    # ------------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._rows[row][col]
        if role == Qt.ItemDataRole.ForegroundRole:
            # 只给明确指定的单元格上色；整行着色会让"通过"行整片变绿，噪音太大。
            return self._colors.get((row, col))
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = self._tooltips.get((row, col))
            if tip:
                return tip
            # 内容被列宽截断时，至少让用户能看到全文
            return self._rows[row][col] or None
        if role == Qt.ItemDataRole.TextAlignmentRole and col >= self._numeric_from():
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return str(section + 1)

    def _numeric_from(self) -> int:
        """哪些列右对齐——目前全部左对齐，子类可覆盖。"""
        return len(self._headers) + 1
