# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准集的均匀采样。

背景：官方建议校准集 20~100 张，而真实数据集动辄几千张。实测用户的
RM2025 装甲板数据集有 **3504 张、1474 种分辨率** —— 直接拿来当校准集既不合规
也无法校验，必须先挑出有代表性的子集。

为什么是"均匀"而不是"随机"：
  * 确定 —— 同样的输入永远得到同样的子集，用户能复现
  * 数据集通常按时间/场景排序，间隔取样天然覆盖各个片段；随机取样可能全挤在一段
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import rdkquant

PROBE = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"


@pytest.fixture(scope="module")
def probe():
    """把探针当模块加载。

    探针的顶层只有导入和函数定义（真正干活的都藏在 `if __name__ == "__main__"` 后面），
    所以直接 exec 是安全的。
    """
    spec = importlib.util.spec_from_file_location("rdkquant_probe_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spreads_across_the_whole_list(probe):
    """3504 张里取 50 张：应当每 70 张取一个，从开头一直覆盖到结尾。"""
    files = [f"{i:04d}.jpg" for i in range(3504)]
    picked = probe.sample_evenly(files, 50)

    assert len(picked) == 50
    assert picked[0] == "0000.jpg", "要从开头就开始"
    assert picked[-1] == files[int(49 * 3504 / 50)], "要一直覆盖到结尾"

    indexes = [int(name[:4]) for name in picked]
    steps = [b - a for a, b in zip(indexes, indexes[1:])]
    ideal = 3504 / 50                      # 70.08，不是整数
    # 间隔会在 floor/ceil 之间浮动（实测是 70 和 71），这是 float 取整的正常结果。
    # 真正的不变量是"每个间隔都贴着理想步长"，而不是"全都相等"。
    assert all(abs(step - ideal) <= 1 for step in steps), (
        f"间隔偏离理想步长 {ideal:.2f} 太多：{sorted(set(steps))}"
    )
    assert len(set(steps)) <= 2, f"间隔种类不该超过两种：{sorted(set(steps))}"


def test_is_deterministic(probe):
    """同样的输入必须给出同样的结果 —— 用户要能复现自己看到的那一批。"""
    files = [f"f{i}" for i in range(500)]
    assert probe.sample_evenly(files, 30) == probe.sample_evenly(files, 30)


@pytest.mark.parametrize("count", [0, -1])
def test_zero_or_negative_means_all(probe, count):
    files = [f"f{i}" for i in range(10)]
    assert probe.sample_evenly(files, count) == files


def test_more_than_available_returns_everything(probe):
    files = [f"f{i}" for i in range(10)]
    assert probe.sample_evenly(files, 999) == files


def test_short_list_has_no_duplicates(probe):
    picked = probe.sample_evenly(["a", "b", "c"], 3)
    assert len(set(picked)) == len(picked), "不能取重"
    assert set(picked) == {"a", "b", "c"}


def test_sampled_subset_preserves_order(probe):
    """结果保持原顺序 —— 输出文件名跟着源文件走，顺序乱了会让产物难以对照。"""
    files = [f"f{i:03d}" for i in range(200)]
    picked = probe.sample_evenly(files, 17)
    assert picked == sorted(picked)
