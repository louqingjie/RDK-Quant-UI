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
# 路径转换
# --------------------------------------------------------------------------
def test_container_paths_are_workspace_relative():
    """真实后端必须把宿主路径翻成容器内路径（/workspace/...）。"""
    ws = B.Workspace(Path("/tmp/whatever"))
    assert ws.rel(ws.model_dir / "m.onnx") == "/workspace/model/m.onnx"
    assert ws.rel(ws.calib_dir) == "/workspace/cal_images"
    assert PROBE_CONTAINER_PATH.startswith("/workspace/")
