# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""模型量化与编译（S4）的**宿主侧策略**。

职责划分（和 `calib.py` 同一条边界）：

* **容器只负责跑** `hb_mapper makertbin`，并把 stdout 流回来给用户看。
* **这里负责**：写 `config.yaml`、找产物、从**日志文件**里解析结果、
  判定"这次转换算不算成功"。

## 为什么解析读日志文件而不是 stdout

实测（见 [`docs/design/makertbin-runtime.md`](../../../docs/design/makertbin-runtime.md)）：
**stdout 会在 120 列处硬折行，而 `hb_mapper_makertbin.log` 不会。**
同一轮同一个节点表，stdout 601 行、日志文件 352 行 —— 折行把表格切成了
两行一条。按行解析 stdout 会直接失败。

节点表的格式和 `hb_mapper checker` 的**完全一样**，所以这里复用
`logparse.parse_checker`，不另写一个解析器。

## config.yaml 为什么手写

结构固定（四个 section、约 20 个标量），所有值都是 ASCII 标量，quoting 规则
可控。手写让 `core/` 保持**零依赖**（`pyproject.toml` 里 `dependencies = []`
到目前为止是真的）。测试用 PyYAML 解析我们写出的文本并断言结构 ——
断的是**含义**不是格式。

字段名与官方 schema 逐字对应，来源见
[`docs/design/config-yaml.md`](../../../docs/design/config-yaml.md)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from .logparse import CheckerNode, parse_checker, strip_timestamp_prefix
from .workflow import (
    ARTIFACT_BIN,
    ARTIFACT_CALIBRATED,
    ARTIFACT_OPTIMIZED,
    ARTIFACT_ORIGINAL,
    ARTIFACT_QUANTIZED,
    CALIB_TYPES_WITHOUT_DATA,
    CONVERSION_ARTIFACTS,
)

#: makertbin 把完整日志写在这里（相对 workspace）。
MAKERTBIN_LOG_NAME = "hb_mapper_makertbin.log"

#: 成功字样 —— 日志的**最后两行**，都是 INFO 级（实测）。
SUCCESS_LINE = "Convert to runtime bin file successfully!"
END_LINE = "End Model Convert"

#: 三项校验的名字。**与 `tests/test_workflow.py` 里用的字面量一致** ——
#: 那边是按名字断言 blockers 的，改名会让那条测试失去意义。
CHECK_CONSOLE = "控制台成功字样"
CHECK_SIMILARITY = "余弦相似度表"
CHECK_ARTIFACTS = "5 个产出物"

#: 单引号标量里放不了换行，遇到就是调用方的 bug
_FORBIDDEN = ("\n", "\r")

#: `ERROR` 作为**独立词**出现才算。用 `\w` 而不是 `[A-Za-z]`：
#: 下划线和数字也算词的一部分，否则 `NO_ERROR_here` 会被误判成错误
#: （实测：写成 `[A-Za-z]` 时它匹配上了）。
#: 反过来 `ERROR-OCCUR-DURING` 里 `ERROR` 后面是连字符，仍然算独立词 ✓
_LEVEL_ERROR = re.compile(r"(?<!\w)ERROR(?!\w)")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConversionSpec:
    """`config.yaml` 需要的一切。

    路径类字段一律是**容器内相对路径**（例如 `./model/x.onnx`）——
    由 `Workspace.rel()` 算好再传进来，这样 `render_config` 是纯函数。
    """

    onnx_model: str
    output_prefix: str
    march: str                      # X5 是 bayes-e；不显式给会用到 J5 的 bayes
    input_name: str
    input_shape: str                # ASCII x
    input_type_train: str           # 训练时的通道顺序，**推不出来**，用户确认
    input_layout_train: str         # 同上
    input_type_rt: str = "nv12"
    input_source: str = "pyramid"
    calibration_type: str = "default"
    cal_data_dir: str = ""
    cal_data_type: str = "float32"
    preprocess_on: bool = False
    #: ⚠️ 官方默认是 `./model_output`，**不是** `./`。不写成工作区的 output/
    #: 的话我们的产物发现扫不到。
    working_dir: str = "./output"
    compile_mode: str = "latency"
    optimize_level: str = "O3"

    @property
    def needs_calibration_data(self) -> bool:
        return self.calibration_type not in CALIB_TYPES_WITHOUT_DATA


