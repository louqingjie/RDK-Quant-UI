# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""流程模型 —— 整个工具的"事实来源"，**零 Qt 依赖**。

为什么单独成模块：门禁逻辑（哪一步能跑、缺什么）是最容易写错、又最难在界面上
发现的地方。放在这里就能脱离 UI 单测，见 tests/test_workflow.py。

步骤与地平线官方文档的对应关系是硬约束，逐条写在 `official_ref` 里：

    概要五阶段（4.1.1.1）：浮点模型准备 / 模型检查 / 模型转换 / 性能评估 / 精度评估
    详细章节序（4.1.1.3 ~ 4.1.1.8）：浮点模型准备 / 验证模型 / 校准数据准备 /
                                        模型量化与编译 / 模型性能分析与调优 /
                                        模型精度分析与调优

我们采用**详细章节序**，并把 `校准数据准备` 独立成一步——官方要求它必须在
makertbin 之前完成，做成独立一步，用户才不会把校准集准备塞进"转换"按钮里。
最前面再加一步 `环境准备`（官方 workflow 图的第一步，官方注明只需做一次）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# 官方产出物（4.1.1.6.4 转换产出物解读）
ARTIFACT_ORIGINAL = "original_float_model"
ARTIFACT_OPTIMIZED = "optimized_float_model"
ARTIFACT_CALIBRATED = "calibrated_model"
ARTIFACT_QUANTIZED = "quantized_model"
ARTIFACT_BIN = "bin"

CONVERSION_ARTIFACTS: tuple[tuple[str, str], ...] = (
    (ARTIFACT_ORIGINAL, "*_original_float_model.onnx"),
    (ARTIFACT_OPTIMIZED, "*_optimized_float_model.onnx"),
    (ARTIFACT_CALIBRATED, "*_calibrated_model.onnx"),
    (ARTIFACT_QUANTIZED, "*_quantized_model.onnx"),
    (ARTIFACT_BIN, "*.bin"),
)

#: 官方要求：校准集一般从测试集里选 20~100 张
CALIB_MIN = 20
CALIB_MAX = 100

#: 官方：校准算法为 load / skip 时不需要校准数据
CALIB_TYPES_WITHOUT_DATA = ("load", "skip")


class Stage(str, Enum):
    ENV = "env"
    MODEL = "model"
    CHECK = "check"
    CALIB = "calib"
    CONVERT = "convert"
    PERF = "perf"
    ACCURACY = "accuracy"


class StageState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StageSpec:
    stage: Stage
    title: str
    official_ref: str
    goal: str
    produces: tuple[str, ...] = ()
    tool: str = ""


#: 主流程。顺序即官方章节顺序，不要随意调整。
STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        Stage.ENV,
        "环境准备",
        "环境部署（工具链首次使用做一遍）",
        "安装 Docker、导入官方 OpenExplorer 离线镜像、创建工作区。",
        ("已导入的工具链镜像", "工作区目录"),
        "docker / 本工具的镜像获取",
    ),
    StageSpec(
        Stage.MODEL,
        "浮点模型准备",
        "4.1.1.3 浮点模型准备（约束见 4.1.1.2 算子约束）",
        "导入 ONNX 浮点模型，推导输入参数，确认形状是静态的。",
        ("model/*.onnx", "input_parameters 建议值"),
        "ONNX 解析",
    ),
    StageSpec(
        Stage.CHECK,
        "模型检查",
        "4.1.1.4 验证模型",
        "确认算子与模型分段满足计算平台要求——重点是别让中间层被 CPU 算子劈开。",
        ("logs/checker.log", "节点与子图分段表"),
        "hb_mapper checker",
    ),
    StageSpec(
        Stage.CALIB,
        "校准数据准备",
        f"4.1.1.5 校准数据准备（{CALIB_MIN}~{CALIB_MAX} 张典型场景）",
        "按输入要求准备校准数据：图片或已预处理好的二进制文件。",
        ("cal_images/",),
        "内置校验器 + 预处理",
    ),
    StageSpec(
        Stage.CONVERT,
        "模型量化与编译",
        "4.1.1.6 模型量化与编译",
        "依次完成模型解析、优化、校准、量化、编译，产出可上板的模型。",
        tuple(pattern for _key, pattern in CONVERSION_ARTIFACTS),
        "hb_mapper makertbin",
    ),
    StageSpec(
        Stage.PERF,
        "性能评估",
        "4.1.1.7 模型性能分析与调优",
        "先做 hb_perf 静态分析，再上板用 hrt_model_exec perf 实测。",
        ("perf/hb_perf_result/**", "板端 latency 与 FPS"),
        "hb_perf / hrt_model_exec perf",
    ),
    StageSpec(
        Stage.ACCURACY,
        "精度评估",
        "4.1.1.8 模型精度分析与调优",
        "对比量化前后的逐层输出，必要时调校准参数，再不行转 QAT。",
        ("accuracy/**",),
        "hb_mapper infer / vec_diff",
    ),
)

STAGE_BY_KEY = {spec.stage.value: spec for spec in STAGES}

