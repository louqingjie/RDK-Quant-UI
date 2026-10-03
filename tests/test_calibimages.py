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
# 文件头解析（色彩模式）
#
# 为什么要它：全解码 3504 张要 ~15 秒，而模式就藏在文件头前几十个字节里。
# 实测（用户那份 3504 张的数据集）：
#     容器版（PIL）18.0 秒  →  文件头解析 0.6 秒   = 32x
#     模式、宽高**零不一致**
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fmt,expected", [
    (QImage.Format.Format_RGB32, "RGB"),
    (QImage.Format.Format_Grayscale8, "L"),
    (QImage.Format.Format_ARGB32, "RGBA"),
])
def test_read_mode_matches_pil_names_for_png(qapp, tmp_path: Path, fmt, expected):
    path = tmp_path / "x.png"
    image = QImage(20, 20, fmt)
    image.fill(QColor("white"))
    assert image.save(str(path), "PNG")
    assert calibimages.read_mode(path) == expected


def test_png_mode_reads_color_type(qapp):
    """PNG 的 color type 直接决定模式。

    走 synth 而不是让 Qt 编码：**Qt 会自己挑怎么写** —— 实测把
    `Format_Indexed8` 存成了灰度 PNG（color type 0），于是拿它测"索引色 → P"
    会假失败（我第一版就是这么写错的）。
    """
    def png_with(color_type: int) -> bytes:
        ihdr = (
            b"\x00\x00\x00\x0d" + b"IHDR"
            + b"\x00\x00\x00\x10" + b"\x00\x00\x00\x10"   # 16×16
            + bytes([8, color_type, 0, 0, 0])
        )
        return b"\x89PNG\r\n\x1a\n" + ihdr

    assert calibimages._png_mode(png_with(0)) == "L"
    assert calibimages._png_mode(png_with(2)) == "RGB"
    assert calibimages._png_mode(png_with(3)) == "P"
    assert calibimages._png_mode(png_with(4)) == "LA"
    assert calibimages._png_mode(png_with(6)) == "RGBA"
    assert calibimages._png_mode(png_with(7)) is None, "未知 color type 不该瞎猜"


def test_qt_indexed_png_is_actually_gray(qapp, tmp_path: Path):
    """把上面那个坑本身钉住：Qt 把 Indexed8 存成灰度 PNG。

    写这条是因为**我当时就是被它绊了一下** —— 以为在测"索引色 → P"，
    实际上 Qt 根本没写索引色。以后有人再想当然，这条会解释原因。
    """
    path = tmp_path / "idx.png"
    image = QImage(20, 20, QImage.Format.Format_Indexed8)
    image.fill(QColor("white"))
    assert image.save(str(path), "PNG")

    data = path.read_bytes()
    color_type_at = 8 + 4 + 4 + 4 + 4 + 1
    assert data[color_type_at] == 0, "Qt 把它写成了灰度（color type 0），不是索引色"
    assert calibimages.read_mode(path) == "L"


def test_read_mode_for_jpeg_is_rgb(qapp, tmp_path: Path):
    """三通道 JPEG → PIL 报 `RGB`（底层是 YCbCr，但 PIL 的命名是 RGB）。"""
    path = tmp_path / "x.jpg"
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor(200, 120, 60))
    assert image.save(str(path), "JPEG")
    assert calibimages.read_mode(path) == "RGB"


def test_read_mode_returns_none_for_unknown_format(qapp, tmp_path: Path):
    """认不出的格式返回 None，**不要在这里悄悄解码** ——
    那会让快路径在某张图上突然变慢，而且看不出来。调用方自己决定退不退。"""
    path = tmp_path / "x.bmp"
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    image.save(str(path), "BMP")
    assert calibimages.read_mode(path) is None


def test_read_mode_on_garbage(qapp, tmp_path: Path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8not really a jpeg")
    assert calibimages.read_mode(bad) is None
    missing = tmp_path / "不存在.jpg"
    assert calibimages.read_mode(missing) is None


def test_jpeg_mode_reads_component_count(qapp, tmp_path: Path):
    """分量数直接决定模式：1→`L`，3→`RGB`，4→`CMYK`。

    这里构造一段最小的 SOF0 段来验证映射表本身，
    免得只靠 Qt 写出来的文件覆盖不到灰度/CMYK。
    """
    def jpeg_with(components: int) -> bytes:
        sof = bytes([0xFF, 0xC0, 0x00, 0x11, 0x08,
                     0x00, 0x10, 0x00, 0x10, components])
        return b"\xff\xd8" + sof + b"\x00" * 32

    assert calibimages._jpeg_mode(jpeg_with(1)) == "L"
    assert calibimages._jpeg_mode(jpeg_with(3)) == "RGB"
    assert calibimages._jpeg_mode(jpeg_with(4)) == "CMYK"
    assert calibimages._jpeg_mode(jpeg_with(2)) is None, "2 分量不是常见情况，不该瞎猜"


def test_read_size_does_not_decode_pixels(qapp, tmp_path: Path):
    """`read_size` 只读文件头 —— 这是它 3504 张只要 0.79 秒的原因。

    用一个**只有文件头、像素数据被截断**的 PNG 验证：尺寸照样读得出来。
    如果哪天有人把它改成 `QImage(path)`，这条会红。
    """
    path = tmp_path / "truncated.png"
    image = QImage(640, 480, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    image.save(str(path), "PNG")
    data = path.read_bytes()
    path.write_bytes(data[:64])          # 只留签名 + IHDR

    assert calibimages.read_size(path) == (640, 480), "读尺寸不该依赖像素数据"


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
