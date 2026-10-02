# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准集筛选逻辑。

背景：官方要求校准集 20~100 张、有代表性，并明确要排除过曝/欠曝/模糊/纯黑/纯白。
而真实数据集动辄几千张、分辨率五花八门——实测用户的一个装甲板数据集有
**3504 张、1474 种分辨率、29 组重复**。逐个勾选不现实，必须先按条件筛。

这个模块是纯标准库、零 Qt，所以能脱离界面和 Docker 直接测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdkquant.core.calibfilter import (
    CalibStat,
    FilterSpec,
    apply_filter,
    aspect_bounds,
    duplicate_names,
    filter_and_sample,
    load_stats,
    most_common_resolution,
    resolution_bounds,
    sample_evenly,
)


def stat(name: str, w=640, h=640, size=50_000, mode="RGB") -> CalibStat:
    return CalibStat(name=name, size=size, width=w, height=h, mode=mode)


@pytest.fixture()
def pool() -> list[CalibStat]:
    return [
        stat("a.jpg", 640, 640, 50_000, "RGB"),
        stat("b.jpg", 1920, 1080, 200_000, "RGB"),
        stat("c.jpg", 1280, 720, 120_000, "RGB"),
        stat("d.jpg", 320, 240, 20_000, "L"),
        stat("e.jpg", 640, 480, 45_000, "RGB"),
    ]


# --------------------------------------------------------------------------
# 单个维度
# --------------------------------------------------------------------------
def test_no_filter_keeps_everything(pool):
    result = apply_filter(pool, FilterSpec())
    assert len(result.kept) == len(pool)
    assert result.dropped_total == 0
    assert result.explain() == ""


def test_width_range_is_inclusive(pool):
    result = apply_filter(pool, FilterSpec(min_width=640, max_width=1280))
    assert [s.name for s in result.kept] == ["a.jpg", "c.jpg", "e.jpg"]
    assert result.dropped["size"] == 2


def test_height_range(pool):
    result = apply_filter(pool, FilterSpec(min_height=720))
    assert [s.name for s in result.kept] == ["b.jpg", "c.jpg"]


def test_aspect_range(pool):
    """长宽比 = 宽/高。1920x1080≈1.78，1280x720≈1.78，640x640=1.0。"""
    result = apply_filter(pool, FilterSpec(min_aspect=1.5))
    assert [s.name for s in result.kept] == ["b.jpg", "c.jpg"]


def test_byte_range(pool):
    result = apply_filter(pool, FilterSpec(min_bytes=45_000, max_bytes=120_000))
    assert [s.name for s in result.kept] == ["a.jpg", "c.jpg", "e.jpg"]


def test_mode_filter(pool):
    result = apply_filter(pool, FilterSpec(modes=("L",)))
    assert [s.name for s in result.kept] == ["d.jpg"]


def test_mode_filter_is_case_insensitive(pool):
    assert len(apply_filter(pool, FilterSpec(modes=("rgb",))).kept) == 4


# --------------------------------------------------------------------------
# 多维度是 AND
# --------------------------------------------------------------------------
def test_dimensions_combine_with_and(pool):
    result = apply_filter(pool, FilterSpec(min_width=640, modes=("RGB",)))
    assert [s.name for s in result.kept] == ["a.jpg", "b.jpg", "c.jpg", "e.jpg"]


def test_empty_result_is_reported_not_hidden(pool):
    result = apply_filter(pool, FilterSpec(min_width=99999))
    assert result.kept == ()
    assert result.dropped["size"] == len(pool)


# --------------------------------------------------------------------------
# min/max 写反
# --------------------------------------------------------------------------
def test_swapped_bounds_are_normalized(pool):
    """用户拖数值时很自然会先填大的。静默交换比弹错好。"""
    result = apply_filter(pool, FilterSpec(min_width=1280, max_width=640))
    assert [s.name for s in result.kept] == ["a.jpg", "c.jpg", "e.jpg"]


def test_normalized_keeps_other_fields():
    spec = FilterSpec(min_width=100, max_width=10, modes=("RGB",), exclude_duplicates=True)
    fixed = spec.normalized()
    assert (fixed.min_width, fixed.max_width) == (10, 100)
    assert fixed.modes == ("RGB",)
    assert fixed.exclude_duplicates is True


# --------------------------------------------------------------------------
# 尺寸读不出来的图
# --------------------------------------------------------------------------
def test_unreadable_is_dropped_only_when_size_matters():
    """读不出尺寸的图无法校验。

    启用了尺寸/比例条件时必须剔除；用户根本不在乎尺寸时就不该拦它。
    """
    pool = [stat("ok.jpg", 640, 640), CalibStat(name="bad.jpg", size=100)]

    assert len(apply_filter(pool, FilterSpec()).kept) == 2
    assert len(apply_filter(pool, FilterSpec(min_bytes=1)).kept) == 2

    result = apply_filter(pool, FilterSpec(min_width=100))
    assert [s.name for s in result.kept] == ["ok.jpg"]
    assert result.dropped["unreadable"] == 1


def test_zero_height_has_no_aspect():
    zero = CalibStat(name="z.jpg", size=10, width=100, height=0)
    assert zero.aspect is None
    assert zero.readable is False
    result = apply_filter([zero], FilterSpec(min_aspect=1.0))
    assert result.kept == ()


# --------------------------------------------------------------------------
# 重复
# --------------------------------------------------------------------------
def test_duplicate_names_takes_the_second_of_each_pair():
    """回包形状是 [[保留的, 重复的], ...] —— 要丢的是第二个。"""
    assert duplicate_names([["a.jpg", "b.jpg"], ["a.jpg", "c.jpg"]]) == {"b.jpg", "c.jpg"}


