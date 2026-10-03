# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""界面几何与文案的机械自检。

## 为什么需要

本会话出现过三次「代码能跑、测试全绿、界面上是错的」。其中最典型的一次：
`ThumbnailGrid` 的条目只有 **14 px 高**（`visualRect` 实测），96 px 的缩略图
直接压在邻居的文件名和勾选框上 —— 而所有测试都只断言了 model 的行为，
**没人量过视图的矩形**。

## 它能抓什么、不能抓什么（别高估）

| 问题类型 | 这个模块 | 说明 |
|---|---|---|
| 条目装不下图标 + 文字 | ✅ | `check_icon_items_fit` |
| 可见条目互相重叠 | ✅ | `check_no_overlap` |
| 可见控件尺寸为 0 | ✅ | `check_interactive_widgets` |
| 文案里漏出 `None` / `Traceback` / repr | ✅ | `check_no_repr_leak` |
| **某条代码路径根本没被走到** | ❌ | 那是「少写了一条测试」，框架帮不上 |
| **文案说的和代码相反** | ❌ | 没有通用定义，只能**逐处写断言** |

后两类占本会话实际问题的三分之二。这个模块锁的是**第一类**，
不要因为它在就以为界面不会再错。

## 约定

- 检查函数**返回问题清单**，不直接断言。这样一处调用能报出全部问题，
  由调用方决定怎么用（测试里 `assert not problems`，将来别的工具可以打印）。
