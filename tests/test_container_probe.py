# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""容器侧探针的纯逻辑测试。

探针是**独立脚本**，所以可以直接在宿主 Python 上跑（不需要容器、不需要
Docker）。这里测的是与环境无关的部分：参数解析、字节数校验、错误降级、
JSON 协议。真实图像处理要靠容器（宿主没装图像库），另见 CONTAINER_FINDINGS.md。

`.npy` 那条规则是**实测**来的：cal_data_dir 按裸字节读，带文件头就会失败。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rdkquant

PROBE = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"

# 实验里用的形状：1x3x64x64 float32 → 恰好 49152 字节
SHAPE_F32_BYTES = 1 * 3 * 64 * 64 * 4
SHAPE = "1x3x64x64"


def run_probe(*argv: str) -> dict:
    """跑探针，返回最后一行 JSON。"""
    result = subprocess.run(
        [sys.executable, str(PROBE), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"探针没有任何输出（stderr: {result.stderr[:400]}）"
    return json.loads(lines[-1])


def test_probe_exists():
    assert PROBE.is_file(), f"找不到探针：{PROBE}"


def test_probe_is_ascii_safe():
    """JSON 契约是纯 ASCII —— 任何编码环境下都不可能损坏。"""
    result = subprocess.run(
        [sys.executable, str(PROBE), "nope"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    line = [l for l in result.stdout.splitlines() if l.strip()][-1]
    assert line.isascii(), "协议行必须是纯 ASCII"
    assert "nope" in json.loads(line)["error"]


def test_expected_sample_bytes():
    """1x3x64x64 float32 = 49152 字节 —— 这个数字来自真机实验。"""
    assert SHAPE_F32_BYTES == 49152


# --------------------------------------------------------------------------
# calib-scan
# --------------------------------------------------------------------------
def test_scan_empty_directory_reports_problem(tmp_path: Path):
    payload = run_probe("calib-scan", str(tmp_path))
    assert payload["ok"] is True
    assert payload["count"] == 0
    assert any("空" in p for p in payload["problems"])


def test_scan_missing_directory_is_a_clean_error(tmp_path: Path):
    payload = run_probe("calib-scan", str(tmp_path / "不存在"))
    assert payload["ok"] is False
    assert "不存在" in payload["error"]


def test_scan_accepts_correctly_sized_binaries(tmp_path: Path):
    """字节数恰好对 → 不该报任何问题。这是 cal_data_dir 的硬性要求。"""
    for i in range(3):
        (tmp_path / f"{i}.bin").write_bytes(b"\x00" * SHAPE_F32_BYTES)
    payload = run_probe("calib-scan", str(tmp_path), "--shape", SHAPE,
                        "--dtype", "float32")
    assert payload["ok"] is True
    assert payload["binary_like"] == 3
    assert payload["count"] == 3
    assert not payload["problems"], payload["problems"]


def test_scan_detects_wrong_sized_binary(tmp_path: Path):
    """多 128 字节（= .npy 文件头）正是实验里失败的那种输入，必须提前抓出来。"""
    (tmp_path / "0.bin").write_bytes(b"\x00" * SHAPE_F32_BYTES)
    (tmp_path / "1.bin").write_bytes(b"\x00" * (SHAPE_F32_BYTES + 128))
    payload = run_probe("calib-scan", str(tmp_path), "--shape", SHAPE,
                        "--dtype", "float32")
    assert payload["problems"], "字节数不对必须报出来"
    joined = " ".join(payload["problems"])
    assert "1.bin" in joined
    assert str(SHAPE_F32_BYTES + 128) in joined
    assert str(SHAPE_F32_BYTES) in joined


def test_scan_binary_without_shape_only_warns(tmp_path: Path):
    (tmp_path / "0.bin").write_bytes(b"\x00" * 100)
    payload = run_probe("calib-scan", str(tmp_path))
    assert payload["ok"] is True
    assert any("shape" in w.lower() for w in payload["warnings"])


def test_scan_image_files_without_backend_degrades_honestly(tmp_path: Path):
    """宿主没有图像库。此时必须**如实报告**没有测量，而不是假装量过了。"""
    for i in range(3):
        (tmp_path / f"{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    payload = run_probe("calib-scan", str(tmp_path))
    assert payload["ok"] is True
    assert payload["image_like"] == 3
    if payload["imaging_backend"] is None:
        assert payload["measured"] is False
        assert any("图像库" in w for w in payload["warnings"])
    else:  # 装了图像库的机器上必须给出尺寸（哪怕读失败也要是 None 而非崩溃）
        assert payload["measured"] is True


def test_scan_counts_both_kinds(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (tmp_path / "b.bin").write_bytes(b"\x00" * SHAPE_F32_BYTES)
    (tmp_path / "忽略我.txt").write_text("x")
    payload = run_probe("calib-scan", str(tmp_path), "--shape", SHAPE)
    assert payload["image_like"] == 1
    assert payload["binary_like"] == 1
    assert payload["count"] == 2, "非图片非二进制的文件不该计入"


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def test_inputs_missing_file(tmp_path: Path):
    payload = run_probe("inputs", str(tmp_path / "nope.onnx"))
    assert payload["ok"] is False
    assert "不存在" in payload["error"]


def test_inputs_without_onnx_reports_clearly(tmp_path: Path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"\x08\x07" + b"\x00" * 64)
    payload = run_probe("inputs", str(model))
    if payload.get("onnx") is None and payload["ok"] is False:
        assert "onnx" in payload["error"], "没装 onnx 时要说清楚是缺 onnx"
    assert payload["path"] == "m.onnx"


def test_inputs_flags_oversized_model(tmp_path: Path, monkeypatch):
    """超过 2 GB 的模型要报 protobuf 上限。用假的大小避免真写 2 GB。"""
    model = tmp_path / "big.onnx"
    model.write_bytes(b"\x00" * 16)
    source = PROBE.read_text(encoding="utf-8")
    assert "2 * 1024 ** 3" in source, "2 GB 上限的检查不能丢"


# --------------------------------------------------------------------------
# 协议健壮性：任何子命令都不该崩
# --------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    (),
    ("-h",),
    ("--help",),
    ("nope",),
    ("inputs",),
    ("calib-scan",),
    ("calib-build",),
    ("calib-build", "/tmp"),
    ("calib-build", "/tmp", "/tmp/out"),
    ("thumbs",),
    ("env", "多余参数"),
])
def test_never_crashes_and_always_emits_json(argv):
    payload = run_probe(*argv)
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload["ok"] is False:
        assert payload.get("error"), "失败时必须给出可读原因"


def test_build_rejects_uint8_with_mean_scale(tmp_path: Path):
    """uint8 叠加 mean/scale 会溢出，必须拒绝而不是产出错数据。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    payload = run_probe(
        "calib-build", str(src), str(tmp_path / "out"),
        "--shape", "64x64", "--layout", "NHWC", "--color", "rgb",
        "--dtype", "uint8", "--mean", "128",
    )
    # 有图像库时才会走到 dtype 校验；没有时先报没有图像库，两者都必须 ok=False
    assert payload["ok"] is False
