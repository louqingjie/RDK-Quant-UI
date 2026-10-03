# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""后端契约测试。

最重要的一条是**契约漂移检测**：MockBackend 返回的 JSON 形状必须与真实探针
完全一致。否则页面代码会在 Mock 下跑得好好的、到真机上 KeyError——这正是
"没有 Docker 也能把 UI 开发完"这个承诺的代价所在，必须用测试守住。

能动态比对的地方就动态比对（在宿主上直接跑探针）；探针在宿主上跑不出来的
分支（比如 inputs 需要 onnx）用静态键集断言，至少防止误改。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rdkquant
from rdkquant.core import backends as B
from rdkquant.core.calib import CalibBuildSpec
from rdkquant.core.containerops import PROBE_CONTAINER_PATH

PROBE = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"

#: 真实探针在"成功路径"上会给出的顶层键。改了这里就得同步改 Mock，反之亦然。
INPUTS_CONTRACT = {
    "ok", "path", "size", "python", "onnx", "ir_version", "opset",
    "inputs", "outputs", "op_histogram", "problems", "warnings",
}
SCAN_CONTRACT = {
    "ok", "directory", "imaging_backend", "count", "image_like", "binary_like",
    "measured", "files", "resolutions", "duplicates", "exposure",
    "problems", "warnings",
    # 逐张统计的条数。**总是**返回（stats_file 才是按需的）——
    # 宿主机靠它判断"筛选用不筛得了"
    "stats_count",
}
BUILD_CONTRACT = {
    "ok", "backend", "written", "failed", "failures", "bytes",
    "bytes_per_sample", "out_dir", "shape", "layout", "color", "dtype",
    "normalized",
}
THUMBS_CONTRACT = {"ok", "backend", "generated", "failed", "files"}
ENV_CONTRACT = {
    "ok", "python", "executable", "imaging_backend", "imaging_candidates",
    "onnx", "numpy",
}


