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

from rdkquant.ui.widgets.thumbnailgrid import ThumbnailGrid  # noqa: E402


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
# 按需解码
#
# 前提（实测，3504 个条目 / 1000×540 视口）：
#     data(DecorationRole) 被请求 **42 次**（1.2%），滚到底再新增 32 次
# 也就是说 Qt 只为可见项要图标 —— "随滚动随生成"顺着它本来的工作方式。
# --------------------------------------------------------------------------
@pytest.fixture()
def real_images(tmp_path: Path) -> Path:
    """真图。按需解码要真读文件，假 PNG header 解不出来。"""
    from PySide6.QtGui import QColor, QImage

    directory = tmp_path / "src"
    directory.mkdir()
    for index in range(7):
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(QColor(120, 130, 140))
        assert image.save(str(directory / f"{index:04d}.jpg"), "JPEG")
    return directory


def _wait(predicate, qapp, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_set_images_decodes_nothing(qapp, real_images: Path):
    """**装进网格是免费的** —— 一步都不解码。

    这是本次改动的核心：原来 `set_images()` 会给每个条目建 `QIcon`，
    3504 张要先批量生成 26.5 秒、写 35 MB。现在只装名字。
    """
    widget = ThumbnailGrid()
    widget.set_images(real_images)

    assert widget.all_names() == [f"{i:04d}.jpg" for i in range(7)]
    assert widget.decoded_rows() == 0, "装条目不该解码任何一张"
    assert widget._model.icon_rows() == set()


def test_icon_is_only_requested_when_asked(qapp, real_images: Path):
    """没被问到的格子不登记请求 —— 这是"按需"两个字的落点。"""
    widget = ThumbnailGrid()
    widget.set_images(real_images)

    from PySide6.QtCore import Qt

    icon = widget._model.data(widget._model.index(3, 0), Qt.ItemDataRole.DecorationRole)
    assert icon is not None and icon.isNull(), "没有缓存时先给空图标"
    assert widget._pending == {3}, "只该登记被问到的那一行"


def test_loaded_image_becomes_an_icon(qapp, real_images: Path):
    """结果回到主线程后才转成 QIcon，并通知视图重画。"""
    widget = ThumbnailGrid()
    widget.set_images(real_images)

    from PySide6.QtCore import Qt

    widget._request_icon(2)
    widget._flush_pending()
    assert _wait(lambda: 2 in widget._model.icon_rows(), qapp), "解码结果没回来"

    icon = widget._model.data(widget._model.index(2, 0), Qt.ItemDataRole.DecorationRole)
    assert not icon.isNull()
    assert widget.decoded_rows() == 1


def test_stale_generation_is_dropped(qapp, real_images: Path):
    """**换筛选条件时，飞行中的旧结果必须整批丢弃。**

    不丢的话，上一个条件解出来的图会画到这一批的格子上 ——
    而且看不出错，只是"图不对"。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage

    widget = ThumbnailGrid()
    widget.set_images(real_images)
    stale = widget._generation

    widget.set_images(real_images)          # 世代自增
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))

    widget._on_loaded(stale, {0: image}, {0: None})
    assert widget._model.icon_rows() == set(), "旧世代的结果不该被画上去"


def test_exposure_covers_only_decoded_rows(qapp, real_images: Path):
    """曝光和解码同源，所以只覆盖**已经解出来的**那些。

    「排除过曝/欠曝」必须如实说明覆盖范围 —— 不能假装检查过全部。
    """
    widget = ThumbnailGrid()
    widget.set_images(real_images)
    assert widget.exposure_for(widget.all_names()) == {}

    widget._request_icon(1)
    widget._flush_pending()
    assert _wait(lambda: widget.decoded_rows() == 1, qapp)

    covered = widget.exposure_for(widget.all_names())
    assert set(covered) == {"0001.jpg"}


def test_icon_cache_is_bounded(qapp, real_images: Path, monkeypatch):
    """图标缓存有上限，内存不随条目数线性增长。"""
    import rdkquant.ui.widgets.thumbnailgrid as grid_mod

    monkeypatch.setattr(grid_mod, "ICON_CACHE_MAX", 3)
    widget = ThumbnailGrid()
    widget.set_images(real_images)

    for row in range(7):
        widget._request_icon(row)
    widget._flush_pending()
    assert _wait(lambda: len(widget._model.icon_rows()) >= 3, qapp)
    # 再多解几轮，缓存不该一路涨上去
    for _ in range(3):
        for row in range(7):
            widget._request_icon(row)
        widget._flush_pending()
        qapp.processEvents()
    assert len(widget._model.icon_rows()) <= 3


def test_grid_shows_formats_the_scanner_accepts(qapp, tmp_path: Path):
    """网格认的后缀必须和扫描白名单一致。

    原来这里硬编码 `(".png", ".jpg", ".jpeg", ".bmp")`，比扫描窄 ——
    于是 `.webp` / `.tiff` / `.ppm` **扫得到却在预览里看不到**。
    """
    from PySide6.QtGui import QColor, QImage

    directory = tmp_path / "mixed"
    directory.mkdir()
    for index, suffix in enumerate((".jpg", ".png", ".webp", ".tiff")):
        image = QImage(40, 30, QImage.Format.Format_RGB32)
        image.fill(QColor(90, 90, 90))
        image.save(str(directory / f"pic{index}{suffix}"))

    widget = ThumbnailGrid()
    widget.set_images(directory)

    shown = set(widget.all_names())
    for suffix in (".jpg", ".png", ".webp", ".tiff"):
        assert any(name.endswith(suffix) for name in shown), (
            f"扫描认 {suffix}，网格却没显示 —— 白名单不一致"
        )


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


def test_set_images_does_not_truncate(qapp, tmp_path):
    """**不再有数量上限。**

    原来是 `MAX_TILES = 200`，理由是"批量生成缩略图太贵"。现在装条目免费、
    解码按需，所以上限没有存在理由。而且它当时是个**隐藏的坑**：
    采样张数设成 300 也只能勾到 200 张。

    （本条原为 `test_set_images_respects_the_tile_limit`，行为有意改变。）
    """
    directory = tmp_path / "many"
    directory.mkdir()
    for index in range(1500):
        (directory / f"{index:05d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    widget = ThumbnailGrid()
    count = widget.set_images(directory)
    assert count == 1500
    assert widget.decoded_rows() == 0, "装 1500 个条目不该解码任何一张"


def test_set_images_on_missing_directory_is_empty(grid, tmp_path):
    assert grid.set_images(tmp_path / "不存在") == 0
    assert grid.checked_names() == []


def test_clear(grid):
    grid.clear()
    assert grid.all_names() == []
    assert grid.checked_names() == []
