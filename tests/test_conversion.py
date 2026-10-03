# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S4 宿主侧策略的测试。

**YAML 用 PyYAML 解析后断言结构**，不断言字符串相等 —— 断的是含义不是格式，
否则一次缩进调整就会把测试弄红而行为没变。PyYAML 因此进了 `dev` extra，
运行时依赖仍然为空（见 `core/conversion.py` 的模块说明）。
"""

from __future__ import annotations

import yaml
import pytest

from rdkquant.core.conversion import (
    CHECK_ARTIFACTS,
    CHECK_CONSOLE,
    CHECK_SIMILARITY,
    END_LINE,
    MAKERTBIN_LOG_NAME,
    SUCCESS_LINE,
    ConversionSpec,
    clear_output,
    discover_artifacts,
    evaluate_conversion,
    missing_artifacts,
    parse_makertbin,
    read_log,
    render_config,
    summarize_artifacts,
    write_config,
)
from rdkquant.core.workflow import ARTIFACT_BIN, CONVERSION_ARTIFACTS


def make_spec(**overrides) -> ConversionSpec:
    base = dict(
        onnx_model="./model/0708.onnx",
        output_prefix="0708",
        march="bayes-e",
        input_name="images",
        input_shape="1x3x640x640",
        input_type_train="rgb",
        input_layout_train="NCHW",
        calibration_type="skip",
        cal_data_dir="./calib_bin_f32",
    )
    base.update(overrides)
    return ConversionSpec(**base)


# --------------------------------------------------------------------------
# render_config —— 结构
# --------------------------------------------------------------------------
def test_config_parses_and_has_the_four_sections():
    """四个 section 一个都不能少 —— 少一个官方 schema 会直接拒。"""
    data = yaml.safe_load(render_config(make_spec()))
    assert set(data) == {
        "model_parameters", "input_parameters",
        "calibration_parameters", "compiler_parameters",
    }


def test_config_carries_the_values_through():
    data = yaml.safe_load(render_config(make_spec()))
    assert data["model_parameters"]["onnx_model"] == "./model/0708.onnx"
    assert data["model_parameters"]["march"] == "bayes-e"
    assert data["model_parameters"]["output_model_file_prefix"] == "0708"
    assert data["input_parameters"]["input_name"] == "images"
    assert data["input_parameters"]["input_shape"] == "1x3x640x640"
    assert data["calibration_parameters"]["calibration_type"] == "skip"


def test_working_dir_defaults_to_our_output_dir():
    """⚠️ 官方默认是 `./model_output`。不写成 ./output 的话产物我们扫不到。"""
    data = yaml.safe_load(render_config(make_spec()))
    assert data["model_parameters"]["working_dir"] == "./output"


def test_config_has_no_input_layout_rt():
    """`input_type_rt = nv12` 时写了会打 WARNING「is not needed」（实测）。"""
    text = render_config(make_spec())
    assert "input_layout_rt" not in text
    assert "input_layout_rt" not in yaml.safe_load(text)["input_parameters"]


def test_norm_type_is_no_preprocess():
    """校准数据是原始像素，归一化由工具插进模型里。"""
    data = yaml.safe_load(render_config(make_spec()))
    assert data["input_parameters"]["norm_type"] == "no_preprocess"


def test_input_source_is_a_dict_under_compiler_parameters():
    """⚠️ 两层坑：不能在 input_parameters 里，也不能是字符串（实测报错）。"""
    data = yaml.safe_load(render_config(make_spec()))
    assert "input_source" not in data["input_parameters"]
    source = data["compiler_parameters"]["input_source"]
    assert isinstance(source, dict), "运行期只认 dict，字符串会被拒"
    assert source == {"images": "pyramid"}


def test_multi_input_splits_input_source_into_one_key_each():
    """多输入时官方约定 `input_name` 用 `;` 分隔，`input_source` 要**逐个**给键。

    整串当一个键的话，工具会去找一个名叫 `data1;data2` 的输入，找不到。
    这也正是 `input_source` 必须是 dict 的原因 —— 它是按输入名查的映射。
    """
    data = yaml.safe_load(render_config(make_spec(input_name="data1;data2")))
    assert data["input_parameters"]["input_name"] == "data1;data2"
    assert data["compiler_parameters"]["input_source"] == {
        "data1": "pyramid", "data2": "pyramid",
    }


def test_input_names_property_handles_spaces_and_empties():
    spec = make_spec(input_name=" a ; ; b ")
    assert spec.input_names == ("a", "b")
    assert make_spec(input_name="only").input_names == ("only",)


def test_preprocess_on_is_a_real_bool():
    data = yaml.safe_load(render_config(make_spec(preprocess_on=True)))
    assert data["calibration_parameters"]["preprocess_on"] is True


def test_cal_data_dir_left_out_when_empty():
    """官方 schema 是 `And(str, len)` —— 空字符串会被拒，宁可不写这个键。"""
    data = yaml.safe_load(render_config(make_spec(cal_data_dir="")))
    assert "cal_data_dir" not in data["calibration_parameters"]


def test_cal_data_dir_written_when_present():
    data = yaml.safe_load(render_config(make_spec(cal_data_dir="./calib_bin_f32")))
    assert data["calibration_parameters"]["cal_data_dir"] == "./calib_bin_f32"


# --------------------------------------------------------------------------
# render_config —— 转义
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    "0708",
    "有 空格",
    "有'单引号",
    "有: 冒号",
    "有 # 井号",
    " 前后有空格 ",
    "有\\反斜杠",
    "有\"双引号",
    "emoji🙂",
])
def test_quoting_survives_awkward_prefixes(value):
    """`output_model_file_prefix` 来自用户的文件名 —— 什么都可能有。

    一律单引号 + `'` 写成 `''`，PyYAML 读回来必须一模一样。
    """
    data = yaml.safe_load(render_config(make_spec(output_prefix=value)))
    assert data["model_parameters"]["output_model_file_prefix"] == value


def test_newline_in_a_value_is_refused():
    """单引号标量表示不了换行 —— 与其写出一个坏文件，不如当场拒。"""
    with pytest.raises(ValueError):
        render_config(make_spec(output_prefix="a\nb"))


def test_prefix_with_quote_still_yields_valid_yaml():
    """转义错了的话 PyYAML 会抛 —— 这条确认整份文档仍然可解析。"""
    text = render_config(make_spec(output_prefix="it's"))
    assert yaml.safe_load(text)["model_parameters"]["output_model_file_prefix"] == "it's"


# --------------------------------------------------------------------------
# 写盘
# --------------------------------------------------------------------------
def test_write_config_creates_parent_and_uses_utf8(tmp_path):
    target = tmp_path / "deep" / "config.yaml"
    write_config(target, make_spec())
    assert target.is_file()
    assert "march" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 产物发现
# --------------------------------------------------------------------------
def _touch(directory, name, size=16, mtime=None):
    path = directory / name
    path.write_bytes(b"x" * size)
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


def test_discover_finds_all_five_patterns(tmp_path):
    for name in (
        "0708_original_float_model.onnx",
        "0708_optimized_float_model.onnx",
        "0708_calibrated_model.onnx",
        "0708_quantized_model.onnx",
        "0708.bin",
    ):
        _touch(tmp_path, name)
    found = discover_artifacts(tmp_path)
    assert set(found) == {key for key, _ in CONVERSION_ARTIFACTS}
    assert missing_artifacts(found) == []


def test_discover_ignores_extra_files(tmp_path):
    """实测：output/ 里还会有 quant_info.json、subgraph html —— 不算产物也不算错。"""
    _touch(tmp_path, "0708.bin")
    _touch(tmp_path, "0708_quant_info.json")
    _touch(tmp_path, "torch_jit_subgraph_0.html")
    found = discover_artifacts(tmp_path)
    assert set(found) == {ARTIFACT_BIN}


def test_discover_picks_the_newest_per_pattern(tmp_path):
    _touch(tmp_path, "old.bin", mtime=1_000_000)
    _touch(tmp_path, "new.bin", mtime=2_000_000)
    assert discover_artifacts(tmp_path)[ARTIFACT_BIN].name == "new.bin"


def test_discover_skips_empty_files(tmp_path):
    """0 字节的产物是半成品，不能算数。"""
    _touch(tmp_path, "0708.bin", size=0)
    assert discover_artifacts(tmp_path) == {}


def test_discover_returns_empty_for_missing_dir(tmp_path):
    assert discover_artifacts(tmp_path / "nope") == {}


def test_missing_artifacts_reports_patterns(tmp_path):
    _touch(tmp_path, "0708.bin")
    missing = missing_artifacts(discover_artifacts(tmp_path))
    assert len(missing) == 4
    assert "*.bin" not in missing


def test_clear_output_removes_leftovers(tmp_path):
    """失败也会往 output/ 里写（实测 shape_inference_fail.onnx）。"""
    _touch(tmp_path, "shape_inference_fail.onnx")
    _touch(tmp_path, "old.bin")
    assert clear_output(tmp_path) == 2
    assert list(tmp_path.iterdir()) == []


def test_clear_output_on_missing_dir_is_a_noop(tmp_path):
    assert clear_output(tmp_path / "nope") == 0


def test_summarize_artifacts_lists_all_five_even_when_missing(tmp_path):
    _touch(tmp_path, "0708.bin")
    rows = summarize_artifacts(discover_artifacts(tmp_path))
    assert len(rows) == 5, "表格要固定 5 行，缺的显示（缺失）而不是消失"
    assert any(name == "（缺失）" for _label, name, _size in rows)


# --------------------------------------------------------------------------
# 日志解析 —— **用真日志的原文**
# --------------------------------------------------------------------------
#: 从真日志里抄下来的节点表（X5 CPU 容器，calibration_type=skip）。
#: 表头里 `Cosine Similarity` 是**两个词一列**，这是列错位的根源。
REAL_TABLE = """\
Node                                                ON   Subgraph  Type                           Cosine Similarity  Threshold  DataType  
-----------------------------------------------------------------------------------------------------------------------
HZ_PREPROCESS_FOR_images                            BPU  id(0)     HzSQuantizedPreprocess         --                 127.0      int8      
/m/model.0/conv/Conv                                BPU  id(0)     HzSQuantizedConv               --                 1.0        int8      
/m/model.0/act/Mul                                  BPU  id(0)     HzLut                          --                 1.0        int8      
"""

REAL_TAIL = (
    "2026-10-03 20:26:24,831 file: onnx2horizonrt.py func: onnx2horizonrt line No: 4310 "
    "input-source        : {'images': 'pyramid', '_default_value': 'ddr'}\n"
    "2026-10-03 20:26:24,844 file: hb_mapper_makertbin.py func: hb_mapper_makertbin "
    f"line No: 783 {SUCCESS_LINE}\n"
    "2026-10-03 20:26:24,845 file: hb_mapper_makertbin.py func: hb_mapper_makertbin "
    f"line No: 784 {END_LINE}\n"
)


def test_parse_reads_the_success_lines():
    report = parse_makertbin(REAL_TAIL.splitlines())
    assert report.saw_success is True
    assert report.saw_end is True
    assert report.succeeded is True


def test_parse_recognizes_the_node_table():
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    assert report.saw_node_table is True
    assert len(report.nodes) == 3
    assert report.nodes[1].name == "/m/model.0/conv/Conv"
    assert report.nodes[1].device == "BPU"


def test_threshold_column_is_not_shifted_by_the_two_word_header():
    """**回归**：`Cosine Similarity` 占两个 token，它后面的列会错位一格。

    改之前 `threshold` 读到的是 `DataType` —— `1.0` 读成 `int8`。
    表是同一张、数据行没被切，所以只有"跨词列之后的列"会错。
    """
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    assert report.nodes[1].threshold == "1.0", "错位时这里会是 int8"
    assert report.nodes[1].cosine == "--"


def test_dash_similarity_is_not_zero():
    """`--` 是"没算过"，不是"相似度为 0" —— 这个区分决定了 skip 模式怎么判。"""
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    assert report.nodes[0].cosine == "--"
    assert report.nodes[0].similarity is None
    assert report.rated_nodes == ()


def test_numeric_similarity_is_read():
    table = (
        "Node    ON   Subgraph  Type         Cosine Similarity  Threshold  DataType\n"
        "--------\n"
        "/a/Conv BPU  id(0)     HzSQuantizedConv  0.9985     1.0        int8\n"
    )
    report = parse_makertbin(table.splitlines())
    assert len(report.rated_nodes) == 1
    assert report.rated_nodes[0].similarity == pytest.approx(0.9985)


def test_parse_without_table_is_not_an_error():
    """表格解析不出来不算失败 —— 上层按 saw_node_table=False 处理。"""
    report = parse_makertbin(["随便一行", "又一行"])
    assert report.saw_node_table is False
    assert report.succeeded is False


def test_parse_collects_error_lines():
    # `ERROR-OCCUR-DURING` 这种连字符写法也要认出来
    lines = [
        "2026-01-01 00:00:00,000 file: a.py func: b line No: 1 "
        "ERROR-OCCUR-DURING {horizon_nn.build_onnx} ***, error message: boom",
        "2026-01-01 00:00:00,001 file: a.py func: b line No: 2 NO_ERROR_here",
    ]
    report = parse_makertbin(lines)
    assert len(report.errors) == 1
    assert "boom" in report.errors[0]


def test_success_marker_scanned_over_the_whole_log():
    """成功字样可能不在最后 8 行之内（后面还有尾声），所以它扫全长。"""
    lines = REAL_TAIL.splitlines() + [f"尾声 {i}" for i in range(20)]
    report = parse_makertbin(lines)
    assert report.saw_success is True
    assert report.saw_end is False, "End Model Convert 只看尾部，避免读到中途的痕迹"


def test_read_log_is_utf8_and_survives_missing_file(tmp_path):
    target = tmp_path / "x.log"
    target.write_text("中文行\n", encoding="utf-8")
    assert read_log(target) == ["中文行"]
    assert read_log(tmp_path / "nope.log") == []


def test_log_name_matches_reality():
    assert MAKERTBIN_LOG_NAME == "hb_mapper_makertbin.log"


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def _full_artifacts(tmp_path):
    for name in (
        "0708_original_float_model.onnx",
        "0708_optimized_float_model.onnx",
        "0708_calibrated_model.onnx",
        "0708_quantized_model.onnx",
        "0708.bin",
    ):
        _touch(tmp_path, name)
    return discover_artifacts(tmp_path)


def test_verdict_ok_when_everything_lines_up(tmp_path):
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    verdict = evaluate_conversion(report, _full_artifacts(tmp_path),
                                  spec=make_spec(), returncode=0)
    assert verdict.ok
    assert verdict.checks == {
        CHECK_CONSOLE: True, CHECK_SIMILARITY: True, CHECK_ARTIFACTS: True,
    }


def test_verdict_fails_without_log(tmp_path):
    """日志没读到就**不猜** —— 两项都判 False。"""
    verdict = evaluate_conversion(None, _full_artifacts(tmp_path))
    assert verdict.checks[CHECK_CONSOLE] is False
    assert verdict.checks[CHECK_SIMILARITY] is False
    assert verdict.checks[CHECK_ARTIFACTS] is True
    assert not verdict.ok


def test_verdict_reports_partial_artifacts(tmp_path):
    _touch(tmp_path, "0708.bin")
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    verdict = evaluate_conversion(report, discover_artifacts(tmp_path))
    assert verdict.checks[CHECK_ARTIFACTS] is False
    assert any("只产出 1/5" in f.message for f in verdict.findings)


def test_verdict_flags_missing_success_line(tmp_path):
    report = parse_makertbin(REAL_TABLE.splitlines())
    verdict = evaluate_conversion(report, _full_artifacts(tmp_path))
    assert verdict.checks[CHECK_CONSOLE] is False
    assert any(f.code == "CONVERT_CONSOLE_MISSING" for f in verdict.findings)


def test_skip_mode_does_not_fail_on_dash_similarities(tmp_path):
    """⚠️ skip / load 下相似度列**全是 `--`**（实测 196/196）——那是正常结果。"""
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    verdict = evaluate_conversion(report, _full_artifacts(tmp_path),
                                  spec=make_spec(calibration_type="skip"))
    assert verdict.checks[CHECK_SIMILARITY] is True, "表存在就算通过，不能因为 -- 判死"
    finding = next(f for f in verdict.findings if f.code == "CONVERT_SIMILARITY_SKIPPED")
    assert "skip" in finding.message


def test_rated_similarities_report_the_worst_three_without_a_threshold(tmp_path):
    """**刻意不给逐节点阈值** —— 官方那道线是自动搜索的内部目标，不是验收标准。"""
    table = (
        "Node      ON   Subgraph  Type               Cosine Similarity  Threshold  DataType\n"
        "----------\n"
        "/a/Conv   BPU  id(0)     HzSQuantizedConv   0.9990             1.0        int8\n"
        "/b/Conv   BPU  id(0)     HzSQuantizedConv   0.9000             1.0        int8\n"
        "/c/Lut    BPU  id(0)     HzLut              0.9500             1.0        int8\n"
        "/d/Mul    BPU  id(0)     HzSElementwiseMul  0.9800             1.0        int8\n"
    )
    report = parse_makertbin((table + REAL_TAIL).splitlines())
    verdict = evaluate_conversion(report, _full_artifacts(tmp_path))
    finding = next(f for f in verdict.findings if f.code == "CONVERT_SIMILARITY")
    assert "0.9000" in finding.message
    assert "0.9990" not in finding.message, "只报最低的三个"
    assert verdict.checks[CHECK_SIMILARITY] is True, "有数值也不拿它当门槛"


def test_log_errors_become_findings(tmp_path):
    lines = [
        "2026-01-01 00:00:00,000 file: a.py func: b line No: 1 "
        "ERROR *** ERROR-OCCUR-DURING {x} ***, error message: 炸了",
        REAL_TAIL,
    ]
    report = parse_makertbin("\n".join(lines).splitlines())
    verdict = evaluate_conversion(report, _full_artifacts(tmp_path))
    assert any("炸了" in f.message for f in verdict.findings)


def test_nonzero_returncode_is_surfaced(tmp_path):
    report = parse_makertbin(REAL_TABLE.splitlines())
    verdict = evaluate_conversion(report, {}, returncode=255)
    assert any(f.code == "CONVERT_RC" for f in verdict.findings)


def test_verdict_with_no_artifacts_says_so(tmp_path):
    report = parse_makertbin((REAL_TABLE + REAL_TAIL).splitlines())
    verdict = evaluate_conversion(report, {})
    assert any(f.code == "CONVERT_ARTIFACTS_NONE" for f in verdict.findings)


# --------------------------------------------------------------------------
# 自检：上面那些测试不是空跑
# --------------------------------------------------------------------------
def test_the_three_check_names_match_the_workflow_tests():
    """`tests/test_workflow.py` 是按**字面量**断言 blockers 的。

    这里改名而那边没跟着改，那条测试会静默失去意义（它仍然绿，只是不再
    校验任何东西）。
    """
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "tests" / "test_workflow.py")
    text = source.read_text(encoding="utf-8")
    for name in (CHECK_CONSOLE, CHECK_SIMILARITY, CHECK_ARTIFACTS):
        assert name in text, f"{name} 在 test_workflow.py 里找不到，两边漂移了"