#: 官方标注为"可选"的工具，刻意不做成流程步骤，避免污染主流程
TOOLBOX_REF = "4.1.1.9 其他模型工具（可选）"
TOOLBOX_TOOLS = ("hb_pack 模型打包", "hb_model_info 模型信息查看",
                 "hb_model_modifier bin 模型节点修改")


# --------------------------------------------------------------------------
# 上下文：全部是"事实"，由磁盘探测与用户输入填充
# --------------------------------------------------------------------------
@dataclass
class CalibSummary:
    count: int = 0
    cal_data_type: str = "float32"
    preprocess_on: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.count > 0 and not self.problems


@dataclass
class RunContext:
    # --- S0 环境 ---
    docker_ok: bool = False
    image_ref: str | None = None
    image_required: bool = True

    # --- S1 模型 ---
    model_path: Path | None = None
    model_static: bool | None = None      # None = 还没解析过
    model_problems: list[str] = field(default_factory=list)

    # --- S2 检查 ---
    checker_passed: bool = False
    checker_log: Path | None = None

    # --- S3 校准数据 ---
    calib: CalibSummary | None = None
    calibration_type: str = "default"

    # --- S4 转换 ---
    artifacts: dict[str, Path] = field(default_factory=dict)
    conversion_checks: dict[str, bool] = field(default_factory=dict)
    conversion_override: bool = False     # 用户显式跳过模型检查

    # --- S5 性能 ---
    perf_sim: Path | None = None
    perf_board: Path | None = None

    # --- S6 精度 ---
    accuracy: Path | None = None

    # ------------------------------------------------------------------
    @property
    def calibration_needs_data(self) -> bool:
        return self.calibration_type not in CALIB_TYPES_WITHOUT_DATA

    @property
    def present_artifacts(self) -> set[str]:
        return set(self.artifacts)

    def artifact(self, key: str) -> Path | None:
        return self.artifacts.get(key)


@dataclass
class StageStatus:
    stage: Stage
    state: StageState
    blockers: list[str] = field(default_factory=list)   # 可操作的中文句子
    warnings: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return self.state is not StageState.BLOCKED


# --------------------------------------------------------------------------
# 评估
# --------------------------------------------------------------------------
def _env_status(ctx: RunContext) -> StageStatus:
    missing: list[str] = []
    if not ctx.docker_ok:
        missing.append("Docker 不可用 —— 去「环境准备」页按体检结果处理。")
    if ctx.image_required and not ctx.image_ref:
        missing.append("尚未导入 OpenExplorer 工具链镜像 —— 去「环境准备」页选一个下载并导入。")
    if missing:
        return StageStatus(Stage.ENV, StageState.BLOCKED, missing)
    name = ctx.image_ref or "未指定镜像"
    return StageStatus(Stage.ENV, StageState.DONE, warnings=[f"工具链镜像：{name}"])


def _model_status(ctx: RunContext) -> StageStatus:
    if ctx.model_path is None:
        return StageStatus(Stage.MODEL, StageState.PENDING, ["尚未导入 ONNX 模型。"])
    if ctx.model_problems:
        # 有硬问题就挡在转换之前，但不挡在检查之前——官方流程允许先跑 checker 看结论
        return StageStatus(Stage.MODEL, StageState.FAILED, list(ctx.model_problems))
    warn = []
    if ctx.model_static is False:
        warn.append(
            "模型含动态维度：hb_mapper infer 不支持动态 shape，"
            "请先把输入形状固定下来。"
        )
    return StageStatus(Stage.MODEL, StageState.DONE, warnings=warn)


def _check_status(ctx: RunContext) -> StageStatus:
    if ctx.model_path is None:
        return StageStatus(Stage.CHECK, StageState.BLOCKED, ["先去「浮点模型准备」导入模型。"])
    if ctx.checker_passed:
        return StageStatus(Stage.CHECK, StageState.DONE)
    return StageStatus(
        Stage.CHECK,
        StageState.PENDING,
        warnings=["检查阶段不必纠结 CPU 算子——真实开销要看「性能评估」的逐节点耗时。"],
    )


def _calib_status(ctx: RunContext) -> StageStatus:
    if not ctx.calibration_needs_data:
        return StageStatus(
            Stage.CALIB,
            StageState.SKIPPED,
            warnings=[
                f"calibration_type = {ctx.calibration_type}，官方说明此时不需要校准数据。"
            ],
        )
    if ctx.calib is None:
        return StageStatus(Stage.CALIB, StageState.PENDING, ["尚未准备校准数据。"])
    if not ctx.calib.usable:
        return StageStatus(Stage.CALIB, StageState.FAILED, list(ctx.calib.problems))
    warnings: list[str] = []
    if not (CALIB_MIN <= ctx.calib.count <= CALIB_MAX):
        warnings.append(
            f"官方建议 {CALIB_MIN}~{CALIB_MAX} 张，当前 {ctx.calib.count} 张。"
        )
    if ctx.calib.preprocess_on:
        warnings.append(
            "preprocess_on = True：官方建议保持关闭，以保证校准效果。"
        )
    return StageStatus(Stage.CALIB, StageState.DONE, warnings=warnings)