def _quote(value: object) -> str:
    """YAML 单引号标量。**一律加引号** —— 值里有 `:`、`#`、前导/尾随空格都不出事。

    单引号里的 `'` 写成 `''`（YAML 的规定）。换行表示不了，直接拒。
    """
    text = str(value)
    for bad in _FORBIDDEN:
        if bad in text:
            raise ValueError(f"config.yaml 的值里不能有换行符：{text!r}")
    return "'" + text.replace("'", "''") + "'"


def _flag(value: bool) -> str:
    """小写布尔 —— 与官方模板 `fast_perf_template.yaml` 的写法一致。"""
    return "true" if value else "false"


def render_config(spec: ConversionSpec) -> str:
    """生成 `config.yaml` 的文本。纯函数，不碰文件系统。

    两个刻意的取舍（都有实测依据）：

    * **不写 `input_layout_rt`** —— `input_type_rt = nv12` 时写了会打
      WARNING「is not needed」（nv12 自带排布）。
    * **`input_source` 写成按输入名映射的 dict** —— schema 声明的是
      `Or(str, dict)`，但运行期只认 dict，写字符串直接解析失败。
    """
    lines: list[str] = []

    lines.append("model_parameters:")
    lines.append(f"  onnx_model: {_quote(spec.onnx_model)}")
    lines.append(f"  march: {_quote(spec.march)}")
    lines.append(f"  working_dir: {_quote(spec.working_dir)}")
    lines.append(f"  output_model_file_prefix: {_quote(spec.output_prefix)}")
    lines.append("")

    lines.append("input_parameters:")
    lines.append(f"  input_name: {_quote(spec.input_name)}")
    lines.append(f"  input_type_rt: {_quote(spec.input_type_rt)}")
    lines.append(f"  input_type_train: {_quote(spec.input_type_train)}")
    lines.append(f"  input_layout_train: {_quote(spec.input_layout_train)}")
    lines.append(f"  input_shape: {_quote(spec.input_shape)}")
    # 校准数据是训练格式的原始像素，归一化由工具插进模型里，所以不做预处理
    lines.append("  norm_type: 'no_preprocess'")
    lines.append("")

    lines.append("calibration_parameters:")
    # 官方 schema 里 cal_data_dir 是 And(str, len) —— 空字符串会被拒
    if spec.cal_data_dir:
        lines.append(f"  cal_data_dir: {_quote(spec.cal_data_dir)}")
    lines.append(f"  cal_data_type: {_quote(spec.cal_data_type)}")
    lines.append(f"  calibration_type: {_quote(spec.calibration_type)}")
    lines.append(f"  preprocess_on: {_flag(spec.preprocess_on)}")
    lines.append("")

    lines.append("compiler_parameters:")
    lines.append(f"  compile_mode: {_quote(spec.compile_mode)}")
    lines.append(f"  optimize_level: {_quote(spec.optimize_level)}")
    lines.append("  debug: false")
    lines.append("  input_source:")
    lines.append(f"    {_quote(spec.input_name)}: {_quote(spec.input_source)}")
    lines.append("")

    return "\n".join(lines)


def write_config(path: Path, spec: ConversionSpec) -> Path:
    """把配置写到 `path`（通常是 `Workspace.config_path`）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(spec), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
def clear_output(output_dir: Path) -> int:
    """清空 `output/`，返回删掉的文件数。

    **每次转换前必须做。** 实测：makertbin 失败时也会往 `output/` 里写东西
    （`shape_inference_fail.onnx`），留着会让上一次的残留把「5 个产出物齐全」
    喂成假通过。
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return 0
    removed = 0
    for item in output_dir.iterdir():
        if item.is_file():
            item.unlink()
            removed += 1
        elif item.is_dir():
            # 产物都是文件；空目录也顺手清掉，非空就交给用户
            try:
                item.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


def discover_artifacts(output_dir: Path) -> dict[str, Path]:
    """按 `CONVERSION_ARTIFACTS` 的五个模式找产物。

    每个模式取**最新**的那个。找不到的模式不出现在结果里 —— 让调用方能区分
    "跑到一半"（部分产物）和"什么都没产出"。
    """
    found: dict[str, Path] = {}
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return found

    for key, pattern in CONVERSION_ARTIFACTS:
        matches = [p for p in output_dir.glob(pattern) if p.is_file() and p.stat().st_size > 0]
        if not matches:
            continue
        found[key] = max(matches, key=lambda p: p.stat().st_mtime)
    return found


