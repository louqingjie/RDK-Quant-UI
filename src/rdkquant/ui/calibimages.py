# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""宿主机侧的图像读取 —— 用 Qt，**不引入任何新依赖**。

## 为什么把它从容器搬到这里

实测（用户那份 3504 张 JPEG 的数据集，Windows + Docker Desktop）：

    操作                容器里      宿主机 Qt      倍数
    读 3504 张文件头     ~24 秒      0.79 秒        ~30x
    50 张缩略图          4.2 秒      0.22 秒        19x
    200 张缩略图         5.8 秒      1.07 秒        5.4x
    1172 张缩略图       17.4 秒      7.02 秒        2.5x

读文件头和缩略图**不是量化工作** —— 只是界面要用的东西。放进容器意味着
每点一次「应用筛选并刷新预览」都要付一次 `docker run` 往返，而其中约
**3.3 秒的固定开销至今没定位**（不是启容器 0.4 秒、也不是列目录 0 秒），
却每次都在付。

## 为什么不需要 Pillow

`ui` extra 已经依赖 PySide6，而 PySide6 **自带完整的图像栈**：

    QImageReader(path).size()   → 只读文件头给宽高（不解码像素）
    QImage(path).scaled(...)    → 缩略图

实测 Qt 支持的读取格式：

    bmp gif jpeg jpg pbm pgm png ppm tga tif tiff webp xbm xpm …

`probe.py` 的 `IMAGE_EXTS` 白名单**一个都不少**。

## 为什么放在 ui/ 而不是 core/

`core/` 必须保持**零 Qt** —— 它要能在 OE 容器（Ubuntu 20.04 / Python 3.8）
里装而不拉进 Qt。图像能力只在界面里用。

⚠️ 这与 `AGENTS.md` 里「宿主机不碰像素」那条铁律相反。那条的理由是
"宿主机没有图像库"，而一个 PySide6 应用**本来就装着完整的图像栈** ——
前提不成立，所以规则改了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader

#: 认得的图片后缀。与 `data/rdkquant_probe.py` 的 `IMAGE_EXTS` 保持一致 ——
#: 两边必须认得同一批文件，否则"容器扫得到、宿主机看不到"会变成新的坑。
IMAGE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
    ".ppm", ".pgm", ".pbm", ".gif",
)

CancelCheck = Callable[[], bool]


def qt_readable_formats() -> set[str]:
    """Qt 当前能读的格式（小写，不带点）。用来在缺插件时给用户明确原因。"""
    return {bytes(f).decode("ascii").lower() for f in QImageReader.supportedImageFormats()}


def image_files(directory: Path) -> list[Path]:
    """目录下的图片，按名字排序。只取本层，不递归。"""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def read_size(path: Path) -> tuple[int, int] | None:
    """宽高。**只读文件头**，不解码像素。

    读不出来就返回 `None` —— 调用方据此报"尺寸读不出"，**不要替它猜 0**。
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        return size.width(), size.height()
    return None


@dataclass
class ThumbnailResult:
    generated: int = 0
    failed: int = 0
    files: list[str] = field(default_factory=list)
    #: 读不出来的文件（格式不支持或文件损坏）
    unreadable: list[str] = field(default_factory=list)


def build_thumbnails(
    directory: Path,
    out_dir: Path,
    names: Sequence[str] | None = None,
    *,
    size: int = 96,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelCheck | None = None,
) -> ThumbnailResult:
    """把 `directory` 里的图片缩成 PNG 写进 `out_dir`。

    `names` 为 `None` 时处理目录里全部图片。返回实际生成了哪些 ——
    和容器版一样，**失败不抛异常**，只如实计数（一张坏图不该毁掉整次预览）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(names) if names is not None else None
    targets = [p for p in image_files(directory) if wanted is None or p.name in wanted]

    result = ThumbnailResult()
    total = len(targets)
    for index, path in enumerate(targets, start=1):
        if cancel is not None and cancel():
            break
        image = QImage(str(path))
        if image.isNull():
            result.failed += 1
            result.unreadable.append(path.name)
            continue
        # ⚠️ **只缩不放**：和容器版 PIL 的 `thumbnail()` 语义一致。
        #
        # `QImage.scaled()` 会把小图**放大**到贴着 96 —— 实测差得很明显：
        # 源图 62×77 在容器版里还是 62×77，在 Qt 版里被拉到 77×96。
        # 小图放大只会糊，而且同一屏里格子大小不一，看着更乱。
        if image.width() > size or image.height() > size:
            thumb = image.scaled(
                QSize(size, size),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            thumb = image
        target = out_dir / f"{path.stem}.png"
        if thumb.save(str(target), "PNG"):
            result.generated += 1
            result.files.append(target.name)
        else:
            result.failed += 1
        if on_progress is not None:
            on_progress(index, total)
    return result


def clear_thumbnails(out_dir: Path, keep: Iterable[str] = ()) -> int:
    """删掉 `out_dir` 里不再需要的缩略图，返回删了几个。

    缩略图目录是**累积**的：换了筛选条件之后，上一批的 PNG 还留在那里。
    预览只显示当前这批（按名字过滤），所以不删也不显示错图，
    但会让目录越来越乱、下次列目录变慢。
    """
    keep_set = set(keep)
    removed = 0
    if not out_dir.is_dir():
        return 0
    for path in out_dir.glob("*.png"):
        if path.name not in keep_set:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
