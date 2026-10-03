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
from typing import Protocol, Sequence

from . import containerops
from .calib import CalibBuildSpec
from .containerops import ProgressCallback
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
    """工具链后端。

    前两个是"跑工具链命令"，后五个是"在容器里做计算"。之所以都放在后端协议上：
    页面代码只认后端，于是 Mock 与真实容器的代码路径**完全一致**，CI 不需要
    Docker 也能把整条 UI 流程压出来。
    """

    name: str

    def check(
        self, onnx_path: Path, march: str, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> int: ...

    def compile(
        self, config_path: Path, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> int: ...

    # --- 以下五个都由容器侧探针实现（见 core/containerops.py）---

    def inspect_model(
        self, onnx_path: Path, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> dict: ...

    def scan_calib(
        self, directory: Path, *, shape: str | None = None, dtype: str = "float32",
        want_stats: bool = False, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> dict: ...

    def build_calib(
        self, src: Path, out: Path, spec: CalibBuildSpec, *, on_line: LineCallback,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> dict: ...

    def thumbnails(
        self, src: Path, out: Path, *, size: int = 96, count: int = 0,
        only: Sequence[str] = (), on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> dict: ...

    def probe_env(
        self, *, on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> dict: ...


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

    # ------------------------------------------------------------------
    # 容器侧计算：全部委托给探针客户端（core/containerops.py）
    # ------------------------------------------------------------------
    def probe_env(self, *, on_line: LineCallback, cancel=None) -> dict:
        return containerops.probe_env(
            self.runner, self.workspace, on_line=on_line, cancel=cancel
        )

    def inspect_model(self, onnx_path: Path, *, on_line: LineCallback, cancel=None) -> dict:
        return containerops.probe_inputs(
            self.runner, self.workspace, self.workspace.rel(onnx_path),
            on_line=on_line, cancel=cancel,
        )

    def scan_calib(self, directory: Path, *, shape=None, dtype="float32",
                   want_stats: bool = False, on_line: LineCallback, cancel=None) -> dict:
        return containerops.probe_calib_scan(
            self.runner, self.workspace, self.workspace.rel(directory),
            shape=shape, dtype=dtype, want_stats=want_stats,
            on_line=on_line, cancel=cancel,
        )

    def build_calib(self, src: Path, out: Path, spec: CalibBuildSpec, *,
                    on_line: LineCallback, on_progress=None, cancel=None) -> dict:
        return containerops.probe_calib_build(
            self.runner, self.workspace,
            self.workspace.rel(src), self.workspace.rel(out),
            shape=spec.shape, layout=spec.layout, color=spec.color, dtype=spec.dtype,
            count=spec.count, only=spec.only,
            mean=spec.mean, scale=spec.scale,
            on_line=on_line, on_progress=on_progress, cancel=cancel,
        )

    def thumbnails(self, src: Path, out: Path, *, size: int = 96, count: int = 0,
                   only: Sequence[str] = (), on_line: LineCallback, cancel=None) -> dict:
        return containerops.probe_thumbs(
            self.runner, self.workspace,
            self.workspace.rel(src), self.workspace.rel(out),
            size=size, count=count, only=only, on_line=on_line, cancel=cancel,
        )


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


# --------------------------------------------------------------------------
# 假后端返回的 JSON：**形状与真实探针完全一致**
#
# 这不是"随便编点数据"——test_backends.py 会拿这里和真实探针的契约对比，防止
# 两边漂移。数值取自 X5 CPU 容器的实测环境（见 docs/design/container-findings.md）。
# --------------------------------------------------------------------------
#: 假校准集张数。取 48 是因为它落在官方建议区间 20~100 之内，不必额外造警告。
MOCK_CALIB_COUNT = 48

MOCK_ENV = {
    "ok": True,
    "python": "3.10.15",
    "executable": "/usr/bin/python3",
    "imaging_backend": "PIL",
    "imaging_candidates": ["PIL", "skimage", "cv2"],
    "onnx": "1.12.0",
    "numpy": "1.23.0",
}

MOCK_INPUTS = {
    "ok": True,
    "path": "model.onnx",
    "size": 0,
    "python": "3.10.15",
    "onnx": "1.12.0",
    "ir_version": 8,
    "opset": 11,
    "inputs": [
        {"name": "images", "shape": [1, 3, 640, 640],
         "dtype": "float32", "dynamic": False},
    ],
    "outputs": [
        {"name": "output0", "shape": [1, 84, 8400],
         "dtype": "float32", "dynamic": False},
    ],
    "op_histogram": [
        ["Conv", 120], ["Mul", 40], ["Sigmoid", 33], ["Concat", 21],
        ["Resize", 4], ["Softmax", 3], ["MaxPool", 3], ["Add", 2],
    ],
    "problems": [],
    "warnings": [],
}

MOCK_SCAN = {
    "ok": True,
    "directory": "cal_images",
    "imaging_backend": "PIL",
    "count": MOCK_CALIB_COUNT,
    "image_like": MOCK_CALIB_COUNT,
    "binary_like": 0,
    "measured": True,
    "files": [],
    "resolutions": [[640, 640, MOCK_CALIB_COUNT]],
    "duplicates": [],
    "exposure": {
        "sampled": 32, "min_mean": 42.1, "max_mean": 187.3,
        "max_near_black": 0.008, "max_near_white": 0.011,
    },
    "problems": [],
    "warnings": [],
    # 逐张统计的条数。真实探针**总是**返回它（stats_file 才是按需的）
    "stats_count": MOCK_CALIB_COUNT,
}

#: 每个校准样本的字节数：1×3×640×640 float32
MOCK_SAMPLE_BYTES = 3 * 640 * 640 * 4

MOCK_BUILD = {
    "ok": True,
    "backend": "PIL",
    "written": MOCK_CALIB_COUNT,
    "failed": 0,
    "failures": [],
    "bytes": MOCK_CALIB_COUNT * MOCK_SAMPLE_BYTES,
    "bytes_per_sample": MOCK_SAMPLE_BYTES,
    "out_dir": "calib_bin_f32",
    "shape": [1, 3, 640, 640],
    "layout": "NCHW",
    "color": "bgr",
    "dtype": "float32",
    "normalized": False,
}


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

    # ------------------------------------------------------------------
    # 容器侧计算的假实现
    #
    # 返回的 JSON **形状与真实探针完全一致**（test_backends.py 里有契约漂移检测），
    # 所以页面代码在 Mock 下走的是同一条路径。数值取 X5 容器实测到的环境。
    # ------------------------------------------------------------------
    def probe_env(self, *, on_line: LineCallback, cancel=None) -> dict:
        on_line("$ (mock) probe.py env")
        on_line("[mock] 容器环境：python 3.10.15 / onnx 1.12.0 / 图像库 PIL")
        return dict(MOCK_ENV)

    def inspect_model(self, onnx_path: Path, *, on_line: LineCallback, cancel=None) -> dict:
        self.workspace.ensure()
        on_line(f"$ (mock) probe.py inputs {onnx_path.name}")
        if not onnx_path.is_file():
            # 真实探针会返回 ok:false，这里也照做，不让 Mock 比真实更宽松
            on_line(f"[mock] 模型文件不存在：{onnx_path}")
        payload = dict(MOCK_INPUTS)
        payload["path"] = onnx_path.name
        payload["size"] = onnx_path.stat().st_size if onnx_path.is_file() else 0
        return payload

    def scan_calib(self, directory: Path, *, shape=None, dtype="float32",
                   want_stats: bool = False, on_line: LineCallback, cancel=None) -> dict:
        self.workspace.ensure()
        on_line(f"$ (mock) probe.py calib-scan {directory.name}")
        entries = (
            [p for p in directory.iterdir() if p.is_file()] if directory.is_dir() else []
        )
        if not entries:
            on_line("[mock] 目录不存在或为空")
            payload = dict(MOCK_SCAN)
            payload.update({
                "ok": True, "directory": directory.name, "count": 0,
                "image_like": 0, "binary_like": 0, "resolutions": [],
                "duplicates": [], "exposure": None,
                "files": [], "warnings": [], "problems": ["目录是空的"],
            })
            return payload
        payload = dict(MOCK_SCAN)
        payload["directory"] = directory.name
        if want_stats:
            target = _write_mock_stats(self.workspace)
            payload["stats_count"] = MOCK_SCAN["count"]
            payload["stats_file"] = self.workspace.rel(target)
        return payload

    def build_calib(self, src: Path, out: Path, spec: CalibBuildSpec, *,
                    on_line: LineCallback, on_progress=None, cancel=None) -> dict:
        self.workspace.ensure()
        on_line(f"$ (mock) probe.py calib-build {src.name} {out.name}")
        out.mkdir(parents=True, exist_ok=True)
        # 名单优先于采样（和真实探针一致）
        count = len(spec.only) if spec.only else (spec.count or MOCK_SCAN["count"])
        per_sample = MOCK_BUILD["bytes_per_sample"]
        written = 0
        for index in range(count):
            if cancel is not None and cancel.is_set():
                on_line("[已取消]")
                return {"ok": False, "error": "已取消"}
            target = out / f"{index:04d}.bin"
            # truncate 造出**字节数正确**的文件但不真的写数据：UI 开发关心的是
            # 长度契约与进度，不是几百 MB 的内容。
            with open(target, "wb") as handle:
                handle.truncate(per_sample)
            written += 1
            if on_progress:
                on_progress(index + 1, count)
            if self.delay:
                time.sleep(self.delay / 4)
        payload = dict(MOCK_BUILD)
        payload["written"] = written
        payload["bytes"] = written * per_sample
        payload["out_dir"] = out.name
        on_line(f"[mock] 已写出 {written} 个校准样本到 {out.name}/")
        return payload

    def thumbnails(self, src: Path, out: Path, *, size: int = 96, count: int = 0,
                   only: Sequence[str] = (), on_line: LineCallback, cancel=None) -> dict:
        out.mkdir(parents=True, exist_ok=True)
        on_line(f"$ (mock) probe.py thumbs {src.name} {out.name}")
        return {
            "ok": True, "backend": "PIL", "generated": 0, "failed": 0, "files": [],
        }


def _write_mock_stats(workspace: Workspace) -> Path:
    """假后端也写一份逐张统计，让筛选那条界面链路在 Mock 下能走通。

    刻意造出**几种不同的分辨率与长宽比** —— 全一样的假数据没法压出筛选的边界
    （比例筛选、范围筛选都会变成恒真）。
    """
    import json

    target = containerops.stats_path(workspace.root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shapes = ((640, 640), (1920, 1080), (1280, 720), (320, 240))
    files = []
    for index in range(MOCK_SCAN["count"]):
        width, height = shapes[index % len(shapes)]
        files.append({
            "n": "%04d.jpg" % index,
            "w": width, "h": height,
            "s": 40000 + index, "m": "RGB",
        })
    target.write_text(
        json.dumps({"version": 1, "count": len(files), "files": files}),
        encoding="utf-8",
    )
    return target


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