def _convert_status(ctx: RunContext) -> StageStatus:
    present = ctx.present_artifacts
    if ARTIFACT_BIN in present:
        return StageStatus(Stage.CONVERT, StageState.DONE)

    # 产物存在但没跑完三方面校验 —— 官方要求三项齐全才算转换成功
    if ctx.conversion_checks and not all(ctx.conversion_checks.values()):
        failed = [name for name, ok in ctx.conversion_checks.items() if not ok]
        return StageStatus(
            Stage.CONVERT,
            StageState.FAILED,
            [f"转换成功校验未通过：{('、'.join(failed))}。"],
        )
    if present:
        return StageStatus(
            Stage.CONVERT,
            StageState.FAILED,
            ["目录里有中间产物但没有 .bin —— 转换没走完，详见日志。"],
        )

    blockers: list[str] = []
    if not ctx.checker_passed and not ctx.conversion_override:
        blockers.append(
            "尚未通过模型检查。官方要求转换前先通过该步骤；"
            "确实要跳过，请显式勾选「跳过模型检查」。"
        )
    if ctx.calibration_needs_data and not (ctx.calib and ctx.calib.usable):
        blockers.append("校准数据未就绪。")
    if blockers:
        return StageStatus(Stage.CONVERT, StageState.BLOCKED, blockers)
    return StageStatus(Stage.CONVERT, StageState.PENDING)


def _perf_status(ctx: RunContext) -> StageStatus:
    if ARTIFACT_BIN not in ctx.present_artifacts:
        return StageStatus(Stage.PERF, StageState.BLOCKED, ["先去「模型量化与编译」产出 .bin。"])
    if ctx.perf_board:
        return StageStatus(Stage.PERF, StageState.DONE)
    if ctx.perf_sim:
        return StageStatus(
            Stage.PERF,
            StageState.DONE,
            warnings=["只做了 hb_perf 静态分析，还没上板实测。"],
        )
    return StageStatus(
        Stage.PERF,
        StageState.PENDING,
        warnings=["hb_perf 不含 CPU 部分耗时；要判断真实瓶颈必须上板实测。"],
    )


def _accuracy_status(ctx: RunContext) -> StageStatus:
    if ARTIFACT_QUANTIZED not in ctx.present_artifacts:
        return StageStatus(
            Stage.ACCURACY,
            StageState.BLOCKED,
            ["需要转换产出物 *_quantized_model.onnx 才能做精度对比。"],
        )
    if ctx.accuracy:
        return StageStatus(Stage.ACCURACY, StageState.DONE)
    warn: list[str] = []
    if ctx.calibration_type == "skip":
        warn.append(
            "当前 calibration_type = skip：官方明确说明该方式得到的模型"
            "**不可用于精度验证**。"
        )
    return StageStatus(Stage.ACCURACY, StageState.PENDING, warnings=warn)


_EVALUATORS = {
    Stage.ENV: _env_status,
    Stage.MODEL: _model_status,
    Stage.CHECK: _check_status,
    Stage.CALIB: _calib_status,
    Stage.CONVERT: _convert_status,
    Stage.PERF: _perf_status,
    Stage.ACCURACY: _accuracy_status,
}


def evaluate(
    ctx: RunContext,
    *,
    current: Stage | None = None,
    mark_current: bool = True,
) -> list[StageStatus]:
    """算出每一步的状态。纯函数。

    `mark_current=False` 表示"我只想知道状态，不要替我挑当前步骤"——单测与
    CLI 用这个；UI 保留默认行为，把用户正在看的那一步点亮。

    `current` 用来把某一步标成 ACTIVE；不传则自动取第一个尚未完成的步骤。
    """
    statuses = [evaluator(ctx) for evaluator in _EVALUATORS.values()]
    if not mark_current:
        return statuses

    if current is not None:
        for status in statuses:
            if status.stage is current and status.state in (
                StageState.PENDING,
                StageState.ACTIVE,
            ):
                status.state = StageState.ACTIVE
                break
    else:
        for status in statuses:
            if status.state in (StageState.PENDING, StageState.FAILED):
                status.state = StageState.ACTIVE
                break
    return statuses


def next_stage(statuses: list[StageStatus]) -> Stage | None:
    """下一个可执行的步骤——用于"下一步"按钮。"""
    for status in statuses:
        if status.state in (StageState.PENDING, StageState.FAILED, StageState.ACTIVE):
            return status.stage
    return None


def blocked_reasons(statuses: list[StageStatus]) -> dict[str, list[str]]:
    """只收**真正被阻塞**的步骤及其原因。

    注意 `blockers` 字段在 PENDING 上也有内容——那里装的是"下一步该干什么"的
    引导语（例如"尚未导入模型"），不是"被挡住"的理由。两者语义不同，所以这里
    按状态过滤，而不是按 blockers 是否为空。
    """
    return {
        status.stage.value: list(status.blockers)
        for status in statuses
        if status.state is StageState.BLOCKED
    }
