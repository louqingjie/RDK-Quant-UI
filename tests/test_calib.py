# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准数据**策略层**的测试。

这些都是"依赖用户配置"的判断——探针只报事实，不知道该按哪种口径解读。
每条规则一个用例，因为它们是官方口径的落点，改错了不会有人发现。
"""

from __future__ import annotations

import pytest

from rdkquant.core.calib import (
    CalibBuildSpec,
    evaluate_probe,
    infer_cal_data_type,
)
from rdkquant.core.findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN


def payload(**overrides) -> dict:
    """一份"一切正常"的探针回包，按需覆盖字段。"""
    base = {
        "ok": True,
        "directory": "cal_images",
        "imaging_backend": "PIL",
        "count": 48,
        "image_like": 48,
        "binary_like": 0,
        "measured": True,
        "files": [],
        "resolutions": [[640, 640, 48]],
        "duplicates": [],
        "exposure": None,
        "problems": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def codes(verdict) -> set[str]:
    return {f.code for f in verdict.findings}


def level_of(verdict, code: str) -> str:
    return next(f.level for f in verdict.findings if f.code == code)


# --------------------------------------------------------------------------
# 基本形状
# --------------------------------------------------------------------------
def test_healthy_data_is_not_blocked():
    verdict = evaluate_probe(payload())
    assert not verdict.blocked
    assert verdict.summary.count == 48
    assert verdict.summary.problems == []
    assert level_of(verdict, "CALIB_OK") == LEVEL_OK


def test_summary_carries_directory_for_config_generation():
    verdict = evaluate_probe(payload(), directory="cal_images")
    assert verdict.summary.cal_data_dir == "cal_images"


def test_directory_can_come_from_payload():
    verdict = evaluate_probe(payload(directory="my_calib"))
    assert verdict.summary.cal_data_dir == "my_calib"


# --------------------------------------------------------------------------
# 张数：官方建议 20~100
# --------------------------------------------------------------------------
def test_too_few_samples_warns():
    verdict = evaluate_probe(payload(count=8, image_like=8,
                                     resolutions=[[640, 640, 8]]))
    assert level_of(verdict, "CALIB_COUNT_LOW") == LEVEL_WARN
    assert not verdict.blocked, "张数只是建议，不该阻塞流程"


def test_too_many_samples_warns():
    verdict = evaluate_probe(payload(count=500, image_like=500,
                                     resolutions=[[640, 640, 500]]))
    assert level_of(verdict, "CALIB_COUNT_HIGH") == LEVEL_WARN
    assert not verdict.blocked


@pytest.mark.parametrize("count", [20, 21, 60, 99, 100])
def test_count_inside_official_range_is_silent(count):
    verdict = evaluate_probe(payload(count=count, image_like=count,
                                     resolutions=[[640, 640, count]]))
    assert "CALIB_COUNT_LOW" not in codes(verdict)
    assert "CALIB_COUNT_HIGH" not in codes(verdict)


def test_empty_directory_blocks():
    verdict = evaluate_probe(payload(count=0, image_like=0, resolutions=[]))
    assert verdict.blocked
    assert "CALIB_EMPTY" in codes(verdict)
    assert verdict.summary.problems, "阻塞必须体现在 summary 里，否则门禁看不到"


# --------------------------------------------------------------------------
# 分辨率：严重程度取决于 preprocess_on
# --------------------------------------------------------------------------
MIXED = [[640, 640, 40], [320, 240, 8]]


def test_mixed_resolution_blocks_when_preprocess_off():
    """preprocess_on=False 时工具不会替你 resize，尺寸不一致就是硬错误。"""
    verdict = evaluate_probe(payload(resolutions=MIXED), preprocess_on=False)
    assert verdict.blocked
    assert level_of(verdict, "CALIB_RESOLUTION_MISMATCH") == LEVEL_ERROR


def test_mixed_resolution_only_warns_when_preprocess_on():
    """preprocess_on=True 时工具会 resize，所以降级为警告（官方仍建议关闭）。"""
    verdict = evaluate_probe(payload(resolutions=MIXED), preprocess_on=True)
    assert not verdict.blocked
    assert level_of(verdict, "CALIB_RESOLUTION_MIXED") == LEVEL_WARN
    assert "CALIB_RESOLUTION_MISMATCH" not in codes(verdict)


def test_uniform_resolution_is_silent():
    verdict = evaluate_probe(payload(resolutions=[[640, 640, 48]]))
    assert "CALIB_RESOLUTION_MISMATCH" not in codes(verdict)
    assert "CALIB_RESOLUTION_MIXED" not in codes(verdict)


# --------------------------------------------------------------------------
# 与模型输入尺寸比对
# --------------------------------------------------------------------------
def test_size_mismatch_against_model_input_blocks():
    verdict = evaluate_probe(
        payload(resolutions=[[320, 320, 48]]),
        preprocess_on=False, expected_hw=(640, 640),
    )
    assert verdict.blocked
    assert level_of(verdict, "CALIB_SIZE_MISMATCH") == LEVEL_ERROR


def test_size_match_against_model_input_is_silent():
    verdict = evaluate_probe(
        payload(resolutions=[[640, 640, 48]]),
        preprocess_on=False, expected_hw=(640, 640),
    )
    assert "CALIB_SIZE_MISMATCH" not in codes(verdict)


def test_size_mismatch_is_ignored_when_preprocess_on():
    """工具会自己 resize，所以不应再报尺寸不符。"""
    verdict = evaluate_probe(
        payload(resolutions=[[320, 320, 48]]),
        preprocess_on=True, expected_hw=(640, 640),
    )
    assert "CALIB_SIZE_MISMATCH" not in codes(verdict)


def test_no_expected_hw_means_no_size_check():
    """调用方没给确定尺寸时不猜布局，也就不做尺寸判断。"""
    verdict = evaluate_probe(payload(), expected_hw=None)
    assert "CALIB_SIZE_MISMATCH" not in codes(verdict)


# --------------------------------------------------------------------------
# 图片与二进制混放
# --------------------------------------------------------------------------
def test_mixing_images_and_binaries_blocks():
    verdict = evaluate_probe(payload(count=50, image_like=30, binary_like=20))
    assert verdict.blocked
    assert level_of(verdict, "CALIB_MIXED_KINDS") == LEVEL_ERROR


def test_binary_only_is_fine():
    verdict = evaluate_probe(payload(
        count=48, image_like=0, binary_like=48, measured=False, resolutions=[],
    ))
    assert not verdict.blocked
    assert "CALIB_BINARY_OK" in codes(verdict)


# --------------------------------------------------------------------------
# calibration_type = load / skip：官方说不需要校准数据
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ctype", ["load", "skip"])
def test_load_and_skip_do_not_need_calibration_data(ctype):
    verdict = evaluate_probe(
        payload(count=0, image_like=0, resolutions=[]), calibration_type=ctype,
    )
    assert not verdict.blocked, f"{ctype} 不需要校准数据，不该因为空目录报错"
    assert "CALIB_NOT_NEEDED" in codes(verdict)
    assert "CALIB_EMPTY" not in codes(verdict)


@pytest.mark.parametrize("ctype", ["default", "mix", "kl", "max"])
def test_types_needing_data_still_validate(ctype):
    verdict = evaluate_probe(
        payload(count=0, image_like=0, resolutions=[]), calibration_type=ctype,
    )
    assert verdict.blocked


# --------------------------------------------------------------------------
# 探针失败与透传
# --------------------------------------------------------------------------
def test_probe_failure_is_passed_through():
    verdict = evaluate_probe({"ok": False, "error": "容器里没有可用的图像库"})
    assert verdict.blocked
    assert level_of(verdict, "CALIB_SCAN_FAILED") == LEVEL_ERROR
    assert "图像库" in verdict.findings[0].message
    assert verdict.summary.problems == ["容器里没有可用的图像库"]


def test_probe_problems_become_blocking_findings():
    verdict = evaluate_probe(payload(problems=["有图片几乎全白"]))
    assert verdict.blocked
    assert any("几乎全白" in f.message for f in verdict.findings)
    assert verdict.summary.problems == ["有图片几乎全白"]


def test_probe_warnings_do_not_block():
    verdict = evaluate_probe(payload(warnings=["检测到 2 组内容重复的图片"]))
    assert not verdict.blocked
    assert any(f.level == LEVEL_WARN for f in verdict.findings)


# --------------------------------------------------------------------------
# 后缀启发式
# --------------------------------------------------------------------------
def test_infer_cal_data_type_follows_official_suffix_rule():
    assert infer_cal_data_type("calib_bin_f32") == "float32"
    assert infer_cal_data_type("cal_images_f32/") == "float32"
    assert infer_cal_data_type("cal_images") == "uint8"
    assert infer_cal_data_type("") == "uint8"


def test_f32_suffix_is_mentioned_but_not_relied_on():
    verdict = evaluate_probe(payload(), directory="calib_bin_f32")
    assert verdict.summary.cal_data_type == "float32"
    assert "CALIB_SUFFIX_F32" in codes(verdict)


# --------------------------------------------------------------------------
# 生成参数
# --------------------------------------------------------------------------
def test_resolution_description_is_truncated():
    """分辨率多的时候不能把整张清单塞进结论里。

    实测：用户的数据集有 **1474 种分辨率**，全列出来那条消息上万字符，会把结论表撑爆，
    而且对用户毫无帮助——他要的是"多少种"和"最典型的几个"。
    """
    from rdkquant.core.calib import _describe_resolutions

    many = [(100 + i, 200 + i, i + 1) for i in range(1474)]
    text = _describe_resolutions(many)
    assert len(text) < 200, f"描述太长了（{len(text)} 字符）：{text[:80]}…"
    assert "1474" in text, "种类数必须保留"
    assert "100×200(1 张)" in text, "最典型的几个要列出来"
    assert _describe_resolutions([]) == "（无）"
    assert _describe_resolutions([(640, 640, 48)]) == "640×640(48 张)"


def test_many_resolutions_still_blocks_without_preprocess():
    """1474 种分辨率 + preprocess_on=False 必须是硬错误，而且消息要短。"""
    many = [[100 + i, 200 + i, 1] for i in range(1474)]
    verdict = evaluate_probe(payload(resolutions=many), preprocess_on=False)
    assert verdict.blocked
    mismatch = next(f for f in verdict.findings if f.code == "CALIB_RESOLUTION_MISMATCH")
    assert len(mismatch.message) < 300, f"消息太长：{len(mismatch.message)}"
    assert "1474" in mismatch.message


def test_build_spec_output_dir_uses_official_suffix():
    """目录名带官方后缀，让工具的启发式与我们的显式设置一致，不产生告警。"""
    assert CalibBuildSpec(shape="1x3x640x640", dtype="float32").output_dir_name == \
        "calib_bin_f32"
    assert CalibBuildSpec(shape="1x3x640x640", dtype="uint8").output_dir_name == \
        "calib_bin_u8"


def test_build_spec_defaults_to_no_normalization():
    """默认不叠加 mean/scale —— 官方把 norm_type 插进模型，校准数据应是原始像素。
    这一条已被真机实验确认（CONTAINER_FINDINGS.md）。"""
    spec = CalibBuildSpec(shape="1x3x640x640")
    assert spec.mean is None
    assert spec.scale is None
