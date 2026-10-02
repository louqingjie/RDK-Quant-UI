# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准集的**筛选**逻辑。

为什么需要它：官方要求校准集是 20~100 张、有代表性的图片，并明确要排除过曝、
欠曝、模糊、纯黑、纯白的场景。而真实数据集动辄几千张、分辨率五花八门——实测
用户的一个装甲板数据集有 **3504 张、1474 种分辨率、29 组重复**。逐个勾选不现实，
必须先按条件筛。

**这个模块是纯标准库、零 Qt**，因为：

* 筛选是纯数据变换，能脱离界面和 Docker 直接测——这是它最值得测的部分
* 计数要**即时**响应（用户拖一下滑块就想看到"3504 → 412"），不能每次去问容器

数据来源是容器侧 `calib-scan --stats-file` 写出的逐张统计（见 data/rdkquant_probe.py）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CalibStat:
    """一张图的统计。#: 判定用的原始事实，不含任何结论。"""

    name: str
    size: int
    width: int | None = None
    height: int | None = None
    mode: str | None = None

    @property
    def readable(self) -> bool:
        """宽高是否读出来了。读不出来就无法参与尺寸/比例筛选。"""
        return bool(self.width) and bool(self.height)

    @property
    def aspect(self) -> float | None:
        """长宽比 = 宽 / 高。height 为 0/None 时无定义。"""
        if not self.readable:
            return None
        return self.width / self.height


@dataclass(frozen=True)
class FilterSpec:
    """筛选条件。各维度之间是 **AND**；每个维度留空表示不筛。

    `None` 与 `0` 含义不同：`min_bytes=None` 是"不限"，`min_bytes=0` 也是不限
    （图片不可能是 0 字节），但 `max_bytes=0` 会筛掉一切——这是用户自己输的数，
    不替他纠正。
    """

    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    min_aspect: float | None = None
    max_aspect: float | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    modes: tuple[str, ...] = ()
    exclude_duplicates: bool = False

    @property
    def constrains_size(self) -> bool:
        """是否启用了任何依赖宽高/比例的条件。

        读不出尺寸的图在这个条件下会被剔除——它无法被校验。反过来，如果用户
        根本不在乎尺寸，就没理由拦它。
        """
        return any(
            v is not None
            for v in (self.min_width, self.max_width, self.min_height,
                      self.max_height, self.min_aspect, self.max_aspect)
        )

    @property
    def active(self) -> bool:
        return self.constrains_size or self.exclude_duplicates or bool(self.modes) or any(
            v is not None for v in (self.min_bytes, self.max_bytes)
        )

    def normalized(self) -> "FilterSpec":
        """把写反的 min/max 换过来。

        用户拖数值时很自然会先填大的再填小的，为此弹个错很讨厌。静默交换，
        并把这件事记在 `swapped` 里，让界面能提一句。
        """
        spec = self
        pairs = (
            ("min_width", "max_width"),
            ("min_height", "max_height"),
            ("min_aspect", "max_aspect"),
            ("min_bytes", "max_bytes"),
        )
        for low_name, high_name in pairs:
            low = getattr(spec, low_name)
            high = getattr(spec, high_name)
            if low is not None and high is not None and low > high:
                spec = replace(spec, **{low_name: high, high_name: low})
        return spec


@dataclass(frozen=True)
class FilterResult:
    kept: tuple[CalibStat, ...]
    #: 原因 → 被筛掉的数量。**必须能解释"为什么少了这么多"**，
    #: 否则用户会以为程序坏了。
    dropped: dict[str, int]

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def explain(self) -> str:
        """一句话说清筛掉了什么。"""
        if not self.dropped_total:
            return ""
        labels = {
            "unreadable": "尺寸读不出",
            "size": "宽高不符",
            "aspect": "长宽比不符",
            "bytes": "文件大小不符",
            "mode": "色彩模式不符",
            "duplicate": "内容重复",
        }
        parts = [
            f"{labels.get(reason, reason)} {count}"
            for reason, count in sorted(self.dropped.items(), key=lambda kv: -kv[1])
        ]
        return "、".join(parts)


# --------------------------------------------------------------------------
# 统计文件的读取
# --------------------------------------------------------------------------
def sample_evenly(files: Sequence[str], count: int) -> list[str]:
    """从有序列表里**均匀**取 `count` 个。

    ⚠️ **这个函数与容器侧探针里的同名函数必须完全一致**（`data/rdkquant_probe.py`）。
    宿主现在负责算出"筛选 + 采样"后的名单再交给容器，所以两份实现都存在：
    探针那份在没给 `--only-file` 时仍然用得上（命令行直接调用）。
    `tests/test_sampling.py` 里有一条交叉校验测试，逐个比对两者的输出——
    把"重复实现"变成"被守住的不变量"。

    为什么均匀而不是随机：确定、可复现，而且数据集通常按时间/场景排序，
    间隔取样天然覆盖各片段。
    """
    count = int(count or 0)
    if count <= 0 or count >= len(files):
        return list(files)
    step = len(files) / float(count)
    picked: list[str] = []
    seen: set[int] = set()
    for i in range(count):
        index = int(i * step)
        if index not in seen:              # 极短列表下可能重复，去一下
            seen.add(index)
            picked.append(files[index])
    return picked


