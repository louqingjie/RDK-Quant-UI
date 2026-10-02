# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""工具链后端抽象。

有了这一层，UI 可以完全不关心「量化到底在哪跑」：

    MockBackend   —— 本机（Windows/macOS 也行），产出假日志与假产物。
                     它的价值是让整个 UI 在没装 Docker 的机器上就能开发完。
    DockerBackend —— 在 OpenExplorer 容器里跑真正的 hb_mapper。

两者的接口完全一致，所以 UI 代码一行都不用改。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .proc import LineCallback, which
from .runner import ContainerRunner, Mount

# --------------------------------------------------------------------------
# 工作区约定
# --------------------------------------------------------------------------
MODEL_DIR = "model"
CALIB_DIR = "cal_images"
OUTPUT_DIR = "output"
LOG_DIR = "logs"
CONFIG_NAME = "config.yaml"


@dataclass
class Workspace:
    """一次量化任务的工作目录。

    之所以强制这个约定：容器把 workspace 挂成 /workspace，config.yaml 里的
    相对路径（cal_data_dir、working_dir）都是相对它解析的。只要把模型和校准集
    都放进 workspace，路径就不会因为宿主机不同而炸掉。
    """

    root: Path

    @property
    def model_dir(self) -> Path:
        return self.root / MODEL_DIR

    @property
    def calib_dir(self) -> Path:
        return self.root / CALIB_DIR

    @property
    def output_dir(self) -> Path:
        return self.root / OUTPUT_DIR

    @property
    def log_dir(self) -> Path:
        return self.root / LOG_DIR

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    def ensure(self) -> "Workspace":
        for d in (self.root, self.model_dir, self.calib_dir, self.output_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def rel(self, path: os.PathLike | str) -> str:
        """转成容器内的路径（/workspace/...）。不在 workspace 内则原样返回。"""
        p = Path(path)
        try:
            rel = p.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return str(p)
        return "/workspace/" + rel.as_posix()


# --------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------
class ToolchainBackend(Protocol):
    name: str

    def check(
        self, onnx_path: Path, march: str, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> int: ...

    def compile(
        self, config_path: Path, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> int: ...


# --------------------------------------------------------------------------
# 真实后端
# --------------------------------------------------------------------------
@dataclass
class DockerBackend:
    image: str
    workspace: Workspace
    march: str = "bayes-e"
    shm_size: str = "4g"
    gpus: bool = False
    extra_mounts: tuple[Mount, ...] = ()
    name: str = "docker"

    def __post_init__(self) -> None:
        self.runner = ContainerRunner(
            image=self.image,
            workspace=self.workspace.root,
            shm_size=self.shm_size,
            gpus=self.gpus,
            extra_mounts=self.extra_mounts,
        )

    def check(self, onnx_path: Path, march: str | None = None, *, on_line: LineCallback, cancel=None) -> int:
        march = march or self.march
        return self.runner.run(
            [
                "hb_mapper", "checker",
                "--model-type", "onnx",
                "--march", march,
                "--model", self.workspace.rel(onnx_path),
            ],
            on_line=on_line,
            cancel=cancel,
        )

    def compile(self, config_path: Path | None = None, *, on_line: LineCallback, cancel=None) -> int:
        config_path = config_path or self.workspace.config_path
        return self.runner.run(
            [
                "hb_mapper", "makertbin",
                "--model-type", "onnx",
                "--config", self.workspace.rel(config_path),
            ],
            on_line=on_line,
            cancel=cancel,
        )

    def version(self) -> str:
        """容器里 hb_mapper 的版本——排查问题时的第一条信息。"""
        rc = self.runner.run(["hb_mapper", "--version"], on_line=lambda _l: None)
        return "ok" if rc == 0 else f"不可用(rc={rc})"


# --------------------------------------------------------------------------
# 假后端：让 UI 在没有 Docker 的机器上也能开发完
# --------------------------------------------------------------------------
# 这段假输出**刻意模仿真实格式**（实测自 X5 CPU 容器的 hb_mapper_checker.log）：
#   * 行首是 `时间戳 file: … func: … line No: N` 前缀
#   * 表头是 `Node ON Subgraph Type Cosine Similarity Threshold DataType`
#   * **`ON` 列装的是 BPU/CPU**
#   * 表格后面紧跟着 `The quantized model output:` 这类句子
# 只有这样 Mock 后端才能真正压到解析器的容错路径；用简化格式写等于没测。
_MOCK_CHECK = """\
2026-01-01 00:00:00,001 INFO log will be stored in hb_mapper_checker.log
2026-01-01 00:00:00,001 INFO Start hb_mapper....
2026-01-01 00:00:00,002 INFO hbdk version 3.49.15
2026-01-01 00:00:00,002 INFO horizon_nn version 1.1.0
2026-01-01 00:00:00,002 INFO hb_mapper version 1.24.3
2026-01-01 00:00:00,003 INFO Model type: onnx
2026-01-01 00:00:00,004 INFO The specified model compilation architecture: {march}.
2026-01-01 00:00:00,005 INFO Input ONNX Model Information:
ONNX IR version:          8
Opset version:            ['ai.onnx v11']
Graph input:
    images:               shape=[1, 3, 640, 640], dtype=FLOAT32
Graph output:
    output0:              shape=[1, 84, 8400], dtype=FLOAT32
2026-01-01 00:00:00,010 INFO The main quantized node information:
=====================================================================================
Node        ON   Subgraph  Type              Cosine Similarity  Threshold  DataType  
-------------------------------------------------------------------------------------
Conv_0      BPU  id(0)     HzSQuantizedConv  0.9992             1.0        int8
Conv_2      BPU  id(0)     HzSQuantizedConv  0.9985             1.0        int8
Softmax_91  CPU  id(0)     Softmax           0.9623             1.0        int8
Resize_118  CPU  id(1)     Resize            0.9401             1.0        int8
2026-01-01 00:00:00,020 INFO The quantized model output:
============
Output      
------------
2026-01-01 00:00:00,021 INFO End model checking....
"""

_MOCK_COMPILE = """\
[INFO] hb_mapper makertbin 开始
[INFO] 读取配置 {config}
[INFO] input_name = images, input_shape = 1x3x640x640
[INFO] input_type_rt = nv12  ->  将自动插入 YUV->RGB 转换节点
[INFO] calibration_parameters.cal_data_dir = ./cal_images （检测到 {n_cal} 张图片）
[INFO] 校准中 ... {n_cal}/{n_cal} samples
[INFO] 量化中 ... 生成 int8 模型
[INFO] 编译中 ... optimize_level = O3, compile_mode = latency
[SUCCESS] 转换完成
[SUCCESS] 输出: {stem}_{tag}_640x640_nv12.bin
[INFO] 建议下一步: hrt_model_exec perf --model_file {stem}_{tag}_640x640_nv12.bin --thread_num 1
"""


@dataclass
class MockBackend:
    """不依赖 Docker 的假实现。刻意把耗时做出来，好验证进度条与取消逻辑。"""

    workspace: Workspace
    march: str = "bayes-e"
    delay: float = 0.04
    name: str = "mock"

    def _emit(self, text: str, on_line: LineCallback, cancel) -> int:
        for line in text.splitlines():
            if cancel is not None and cancel.is_set():
                on_line("[已取消]")
                return 130
            on_line(line)
            if self.delay:
                time.sleep(self.delay)
        return 0

    def check(self, onnx_path: Path, march: str | None = None, *, on_line: LineCallback, cancel=None) -> int:
        on_line(f"$ (mock) hb_mapper checker --model {onnx_path.name}")
        return self._emit(_MOCK_CHECK.format(march=march or self.march), on_line, cancel)

    def compile(self, config_path: Path | None = None, *, on_line: LineCallback, cancel=None) -> int:
        self.workspace.ensure()
        cfg = config_path or self.workspace.config_path
        n_cal = len(list(self.workspace.calib_dir.glob("*"))) if self.workspace.calib_dir.exists() else 48
        tag = "bayes-e" if self.march == "bayes-e" else "bernoulli2"
        stem = "model"
        on_line(f"$ (mock) hb_mapper makertbin --config {cfg.name}")
        rc = self._emit(
            _MOCK_COMPILE.format(config=cfg.name, n_cal=n_cal, stem=stem, tag=tag),
            on_line,
            cancel,
        )
        if rc == 0:
            out = self.workspace.output_dir / f"{stem}_{tag}_640x640_nv12.bin"
            out.write_bytes(b"MOCK-BIN" + b"\x00" * 1024)
            on_line(f"[mock] 已写出假产物 {out}")
        return rc

    def version(self) -> str:
        return "mock"


def make_backend(
    *,
    workspace: Workspace,
    image: str | None = None,
    march: str = "bayes-e",
    shm_size: str = "4g",
    gpus: bool = False,
    prefer_mock: bool = False,
) -> ToolchainBackend:
    """按环境自动选择后端：没有 docker 就退回 mock，让 UI 始终可用。"""
    workspace.ensure()
    if prefer_mock or not which("docker") or not image:
        return MockBackend(workspace=workspace, march=march)
    return DockerBackend(
        image=image, workspace=workspace, march=march, shm_size=shm_size, gpus=gpus
    )