def run_probe(*argv: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(PROBE), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


# --------------------------------------------------------------------------
# 契约漂移检测
# --------------------------------------------------------------------------
def test_mock_env_matches_real_probe_keys():
    real = run_probe("env")
    assert set(B.MOCK_ENV) == set(real), (
        "Mock 与真实探针的 env 契约漂移了："
        f"Mock 多出 {set(B.MOCK_ENV) - set(real)}，"
        f"真实多出 {set(real) - set(B.MOCK_ENV)}"
    )


def test_mock_scan_matches_real_probe_keys(tmp_path: Path):
    """用一个字节数正确的二进制文件，走真实探针的成功路径。"""
    (tmp_path / "0.bin").write_bytes(b"\x00" * B.MOCK_SAMPLE_BYTES)
    real = run_probe("calib-scan", str(tmp_path), "--shape", "1x3x640x640")
    assert set(B.MOCK_SCAN) == set(real), (
        "Mock 与真实探针的 calib-scan 契约漂移了："
        f"Mock 多出 {set(B.MOCK_SCAN) - set(real)}，"
        f"真实多出 {set(real) - set(B.MOCK_SCAN)}"
    )


def test_mock_thumbs_matches_real_probe_keys(tmp_path: Path):
    """没有图像库时探针会提前失败，只有成功路径才有完整契约。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    real = run_probe("thumbs", str(src), str(tmp_path / "out"))
    if real.get("ok"):
        assert set(real) == THUMBS_CONTRACT
    mock = {
        "ok": True, "backend": "PIL", "generated": 0, "failed": 0, "files": [],
    }
    assert set(mock) == THUMBS_CONTRACT


@pytest.mark.parametrize("constant,contract", [
    ("MOCK_INPUTS", INPUTS_CONTRACT),
    ("MOCK_SCAN", SCAN_CONTRACT),
    ("MOCK_BUILD", BUILD_CONTRACT),
    ("MOCK_ENV", ENV_CONTRACT),
])
def test_mock_constants_match_documented_contract(constant, contract):
    assert set(getattr(B, constant)) == contract, (
        f"{constant} 与文档化的契约不一致——"
        "如果是有意改动，请同步更新契约集与真实探针"
    )


def test_mock_scan_is_a_healthy_dataset():
    """Mock 的默认场景必须是"没问题"的，否则 UI 开发时处处是警告，看不出真实观感。"""
    from rdkquant.core.calib import evaluate_probe

    verdict = evaluate_probe(dict(B.MOCK_SCAN), directory="cal_images")
    assert not verdict.blocked, [f.message for f in verdict.findings]


def test_mock_inputs_is_a_healthy_model():
    assert B.MOCK_INPUTS["ok"] is True
    assert B.MOCK_INPUTS["problems"] == []
    assert B.MOCK_INPUTS["inputs"][0]["dynamic"] is False


# --------------------------------------------------------------------------
# 协议完整性
# --------------------------------------------------------------------------
PROTOCOL_METHODS = [
    "check", "compile", "inspect_model", "scan_calib", "build_calib",
    "thumbnails", "probe_env",
]


@pytest.mark.parametrize("name", PROTOCOL_METHODS)
def test_both_backends_implement_every_protocol_method(name, tmp_path: Path):
    ws = B.Workspace(tmp_path / "run").ensure()
    for backend in (B.MockBackend(workspace=ws, delay=0.0),
                    B.DockerBackend(image="dummy:tag", workspace=ws)):
        assert callable(getattr(backend, name, None)), (
            f"{type(backend).__name__} 没有实现 {name}"
        )


def test_no_lines_callback_is_required_for_mock(tmp_path: Path):
    """每个容器侧方法都要能把日志交出来（页面要显示它们）。"""
    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    lines: list[str] = []

    payload = backend.inspect_model(ws.model_dir / "m.onnx", on_line=lines.append)
    assert payload["ok"] is True
    assert lines, "至少要打一行日志"

    lines.clear()
    backend.probe_env(on_line=lines.append)
    assert lines


# --------------------------------------------------------------------------
# Mock 的产物必须真的对
# --------------------------------------------------------------------------
def test_mock_inspect_reports_missing_file(tmp_path: Path):
    """真实探针在文件不存在时给 ok:false；Mock 不能比真实更宽松。"""
    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    payload = backend.inspect_model(ws.model_dir / "不存在.onnx", on_line=lambda _l: None)
    assert payload["size"] == 0


def test_mock_scan_of_empty_dir_matches_probe_semantics(tmp_path: Path):
    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    payload = backend.scan_calib(ws.calib_dir, on_line=lambda _l: None)
    assert payload["count"] == 0
    assert payload["problems"], "空目录必须报问题，和真实探针一致"


def test_mock_build_writes_files_with_exact_byte_count(tmp_path: Path):
    """字节数正是 cal_data_dir 的硬性要求（多一个文件头就失败），所以 Mock 也要守。"""
    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    out = ws.root / "calib_bin_f32"

    marks: list[tuple[int, int]] = []
    payload = backend.build_calib(
        ws.calib_dir, out, CalibBuildSpec(shape="1x3x640x640"),
        on_line=lambda _l: None, on_progress=lambda d, t: marks.append((d, t)),
    )

    assert payload["ok"] is True
    files = sorted(out.iterdir())
    assert len(files) == payload["written"] == B.MOCK_CALIB_COUNT
    for path in files:
        assert path.stat().st_size == B.MOCK_SAMPLE_BYTES, path.name
    assert payload["bytes"] == B.MOCK_CALIB_COUNT * B.MOCK_SAMPLE_BYTES
    assert marks and marks[-1] == (B.MOCK_CALIB_COUNT, B.MOCK_CALIB_COUNT)


def test_mock_build_honours_cancellation(tmp_path: Path):
    import threading

    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    cancel = threading.Event()
    cancel.set()
    payload = backend.build_calib(
        ws.calib_dir, ws.root / "out", CalibBuildSpec(shape="1x3x640x640"),
        on_line=lambda _l: None, cancel=cancel,
    )
    assert payload["ok"] is False


# --------------------------------------------------------------------------
# Mock 的 compile 必须和真 makertbin 对齐
#
# 三处原先和实测不符（见 docs/design/makertbin-runtime.md），都会让 mock 路径
# 测不出真问题：假成功字样、只写一个产物、bin 的名字带一堆后缀。
# --------------------------------------------------------------------------
def _compiled(tmp_path: Path):
    """跑一次假转换，返回 (workspace, backend, 控制台行)。"""
    from rdkquant.core import conversion as C

    ws = B.Workspace(tmp_path / "run").ensure()
    C.write_config(ws.config_path, C.ConversionSpec(
        onnx_model="./model/x.onnx", output_prefix="0708", march="bayes-e",
        input_name="images", input_shape="1x3x640x640",
        input_type_train="rgb", input_layout_train="NCHW",
        calibration_type="skip", cal_data_dir="./calib_bin_f32",
    ))
    lines: list[str] = []
    backend = B.MockBackend(workspace=ws, delay=0.0)
    assert backend.compile(on_line=lines.append) == 0
    return ws, lines


def test_mock_compile_writes_all_five_artifacts(tmp_path: Path):
    """只写 `.bin` 的话「5 个产出物」那项在 mock 下**永远**是 False。"""
    from rdkquant.core.conversion import discover_artifacts, missing_artifacts

    ws, _lines = _compiled(tmp_path)
    found = discover_artifacts(ws.output_dir)
    assert missing_artifacts(found) == [], f"缺：{missing_artifacts(found)}"
    assert set(found) == {
        "original_float_model", "optimized_float_model",
        "calibrated_model", "quantized_model", "bin",
    }


def test_mock_artifact_names_take_the_prefix_from_config(tmp_path: Path):
    """真产物名是 `<output_model_file_prefix>_<阶段>_model.onnx`，bin 就是 `<前缀>.bin`。"""
    ws, _lines = _compiled(tmp_path)
    names = sorted(p.name for p in ws.output_dir.iterdir())
    assert names == [
        "0708.bin",
        "0708_calibrated_model.onnx",
        "0708_optimized_float_model.onnx",
        "0708_original_float_model.onnx",
        "0708_quantized_model.onnx",
    ]


def test_mock_bin_has_no_march_or_shape_suffix(tmp_path: Path):
    """原先写的是 `model_bayes-e_640x640_nv12.bin` —— 那是编的。"""
    ws, _lines = _compiled(tmp_path)
    binary = ws.output_dir / "0708.bin"
    assert binary.is_file()
    assert not list(ws.output_dir.glob("*_640x640_nv12.bin"))


def test_mock_console_uses_the_real_success_marker(tmp_path: Path):
    """原先写的是 `[SUCCESS] 转换完成` —— 真工具打的是这个（都是 INFO 级）。"""
    from rdkquant.core.conversion import END_LINE, SUCCESS_LINE

    _ws, lines = _compiled(tmp_path)
    text = "\n".join(lines)
    assert SUCCESS_LINE in text
    assert END_LINE in text
    assert "[SUCCESS]" not in text


def test_mock_writes_the_log_file_that_s4_parses(tmp_path: Path):
    """**没有这个文件，S4 的解析路径在 mock 下走的是"日志读不到"那条分支。**"""
    from rdkquant.core.conversion import (
        MAKERTBIN_LOG_NAME, evaluate_conversion, parse_makertbin, read_log,
        discover_artifacts, ConversionSpec,
    )

    ws, _lines = _compiled(tmp_path)
    log = ws.root / MAKERTBIN_LOG_NAME
    assert log.is_file()

    report = parse_makertbin(read_log(log))
    assert report.succeeded is True
    assert report.saw_node_table is True
    assert len(report.nodes) == 4

    verdict = evaluate_conversion(
        report, discover_artifacts(ws.output_dir),
        spec=ConversionSpec(
            onnx_model="./model/x.onnx", output_prefix="0708", march="bayes-e",
            input_name="images", input_shape="1x3x640x640",
            input_type_train="rgb", input_layout_train="NCHW",
            calibration_type="skip", cal_data_dir="./calib_bin_f32",
        ),
        returncode=0,
    )
    assert verdict.ok, verdict.checks


def test_mock_log_table_has_the_two_word_header(tmp_path: Path):
    """假日志的表头也要是**两个词一列**，否则 mock 路径测不出列错位那个 bug。"""
    from rdkquant.core.conversion import (
        MAKERTBIN_LOG_NAME, parse_makertbin, read_log,
    )

    ws, _lines = _compiled(tmp_path)
    report = parse_makertbin(read_log(ws.root / MAKERTBIN_LOG_NAME))
    assert report.nodes[0].threshold == "1.0", "错位时这里会是 int8"
    assert report.nodes[0].cosine == "--"


def test_mock_compile_falls_back_when_config_is_missing(tmp_path: Path):
    """没有 config.yaml 时用官方默认前缀 `model`，不能崩。"""
    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    assert backend.compile(on_line=lambda _l: None) == 0
    assert (ws.output_dir / "model.bin").is_file()


def test_mock_compile_honours_cancellation(tmp_path: Path):
    import threading

    ws = B.Workspace(tmp_path / "run").ensure()
    backend = B.MockBackend(workspace=ws, delay=0.0)
    cancel = threading.Event()
    cancel.set()
    rc = backend.compile(on_line=lambda _l: None, cancel=cancel)
    assert rc != 0
    assert not (ws.output_dir / "model.bin").exists(), "取消时不该写产物"


# --------------------------------------------------------------------------
# 路径转换
# --------------------------------------------------------------------------
def test_container_paths_are_workspace_relative():
    """真实后端必须把宿主路径翻成容器内路径（/workspace/...）。"""
    ws = B.Workspace(Path("/tmp/whatever"))
    assert ws.rel(ws.model_dir / "m.onnx") == "/workspace/model/m.onnx"
    assert ws.rel(ws.calib_dir) == "/workspace/cal_images"
    assert PROBE_CONTAINER_PATH.startswith("/workspace/")


def test_rel_dot_is_the_form_config_yaml_wants():
    """`config.yaml` 里的路径一律**相对**形式（`./model/x.onnx`）。

    两种形式容器里都能用（工作目录就是 /workspace），但官方写的是相对形式，
    而且真跑验证过的是 `./model/0708.onnx` 那一种。见 docs/design/config-yaml.md。
    """
    ws = B.Workspace(Path("/tmp/whatever"))
    assert ws.rel_dot(ws.model_dir / "m.onnx") == "./model/m.onnx"
    assert ws.rel_dot(ws.calib_dir) == "./cal_images"


def test_rel_dot_leaves_outside_paths_alone():
    """workspace 之外的路径原样返回 —— 和 `rel()` 一致，不编一个假路径出来。"""
    ws = B.Workspace(Path("/tmp/whatever"))
    outside = Path("/somewhere/else/m.onnx")
    assert ws.rel_dot(outside) == str(outside)
    assert ws.rel(outside) == str(outside)