- **视图不可见时返回空清单**：没有显示就没有 `visualRect` 可言。
- 空网格返回空清单，**那是正常的**，不是问题。
"""

from __future__ import annotations

import re

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QListView,
    QWidget,
)

#: 可见时不可能合法地为 0 尺寸的控件类型
_INTERACTIVE = (QAbstractButton, QComboBox, QLineEdit, QAbstractSpinBox, QAbstractItemView)

#: 文案里不该出现的「明显是程序漏出来的东西」
#:
#: 用词边界匹配 `None` / `nan`：中文界面里它们只可能来自漏格式化的 f-string 或
#: 直接把 Python 值塞进 `setText()`。**不用**搜 `{}` —— 提示文案里合法地演示
#: 模板是会出现的，那会变成假阳性。
_REPR_PATTERNS = (
    (re.compile(r"(?<![A-Za-z])None(?![A-Za-z])"), "None"),
    (re.compile(r"(?<![A-Za-z])nan(?![A-Za-z])", re.IGNORECASE), "nan"),
    (re.compile(r"Traceback \(most recent call last\)"), "Traceback"),
    (re.compile(r"object at 0x"), "repr 泄漏"),
)

#: 问题太多时只报前几条 —— 同一种错通常会重复几十次
_MAX_REPORTED = 3


def _summarize(problems: list[str]) -> list[str]:
    if len(problems) <= _MAX_REPORTED:
        return problems
    return problems[:_MAX_REPORTED] + [f"…… 共 {len(problems)} 条同类问题"]


def _is_icon_view(view: QAbstractItemView) -> bool:
    """只有图标模式才有「图标 + 文字」这个尺寸模型。

    ⚠️ `ViewMode` 挂在 `QListView` 上，**不在** `QAbstractItemView` 上 ——
    写成后者会 `AttributeError: type object 'QAbstractItemView' has no
    attribute 'ViewMode'`（实测撞过）。
    """
    return isinstance(view, QListView) and view.viewMode() == QListView.ViewMode.IconMode


def visible_indexes(view: QAbstractItemView) -> list[QModelIndex]:
    """**真的看得见**的条目。

    视图不可见时返回空 —— offscreen 平台上未显示的控件量不出 `visualRect`，
    强行量会得到一堆 0 矩形，于是变成假阳性。
    """
    model = view.model()
    if model is None or not view.isVisible():
        return []
    viewport = view.viewport().rect()
    found: list[QModelIndex] = []
    for row in range(model.rowCount()):
        index = model.index(row, 0)
        rect = view.visualRect(index)
        if rect.isValid() and rect.width() > 0 and rect.intersects(viewport):
            found.append(index)
    return found


def check_icon_items_fit(view: QAbstractItemView, indexes=None) -> list[str]:
    """条目矩形必须装得下**图标 + 一行文字**。

    **这条就是本会话那个 bug 的检查。** `QListView` 的条目高度由 delegate 的
    `sizeHint` 算，而它包含图标尺寸；未命中缓存时若 `data(DecorationRole)`
    返回**空 `QIcon()`**，delegate 会按「只有文字」算出 14 px，再被
    `setUniformItemSizes(True)` 用到全部条目上。
    """
    if not _is_icon_view(view):
        return []
    icon_h = view.iconSize().height()
    text_h = view.fontMetrics().height()
    if icon_h <= 0 or text_h <= 0:
        return []

    needed = icon_h + text_h
    rows = visible_indexes(view) if indexes is None else indexes
    problems = [
        f"第 {index.row()} 项只有 {view.visualRect(index).height()}px 高，"
        f"装不下图标 {icon_h} + 文字 {text_h} = {needed}px"
        for index in rows
        if view.visualRect(index).height() < needed
    ]
    return _summarize(problems)


def check_no_overlap(view: QAbstractItemView, indexes=None) -> list[str]:
    """可见条目**两两不相交**。

    只看可见的那些：全量是 O(n²)，3504 个条目会变成 600 万次判断。
    实测一屏约 45 个 → 约 1000 次，可以忽略。
    """
    rows = visible_indexes(view) if indexes is None else indexes
    rects = [(index.row(), view.visualRect(index)) for index in rows]
    problems = [
        f"第 {a_row} 项与第 {b_row} 项的矩形重叠"
        for position, (a_row, a_rect) in enumerate(rects)
        for b_row, b_rect in rects[position + 1:]
        if a_rect.intersects(b_rect)
    ]
    return _summarize(problems)


def check_interactive_widgets(root: QWidget) -> list[str]:
    """可见的按钮 / 输入框 / 列表不得是 **0 尺寸**。

    隐藏的不管：`CollapsibleSection` 折叠起来的时候里面的控件正是 0 尺寸，
    那是正常的。
    """
    problems = []
    for child in root.findChildren(QWidget):
        if not isinstance(child, _INTERACTIVE) or not child.isVisible():
            continue
        if child.width() <= 0 or child.height() <= 0:
            name = child.objectName() or type(child).__name__
            problems.append(f"{name} 可见但尺寸是 {child.width()}x{child.height()}")
    return _summarize(problems)


def check_no_repr_leak(root: QWidget) -> list[str]:
    """可见文案里不该漏出 `None` / `Traceback` / `<... object at 0x...>`。

    这类东西一旦出现，说明某个值没格式化就塞进了界面 —— 而且**不会有异常**。
    """
    problems = []
    for child in root.findChildren(QWidget):
        if not child.isVisible():
            continue
        if isinstance(child, QAbstractButton):
            text = child.text()
        elif isinstance(child, QLineEdit):
            text = child.text()
        elif hasattr(child, "text") and callable(child.text):
            text = child.text()
        else:
            continue
        for pattern, what in _REPR_PATTERNS:
            if pattern.search(text):
                problems.append(f"可见文案里有 {what}：{text.strip()[:40]!r}")
                break
    return _summarize(problems)


def check_all(view: QWidget) -> list[str]:
    """一次跑完所有检查，**包括嵌在里面的视图**。

    `visible_indexes` 每个视图只算一次然后复用 —— 单独调每个检查会各算一遍，
    3504 个条目时那是三倍的 `visualRect` 调用。

    ⚠️ 传一个**页面**进来时，页面本身不是 `QAbstractItemView`，
    但它里面嵌着表格和网格 —— 不递归下去的话，几何检查等于没跑
    （实测：先写成不递归，校准页"通过"了，而网格一个矩形都没量）。
    """
    problems: list[str] = []

    views = [view] if isinstance(view, QAbstractItemView) else []
    views += view.findChildren(QAbstractItemView)
    for item_view in views:
        indexes = visible_indexes(item_view)
        problems += check_icon_items_fit(item_view, indexes)
        problems += check_no_overlap(item_view, indexes)

    problems += check_interactive_widgets(view)
    problems += check_no_repr_leak(view)
    return problems
