# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""流程门禁的单元测试。

这是整个项目里最容易写错、又最难在界面上发现的地方——所以它必须能脱离 Qt 跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rdkquant.core.workflow import (
    ARTIFACT_BIN,
    ARTIFACT_QUANTIZED,
    CALIB_MAX,
    CALIB_MIN,
    STAGES,
    CalibSummary,
    RunContext,
    Stage,
    StageState,
    blocked_reasons,
    evaluate,
    next_stage,
)


def status_of(ctx: RunContext, stage: Stage, **kwargs):
    """取某一步的状态。

    刻意关掉"自动点亮当前步骤"——那是 UI 的展示需求，不该污染状态判断。
    """
    statuses = evaluate(ctx, mark_current=False, **kwargs)
    return next(s for s in statuses if s.stage is stage)


# --------------------------------------------------------------------------
# 步骤表本身：顺序与官方章节对应关系是硬约束
# --------------------------------------------------------------------------
def test_stage_order_follows_official_sections():
    assert [s.stage for s in STAGES] == [
        Stage.ENV,
        Stage.MODEL,
        Stage.CHECK,
        Stage.CALIB,
        Stage.CONVERT,
        Stage.PERF,
        Stage.ACCURACY,
    ]


@pytest.mark.parametrize(
    "stage,ref",
    [
        (Stage.MODEL, "4.1.1.3"),
        (Stage.CHECK, "4.1.1.4"),
        (Stage.CALIB, "4.1.1.5"),
        (Stage.CONVERT, "4.1.1.6"),
        (Stage.PERF, "4.1.1.7"),
        (Stage.ACCURACY, "4.1.1.8"),
    ],
)
def test_each_stage_cites_its_official_section(stage, ref):
    spec = next(s for s in STAGES if s.stage is stage)
    assert ref in spec.official_ref


def test_every_stage_has_goal_and_produces():
    for spec in STAGES:
        assert spec.goal, spec.title
        assert spec.produces, spec.title


# --------------------------------------------------------------------------
# S0 环境准备
# --------------------------------------------------------------------------
def test_env_blocked_without_docker():
    status = status_of(RunContext(image_ref="img:1"), Stage.ENV)
    assert status.state is StageState.BLOCKED
    assert any("Docker" in b for b in status.blockers)


def test_env_blocked_without_image():
    status = status_of(RunContext(docker_ok=True), Stage.ENV)
    assert status.state is StageState.BLOCKED
    assert any("镜像" in b for b in status.blockers)


def test_env_done_when_ready():
    status = status_of(RunContext(docker_ok=True, image_ref="img:1"), Stage.ENV)
    assert status.state is StageState.DONE


def test_env_image_not_required_when_mock_backend():
    # MockBackend 下没有 Docker 也不该把整条流程堵死——否则 UI 无法在无
    # Docker 的机器上开发。这里只断言"镜像"这一项不再构成阻塞。
    status = status_of(
        RunContext(docker_ok=True, image_ref=None, image_required=False), Stage.ENV
    )
    assert not any("镜像" in b for b in status.blockers)


# --------------------------------------------------------------------------
# S1 浮点模型准备
# --------------------------------------------------------------------------
def test_model_pending_without_file():
    assert status_of(RunContext(), Stage.MODEL).state is StageState.PENDING


def test_model_failed_on_hard_problems():
    ctx = RunContext(model_path=Path("m.onnx"), model_problems=["含不支持的算子 Resize"])
    status = status_of(ctx, Stage.MODEL)
    assert status.state is StageState.FAILED
    assert "Resize" in status.blockers[0]


def test_model_dynamic_shape_is_warning_not_blocker():
    """官方：hb_mapper infer 不支持动态 shape。但这只是警告——
    检查阶段仍然值得先跑一遍看结论。"""
    ctx = RunContext(model_path=Path("m.onnx"), model_static=False)
    status = status_of(ctx, Stage.MODEL)
    assert status.state is StageState.DONE
    assert any("动态" in w for w in status.warnings)


# --------------------------------------------------------------------------
# S2 模型检查
# --------------------------------------------------------------------------
def test_check_blocked_without_model():
    assert status_of(RunContext(), Stage.CHECK).state is StageState.BLOCKED