def filter_and_sample(
    stats: Sequence[CalibStat],
    spec: FilterSpec,
    *,
    count: int = 0,
    duplicates: Iterable[Sequence[str]] = (),
) -> tuple[FilterResult, list[str]]:
    """筛选 → 采样，返回 (筛选结果, 最终要用的文件名)。

    两步合在一起是因为它们是同一条流水线上的相邻环节，分开调用容易让调用方
    在中间插入不一致的逻辑。
    """
    result = apply_filter(stats, spec, duplicates=duplicates)
    names = sample_evenly([s.name for s in result.kept], count)
    return result, names


def load_stats(path: Path | str) -> list[CalibStat]:
    """读容器写出来的逐张统计。

    文件格式（探针写，键名刻意缩短以控制体积）::

        {"version": 1, "count": 3504,
         "files": [{"n": "0000.jpg", "w": 197, "h": 120, "s": 3598, "m": "RGB"}, ...]}

    读不出来就抛——**不静默返回空列表**。空列表意味着"筛完一张都不剩"，
    那会误导用户去改筛选条件，而真正的问题是文件没写成。
    """
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    files = payload.get("files") or []
    stats: list[CalibStat] = []
    for item in files:
        name = str(item.get("n") or "")
        if not name:
            continue
        width = item.get("w")
        height = item.get("h")
        stats.append(CalibStat(
            name=name,
            size=int(item.get("s") or 0),
            width=int(width) if width else None,
            height=int(height) if height else None,
            mode=(item.get("m") or None),
        ))
    return stats


# --------------------------------------------------------------------------
# 筛选
# --------------------------------------------------------------------------
def _in_range(value, low, high) -> bool:
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def duplicate_names(duplicates: Iterable[Sequence[str]]) -> set[str]:
    """从 `calib-scan` 的 duplicates 里取出"该丢掉的那些"。

    回包里的形状是 `[[保留的, 重复的], ...]`——按扫描顺序，先出现的保留。
    所以要丢的是每一组的**第二个**。
    """
    names: set[str] = set()
    for pair in duplicates or ():
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            names.add(str(pair[1]))
    return names


def apply_filter(
    stats: Sequence[CalibStat],
    spec: FilterSpec,
    *,
    duplicates: Iterable[Sequence[str]] = (),
) -> FilterResult:
    """按条件筛。**纯函数**，顺序与输入一致。"""
    spec = spec.normalized()
    dupes = duplicate_names(duplicates) if spec.exclude_duplicates else set()
    wanted_modes = {m.upper() for m in spec.modes}

    kept: list[CalibStat] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for stat in stats:
        # 尺寸/比例类条件需要宽高；读不出来的图无法校验，启用这类条件时剔除。
        if spec.constrains_size and not stat.readable:
            drop("unreadable")
            continue

        if stat.readable:
            if not _in_range(stat.width, spec.min_width, spec.max_width) or \
                    not _in_range(stat.height, spec.min_height, spec.max_height):
                drop("size")
                continue

        aspect = stat.aspect
        if spec.min_aspect is not None or spec.max_aspect is not None:
            if aspect is None:
                drop("unreadable")
                continue
            if not _in_range(aspect, spec.min_aspect, spec.max_aspect):
                drop("aspect")
                continue

        if not _in_range(stat.size, spec.min_bytes, spec.max_bytes):
            drop("bytes")
            continue

        if wanted_modes and (stat.mode or "").upper() not in wanted_modes:
            drop("mode")
            continue

        if stat.name in dupes:
            drop("duplicate")
            continue

        kept.append(stat)

    return FilterResult(kept=tuple(kept), dropped=dropped)


def most_common_resolution(stats: Sequence[CalibStat]) -> tuple[int, int] | None:
    """出现最多的那种分辨率。

    对"1474 种分辨率"的数据集是最省事的一键收窄：直接锁定主流那一档，
    比让用户自己试数值快得多。并列时取面积大的（信息量更多）。
    """
    counts: dict[tuple[int, int], int] = {}
    for stat in stats:
        if stat.readable:
            key = (stat.width, stat.height)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1]))[0]


def resolution_bounds(stats: Sequence[CalibStat]) -> tuple[int, int, int, int] | None:
    """(最小宽, 最大宽, 最小高, 最大高)。用来给输入框一个合理的默认范围提示。"""
    readable = [s for s in stats if s.readable]
    if not readable:
        return None
    return (
        min(s.width for s in readable),
        max(s.width for s in readable),
        min(s.height for s in readable),
        max(s.height for s in readable),
    )


def aspect_bounds(stats: Sequence[CalibStat]) -> tuple[float, float] | None:
    aspects = [s.aspect for s in stats if s.aspect]
    if not aspects:
        return None
    return min(aspects), max(aspects)
