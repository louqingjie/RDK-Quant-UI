# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""宿主机侧的图像读取（`ui/calibimages.py`）。

实测背景（3504 张 JPEG 的数据集）：

    操作              容器里      宿主机 Qt
    读 3504 张文件头   ~24 秒      0.79 秒
    120 张缩略图       4.96 秒     0.71 秒     ← 7x

所以缩略图和扫描统计搬到宿主机做，**不新增依赖**（PySide6 自带图像栈）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rdkquant.ui import calibimages  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def make_png(path: Path, width: int, height: int, color="#336699") -> Path:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")
    return path


# --------------------------------------------------------------------------
# 格式覆盖
# --------------------------------------------------------------------------
def test_qt_reads_every_suffix_we_accept(qapp):
    """我们认的后缀，Qt 必须都读得了。

    对不上的话，会出现"扫描说这是图片、预览却读不出"这种自相矛盾的状态 ——
    而这正是当初把图像处理放进容器的理由（宿主机没有图像库）。
    """
    readable = calibimages.qt_readable_formats()
    missing = [s.lstrip(".") for s in calibimages.IMAGE_SUFFIXES if s.lstrip(".") not in readable]
    assert not missing, f"Qt 读不了这些后缀：{missing}"


def test_probe_and_host_agree_on_suffixes():
    """宿主机的白名单必须和探针里那份一致。

    两边认得不同的文件集，会让"容器扫得到、宿主机看不到"变成新的坑。
    """
    import importlib.util

    import rdkquant

    probe = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"
    spec = importlib.util.spec_from_file_location("probe_for_suffixes", probe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    probe_exts = {e.lower() for e in module.IMAGE_EXTS}
    host_exts = {e.lower() for e in calibimages.IMAGE_SUFFIXES}
    assert probe_exts == host_exts, (
        f"探针多出 {probe_exts - host_exts}，宿主机多出 {host_exts - probe_exts}"
    )


# --------------------------------------------------------------------------
# 读尺寸
# --------------------------------------------------------------------------
def test_read_size(qapp, tmp_path: Path):
    make_png(tmp_path / "a.png", 640, 480)
    assert calibimages.read_size(tmp_path / "a.png") == (640, 480)


def test_read_size_returns_none_for_garbage(qapp, tmp_path: Path):
    """读不出来要返回 None，**不能猜 0** —— 0 会被当成"尺寸不符"误报。"""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"this is not an image")
    assert calibimages.read_size(bad) is None


def test_image_files_only_takes_known_suffixes(qapp, tmp_path: Path):
    make_png(tmp_path / "a.png", 10, 10)
    make_png(tmp_path / "b.png", 10, 10)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    names = [p.name for p in calibimages.image_files(tmp_path)]
    assert names == ["a.png", "b.png"]


# --------------------------------------------------------------------------
# 缩略图
# --------------------------------------------------------------------------
def test_build_thumbnails_makes_one_png_per_source(qapp, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a", "b", "c"):
        make_png(src / f"{name}.png", 400, 300)

    out = tmp_path / "out"
    result = calibimages.build_thumbnails(src, out, size=96)

    assert result.generated == 3
    assert result.failed == 0
    assert {p.name for p in out.glob("*.png")} == {"a.png", "b.png", "c.png"}


def test_thumbnails_are_not_upscaled(qapp, tmp_path: Path):
    """**只缩不放** —— 和容器版 PIL 的 `thumbnail()` 语义一致。

    Qt 的 `scaled()` 默认会把小图放大到贴着目标框。实测差得很明显：
    源图 62×77 在容器版里还是 62×77，在 Qt 版里被拉到 77×96。
    小图放大只会糊，而且同一屏里格子大小不一。
    """
    src = tmp_path / "src"
    src.mkdir()
    make_png(src / "small.png", 62, 77)

    out = tmp_path / "out"
    calibimages.build_thumbnails(src, out, size=96)

    assert calibimages.read_size(out / "small.png") == (62, 77)


def test_thumbnails_shrink_large_images_keeping_aspect(qapp, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    make_png(src / "wide.png", 720, 432)

    out = tmp_path / "out"
    calibimages.build_thumbnails(src, out, size=96)

    width, height = calibimages.read_size(out / "wide.png")
    assert width == 96
    # 720:432 → 96:57.6，取整差 1 像素是正常的（实测两个库就差这一点）
    assert height in (57, 58)


def test_build_thumbnails_only_listed_names(qapp, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a", "b", "c"):
        make_png(src / f"{name}.png", 100, 100)

    out = tmp_path / "out"
    result = calibimages.build_thumbnails(src, out, names=["b.png"], size=96)

    assert result.generated == 1
    assert {p.name for p in out.glob("*.png")} == {"b.png"}


def test_bad_file_is_counted_not_raised(qapp, tmp_path: Path):
    """一张坏图不该毁掉整次预览 —— 如实计数并列出名字。"""
    src = tmp_path / "src"
    src.mkdir()
    make_png(src / "good.png", 100, 100)
    (src / "broken.png").write_bytes(b"not an image")

    out = tmp_path / "out"
    result = calibimages.build_thumbnails(src, out, size=96)

    assert result.generated == 1
    assert result.failed == 1
    assert result.unreadable == ["broken.png"]


def test_cancel_stops_early(qapp, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        make_png(src / f"{i}.png", 100, 100)

    out = tmp_path / "out"
    result = calibimages.build_thumbnails(
        src, out, size=96, cancel=lambda: True,   # 立刻取消
    )
    assert result.generated == 0


# --------------------------------------------------------------------------
# 清理
# --------------------------------------------------------------------------
def test_clear_thumbnails_keeps_the_listed_ones(qapp, tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    for name in ("keep.png", "drop1.png", "drop2.png"):
        make_png(out / name, 10, 10)

    removed = calibimages.clear_thumbnails(out, keep={"keep.png"})
    assert removed == 2
    assert {p.name for p in out.glob("*.png")} == {"keep.png"}


def test_clear_thumbnails_on_missing_dir(qapp, tmp_path: Path):
    assert calibimages.clear_thumbnails(tmp_path / "不存在") == 0
