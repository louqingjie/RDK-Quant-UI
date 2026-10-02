# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""`hb_mapper checker` 输出的解析测试。

⚠️ 这里的样例文本是**构造的**，不是真实输出——计划期间官方文档站点取不到，
真实格式尚未验证。等导入 OE 镜像后要用真实输出替换（见 M3 计划的风险 1）。
测试的价值在于锁住"容错语法"的行为：列数不匹配、行尾注释、尾声日志、
缺列、CRLF 都不能把解析搞崩。
"""

from __future__ import annotations

import pytest

from rdkquant.core.logparse import (
    CheckerNode,
    parse_checker,
    strip_log_prefix,
)

# 与 core/backends.py 里 MockBackend 的输出同构
MOCK_CHECKER = """\
[INFO] hb_mapper checker 开始
[INFO] model_parameters.march = bayes-e
[INFO] 正在解析 ONNX 图结构 ...
[INFO] The converted model node information:
[INFO]       Node         Type       Device
[INFO]       Conv_0       Conv       BPU
[INFO]       Conv_2       Conv       BPU
[INFO]       Sigmoid_55   Sigmoid    BPU
[INFO]       Softmax_91   Softmax    CPU     <-- 回落到 CPU，可能影响性能
[INFO]       Resize_118   Resize     CPU
[WARN] 检测到 2 个算子运行在 CPU 上，建议检查 node_info 配置
[SUCCESS] checker 通过
"""


def test_strip_log_prefix_variants():
    assert strip_log_prefix("[INFO] hello") == "hello"
    assert strip_log_prefix("[WARN]   hello") == "hello"
    assert strip_log_prefix("INFO: hello") == "hello"
    assert strip_log_prefix("INFO hello") == "hello"
    assert strip_log_prefix("[ERROR][INFO] hello") == "hello"
    assert strip_log_prefix("没有前缀") == "没有前缀"
    assert strip_log_prefix("") == ""


def test_parses_mock_table():
    report = parse_checker(MOCK_CHECKER.splitlines())
    assert report is not None
    assert report.saw_node_table
    assert [n.name for n in report.nodes] == [
        "Conv_0", "Conv_2", "Sigmoid_55", "Softmax_91", "Resize_118",
    ]
    assert report.nodes[0].type == "Conv"
    assert report.nodes[0].device == "BPU"
    assert report.cpu_nodes == ("Softmax_91", "Resize_118")
    assert report.cpu_count == 2
    assert report.bpu_count == 3
    assert report.subgraph_count == 1


def test_trailing_log_lines_are_not_turned_into_nodes():
    """实测抓到的 bug：日志尾部的 `[WARN] 检测到 2 个算子…` 和
    `[SUCCESS] checker 通过` 曾经被当成节点，多出名叫"检测到"和"checker"的假节点。"""
    report = parse_checker(MOCK_CHECKER.splitlines())
    names = [n.name for n in report.nodes]
    assert "检测到" not in names
    assert "checker" not in names
    assert len(names) == 5


def test_inline_comment_does_not_break_device_column():
    report = parse_checker(MOCK_CHECKER.splitlines())
    softmax = next(n for n in report.nodes if n.name == "Softmax_91")
    assert softmax.device == "CPU"
    assert softmax.on_cpu and not softmax.on_bpu


def test_parses_makertbin_style_seven_column_table():
    """makertbin 的余弦相似度表列更多（Node/ON/Subgraph/Type/Cosine/Threshold）。
    同一个解析器要能吃下来，M4 会直接复用它。"""
    text = "\n".join([
        "[INFO] The converted model node information:",
        "Node         ON    Subgraph       Type      Cosine Similarity   Threshold",
        "Conv_0       ON    subgraph_0     Conv      0.9999              0.8",
        "Softmax_91   ON    subgraph_1     Softmax   0.8100              0.8",
    ])
    report = parse_checker(text.splitlines())
    assert report is not None
    assert len(report.nodes) == 2
    assert report.subgraph_count == 2
    assert report.nodes[0].on == "ON"
    assert report.nodes[0].subgraph == "subgraph_0"
    assert report.nodes[1].device == ""  # 这张表没有 device 列，末列是阈值


def test_no_header_returns_none():
    assert parse_checker(["[INFO] 什么都没有"]) is None


def test_empty_input_returns_none():
    assert parse_checker([]) is None
    assert parse_checker(["", "   ", "\t"]) is None


def test_header_without_rows_returns_none():
    """有表头没数据 → None，让上层能区分"格式变了"和"确实没有节点"。"""
    assert parse_checker(["Node  Type  Device"]) is None


def test_crlf_and_fullwidth_spaces():
    text = "[INFO] Node\u3000Type\u3000Device\r\n[INFO] Conv_0\u3000Conv\u3000BPU\r\n"
    report = parse_checker(text.splitlines())
    assert report is not None
    assert report.nodes[0].name == "Conv_0"
    assert report.nodes[0].device == "BPU"


def test_device_column_missing_falls_back_to_last_column():
    text = "Node        Type\nConv_0      BPU\nSoftmax_1   CPU\n"
    report = parse_checker(text.splitlines())
    assert report is not None
    assert report.nodes[0].device == "BPU"
    assert report.nodes[1].device == "CPU"


def test_separator_line_is_skipped():
    text = "\n".join([
        "Node      Type     Device",
        "--------  -------  --------",
        "Conv_0    Conv     BPU",
    ])
    report = parse_checker(text.splitlines())
    assert report is not None
    assert [n.name for n in report.nodes] == ["Conv_0"]


def test_ragged_row_stops_the_table():
    """列数对不上就不再往下吃——否则表格后面的散文会被当节点。"""
    text = "\n".join([
        "Node      Type     Device",
        "Conv_0    Conv     BPU",
        "这一行很长而且明显不是表格里面的内容",
        "Conv_1    Conv     BPU",
    ])
    report = parse_checker(text.splitlines())
    assert report is not None
    assert [n.name for n in report.nodes] == ["Conv_0"]


def test_warnings_are_actionable_chinese():
    report = parse_checker(MOCK_CHECKER.splitlines())
    assert any("CPU" in w for w in report.warnings)
    assert any("不必纠结" in w for w in report.warnings)


def test_multi_subgraph_warning():
    text = "\n".join([
        "Node   ON   Subgraph    Type    Device",
        "a      ON   subgraph_0  Conv    BPU",
        "b      ON   subgraph_1  Resize  CPU",
    ])
    report = parse_checker(text.splitlines())
    assert report.subgraph_count == 2
    assert any("子图" in w for w in report.warnings)


def test_checker_node_helpers():
    assert CheckerNode("a", device="CPU").on_cpu
    assert CheckerNode("a", device="BPU").on_bpu
    assert not CheckerNode("a", device="").on_cpu


@pytest.mark.parametrize("device,cells", [
    ("bpu", ["x", "y", "bpu"]),
    ("BPU", ["x", "y", "BPU"]),
    ("cpu", ["x", "y", "cpu"]),
    ("CPU(回落到)", ["x", "y", "CPU(回落到)"]),
])
def test_device_normalization_is_case_insensitive(device, cells):
    text = "Node  Type  Device\n" + "  ".join(cells)
    report = parse_checker(text.splitlines())
    assert report is not None
    assert report.nodes[0].device == device.upper()[:3]