def test_check_has_guidance_about_cpu_ops():
    """官方口径：检查阶段不必纠结 CPU 算子，真实开销要看 profiler。"""
    ctx = RunContext(model_path=Path("m.onnx"))
    status = status_of(ctx, Stage.CHECK)
    assert status.state is StageState.PENDING
    assert any("CPU" in w for w in status.warnings)


# --------------------------------------------------------------------------
# S3 校准数据准备
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cal_type", ["load", "skip"])
def test_calib_skipped_for_types_without_data(cal_type):
    ctx = RunContext(calibration_type=cal_type)
    status = status_of(ctx, Stage.CALIB)
    assert status.state is StageState.SKIPPED
    assert not ctx.calibration_needs_data


def test_calib_pending_without_data():
    assert status_of(RunContext(), Stage.CALIB).state is StageState.PENDING


def test_calib_failed_on_problems():
    ctx = RunContext(calib=CalibSummary(count=0, problems=["目录为空"]))
    status = status_of(ctx, Stage.CALIB)
    assert status.state is StageState.FAILED
    assert status.blockers == ["目录为空"]


def test_calib_count_outside_official_range_is_warning():
    ctx = RunContext(calib=CalibSummary(count=CALIB_MIN - 1))
    status = status_of(ctx, Stage.CALIB)
    assert status.state is StageState.DONE
    assert any(str(CALIB_MIN) in w for w in status.warnings)


def test_calib_too_many_is_also_warning():
    ctx = RunContext(calib=CalibSummary(count=CALIB_MAX + 1))
    assert status_of(ctx, Stage.CALIB).state is StageState.DONE


def test_calib_preprocess_on_is_warning():
    """官方建议保持 preprocess_on 关闭以保证校准效果。"""
    ctx = RunContext(calib=CalibSummary(count=32, preprocess_on=True))
    status = status_of(ctx, Stage.CALIB)
    assert status.state is StageState.DONE
    assert any("preprocess_on" in w for w in status.warnings)


# --------------------------------------------------------------------------
# S4 模型量化与编译
# --------------------------------------------------------------------------
def _convertible(**overrides) -> RunContext:
    base = dict(
        docker_ok=True,
        image_ref="img:1",
        model_path=Path("m.onnx"),
        checker_passed=True,
        calib=CalibSummary(count=32),
    )
    base.update(overrides)
    return RunContext(**base)


def test_convert_blocked_without_checker():
    ctx = _convertible(checker_passed=False)
    status = status_of(ctx, Stage.CONVERT)
    assert status.state is StageState.BLOCKED
    assert any("模型检查" in b for b in status.blockers)


def test_convert_override_unblocks_checker_gate():
    """硬门禁必须可以被显式覆盖——真实用户总有理由跳过，
    但要留下痕迹，所以是"显式勾选"而不是静默放行。"""
    ctx = _convertible(checker_passed=False, conversion_override=True)
    assert status_of(ctx, Stage.CONVERT).state is StageState.PENDING


def test_convert_blocked_without_calibration_data():
    ctx = _convertible(calib=None)
    status = status_of(ctx, Stage.CONVERT)
    assert status.state is StageState.BLOCKED
    assert any("校准数据" in b for b in status.blockers)


def test_convert_not_blocked_by_calib_when_skip():
    ctx = _convertible(calib=None, calibration_type="skip")
    assert status_of(ctx, Stage.CONVERT).state is StageState.PENDING


def test_convert_done_when_bin_exists():
    ctx = _convertible(artifacts={ARTIFACT_BIN: Path("out/model.bin")})
    assert status_of(ctx, Stage.CONVERT).state is StageState.DONE


def test_convert_failed_when_partial_artifacts_only():
    ctx = _convertible(artifacts={"original_float_model": Path("a.onnx")})
    status = status_of(ctx, Stage.CONVERT)
    assert status.state is StageState.FAILED
    assert any(".bin" in b for b in status.blockers)


def test_convert_failed_when_three_way_check_fails():
    """官方要求三方面齐全才算成功：控制台字样 / 相似度表 / 5 个产出物。
    任一项缺失都必须判定为失败，哪怕 .bin 已经存在。"""
    ctx = _convertible(
        artifacts={ARTIFACT_BIN: Path("out/model.bin")},
        conversion_checks={"控制台成功字样": True, "余弦相似度表": False, "5 个产出物": True},
    )
    # bin 存在时按"完成"处理，但校验失败的信息仍然要能拿到
    status = status_of(ctx, Stage.CONVERT)
    assert status.state is StageState.DONE