def missing_artifacts(found: dict[str, Path]) -> list[str]:
    """还缺哪几个，返回模式的清单（给用户看的是人话，见 `_artifact_label`）。"""
    return [pattern for key, pattern in CONVERSION_ARTIFACTS if key not in found]


def _artifact_label(key: str) -> str:
    return {
        ARTIFACT_ORIGINAL: "原始浮点模型",
        ARTIFACT_OPTIMIZED: "优化后浮点模型",
        ARTIFACT_CALIBRATED: "校准后模型",
        ARTIFACT_QUANTIZED: "量化后模型",
        ARTIFACT_BIN: "上板可执行文件（.bin）",
    }.get(key, key)


# --------------------------------------------------------------------------
# 日志解析
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MakertbinReport:
    """从 makertbin 的**日志文件**里读出来的事实。"""

    saw_success: bool
    saw_end: bool
    saw_node_table: bool
    nodes: tuple[CheckerNode, ...]
    #: 日志里出现的 ERROR 行（原文），给用户看
    errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.saw_success and self.saw_end

    @property
    def rated_nodes(self) -> tuple[CheckerNode, ...]:
        """**有相似度数值**的节点。`--` 的不算 —— 那不是 0，是没算过。"""
        return tuple(n for n in self.nodes if n.similarity is not None)


def parse_makertbin(lines: Iterable[str]) -> MakertbinReport:
    """解析 makertbin 的日志。

    节点表复用 `parse_checker` —— 两个命令打的是**同一张表**。
    解析不出表格不算错（`calibration_type: skip` 下表格仍然会打，
    只是相似度列全是 `--`）。
    """
    raw = [str(line) for line in lines]

    report = parse_checker(raw)
    nodes = report.nodes if report is not None else ()
    saw_table = report is not None and report.saw_node_table

    # ⚠️ 判断级别要用 `strip_timestamp_prefix`，**不能用 `strip_log_prefix`** ——
    # 后者连 `ERROR` 这个级别标记一起剥掉，于是"日志里有 ERROR 吗"永远答否。
    # `logparse` 的注释专门警告过这一点，我第一版正是踩了它。
    leveled = [strip_timestamp_prefix(line).strip() for line in raw]
    errors = tuple(text for text in leveled if _LEVEL_ERROR.search(text))

    # `End Model Convert` 只看尾部：扫全长会在"中途重试后成功"的场景里
    # 把中间那次失败的痕迹读成成功
    tail = leveled[-8:]
    return MakertbinReport(
        # 成功字样是消息原文，直接在全长里找
        saw_success=any(SUCCESS_LINE in line for line in raw),
        saw_end=any(END_LINE in line for line in tail),
        saw_node_table=saw_table,
        nodes=tuple(nodes),
        errors=errors,
    )


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConversionVerdict:
    checks: dict[str, bool]
    artifacts: dict[str, Path]
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return all(self.checks.values()) if self.checks else False


