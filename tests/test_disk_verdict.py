# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""磁盘判定的唯一实现。

原来这段逻辑内联在 `probe_host()` 里，界面想在**选择处**提示装不装得下，
只能再抄一份阈值 —— 两处各写一份，迟早漂移成"结论说不够、下拉栏说够"。
现在只有 `disk_verdict()` 一份，体检结论和下拉栏提示都走它。
"""

from __future__ import annotations

import pytest

from rdkquant.core.probe import (
    GB,
    LEVEL_ERROR,
    LEVEL_OK,
    LEVEL_WARN,
    WORKABLE_DISK_BYTES,
    WORKSPACE_HEADROOM_BYTES,
    disk_verdict,
    image_footprint,
    probe_host,
)

#: 清单里 X5 GPU 版的真实体积
GPU_SIZE = 11_066_222_691
#: 清单里 X5 CPU 版的真实体积
CPU_SIZE = 1_815_507_981


# --------------------------------------------------------------------------
# image_footprint
# --------------------------------------------------------------------------
def test_footprint_is_bigger_than_the_archive():
    """导入期间**压缩包和解压后的层会同时存在**，所以占用不是压缩包本身。"""
    assert image_footprint(CPU_SIZE) == int(CPU_SIZE * 2.2)
    assert image_footprint(CPU_SIZE) > CPU_SIZE


@pytest.mark.parametrize("value", [None, 0])
def test_footprint_of_unknown_size_is_none(value):
    assert image_footprint(value) is None


# --------------------------------------------------------------------------
# 指定了目标镜像
# --------------------------------------------------------------------------
def test_insufficient_reports_the_shortfall():
    verdict = disk_verdict(int(12 * GB), GPU_SIZE)
    assert verdict.level == LEVEL_ERROR
    assert verdict.code == "DISK_INSUFFICIENT"
    # 差多少要说清楚 —— 用户得知道要清出多少空间
    assert "还差" in verdict.short
    assert "10.7" in verdict.short, f"12 GB 剩、22.7 GB 需要 → 差 10.7 GB，实际 {verdict.short}"


def test_tight_when_it_fits_but_leaves_no_headroom():
    """刚好装得下也要提醒 —— 中间产物（校准缓存、量化模型）还要占地方。"""
    needed = image_footprint(GPU_SIZE)
    just_enough = needed + WORKSPACE_HEADROOM_BYTES // 2
    verdict = disk_verdict(just_enough, GPU_SIZE)
    assert verdict.level == LEVEL_WARN
    assert verdict.code == "DISK_TIGHT"
    assert verdict.short == "磁盘偏紧"


def test_ok_with_enough_room():
    verdict = disk_verdict(image_footprint(GPU_SIZE) + WORKSPACE_HEADROOM_BYTES, GPU_SIZE)
    assert verdict.level == LEVEL_OK
    assert verdict.code == "DISK_OK"
    assert verdict.short == "", "没问题时下拉栏不该多出提示"


def test_boundary_is_inclusive_on_the_safe_side():
    """恰好等于需求量 → 算不够（要留解压峰值，不能卡在等于上）。"""
    needed = image_footprint(GPU_SIZE)
    assert disk_verdict(needed - 1, GPU_SIZE).level == LEVEL_ERROR
    assert disk_verdict(needed, GPU_SIZE).level == LEVEL_WARN


def test_message_names_the_image_and_the_path():
    verdict = disk_verdict(int(1 * GB), CPU_SIZE, path="C:\\", label="RDK X5（CPU 版）")
    assert "C:\\" in verdict.message
    assert "RDK X5（CPU 版）" in verdict.message
    assert verdict.hint, "不够时要给出下一步动作"


# --------------------------------------------------------------------------
# 没指定目标镜像
# --------------------------------------------------------------------------
def test_unworkable_disk_is_an_error_even_without_a_target():
    """不知道要装哪个镜像时，磁盘小到没法工作也必须判 ERROR。

    这里踩过：原来的写法只给 WARN，于是 C: 只剩 1.8 GB 时结论仍是"可以执行量化"。
    """
    verdict = disk_verdict(WORKABLE_DISK_BYTES - 1, None)
    assert verdict.level == LEVEL_ERROR
    assert verdict.code == "DISK_INSUFFICIENT"


def test_tight_but_workable_without_a_target():
    verdict = disk_verdict(15 * GB, None)
    assert verdict.level == LEVEL_WARN
    assert verdict.code == "DISK_TIGHT"


def test_plentiful_disk_without_a_target_says_nothing():
    """磁盘宽裕又没指定镜像时**连一条 OK 结论都不产生** ——
    容量已经在汇总表里显示了，不必再占一行。"""
    assert disk_verdict(200 * GB, None) is None


# --------------------------------------------------------------------------
# 与体检结论一致（这是抽取这段逻辑的全部意义）
# --------------------------------------------------------------------------
def test_probe_host_uses_the_same_policy(tmp_path):
    """体检产出的结论必须就是 `disk_verdict()` 的判定，不能是另一套。"""
    report = probe_host(tmp_path, required_image_bytes=GPU_SIZE,
                        required_image_name="RDK X5（GPU 版）")
    disk = [f for f in report.findings if f.code.startswith("DISK_")]
    assert len(disk) == 1
    expected = disk_verdict(
        report.disk.free_bytes, GPU_SIZE, path=report.disk.path, label="RDK X5（GPU 版）"
    )
    assert disk[0].level == expected.level
    assert disk[0].message == expected.message


def test_probe_host_agrees_on_the_no_target_case(tmp_path):
    report = probe_host(tmp_path)
    disk = {f.code for f in report.findings if f.code.startswith("DISK_")}
    expected = disk_verdict(report.disk.free_bytes, None)
    assert disk == ({expected.code} if expected else set())
