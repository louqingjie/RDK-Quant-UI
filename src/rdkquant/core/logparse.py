# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""工具链日志 → 结构化数据。

⚠️ **`hb_mapper checker` 的真实输出格式尚未验证过。** 计划期间官方文档站点
DNS 解析异常，那一页取不到，测试里的样例文本是自己构造的。所以这里刻意写成
**容错语法**：不假设列数、不假设行序、不假设前缀形式，解析不出来就返回 None
让上层把原始文本展示给用户。

M3 的第一个真机步骤就是用导入 OE 镜像后的真实输出替换掉这些样例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 控制台输出带 ANSI 颜色（日志文件里没有）。不剥掉的话 `\x1b[92mINFO\x1b[0m` 会污染解析。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: 真实前缀带毫秒时间戳：`2026-10-02 19:41:44,845 ...`
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d+\s+")

#: 日志文件里时间戳之后还有一段来源信息：`file: x.py func: y line No: 72 `
#: 这一段是**真实抓到的**（hb_mapper_checker.log），不是我猜的。
_FILE_PREFIX_RE = re.compile(r"^file:.*?line No:\s*\d+\s+")

#: 行首的日志级别标记：`[INFO]` / `INFO:` / `INFO ` 等
_PREFIX_RE = re.compile(
    r"^\s*(?:\[[A-Za-z]+\]|(?:DEBUG|INFO|WARN|WARNING|ERROR|SUCCESS|FAIL|OK)\b:?)[\s:]*",
    re.IGNORECASE,
)

#: 行尾的内联注释，例如 `CPU     <-- 回落到 CPU，可能影响性能`
_INLINE_COMMENT_RE = re.compile(r"\s*(?:<--|//|#).*$")

#: 表头里可能出现的列名（全部小写比对）
_COLUMN_ALIASES = {
    "node": "name",
    "name": "name",
    "on": "on",
    "original": "on",
    "subgraph": "subgraph",
    "sub": "subgraph",
    "type": "type",
    "op": "type",
    "device": "device",
    "target": "device",
    "cosine": "cosine",
    "similarity": "cosine",
    "threshold": "threshold",
}


#: 节点名里不该出现中日韩字符——ONNX / hb_mapper 的节点名都是 ASCII 标识符。
#: 这条是**启发式**，用来滤掉表格后面的中文散文（"检测到 2 个算子…"）。
#: 等抓到真实输出后可以按实际格式收紧或去掉。
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def strip_log_prefix(line: str) -> str:
    """剥掉日志前缀，可能有多层。

    真实格式（实测自 hb_mapper_checker.log）：

        2026-10-02 19:41:44,845 file: print_info_dict.py func: print_info_dict line No: 72 The main quantized ...

    控制台上则是另一种（带 ANSI 颜色）：

        2026-10-02 19:41:44,543 \\x1b[92mINFO\\x1b[0m Start hb_mapper....

    两种都要能处理，所以这里是"循环剥到不动为止"而不是单条正则。
    """
    result = _ANSI_RE.sub("", line.rstrip("\r\n"))
    for _ in range(4):
        before = result
        result = _TIMESTAMP_RE.sub("", result, count=1)
        result = _FILE_PREFIX_RE.sub("", result, count=1)
        result = _PREFIX_RE.sub("", result, count=1)
        result = result.lstrip()
        if result == before:
            break
    return result


def _clean_cell(text: str) -> str:
    return _INLINE_COMMENT_RE.sub("", text).strip()


def _split_columns(line: str) -> list[str]:
    """按空白分列。全角空格也算分隔符（中文文档里的表格常见）。"""
    return [cell for cell in re.split(r"[ \t\u3000]+", line.strip()) if cell]


def _looks_like_header(cells: Sequence[str]) -> bool:
    lowered = [cell.lower().strip(":") for cell in cells]
    if "node" not in lowered:
        return False
    return any(name in lowered for name in ("device", "type", "subgraph", "on"))