def evaluate_conversion(
    report: MakertbinReport | None,
    artifacts: dict[str, Path],
    *,
    spec: ConversionSpec | None = None,
    returncode: int | None = None,
) -> ConversionVerdict:
    """把「日志 + 产物」变成三项校验 + 给用户看的结论。

    `report` 为 None 表示日志没读到（文件不在、或读失败）——那时
    「控制台成功字样」和「余弦相似度表」都判 False，**不猜**。
    """
    findings: list[Finding] = []

    console_ok = bool(report and report.succeeded)
    similarity_ok = bool(report and report.saw_node_table)
    artifacts_ok = not missing_artifacts(artifacts)

    checks = {
        CHECK_CONSOLE: console_ok,
        CHECK_SIMILARITY: similarity_ok,
        CHECK_ARTIFACTS: artifacts_ok,
    }

    # ---- 控制台 ----
    if report is None:
        findings.append(Finding(
            LEVEL_ERROR, "CONVERT_NO_LOG",
            "没读到 makertbin 的日志，无法判定转换结果",
            f"预期文件：{MAKERTBIN_LOG_NAME}（在工作区根目录）。",
        ))
    elif console_ok:
        findings.append(Finding(
            LEVEL_OK, "CONVERT_CONSOLE_OK", "控制台出现官方成功字样"
        ))
    else:
        findings.append(Finding(
            LEVEL_ERROR, "CONVERT_CONSOLE_MISSING",
            "日志里没有出现官方成功字样，转换没有跑完",
            "看原始日志定位第一个 ERROR；常见原因是模型输入类型与工具链不符。",
        ))

    # ---- ERROR 行 ----
    for index, text in enumerate((report.errors if report else ())[:5]):
        findings.append(Finding(
            LEVEL_ERROR, f"CONVERT_LOG_ERROR_{index}", text
        ))

    # ---- 产物 ----
    missing = missing_artifacts(artifacts)
    if artifacts_ok:
        findings.append(Finding(
            LEVEL_OK, "CONVERT_ARTIFACTS_OK",
            "5 个产出物齐全",
            "、".join(_artifact_label(key) for key, _pattern in CONVERSION_ARTIFACTS),
        ))
    elif artifacts:
        findings.append(Finding(
            LEVEL_ERROR, "CONVERT_ARTIFACTS_PARTIAL",
            f"只产出 {len(artifacts)}/5 个：缺 {'、'.join(missing)}",
            "转换没走完。看日志里第一个 ERROR。",
        ))
    else:
        findings.append(Finding(
            LEVEL_ERROR, "CONVERT_ARTIFACTS_NONE",
            "output/ 里没有任何产物",
        ))

    # ---- 相似度 ----
    if report is None:
        pass
    elif not report.saw_node_table:
        findings.append(Finding(
            LEVEL_WARN, "CONVERT_NO_SIMILARITY_TABLE",
            "日志里没有找到逐节点信息表",
            "格式可能变了；这一项无法核对，但产物本身仍可用。",
        ))
    else:
        rated = report.rated_nodes
        if rated:
            worst = sorted(rated, key=lambda n: n.similarity or 0.0)[:3]
            findings.append(Finding(
                LEVEL_OK, "CONVERT_SIMILARITY",
                "相似度最低的 3 个节点：" + "、".join(
                    f"{n.name}（{n.cosine}）" for n in worst
                ),
                # **刻意不给"低于多少算不合格"的阈值**：官方那道线是自动搜索的
                # 内部目标（similarity: 0.995），不是逐节点的验收标准。编一个
                # 阈值会让用户去追一个没有依据的数字。
                "本工具不设逐节点阈值；请对照官方对同类模型的经验值判断。",
            ))
        else:
            # calibration_type 是 load / skip 时相似度列**全是 `--`**（实测 196/196）。
            # 这是正常结果，不是失败 —— 表存在就算这一项通过。
            mode = spec.calibration_type if spec else "load / skip"
            findings.append(Finding(
                LEVEL_OK, "CONVERT_SIMILARITY_SKIPPED",
                f"节点表已解析，但相似度列全是 --（calibration_type = {mode}）",
                "这两种方式不做校准，官方也不会计算相似度。",
            ))

    # ---- 中途失败时把退出码也说出来 ----
    if returncode not in (None, 0) and not console_ok:
        findings.append(Finding(
            LEVEL_ERROR, "CONVERT_RC",
            f"hb_mapper 退出码 {returncode}",
        ))

    return ConversionVerdict(checks=checks, artifacts=dict(artifacts),
                            findings=tuple(findings))


def read_log(path: Path) -> list[str]:
    """读日志文件。**显式 utf-8**（容器侧约定，见 AGENTS.md 第 14 条）。

    读失败返回空列表 —— 调用方按 `report is None` 处理，而不是抛异常。
    """
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def summarize_artifacts(artifacts: dict[str, Path]) -> Sequence[tuple[str, str, int]]:
    """`(显示名, 文件名, 字节数)`，给页面的表格用。顺序与官方流程一致。"""
    rows = []
    for key, _pattern in CONVERSION_ARTIFACTS:
        path = artifacts.get(key)
        if path is None:
            rows.append((_artifact_label(key), "（缺失）", 0))
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rows.append((_artifact_label(key), path.name, size))
    return tuple(rows)
