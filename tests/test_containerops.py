# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""容器探针客户端的测试。

全部用**假的 runner** 注入固定输出，不需要 Docker——这样 CI 也能跑。
真机验证是另一条腿（见 M3 计划的风险 1）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdkquant.core import containerops as ops
from rdkquant.core.backends import Workspace


class FakeRunner:
    """按顺序回放给定行，记录收到的命令。"""

    def __init__(self, lines=None, returncode: int = 0):
        self.lines = list(lines or [])
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def run(self, command, *, on_line, cancel=None):
        self.commands.append(list(command))
        for line in self.lines:
            on_line(line)
        return self.returncode


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "run").ensure()


# --------------------------------------------------------------------------
# JSON 提取
# --------------------------------------------------------------------------
def test_extract_json_from_last_line():
    lines = ["普通日志", '{"ok": true, "count": 3}']
    assert ops.extract_json(lines) == {"ok": True, "count": 3}


def test_extract_json_tolerates_trailing_noise():
    """容器可能在探针之后打点别的东西，所以要往回找有限的几行。"""
    lines = ['{"ok": true}', "hb_master 之类的尾部输出", ""]
    assert ops.extract_json(lines) == {"ok": True}


@pytest.mark.parametrize("lines", [
    [],
    ["只有普通日志"],
    ["{不是合法 json"],
    ['"一个字符串，不是对象"'],
    ["[1, 2, 3]"],
    ["{"],
])
def test_extract_json_returns_none_when_unusable(lines):
    assert ops.extract_json(lines) is None


def test_extract_json_skips_non_object_json():
    lines = ["[1,2,3]", '{"ok": false, "error": "x"}']
    assert ops.extract_json(lines) == {"ok": False, "error": "x"}


# --------------------------------------------------------------------------
# 进度行
# --------------------------------------------------------------------------
def test_parse_progress():
    assert ops.parse_progress("PROGRESS 12 48") == (12, 48)
    assert ops.parse_progress("PROGRESS 0 0") == (0, 0)


@pytest.mark.parametrize("line", [
    "PROGRESS 1", "PROGRESS a b", "PROGRESS 1 2 3", "progress 1 2", "普通日志",
])
def test_parse_progress_rejects_other_lines(line):
    assert ops.parse_progress(line) is None


# --------------------------------------------------------------------------
# 探针投放
# --------------------------------------------------------------------------
def test_ensure_probe_writes_and_is_idempotent(workspace):
    target = ops.ensure_probe(workspace)
    assert target.is_file()
    assert target == workspace.root / ".rdkquant" / "probe.py"
    first = target.read_text(encoding="utf-8")
    assert "rdkquant" in first

    mtime = target.stat().st_mtime_ns
    again = ops.ensure_probe(workspace)
    assert again == target
    assert target.stat().st_mtime_ns == mtime, "内容没变就不该重写"


def test_ensure_probe_rewrites_when_content_changes(workspace, monkeypatch):
    ops.ensure_probe(workspace)
    monkeypatch.setattr(ops, "probe_source", lambda: "# 改了\n")
    target = ops.ensure_probe(workspace)
    assert target.read_text(encoding="utf-8") == "# 改了\n"


def test_probe_source_is_python38_parsable():
    """探针要跑在 X3 的容器里（Python 3.8），语法必须兼容。"""
    import ast

    source = ops.probe_source()
    ast.parse(source, feature_version=(3, 8))


def test_probe_container_path_is_inside_the_mount():
    assert ops.PROBE_CONTAINER_PATH.startswith("/workspace/")
    assert ops.PROBE_CONTAINER_PATH.endswith("probe.py")


# --------------------------------------------------------------------------
# run_probe
# --------------------------------------------------------------------------
def test_run_probe_parses_json(workspace):
    runner = FakeRunner(['{"ok": true, "imaging_backend": "PIL"}'])
    payload = ops.run_probe(runner, workspace, ["env"])
    assert payload["ok"] is True
    assert payload["imaging_backend"] == "PIL"
    assert payload["_returncode"] == 0


