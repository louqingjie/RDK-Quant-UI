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

⚠️ 这与早先 `AGENTS.md` 里「宿主机不碰像素」那条铁律相反 —— **那条已经改掉了**。
它当时的理由是"宿主机连图像库都没装"，而一个 PySide6 应用**本来就装着完整的
图像栈**：前提不成立，所以规则改成了"图像处理放宿主机，但只能用 Qt，
且只能在 `ui/` 里"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader

from ..core.calibfilter import CalibStat
from ..core.calibpick import SOLID_SPREAD, Candidate

#: 认得的图片后缀。与 `data/rdkquant_probe.py` 的 `IMAGE_EXTS` 保持一致 ——
#: 两边必须认得同一批文件，否则"容器扫得到、宿主机看不到"会变成新的坑。
IMAGE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
    ".ppm", ".pgm", ".pbm", ".gif",
)

#: 认得的二进制校准数据后缀（探针里那份的对应物，必须一字不差）
BINARY_SUFFIXES = (".bin", ".dat", ".raw", ".f32", ".u8")

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


@dataclass(frozen=True)
class Exposure:
    """一张图的曝光情况。0~1 的占比 + 0~255 的平均亮度。"""

    mean: float
    near_black: float
    near_white: float
    #: 灰度**标准差**。判「纯色图片」用它 —— 两端占比判不出来：
    #: 一张整片 128 灰的图 near_black / near_white 都是 0，而它正是官方
    #: 点名的"纯色图片"。默认 0 只为兼容旧构造点，测量路径都会填。
    spread: float = 0.0

    @property
    def is_bad(self) -> bool:
        """该不该建议排除。

        官方要求排除过曝 / 欠曝。判据刻意**宽松**：宁可漏掉边缘样本，
        也不要误伤正常图 —— 一次误排除会悄悄縮小校准集，而用户看不出来。
        """
        return self.near_black > 0.5 or self.near_white > 0.5 or self.mean < 12 or self.mean > 243

    @property
    def is_solid(self) -> bool:
        """是不是近纯色。阈值在 `core/calibpick.py` 里（策略归 core）。"""
        return self.spread < SOLID_SPREAD


#: 亮度判据（0~255）
_NEAR_BLACK_LUMA = 16
_NEAR_WHITE_LUMA = 240


def measure_exposure(image: QImage) -> Exposure:
    """算一张图的曝光。**输入是已经解码好的 QImage** —— 不要再解一次。

    为什么用缩略图而不是原图：像素已经为了预览解码了，顺手遍历一遍几乎免费。
    缺点是只覆盖**预览集**（50~200 张）而不是全量 3504 —— 但那恰好是用户
    正在挑的那批。全量逐张算就得解码 3504 张，把刚拿到的 32 倍又还回去。

    ⚠️ **这个局限正是「自动挑选」必须另走 `measure_candidates()` 的原因** ——
    按亮度分层要求覆盖**全量**，只统计看过的那几十张给不出分层依据。
    """
    small = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = small.width(), small.height()
    if width <= 0 or height <= 0:
        return Exposure(0.0, 0.0, 0.0)

    stride = small.bytesPerLine()
    raw = bytes(small.constBits())
    count = width * height
    total = near_black = near_white = 0
    squares = 0.0
    for y in range(height):
        row = y * stride
        for x in range(width):
            at = row + x * 3
            # ITU-R BT.601 亮度
            luma = (raw[at] * 299 + raw[at + 1] * 587 + raw[at + 2] * 114) // 1000
            total += luma
            squares += luma * luma
            if luma < _NEAR_BLACK_LUMA:
                near_black += 1
            elif luma > _NEAR_WHITE_LUMA:
                near_white += 1
    mean = total / count
    # 总体标准差（除以 n，不是 n-1）：这里是整张图的描述统计，不是抽样估计
    variance = max(0.0, squares / count - mean * mean)
    return Exposure(mean, near_black / count, near_white / count, variance ** 0.5)


