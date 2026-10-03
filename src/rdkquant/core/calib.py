# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准数据的**宿主侧策略**。

职责划分（这条边界是刻意的）：

* **容器里的探针只报事实**——张数、分辨率分布、曝光统计、重复、文件字节数。
  它不知道 `preprocess_on` 是开是关，所以不该做依赖配置的判断。
* **这里做策略判断**——"分辨率不一致算不算问题"取决于 `preprocess_on`，
  "20~100 张"是官方建议区间，这些都是宿主侧的用户选择与产品口径。

所以本模块的输入是探针的 JSON，输出是 `CalibSummary`（喂给流程门禁）加一串
`Finding`（喂给页面）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from .workflow import CALIB_MAX, CALIB_MIN, CALIB_TYPES_WITHOUT_DATA, CalibSummary

#: 官方启发式：目录名以 `_f32` 结尾按 float32 读，否则按 uint8。
#: 我们**始终显式写出 cal_data_type**，不依赖它；识别出来只是为了提示用户。
FLOAT32_SUFFIX = "_f32"


@dataclass(frozen=True)
class CalibBuildSpec:
    """生成校准数据的参数。

    默认**不做归一化**：官方把 `norm_type` 的均值/缩放插入到模型里，校准数据
    应当是 `input_type_train` 格式的原始像素。这一条已被真机实验确认
    （见 docs/design/container-findings.md）。mean/scale 只在显式给出时应用。
    """

    shape: str                       # 例如 "1x3x640x640"
    layout: str = "NCHW"
    color: str = "bgr"
    dtype: str = "float32"
    mean: tuple[float, ...] | None = None
    scale: tuple[float, ...] | None = None
    #: 从源目录里**均匀采样**多少张；0 表示全部用上。
    #: 官方建议校准集 20~100 张，而实际数据集动辄几千张，采样是必须的。
    count: int = 0
    #: 明确指定要用哪些文件（筛选手动勾选的结果）。**非空时优先于 `count`** ——
    #: 那是用户明确的意图，而 count 只是自动起点。
    only: tuple[str, ...] = ()

    @property
    def output_dir_name(self) -> str:
        """目录名带上官方后缀，让工具的后缀启发式与我们的显式设置一致，避免告警。"""
        return "calib_bin_f32" if self.dtype == "float32" else "calib_bin_u8"


@dataclass(frozen=True)
class CalibVerdict:
    summary: CalibSummary
    findings: tuple[Finding, ...]

    @property
    def blocked(self) -> bool:
        return any(f.level == LEVEL_ERROR for f in self.findings)


def infer_cal_data_type(directory_name: str) -> str:
    """按官方后缀启发式推断 `cal_data_type`。"""
    return "float32" if directory_name.rstrip("/").endswith(FLOAT32_SUFFIX) else "uint8"


