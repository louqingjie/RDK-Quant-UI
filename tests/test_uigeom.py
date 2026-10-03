# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""`tests/uigeom.py` 的自检。

**一个永远不会红的检查等于没有检查。** 所以每条检查都要配一个「故意弄坏 →
断言它报出来了」的用例，外加一个「正常情况 → 空清单」的用例防假阳性。

坏掉的构造方式尽量照着真实成因来，不要为了好写而造一个不可能出现的形状。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPixmap, QStandardItem, QStandardItemModel  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import uigeom  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _icon_view(qapp, *, with_icons: bool = True) -> QListView:
    """一个最小的图标模式列表。

    `with_icons=False` 就是本会话那个 bug 的形状：**条目没有图标**，
    于是 delegate 按"只有文字"算高度。
    """
    view = QListView()
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setIconSize(QSize(96, 96))
    view.setGridSize(QSize(116, 132))
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setMovement(QListView.Movement.Static)
    view.setUniformItemSizes(True)
    view.resize(600, 400)

    model = QStandardItemModel(view)
    pixmap = QPixmap(96, 96)
    pixmap.fill(QColor("steelblue"))
    for index in range(12):
        if with_icons:
            model.appendRow(QStandardItem(QIcon(pixmap), f"{index:04d}.jpg"))
        else:
            model.appendRow(QStandardItem(f"{index:04d}.jpg"))
    view.setModel(model)
    view.show()
    qapp.processEvents()
    return view


# --------------------------------------------------------------------------
# check_icon_items_fit —— 这条就是本会话那个 bug 的检查
# --------------------------------------------------------------------------
def test_fit_passes_for_a_healthy_view(qapp):
    assert uigeom.check_icon_items_fit(_icon_view(qapp)) == []


def test_fit_catches_items_without_icons(qapp):
    """**复现本会话那个 bug 的形状。**

    条目没有图标 → delegate 按"只有文字"算高度（实测 14 px）→ 而 `iconSize`
    还是 96。96 px 的缩略图会压在邻居的名字和勾选框上。
    """
    problems = uigeom.check_icon_items_fit(_icon_view(qapp, with_icons=False))
    assert problems, "没图标的条目必须被报出来"
    assert "装不下" in problems[0]


def test_fit_is_skipped_for_non_icon_views(qapp):
    """非图标模式没有「图标 + 文字」这个尺寸模型，不该报。"""
    view = QListView()
    view.setViewMode(QListView.ViewMode.ListMode)
    view.setIconSize(QSize(96, 96))
    view.resize(400, 300)
    model = QStandardItemModel(view)
    model.appendRow(QStandardItem("x"))
    view.setModel(model)
    view.show()
    qapp.processEvents()
    assert uigeom.check_icon_items_fit(view) == []


# --------------------------------------------------------------------------
# check_no_overlap
# --------------------------------------------------------------------------
def test_overlap_passes_for_a_healthy_view(qapp):
    assert uigeom.check_no_overlap(_icon_view(qapp)) == []


def test_overlap_catches_forced_collision(qapp):
    """**这条检查是防御性的**：正常布局下 Qt 会把条目排在网格上，不会重叠。

    所以只能手动把两个条目挪到同一个位置来让它红。留着它是因为
    `Movement.Static` / `ResizeMode.Adjust` 一旦被改动，重叠就会真的发生，
    而界面上"图叠在一起"很难一眼归因。
    """
    view = _icon_view(qapp)
    view.setMovement(QListView.Movement.Free)
    view.setPositionForIndex(QPoint(0, 0), view.model().index(0, 0))
    view.setPositionForIndex(QPoint(0, 0), view.model().index(1, 0))
    qapp.processEvents()

    problems = uigeom.check_no_overlap(view)
    assert problems, "强行叠在一起的两个条目必须被报出来"
    assert "重叠" in problems[0]


# --------------------------------------------------------------------------
# check_interactive_widgets
# --------------------------------------------------------------------------
def test_widgets_pass_for_a_normal_container(qapp):
    root = QWidget()
    root.resize(200, 100)
    button = QPushButton("确定", root)
    button.resize(80, 30)
    root.show()
    qapp.processEvents()
    assert uigeom.check_interactive_widgets(root) == []