@pytest.mark.parametrize("payload", [[], None, [["只有一项"]], ["不是配对"]])
def test_duplicate_names_tolerates_junk(payload):
    assert duplicate_names(payload or []) == set()


def test_exclude_duplicates(pool):
    result = apply_filter(
        pool, FilterSpec(exclude_duplicates=True),
        duplicates=[["a.jpg", "c.jpg"]],
    )
    assert [s.name for s in result.kept] == ["a.jpg", "b.jpg", "d.jpg", "e.jpg"]
    assert result.dropped["duplicate"] == 1


def test_duplicates_ignored_when_not_asked(pool):
    result = apply_filter(pool, FilterSpec(), duplicates=[["a.jpg", "c.jpg"]])
    assert len(result.kept) == 5


# --------------------------------------------------------------------------
# 解释
# --------------------------------------------------------------------------
def test_dropped_counts_add_up(pool):
    result = apply_filter(pool, FilterSpec(min_width=640, modes=("RGB",)))
    assert result.dropped_total == len(pool) - len(result.kept)


def test_explain_lists_reasons(pool):
    result = apply_filter(pool, FilterSpec(min_width=1280, modes=("L",)))
    text = result.explain()
    assert text, "有东西被筛掉时必须能解释"
    assert "宽高" in text or "色彩模式" in text


# --------------------------------------------------------------------------
# 预设
# --------------------------------------------------------------------------
def test_most_common_resolution():
    stats = [stat(f"a{i}.jpg", 640, 480) for i in range(5)]
    stats += [stat(f"b{i}.jpg", 1920, 1080) for i in range(2)]
    assert most_common_resolution(stats) == (640, 480)


def test_most_common_resolution_breaks_ties_by_area():
    """并列时取面积大的 —— 信息量更多。"""
    stats = [stat("a.jpg", 100, 100), stat("b.jpg", 500, 500)]
    assert most_common_resolution(stats) == (500, 500)


def test_most_common_resolution_on_empty():
    assert most_common_resolution([]) is None
    assert most_common_resolution([CalibStat(name="x", size=1)]) is None


def test_bounds_helpers(pool):
    assert resolution_bounds(pool) == (320, 1920, 240, 1080)
    low, high = aspect_bounds(pool)
    # 池子里最小的是 a.jpg 640/640 = 1.0，最大的是 1920/1080 ≈ 1.78
    assert low == pytest.approx(1.0)
    assert high == pytest.approx(1920 / 1080)
    assert resolution_bounds([]) is None
    assert aspect_bounds([]) is None


# --------------------------------------------------------------------------
# 筛选 + 采样
# --------------------------------------------------------------------------
def test_filter_and_sample_pipeline():
    stats = [stat(f"f{i:03d}.jpg", 640, 640) for i in range(100)]
    result, names = filter_and_sample(stats, FilterSpec(min_width=640), count=10)
    assert len(result.kept) == 100
    assert len(names) == 10
    assert names[0] == "f000.jpg"


def test_filter_and_sample_returns_empty_when_filtered_out():
    stats = [stat("a.jpg", 100, 100)]
    result, names = filter_and_sample(stats, FilterSpec(min_width=9999), count=10)
    assert result.kept == ()
    assert names == []


# --------------------------------------------------------------------------
# sample_evenly 与容器侧必须一致
# --------------------------------------------------------------------------
def test_sample_evenly_matches_the_probe_implementation():
    """宿主和容器各有一份 `sample_evenly`，**必须完全一致**。

    宿主负责算出"筛选 + 采样"后的名单再交给容器（`--only-file`），探针那份在
    没给名单时仍然用得上（命令行直接调用）。两份实现是有意的重复，
    这条测试把"重复"变成"被守住的不变量"。
    """
    import importlib.util

    import rdkquant

    probe_path = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"
    spec = importlib.util.spec_from_file_location("probe_under_test", probe_path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    for total, count in ((3504, 50), (100, 10), (10, 10), (10, 0), (7, 3), (1, 5)):
        files = [f"{i:04d}.jpg" for i in range(total)]
        assert sample_evenly(files, count) == probe.sample_evenly(files, count), (
            f"total={total} count={count} 时两份实现的结果不同"
        )


# --------------------------------------------------------------------------
# 统计文件
# --------------------------------------------------------------------------
def test_load_stats_reads_the_probe_format(tmp_path: Path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({
        "version": 1, "count": 2,
        "files": [
            {"n": "a.jpg", "w": 640, "h": 480, "s": 1000, "m": "RGB"},
            {"n": "b.jpg", "w": None, "h": None, "s": 2000, "m": None},
        ],
    }), encoding="utf-8")

    stats = load_stats(path)
    assert [s.name for s in stats] == ["a.jpg", "b.jpg"]
    assert stats[0].aspect == pytest.approx(640 / 480)
    assert stats[1].readable is False


def test_load_stats_raises_on_missing_file(tmp_path: Path):
    """**不能静默返回空列表** —— 空列表意味着"筛完一张不剩"，
    会把用户引去改筛选条件，而真正的问题是文件没写成。"""
    with pytest.raises(OSError):
        load_stats(tmp_path / "不存在.json")


def test_load_stats_skips_entries_without_a_name(tmp_path: Path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"files": [{"w": 1, "h": 1}, {"n": "ok.jpg"}]}),
                    encoding="utf-8")
    assert [s.name for s in load_stats(path)] == ["ok.jpg"]