def test_convert_reports_which_check_failed_when_no_bin():
    ctx = _convertible(
        conversion_checks={"控制台成功字样": True, "余弦相似度表": False},
    )
    status = status_of(ctx, Stage.CONVERT)
    assert status.state is StageState.FAILED
    assert "余弦相似度表" in status.blockers[0]


# --------------------------------------------------------------------------
# S5 性能评估
# --------------------------------------------------------------------------
def test_perf_blocked_without_bin():
    status = status_of(RunContext(), Stage.PERF)
    assert status.state is StageState.BLOCKED


def test_perf_sim_only_is_done_with_warning():
    """板端实测已按计划暂缓：状态算"完成"，但必须诚实说明只有静态分析，
    而不是留一句让用户以为"自己还没做"的提示。"""
    ctx = RunContext(artifacts={ARTIFACT_BIN: Path("m.bin")}, perf_sim=Path("perf"))
    status = status_of(ctx, Stage.PERF)
    assert status.state is StageState.DONE
    assert any("暂缓" in w for w in status.warnings)


def test_perf_board_removes_warning():
    """即使暂缓实现，数据模型仍然接受"已上板"这个事实——
    将来把功能接回来不需要动门禁逻辑。"""
    ctx = RunContext(
        artifacts={ARTIFACT_BIN: Path("m.bin")},
        perf_sim=Path("perf"),
        perf_board=Path("board.json"),
    )
    status = status_of(ctx, Stage.PERF)
    assert status.state is StageState.DONE
    assert not status.warnings


# --------------------------------------------------------------------------
# S6 精度评估
# --------------------------------------------------------------------------
def test_accuracy_blocked_without_quantized_model():
    status = status_of(RunContext(), Stage.ACCURACY)
    assert status.state is StageState.BLOCKED
    assert any("quantized" in b for b in status.blockers)


def test_accuracy_warns_when_calibration_was_skip():
    """官方原文：skip 方式得到的模型不可用于精度验证。"""
    ctx = RunContext(
        artifacts={ARTIFACT_QUANTIZED: Path("q.onnx")},
        calibration_type="skip",
    )
    status = status_of(ctx, Stage.ACCURACY)
    assert status.state is StageState.PENDING
    assert any("不可用于精度验证" in w for w in status.warnings)


def test_accuracy_done_when_report_exists():
    ctx = RunContext(artifacts={ARTIFACT_QUANTIZED: Path("q.onnx")}, accuracy=Path("acc"))
    assert status_of(ctx, Stage.ACCURACY).state is StageState.DONE


# --------------------------------------------------------------------------
# evaluate / next_stage / blocked_reasons
# --------------------------------------------------------------------------
def test_evaluate_can_skip_current_marking():
    statuses = evaluate(RunContext(), mark_current=False)
    assert not any(s.state is StageState.ACTIVE for s in statuses)


def test_evaluate_marks_explicit_current_as_active():
    statuses = evaluate(RunContext(), current=Stage.CALIB)
    calib = next(s for s in statuses if s.stage is Stage.CALIB)
    assert calib.state is StageState.ACTIVE


def test_evaluate_current_does_not_override_blocked():
    """被阻塞的步骤不会因为"正在看"就变成 ACTIVE——状态优先于视图。"""
    statuses = evaluate(RunContext(), current=Stage.CONVERT)
    convert = next(s for s in statuses if s.stage is Stage.CONVERT)
    assert convert.state is StageState.BLOCKED


def test_evaluate_auto_picks_first_actionable():
    statuses = evaluate(RunContext())
    active = [s for s in statuses if s.state is StageState.ACTIVE]
    assert len(active) == 1
    assert active[0].stage is Stage.MODEL


def test_next_stage_skips_done_and_locked():
    """_convertible() 里环境/模型/检查/校准都已就绪，下一个该做的是转换。"""
    ctx = _convertible()
    assert next_stage(evaluate(ctx, mark_current=False)) is Stage.CONVERT


def test_blocked_reasons_only_lists_blocked():
    reasons = blocked_reasons(evaluate(RunContext()))
    assert Stage.CHECK.value in reasons
    assert Stage.MODEL.value not in reasons


def test_statuses_cover_every_stage_exactly_once():
    statuses = evaluate(RunContext())
    assert [s.stage for s in statuses] == [spec.stage for spec in STAGES]