def _map_header(cells: Sequence[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        key = _COLUMN_ALIASES.get(cell.lower().strip(":"))
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def _normalize_device(text: str) -> str:
    upper = text.upper()
    if "BPU" in upper:
        return "BPU"
    if "CPU" in upper:
        return "CPU"
    return ""


@dataclass(frozen=True)
class CheckerNode:
    name: str
    on: str = ""
    subgraph: str = ""
    type: str = ""
    device: str = ""

    @property
    def on_cpu(self) -> bool:
        return self.device == "CPU"

    @property
    def on_bpu(self) -> bool:
        return self.device == "BPU"


@dataclass(frozen=True)
class CheckerReport:
    nodes: tuple[CheckerNode, ...]
    subgraph_count: int
    cpu_nodes: tuple[str, ...]
    saw_node_table: bool
    warnings: tuple[str, ...] = ()

    @property
    def bpu_count(self) -> int:
        return sum(1 for node in self.nodes if node.on_bpu)

    @property
    def cpu_count(self) -> int:
        return sum(1 for node in self.nodes if node.on_cpu)


def parse_checker(lines: Iterable[str]) -> CheckerReport | None:
    """从 `hb_mapper checker` 的输出里解析出节点表。

    解析不出来返回 **None**（不是空报告、也不是抛异常）——这样上层能区分
    "格式变了，请看原始日志" 与 "解析成功但一个节点都没有"。
    """
    cleaned: list[str] = []
    for raw in lines:
        text = strip_log_prefix(str(raw)).strip()
        if text:
            cleaned.append(text)
    if not cleaned:
        return None

    warnings: list[str] = []
    header_map: dict[str, int] | None = None
    header_width = 0
    lower_bound = 0
    nodes: list[CheckerNode] = []

    for raw in cleaned:
        text = _INLINE_COMMENT_RE.sub("", raw).strip()
        cells = _split_columns(text)

        if header_map is None:
            if _looks_like_header(cells):
                header_map = _map_header(cells)
                header_width = len(cells)
                lower_bound = min(3, header_width)
                # 设备列的位置，按可靠性依次回退：
                #   1) 显式的 Device 列
                #   2) **真实格式里 `ON` 列装的就是 BPU/CPU**（实测自 X5 容器的
                #      `Node ON Subgraph Type Cosine Similarity Threshold DataType`
                #      —— 官方口径里 ON 表示"算子是否跑在 BPU 上"）
                #   3) 最后一列
                if "device" not in header_map:
                    header_map["device"] = header_map.get("on", header_width - 1)
            continue

        # 表头之后只认"列数落在合理区间"的行。
        #
        # 为什么是区间而不是精确相等：表头可能有**多词列名**，例如
        # `Cosine Similarity` 会被切成两个 token，于是表头 7 列而数据行只有 6 列。
        # 精确匹配会把整张表判死（实测踩到）。
        #
        # 为什么下界是 3：日志尾声的 `[SUCCESS] checker 通过` 只有 2 个 token，
        # 不设下界会被当成一个名叫 "checker" 的节点。
        #
        # 为什么还要查首列有没有中日韩字符：这是给上界兜底——中文散文的 token
        # 数有可能恰好落进区间。节点名都是 ASCII，这条能干净地滤掉它们。
        within = lower_bound <= len(cells) <= header_width
        separator = bool(cells[0]) and set(cells[0]) <= set("-=+ ")
        if not within or separator or _has_cjk(cells[0]):
            if nodes:
                break        # 表格已经结束，后面是尾声日志
            continue         # 还没开始，前面是前言
        if cells[0].lower() in ("node", "total", "summary"):
            continue

        device_index = header_map["device"]
        device = _normalize_device(cells[device_index] if device_index < len(cells) else "")
        if not device:
            # **每一行的设备列必然是可识别的 BPU/CPU**（真实格式如此）。
            # 这条比列数区间更硬，干净地滤掉了表格后面的
            # `The quantized model output:` —— 它的列数恰好也落进区间。
            if nodes:
                break
            continue

        def cell(key: str) -> str:
            index = header_map.get(key)
            if index is None or index >= len(cells):
                return ""
            return _clean_cell(cells[index])

        node = CheckerNode(
            name=_clean_cell(cells[header_map.get("name", 0)]),
            on=cell("on"),
            subgraph=cell("subgraph"),
            type=cell("type"),
            device=device,
        )
        if node.name:
            nodes.append(node)

    if header_map is None or not nodes:
        return None

    subgraph_values = {node.subgraph for node in nodes if node.subgraph}
    subgraph_count = len(subgraph_values) if subgraph_values else 1

    cpu_nodes = tuple(node.name for node in nodes if node.on_cpu)
    if cpu_nodes:
        # 官方口径：检查阶段不必过度关注 CPU 算子，真实开销要看性能评估
        warnings.append(
            "有 %d 个算子回落 CPU（%s）；检查阶段不必纠结它们，"
            "真实开销要看性能评估里的逐节点耗时"
            % (len(cpu_nodes), "、".join(cpu_nodes[:5]))
        )
    if subgraph_count > 1:
        warnings.append(
            "模型被切成了 %d 个子图——中间层被 CPU 算子劈开会显著影响性能，"
            "值得检查分段位置" % subgraph_count
        )

    return CheckerReport(
        nodes=tuple(nodes),
        subgraph_count=subgraph_count,
        cpu_nodes=cpu_nodes,
        saw_node_table=True,
        warnings=tuple(warnings),
    )