def _scaled(image: QImage, size: int) -> QImage:
    """缩到 `size` 见方。**只缩不放**。

    和容器版 PIL 的 `thumbnail()` 语义一致。`QImage.scaled()` 默认会把小图
    **放大**到贴着目标框 —— 实测差得很明显：源图 62×77 在容器版里还是 62×77，
    在 Qt 版里被拉到 77×96。小图放大只会糊，而且同屏格子大小不一。
    """
    if image.width() > size or image.height() > size:
        return image.scaled(
            QSize(size, size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def load_thumbnail(path: Path, size: int = 96) -> tuple[QImage, Exposure] | None:
    """解码一张图 → (缩略图, 曝光)。读不出返回 `None`。

    ⚠️ **只产出 `QImage`，绝不碰 `QPixmap` / `QIcon`** —— 这个函数要在
    **工作线程**里跑，而 `QPixmap` 只能在 GUI 线程创建（Qt 的硬约束）。
    主线程拿到 `QImage` 之后再转成 `QIcon`。

    曝光和缩略图共用同一次解码，所以**几乎免费**。
    """
    image = QImage(str(path))
    if image.isNull():
        return None
    thumb = _scaled(image, size)
    return thumb, measure_exposure(thumb)


# --------------------------------------------------------------------------
# 全量测量 —— 给「按官方口径自动挑选」用
#
# ⚠️ 和上面的按需解码**不是一回事**：预览网格只为用户看过的那些格子解码，
# 所以那里的曝光统计只覆盖几十张。而按亮度分层要求覆盖**全量**，否则分层
# 依据是偏的。（这正是当初判断"曝光统计没用"的真正原因 —— 不是这个统计
# 没用，是它被喂了一份有偏的样本。）
# --------------------------------------------------------------------------

#: 测量用缩略尺寸。**32 而不是 96**：这一步要过全量 3504 张，
#: 每张 96² = 9216 个像素的 Python 循环会到十几秒，32² = 1024 只要几秒。
#: 判"纯色"和"亮度"在 32×32 上完全够 —— 纯色图在哪个尺度上都是纯色。
MEASURE_SIZE = 32


def _luma_and_hash(image: QImage, size: int) -> tuple[list[int], str] | None:
    """缩放 → 灰度 → 返回 (亮度列表, aHash 指纹)。

    aHash 是**真正的 8×8 空间分块哈希**：把图切成 8×8 共 64 格，每格取均值，
    再逐格和总均值比大小。

    ⚠️ 第一版不是这么写的 —— 它把**扁平的亮度列表**切成 64 段取均值，
    那等于 64 条**横带**，横向分布整个丢掉。后果实测过：24 张纹理位置相同、
    但内容不同的图，指纹**完全一样**，被当作重复帧剔掉 14 张。
    而 docstring 当时还写着"和参考实现一致" —— 那是假的。
    """
    small = _scaled(image, size).convertToFormat(QImage.Format.Format_RGB888)
    width, height = small.width(), small.height()
    if width <= 0 or height <= 0:
        return None

    stride = small.bytesPerLine()
    raw = bytes(small.constBits())
    luma: list[int] = []
    for y in range(height):
        row = y * stride
        for x in range(width):
            at = row + x * 3
            luma.append((raw[at] * 299 + raw[at + 1] * 587 + raw[at + 2] * 114) // 1000)

    return luma, _ahash(luma, width, height)


def _ahash(luma: list[int], width: int, height: int, side: int = 8) -> str:
    """8×8 均值哈希 → 16 位十六进制字符串。

    每格用**整数边界**切（`bx * width // side`），所以任意尺寸都不会漏格、
    也不会越界 —— 缩略图是"只缩不放"，小图会比 32×32 还小。
    """
    cells: list[float] = []
    for by in range(side):
        y0 = by * height // side
        y1 = max(y0 + 1, (by + 1) * height // side)
        for bx in range(side):
            x0 = bx * width // side
            x1 = max(x0 + 1, (bx + 1) * width // side)
            total = 0
            count = 0
            for y in range(y0, min(y1, height)):
                row = y * width
                for x in range(x0, min(x1, width)):
                    total += luma[row + x]
                    count += 1
            cells.append(total / count if count else 0.0)

    average = sum(cells) / len(cells)
    bits = "".join("1" if value > average else "0" for value in cells)
    return f"{int(bits, 2):016x}"


def measure_candidates(
    directory: Path,
    names: Sequence[str] | None = None,
    *,
    size: int = MEASURE_SIZE,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelCheck | None = None,
) -> list[Candidate]:
    """**逐张全量解码**，量出挑校准集要用的三个事实：亮度、离散度、指纹。

    这一步是 O(全量) 的解码，3504 张大约几秒 —— 所以只在用户**显式**点
    「自动挑选」时做，不做成隐式开销。

    ⚠️ 只产出纯数据（`Candidate`），**不做任何筛选决策** ——
    策略在 `core/calibpick.py`，那里可以脱离 Qt 单独测。
    """
    if names is None:
        files = image_files(directory)
    else:
        files = [directory / name for name in names]

    total = len(files)
    out: list[Candidate] = []
    for index, path in enumerate(files, start=1):
        if cancel is not None and cancel():
            break
        image = QImage(str(path))
        if not image.isNull():
            measured = _luma_and_hash(image, size)
            if measured is not None:
                luma, fingerprint = measured
                count = len(luma)
                mean = sum(luma) / count
                variance = max(0.0, sum(v * v for v in luma) / count - mean * mean)
                out.append(Candidate(
                    name=path.name,
                    brightness=mean,
                    spread=variance ** 0.5,
                    fingerprint=fingerprint,
                    width=image.width(),
                    height=image.height(),
                ))
        if on_progress is not None:
            on_progress(index, total)
    return out


@dataclass
class ThumbnailResult:
    generated: int = 0
    failed: int = 0
    files: list[str] = field(default_factory=list)
    #: 读不出来的文件（格式不支持或文件损坏）
    unreadable: list[str] = field(default_factory=list)
    #: 文件名 → 曝光。**只覆盖本次生成的那些**（复用已解码的像素，不额外解码）
    exposure: dict[str, Exposure] = field(default_factory=dict)


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
        # 见 `_scaled()` 里记的那次实测差异。
        thumb = _scaled(image, size)
        # 顺手算曝光：像素已经解码了，这一步几乎免费
        result.exposure[path.name] = measure_exposure(thumb)
        target = out_dir / f"{path.stem}.png"
        if thumb.save(str(target), "PNG"):
            result.generated += 1
            result.files.append(target.name)
        else:
            result.failed += 1
        if on_progress is not None:
            on_progress(index, total)
    return result


#: 内容指纹只读前 64 KB —— 和 `probe.py` 里那套**必须一致**，
#: 否则同一个目录在两边算出的重复组会不一样。
DIGEST_HEAD_BYTES = 65536


def _mode_of(image: QImage) -> str:
    """把 Qt 的像素格式映射成 PIL 那套名称（`RGB` / `L` / `RGBA`）。

    界面上的「色彩模式」筛选按这些名字匹配，所以**必须和容器侧对得上**。
    """
    fmt = image.format()
    if fmt in (
        QImage.Format.Format_Grayscale8,
        QImage.Format.Format_Grayscale16,
        QImage.Format.Format_Alpha8,
        QImage.Format.Format_Mono,
        QImage.Format.Format_MonoLSB,
        QImage.Format.Format_Indexed8,
    ):
        # Qt 把灰度 JPEG 读成 Grayscale8、调色板 PNG 读成 Indexed8，
        # 而 PIL 分别叫 `L` 和 `P` —— 分开报，否则筛选会张冠李戴。
        return "P" if fmt == QImage.Format.Format_Indexed8 else "L"
    if image.hasAlphaChannel():
        return "RGBA"
    return "RGB"


def _digest(path: Path, size: int) -> str | None:
    """内容指纹：大小 + 前 64 KB 的 sha1。与探针里的算法**逐字一致**。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(DIGEST_HEAD_BYTES)
    except OSError:
        return None
    return f"{hashlib.sha1(head).hexdigest()}:{size}"


@dataclass
class ScanResult:
    """扫一遍校准目录的结果。字段名与探针回包对齐，方便对照。"""

    stats: list[CalibStat] = field(default_factory=list)
    #: 内容重复的配对 `[[保留的, 重复的], ...]`，形状与探针一致
    duplicates: list[list[str]] = field(default_factory=list)
    total: int = 0
    image_like: int = 0
    binary_like: int = 0
    unreadable: list[str] = field(default_factory=list)


def scan(
    directory: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: CancelCheck | None = None,
) -> ScanResult:
    """逐张统计 + 找重复。**全部在宿主机上做，且不解码像素。**

    宽高来自 `read_size`（只读文件头），模式来自 `read_mode`（只读文件头）。
    实测 3504 张：全解码 15.5 秒 → 文件头解析 **0.6 秒**（32x），
    而且模式、宽高与容器侧 PIL **逐张零不一致**。

    认不出的格式（bmp/tiff/webp/ppm…）**退回解码**，只为拿模式 ——
    那些在真实数据集里是少数（实测那份 3504 张全是 JPEG）。
    """
    entries = [
        (p, p.stat().st_size)
        for p in sorted(directory.iterdir())
        if p.is_file()
    ] if directory.is_dir() else []
    images = [(p, size) for p, size in entries if p.suffix.lower() in IMAGE_SUFFIXES]
    binaries = [
        (p, size) for p, size in entries
        if p.suffix.lower() in BINARY_SUFFIXES or "." not in p.name
    ]

    result = ScanResult(
        total=len(images) + len(binaries),
        image_like=len(images),
        binary_like=len(binaries),
    )

    digests: dict[str, str] = {}
    total = len(images)
    for index, (path, size) in enumerate(images, start=1):
        if cancel is not None and cancel():
            break

        measured = read_size(path)
        mode = read_mode(path)
        if measured is None:
            result.unreadable.append(path.name)
            width = height = None
        else:
            width, height = measured
            if mode is None:
                # 认不出的格式才解码，而且**只为拿模式**
                image = QImage(str(path))
                mode = _mode_of(image) if not image.isNull() else None

        result.stats.append(CalibStat(
            name=path.name, size=size,
            width=width, height=height, mode=mode,
        ))

        digest = _digest(path, size)
        if digest:
            if digest in digests:
                result.duplicates.append([digests[digest], path.name])
            else:
                digests[digest] = path.name

        if on_progress is not None:
            on_progress(index, total)

    return result


#: JPEG 里带 SOF（帧头）的标记。这些段里有分量数 = 色彩模式。
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
#: 没有长度字段、要单独跳过的标记
_JPEG_STANDALONE = frozenset({0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)})


def _jpeg_mode(data: bytes) -> str | None:
    """从 JPEG 的 SOF 段读分量数。

    1 → 灰度（PIL 叫 `L`）；3 → YCbCr（PIL 报 `RGB`）；4 → `CMYK`。
    """
    index = 2                                     # 跳过 SOI (FFD8)
    size = len(data)
    while index + 1 < size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _JPEG_STANDALONE:
            index += 2
            continue
        if index + 4 > size:
            return None
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            # 段内布局：长度(2) 精度(1) 高(2) 宽(2) 分量数(1)
            components_at = index + 4 + 5
            if components_at >= size:
                return None
            components = data[components_at]
            return {1: "L", 3: "RGB", 4: "CMYK"}.get(components)
        index += 2 + length
    return None


def _png_mode(data: bytes) -> str | None:
    """从 PNG 的 IHDR 读 color type。

    0→`L`  2→`RGB`  3→`P`  4→`LA`  6→`RGBA`（与 PIL 的叫法对齐）
    """
    # 8 字节签名 + 4 长度 + 4 "IHDR" + 4 宽 + 4 高 + 1 位深 + 1 color type
    color_type_at = 8 + 4 + 4 + 4 + 4 + 1
    if len(data) <= color_type_at:
        return None
    return {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}.get(data[color_type_at])


def read_mode(path: Path) -> str | None:
    """**只读文件头**判断色彩模式（用 PIL 那套名称，要和容器侧对得上）。

    为什么不直接解码：3504 张全解码约 14 秒，而模式就藏在文件头前几十个字节里。
    实测用户那份数据集 3504 张**全是 JPEG**，所以 JPEG 一条路径就覆盖了全部。

    认不出的格式返回 `None` —— 调用方可以退回解码，**不要在这里悄悄解码**：
    那会让"快路径"在某张图上突然变慢，而且看不出来。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if head[:2] == b"\xff\xd8":
        return _jpeg_mode(head)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_mode(head)
    return None


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