def test_widgets_catches_a_visible_zero_size_button(qapp):
    root = QWidget()
    root.resize(200, 100)
    button = QPushButton("看不见的按钮", root)
    button.setFixedSize(0, 0)
    root.show()
    qapp.processEvents()
    assert uigeom.check_interactive_widgets(root), "可见但 0 尺寸的按钮必须被报出来"


def test_widgets_ignores_hidden_ones(qapp):
    """折叠区里的控件就是 0 尺寸且不可见 —— **不能报**，否则满屏假阳性。"""
    root = QWidget()
    root.resize(200, 100)
    button = QPushButton("折叠起来的按钮", root)
    button.setFixedSize(0, 0)
    button.hide()
    root.show()
    qapp.processEvents()
    assert uigeom.check_interactive_widgets(root) == []


# --------------------------------------------------------------------------
# check_no_repr_leak
# --------------------------------------------------------------------------
def test_repr_leak_passes_for_normal_text(qapp):
    root = QWidget()
    label = QLabel("内存充足：15.8 GB", root)
    root.show()
    qapp.processEvents()
    assert uigeom.check_no_repr_leak(root) == []


@pytest.mark.parametrize("bad", [
    "None",
    "nan",
    "Traceback (most recent call last)",
    "<X object at 0x1>",
])
def test_repr_leak_catches_leaked_values(qapp, bad: str):
    root = QWidget()
    QLabel(bad, root)
    root.show()
    qapp.processEvents()
    assert uigeom.check_no_repr_leak(root), f"{bad!r} 必须被报出来"


def test_repr_leak_does_not_flag_words_containing_none(qapp):
    """`NoneType` / `nonempty` 这种含 "None" 的英文词不该误报。"""
    root = QWidget()
    QLabel("nonempty 检查通过", root)
    root.show()
    qapp.processEvents()
    assert uigeom.check_no_repr_leak(root) == []


# --------------------------------------------------------------------------
# 不可见 / 空 —— 最容易变成假阳性的两种
# --------------------------------------------------------------------------
def test_invisible_view_reports_nothing(qapp):
    """视图没显示就量不出 `visualRect`，此时**返回空而不是一堆 0 矩形**。"""
    view = _icon_view(qapp, with_icons=False)
    view.hide()
    qapp.processEvents()
    assert uigeom.visible_indexes(view) == []
    assert uigeom.check_all(view) == []


def test_empty_view_reports_nothing(qapp):
    """空网格是正常的，不是问题。"""
    view = QListView()
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setIconSize(QSize(96, 96))
    view.setGridSize(QSize(116, 132))
    view.resize(600, 400)
    view.setModel(QStandardItemModel(view))
    view.show()
    qapp.processEvents()
    assert uigeom.visible_indexes(view) == []
    assert uigeom.check_all(view) == []


# --------------------------------------------------------------------------
# check_all
# --------------------------------------------------------------------------
def test_check_all_is_clean_for_a_healthy_view(qapp):
    assert uigeom.check_all(_icon_view(qapp)) == []


def test_check_all_reports_the_broken_one(qapp):
    problems = uigeom.check_all(_icon_view(qapp, with_icons=False))
    assert any("装不下" in p for p in problems), "汇总入口要能带出几何问题"


def test_check_all_recurses_into_nested_views(qapp):
    """传一个**页面**进来时，里面嵌的视图也要量。

    先写成不递归，结果校准页"通过"了，而网格一个矩形都没量 ——
    空页面和好页面得到同样的结果，这种检查比没有更糟。
    """
    root = QWidget()
    root.resize(600, 400)
    layout = QVBoxLayout(root)
    layout.addWidget(_icon_view(qapp, with_icons=False))   # 故意坏的网格
    root.show()
    qapp.processEvents()

    problems = uigeom.check_all(root)
    assert any("装不下" in p for p in problems), "嵌套的坏网格必须被带出来"
