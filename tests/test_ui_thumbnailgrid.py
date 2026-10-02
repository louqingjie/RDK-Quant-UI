# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""缩略图网格的勾选行为。

网格是"手动挑选校准集"的落点，所以它的集合语义必须准：勾选顺序决定生成顺序，
产物文件名与之一一对应。

⚠️ 这里不构造完整的 AppShell —— 那需要整套宿主探测的替身，而本文件要测的是
**网格自身**。构造完整壳的代价（约 0.4 秒/个）不值得为这几条断言付。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rdkquant.ui.widgets.thumbnailgrid import MAX_TILES, ThumbnailGrid  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def thumbs(tmp_path: Path) -> Path:
    """造一批假缩略图。网格只看文件名，所以内容无所谓。"""
    directory = tmp_path / "thumbs"
    directory.mkdir()
    for index in range(7):
        (directory / f"{index:04d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return directory


@pytest.fixture()
def grid(qapp, thumbs):
    widget = ThumbnailGrid()
    widget.resize(600, 400)
    widget.set_images(thumbs)
    return widget


# --------------------------------------------------------------------------
# 默认状态
# --------------------------------------------------------------------------
def test_everything_is_checked_by_default(grid):
    """扫描完默认全选 —— 采样的结果本来就是候选，用户是在上面做减法。"""
    assert len(grid.all_names()) == 7
    assert grid.checked_names() == grid.all_names()


def test_checked_names_follows_grid_order(grid):
    """顺序必须稳定：它决定了生成顺序，也就决定了产物与源文件的对照关系。"""
    assert grid.checked_names() == sorted(grid.checked_names())


# --------------------------------------------------------------------------
# 批量操作
# --------------------------------------------------------------------------
def test_select_all_and_none(grid):
    grid.set_all_checked(False)
    assert grid.checked_names() == []
    grid.set_all_checked(True)
    assert len(grid.checked_names()) == 7


def test_invert(grid):
    grid.uncheck(["0000.png", "0001.png"])
    assert len(grid.checked_names()) == 5
    grid.invert_checked()
    assert grid.checked_names() == ["0000.png", "0001.png"]


def test_uncheck_returns_how_many_changed(grid):
    assert grid.uncheck(["0000.png", "0003.png"]) == 2
    # 再取消一次：已经是未勾选状态，不该重复计数
    assert grid.uncheck(["0000.png", "0003.png"]) == 0
    assert grid.uncheck(["不存在.png"]) == 0


def test_uncheck_ignores_unknown_names(grid):
    grid.uncheck(["没有这个文件.png"])
    assert len(grid.checked_names()) == 7


# --------------------------------------------------------------------------
# 信号
# --------------------------------------------------------------------------
def test_checked_changed_fires(grid):
    seen = []
    grid.checkedChanged.connect(lambda: seen.append(1))
    grid.uncheck(["0000.png"])
    assert len(seen) == 1


def test_bulk_operations_emit_once(grid):
    """批量操作只发一次信号。

    不这么做的话，"全选 200 张"会触发 200 次回调，界面要重算 200 次计数。
    """
    seen = []
    grid.checkedChanged.connect(lambda: seen.append(1))
    grid.set_all_checked(False)
    grid.invert_checked()
    assert len(seen) == 2, f"两次批量操作应当只发两次信号，实际 {len(seen)} 次"


# --------------------------------------------------------------------------
# 内容
# --------------------------------------------------------------------------
def test_set_images_resets_everything_to_checked(grid, thumbs):
    grid.set_all_checked(False)
    grid.set_images(thumbs)
    assert len(grid.checked_names()) == 7


def test_set_images_can_filter_by_name(grid, thumbs):
    count = grid.set_images(thumbs, names=["0002.png", "0005.png"])
    assert count == 2
    assert grid.checked_names() == ["0002.png", "0005.png"]


def test_set_images_respects_the_tile_limit(qapp, tmp_path):
    """超过上限只显示前 MAX_TILES 个 —— 所以采样张数的上限就是它，
    否则会有"选了却勾不到"的隐藏坑。"""
    directory = tmp_path / "many"
    directory.mkdir()
    for index in range(MAX_TILES + 25):
        (directory / f"{index:05d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    widget = ThumbnailGrid()
    count = widget.set_images(directory)
    assert count == MAX_TILES


def test_set_images_on_missing_directory_is_empty(grid, tmp_path):
    assert grid.set_images(tmp_path / "不存在") == 0
    assert grid.checked_names() == []


def test_clear(grid):
    grid.clear()
    assert grid.all_names() == []
    assert grid.checked_names() == []