def parse_input_shape(text: str | None) -> tuple[int, ...] | None:
    """把 `1x3x640x640` 解析成 `(1, 3, 640, 640)`。解析不了返回 None。"""
    if not text:
        return None
    dims = []
    for chunk in str(text).lower().replace(",", "x").split("x"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            dims.append(int(chunk))
        except ValueError:
            return None
    return tuple(dims) or None


def expected_hw_from_shape(shape_text: str | None) -> tuple[int, int] | None:
    """从 `input_shape` 推出 (H, W)，**推不出来就返回 None**。

    刻意保守：布局（NCHW / NHWC）是从形状猜的启发式，训练时的真实布局只存在于
    训练脚本里。所以只在**方形输入**下给出结论——那时两种布局推出同一对 (H, W)：

        NCHW 1x3x640x640  → dims[2] == dims[3] == 640
        NHWC 1x640x640x3  → dims[1] == dims[2] == 640

    非方形输入直接放弃比对。**宁可不报，也不误报**——一个假的"尺寸不符"会让
    用户去改本来正确的数据。
    """
    dims = parse_input_shape(shape_text)
    if dims is None or len(dims) != 4:
        return None
    if dims[2] == dims[3]:
        return dims[2], dims[3]
    if dims[1] == dims[2]:
        return dims[1], dims[2]
    return None


def _describe_resolutions(resolutions, limit: int = 5) -> str:
    """把分辨率分布说成人话。

    **只列前几个**：实测过一个数据集有 1474 种分辨率，全列出来这条结论上万字符，
    会把表格撑爆，而且对用户毫无帮助——他需要的是"多少种"和"最典型的几个"。
    """
    if not resolutions:
        return "（无）"
    shown = "、".join(f"{w}×{h}({n} 张)" for w, h, n in resolutions[:limit])
    if len(resolutions) > limit:
        shown += f"…等 {len(resolutions)} 种"
    return shown


def _resolutions(payload: dict) -> list[tuple[int, int, int]]:
    out = []
    for item in payload.get("resolutions") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        w, h, n = item[0], item[1], item[2]
        if w and h:                      # 读不出尺寸的（0/None）不参与判断
            out.append((int(w), int(h), int(n)))
    return out


def evaluate_probe(
    payload: dict,
    *,
    preprocess_on: bool = False,
    calibration_type: str = "default",
    expected_hw: tuple[int, int] | None = None,
    directory: str = "",
) -> CalibVerdict:
    """把探针的 JSON 变成门禁用的摘要 + 给用户看的结论。

    `expected_hw` 由调用方按**已知的** `input_layout_train` 算好再传进来——
    布局是用户在 S4 选的，本模块不猜。
    """
    findings: list[Finding] = []
    problems: list[str] = []
    name = directory.rstrip("/").rsplit("/", 1)[-1] if directory else ""
    directory_name = name or str(payload.get("directory") or "")

    # ---- 探针自己没跑通：直接透传，不做任何进一步解释 ----
    if not payload.get("ok"):
        message = str(payload.get("error") or "校准数据扫描失败")
        problems.append(message)
        return CalibVerdict(
            summary=CalibSummary(
                count=int(payload.get("count") or 0),
                preprocess_on=preprocess_on,
                problems=problems,
                cal_data_dir="",
            ),
            findings=(Finding(LEVEL_ERROR, "CALIB_SCAN_FAILED", message),),
        )

    count = int(payload.get("count") or 0)
    cal_data_type = infer_cal_data_type(directory_name)

    # ---- 官方说明：load / skip 这两种方式不需要校准数据 ----
    if calibration_type in CALIB_TYPES_WITHOUT_DATA:
        findings.append(Finding(
            LEVEL_OK, "CALIB_NOT_NEEDED",
            f"calibration_type = {calibration_type}，官方说明此时不需要校准数据",
            "可以直接进入模型转换。",
        ))
        return CalibVerdict(
            summary=CalibSummary(
                count=count, cal_data_type=cal_data_type, preprocess_on=preprocess_on,
                problems=[], cal_data_dir=directory_name,
            ),
            findings=tuple(findings),
        )

    # ---- 透传探针报的问题与警告（它们与环境无关，探针判断得对）----
    for index, text in enumerate(payload.get("problems") or []):
        problems.append(str(text))
        findings.append(Finding(LEVEL_ERROR, f"CALIB_PROBLEM_{index}", str(text)))
    for index, text in enumerate(payload.get("warnings") or []):
        findings.append(Finding(LEVEL_WARN, f"CALIB_WARNING_{index}", str(text)))

    if count == 0:
        message = "校准目录里没有可用的数据"
        problems.append(message)
        findings.append(Finding(
            LEVEL_ERROR, "CALIB_EMPTY", message,
            "放 20~100 张有代表性的图片，或者把 calibration_type 改成 load / skip。",
        ))

    # ---- 张数：官方建议区间 ----
    if count and count < CALIB_MIN:
        findings.append(Finding(
            LEVEL_WARN, "CALIB_COUNT_LOW",
            f"只有 {count} 张，少于官方建议的 {CALIB_MIN} 张",
            "样本太少会让量化阈值偏斜，建议补齐到 20 张以上。",
        ))
    elif count > CALIB_MAX:
        findings.append(Finding(
            LEVEL_WARN, "CALIB_COUNT_HIGH",
            f"有 {count} 张，多于官方建议的 {CALIB_MAX} 张",
            "更多样本通常收益有限却显著变慢，建议筛到 100 张以内。",
        ))

    # ---- 图片与二进制混放：语义歧义 ----
    image_like = int(payload.get("image_like") or 0)
    binary_like = int(payload.get("binary_like") or 0)
    if image_like and binary_like:
        message = (f"目录里既有图片（{image_like}）又有二进制数据（{binary_like}），"
                   "工具无法判断该按哪种读")
        problems.append(message)
        findings.append(Finding(
            LEVEL_ERROR, "CALIB_MIXED_KINDS", message,
            "把图片和二进制分到两个目录，只让 cal_data_dir 指向其中一个。",
        ))

    # ---- 分辨率：严重程度取决于 preprocess_on ----
    resolutions = _resolutions(payload)
    if len(resolutions) > 1:
        described = _describe_resolutions(resolutions)
        if preprocess_on:
            findings.append(Finding(
                LEVEL_WARN, "CALIB_RESOLUTION_MIXED",
                f"存在 {len(resolutions)} 种分辨率：{described}",
                "preprocess_on=True 时工具会自己 resize，但官方建议保持关闭以获得"
                "更好的校准效果。",
            ))
        else:
            message = (f"存在 {len(resolutions)} 种分辨率：{described}，"
                       "而 preprocess_on=False 时工具不会替你 resize")
            problems.append(message)
            findings.append(Finding(
                LEVEL_ERROR, "CALIB_RESOLUTION_MISMATCH", message,
                "把所有图片统一到模型的输入尺寸，或者打开 preprocess_on 让工具处理"
                "（官方建议前者）。",
            ))

    # ---- 与模型输入尺寸比对（仅在调用方给出了确定尺寸时）----
    if expected_hw and not preprocess_on and resolutions:
        want_h, want_w = expected_hw
        wrong = [(w, h, n) for w, h, n in resolutions if (w, h) != (want_w, want_h)]
        if wrong:
            described = _describe_resolutions(wrong)
            message = (f"有图片的尺寸不是模型输入的 {want_w}×{want_h}：{described}")
            problems.append(message)
            findings.append(Finding(
                LEVEL_ERROR, "CALIB_SIZE_MISMATCH", message,
                "preprocess_on=False 时校准数据必须已经是模型输入尺寸。",
            ))

    # ---- 二进制数据的字节数（探针已按形状核对过，这里只做提示）----
    if binary_like and not payload.get("measured"):
        findings.append(Finding(
            LEVEL_OK, "CALIB_BINARY_OK",
            f"检测到 {binary_like} 个二进制校准样本",
            "二进制数据不做图像层面的检查，字节数由扫描阶段核对。",
        ))

    # ---- 后缀启发式提示 ----
    if cal_data_type == "float32":
        findings.append(Finding(
            LEVEL_OK, "CALIB_SUFFIX_F32",
            "目录名以 _f32 结尾，官方会按 float32 读取",
            "我们仍会显式写出 cal_data_type，不依赖这个后缀。",
        ))

    if not findings:
        findings.append(Finding(
            LEVEL_OK, "CALIB_OK", f"校准数据看起来没问题（{count} 张）"
        ))

    return CalibVerdict(
        summary=CalibSummary(
            count=count,
            cal_data_type=cal_data_type,
            preprocess_on=preprocess_on,
            problems=problems,
            cal_data_dir=directory_name,
        ),
        findings=tuple(findings),
    )