def test_run_probe_command_uses_mounted_path(workspace):
    runner = FakeRunner(['{"ok": true}'])
    ops.run_probe(runner, workspace, ["inputs", "/workspace/model/m.onnx"])
    assert runner.commands[0] == [
        "python3", ops.PROBE_CONTAINER_PATH, "inputs", "/workspace/model/m.onnx",
    ]


def test_run_probe_routes_progress_lines_away_from_log(workspace):
    runner = FakeRunner([
        "PROGRESS 1 2", "PROGRESS 2 2", '{"ok": true}',
    ])
    seen: list[str] = []
    marks: list[tuple[int, int]] = []
    payload = ops.run_probe(
        runner, workspace, ["calib-build"],
        on_line=seen.append, on_progress=lambda d, t: marks.append((d, t)),
    )
    assert payload["ok"] is True
    assert marks == [(1, 2), (2, 2)]
    assert not any("PROGRESS" in line for line in seen), "进度行不该混进日志"


def test_run_probe_raises_with_raw_output_when_json_missing(workspace):
    runner = FakeRunner(["Traceback (most recent call last):", "ImportError: no onnx"])
    with pytest.raises(ops.ProbeError) as excinfo:
        ops.run_probe(runner, workspace, ["inputs", "x.onnx"])
    message = str(excinfo.value)
    assert "ImportError: no onnx" in message, "原始输出必须带出来，方便直接展示"
    assert "退出码 0" in message


def test_run_probe_keeps_ok_false_payload(workspace):
    """探针报告失败是**正常协议**，不该被当成解析错误抛异常。"""
    runner = FakeRunner(['{"ok": false, "error": "容器里没有 onnx"}'])
    payload = ops.run_probe(runner, workspace, ["inputs", "x.onnx"])
    assert payload["ok"] is False
    usable, reason = ops.probe_result_ok(payload)
    assert not usable
    assert "onnx" in reason


# --------------------------------------------------------------------------
# 便捷封装构造的命令行
# --------------------------------------------------------------------------
def test_probe_calib_scan_builds_expected_argv(workspace):
    runner = FakeRunner(['{"ok": true}'])
    ops.probe_calib_scan(
        runner, workspace, "/workspace/cal_images", shape="1x3x640x640", dtype="uint8",
    )
    command = runner.commands[0]
    assert command[2] == "calib-scan"
    assert "/workspace/cal_images" in command
    assert "--shape" in command and "1x3x640x640" in command
    assert "--dtype" in command and "uint8" in command


def test_probe_calib_build_builds_expected_argv(workspace):
    runner = FakeRunner(['{"ok": true}'])
    ops.probe_calib_build(
        runner, workspace, "/workspace/cal_images", "/workspace/calib_bin_f32",
        shape="1x3x640x640", layout="NCHW", color="bgr", dtype="float32",
        mean=[123.675, 116.28, 103.53], scale=[0.017],
    )
    command = runner.commands[0]
    joined = " ".join(command)
    assert "--layout NCHW" in joined
    assert "--color bgr" in joined
    assert "--mean 123.675 116.28 103.53" in joined
    assert "--scale 0.017" in joined


def test_probe_calib_build_omits_mean_scale_by_default(workspace):
    """默认不归一化——官方把 norm_type 插进模型，校准数据应是原始像素。"""
    runner = FakeRunner(['{"ok": true}'])
    ops.probe_calib_build(
        runner, workspace, "a", "b",
        shape="1x3x640x640", layout="NHWC", color="rgb", dtype="uint8",
    )
    assert "--mean" not in runner.commands[0]
    assert "--scale" not in runner.commands[0]


def test_probe_inputs_runs_env_probe_first_time(workspace):
    runner = FakeRunner(['{"ok": true}'])
    ops.probe_inputs(runner, workspace, "/workspace/model/m.onnx")
    assert (workspace.root / ".rdkquant" / "probe.py").is_file()
