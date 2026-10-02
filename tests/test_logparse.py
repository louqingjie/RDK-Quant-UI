# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""`hb_mapper checker` 输出的解析测试。

⚠️ 这里的样例文本是**构造的**，不是真实输出——计划期间官方文档站点取不到，
真实格式尚未验证。等导入 OE 镜像后要用真实输出替换（见 M3 计划的风险 1）。
测试的价值在于锁住"容错语法"的行为：列数不匹配、行尾注释、尾声日志、
缺列、CRLF 都不能把解析搞崩。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rdkquant.core.logparse import (
    CheckerNode,
    ansi_level,
    parse_checker,
    strip_ansi,
    strip_log_prefix,
)

# 用户日志里的真实字符串（hb_mapper 的控制台输出）
REAL_INFO = "2026-10-02 20:57:55,451 \x1b[92mINFO\x1b[0m Start to optimize the onnx model."
REAL_ERROR = ("2026-10-02 20:57:55,805 \x1b[91mERROR\x1b[0m "
              "*** ERROR-OCCUR-DURING {horizon_nn.build_onnx} ***")
REAL_WARN = "2026-10-02 20:57:55,805 \x1b[93mWARNING\x1b[0m something is off"

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


def test_strip_log_prefix_handles_real_logfile_format():
    """实测自 hb_mapper_checker.log 的真实前缀：
    时间戳 + `file: … func: … line No: N `。原来的实现完全处理不了。"""
    line = ("2026-10-02 19:41:44,845 file: print_info_dict.py "
            "func: print_info_dict line No: 72 The main quantized node information:")
    assert strip_log_prefix(line) == "The main quantized node information:"


def test_strip_log_prefix_handles_console_ansi():
    """控制台输出带 ANSI 颜色，日志文件里没有。不剥掉会污染解析。"""
    line = "2026-10-02 19:41:44,543 \x1b[92mINFO\x1b[0m Start hb_mapper...."
    assert strip_log_prefix(line) == "Start hb_mapper...."


# --------------------------------------------------------------------------
# ANSI：剥掉 + 当级别用
# --------------------------------------------------------------------------
def test_strip_ansi_removes_colour_codes():
    assert strip_ansi(REAL_INFO) == (
        "2026-10-02 20:57:55,451 INFO Start to optimize the onnx model."
    )
    assert "\x1b" not in strip_ansi(REAL_ERROR)
    assert "[92m" not in strip_ansi(REAL_INFO)


@pytest.mark.parametrize("line,expected", [
    (REAL_INFO, "info"),
    (REAL_ERROR, "error"),
    (REAL_WARN, "warn"),
    ("\x1b[96mDEBUG\x1b[0m x", "debug"),
    ("\x1b[31mred\x1b[0m", "error"),
    ("\x1b[33myellow\x1b[0m", "warn"),
    ("\x1b[32mgreen\x1b[0m", "success"),
])
def test_ansi_level_reads_the_tools_own_colour(line, expected):
    """颜色是**工具自己给的结论**，比按文字猜可靠，所以要能读出来。"""
    assert ansi_level(line) == expected


@pytest.mark.parametrize("line", [
    "2026-10-02 20:57:55,451 INFO 没有颜色码",
    "纯文本",
    "",
    "\x1b[0m只有重置码",
])
def test_ansi_level_returns_none_without_a_colour(line):
    assert ansi_level(line) is None


def test_ansi_handles_private_modes_and_cursor_moves():
    """容器里的工具会打 `\\x1b[?25l` 这类私有模式，别漏了。"""
    assert strip_ansi("\x1b[?25l进度\x1b[?25h") == "进度"
    assert strip_ansi("a\x1b[2Kb") == "ab"


# --------------------------------------------------------------------------
# 真实夹具：这份文件是导入 X5 CPU 镜像后从容器里抓出来的
# --------------------------------------------------------------------------
REAL_LOG = Path(__file__).parent / "fixtures" / "checker_real.log"


@pytest.mark.skipif(not REAL_LOG.is_file(), reason="缺少真实夹具")
def test_parses_the_real_checker_log():
    lines = REAL_LOG.read_text(encoding="utf-8").splitlines()
    report = parse_checker(lines)

    assert report is not None, "真实日志必须能解析出来"
    assert report.saw_node_table
    assert [n.name for n in report.nodes] == ["Conv_0"]
    assert report.nodes[0].device == "BPU"
    assert report.nodes[0].type == "HzSQuantizedConv"
    assert report.nodes[0].subgraph == "id(0)"
    assert report.subgraph_count == 1
    assert report.cpu_nodes == ()


@pytest.mark.skipif(not REAL_LOG.is_file(), reason="缺少真实夹具")
def test_real_log_tail_is_not_turned_into_nodes():
    """真实的表格后面紧跟着 `The quantized model output:`，它的列数恰好也落进
    "列数区间"里——只靠列数过滤会多出一个名叫 "The" 的假节点。
    真正管用的是"每行的设备列必须是 BPU/CPU"这条。"""
    lines = REAL_LOG.read_text(encoding="utf-8").splitlines()
    report = parse_checker(lines)
    names = [n.name for n in report.nodes]
    assert "The" not in names
    assert "Output" not in names
    assert len(names) == 1


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


def test_parses_makertbin_style_table_with_real_on_semantics():
    """列更多的表（Node/ON/Subgraph/Type/Cosine/Threshold/DataType）。
    实测确认 **ON 列装的就是 BPU/CPU**，不是字面量 "ON"——M4 会复用这个解析器。"""
    text = "\n".join([
        "[INFO] The main quantized node information:",
        "Node        ON   Subgraph  Type              Cosine Similarity  Threshold  DataType",
        "Conv_0      BPU  id(0)     HzSQuantizedConv  0.9999             1.0        int8",
        "Softmax_91  CPU  id(1)     Softmax           0.8100             1.0        int8",
    ])
    report = parse_checker(text.splitlines())
    assert report is not None
    assert len(report.nodes) == 2
    assert report.nodes[0].device == "BPU"
    assert report.nodes[0].subgraph == "id(0)"
    assert report.nodes[0].type == "HzSQuantizedConv"
    assert report.nodes[1].device == "CPU"
    assert report.cpu_nodes == ("Softmax_91",)
    assert report.subgraph_count == 2


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
