# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""容器侧探针的宿主端客户端。

架构：宿主是薄编排层，**容器是唯一的执行环境**。ONNX 图结构内省、图像测量、
二进制校准数据生成这些需要 onnx / 图像库的活，全部放进容器做，于是宿主
一个第三方依赖都不需要（`dependencies = []` 保持为真），而且我们看到的输入
形状与 `hb_mapper` 看到的必然一致——同一个 Python、同一套库。

协议：探针 stdout 的**最后一行**是单行 JSON（纯 ASCII，见探针里的说明）。
任何异常都被探针转成 `{"ok": false, "error": ...}`，所以这里永远不需要
解析 traceback。
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

PROBE_DIR_NAME = ".rdkquant"
PROBE_FILE_NAME = "probe.py"
#: 工作区挂载为 /workspace，所以探针在容器里的固定路径就是这个
PROBE_CONTAINER_PATH = "/workspace/%s/%s" % (PROBE_DIR_NAME, PROBE_FILE_NAME)

#: 逐张统计（calib-scan --stats-file 写、宿主读来做筛选）
STATS_FILE_NAME = "calib-stats.json"
#: 校准集名单（宿主写、探针 --only-file 读）
SELECTION_FILE_NAME = "selection.txt"

#: 往回找 JSON 时最多看多少行（容忍探针之后可能出现的少量尾部输出）
_TAIL_SCAN = 20

ProgressCallback = Callable[[int, int], None]


class ProbeError(RuntimeError):
    """探针没能给出可用的 JSON。原始输出会附在消息里，方便直接展示给用户。"""


# ---------------------------------------------------------------------------
# 探针源码的投放
# ---------------------------------------------------------------------------
def probe_source() -> str:
    """读取打包进来的探针源码。

    探针是 `rdkquant/data/` 下的**包数据**，不是被导入的模块——它有独立的
    Python 3.8 兼容约束，不能混进我们的包。
    """
    resource = (
        importlib.resources.files("rdkquant").joinpath("data").joinpath("rdkquant_probe.py")
    )
    return resource.read_text(encoding="utf-8")


def ensure_probe(workspace: Any) -> Path:
    """把探针写进工作区，返回宿主侧路径。

    写进工作区而不是额外挂载一个目录，是因为工作区**本来就挂载为 /workspace**
    ——这样不用改 OE 镜像、不用加 `-v`，打包之后也能用。

    按内容哈希判断是否需要重写，避免每次启动都动用户的磁盘。
    """
    source = probe_source()
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()
    directory = workspace.root / PROBE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / PROBE_FILE_NAME
    stamp = directory / (PROBE_FILE_NAME + ".sha1")

    if target.is_file() and stamp.is_file():
        try:
            if stamp.read_text(encoding="utf-8").strip() == digest:
                return target
        except OSError:
            pass

    target.write_text(source, encoding="utf-8")
    stamp.write_text(digest, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# 协议解析
# ---------------------------------------------------------------------------
def extract_json(lines: Iterable[str]) -> dict | None:
    """从输出里取出探针的 JSON。

    从后往前找第一个能解析成 JSON 对象的行。按契约它在最后一行，但容器可能
    在探针之后打点别的东西，所以往回看有限的几行更稳。
    解析失败返回 None——**不抛异常**，由调用方决定怎么报。
    """
    tail = [line for line in lines if line and line.strip()][-_TAIL_SCAN:]
    for line in reversed(tail):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def parse_progress(line: str) -> tuple[int, int] | None:
    """识别 `PROGRESS <done> <total>` 行。"""
    parts = line.strip().split()
    if len(parts) == 3 and parts[0] == "PROGRESS":
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------
def run_probe(
    runner: Any,
    workspace: Any,
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: Any = None,
) -> dict:
    """在容器里跑一次探针，返回解析后的 JSON。

    `PROGRESS` 行会被转成 `on_progress` 而不是混进日志，避免把进度条刷进日志面板。
    """
    ensure_probe(workspace)
    collected: list[str] = []

    def handle(line: str) -> None:
        marker = parse_progress(line)
        if marker is not None:
            if on_progress:
                on_progress(*marker)
            return
        collected.append(line)
        if on_line:
            on_line(line)

    command = ["python3", PROBE_CONTAINER_PATH, *[str(a) for a in argv]]
    returncode = runner.run(command, on_line=handle, cancel=cancel)

    payload = extract_json(collected)
    if payload is None:
        tail = "\n".join(collected[-20:])
        raise ProbeError(
            "容器里的探针没有返回可解析的 JSON（退出码 %s）。原始输出：\n%s"
            % (returncode, tail or "（无输出）")
        )
    payload.setdefault("_returncode", returncode)
    return payload


# ---------------------------------------------------------------------------
# 各子命令的便捷封装
# ---------------------------------------------------------------------------
def probe_env(runner, workspace, **kw) -> dict:
    return run_probe(runner, workspace, ["env"], **kw)


def probe_inputs(runner, workspace, onnx_path: str, **kw) -> dict:
    return run_probe(runner, workspace, ["inputs", onnx_path], **kw)


def write_selection(workspace: Any, names: Sequence[str]) -> Path:
    """把选中的文件名写成一份名单，供容器侧 `--only-file` 读取。

    用**文件**而不是 `--only a,b,c`：文件名可能含逗号；几百个名字堆在命令行里
    没法看；写成文件后用户还能自己去核对、去复现。

    写在 `<ws>/.rdkquant/` 下 —— 探针本身就放在那里（`ensure_probe`），
    工作区又已经挂载为 `/workspace`，不需要任何新的挂载约定。
    """
    directory = workspace.root / PROBE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / SELECTION_FILE_NAME
    lines = ["# 校准集名单：每行一个文件名，由界面生成", *names]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def stats_path(workspace_root: Path | str) -> Path:
    """逐张统计文件的**宿主侧**路径。传入工作区**根目录**。

    ⚠️ 参数是目录，不是 `Workspace` 对象。这里踩过一个很阴的坑：原来写的是
    `workspace.root`，而页面传进来的是 `Path` —— **`Path` 恰好也有 `.root`
    属性**，它是字符串（盘符或 `/`），于是 `str / str` 抛
    `TypeError: unsupported operand type(s) for /: 'str' and 'str'`。
    报错信息和真实原因（传错了类型）看起来毫不相干，排查成本很高。
    现在显式 `Path()` 一层，传 Path 或 Workspace.root 都对。
    """
    return Path(workspace_root) / PROBE_DIR_NAME / STATS_FILE_NAME


def probe_calib_scan(
    runner, workspace, directory: str, *, shape: str | None = None,
    dtype: str = "float32", want_stats: bool = False, **kw
) -> dict:
    argv = ["calib-scan", directory]
    if shape:
        argv += ["--shape", shape]
    if dtype:
        argv += ["--dtype", dtype]
    if want_stats:
        # 让探针把逐张统计写到工作区里。走文件而不是回包：3504 条约 150 KB，
        # 作为"最后一行 JSON"会变成一条 150 KB 的日志行，文本框渲染不动。
        argv += ["--stats-file", workspace.rel(stats_path(workspace.root))]
    return run_probe(runner, workspace, argv, **kw)


def probe_calib_build(
    runner, workspace, src: str, out: str, *,
    shape: str, layout: str, color: str, dtype: str,
    count: int = 0,
    only: Sequence[str] = (),
    mean: Sequence[float] | None = None,
    scale: Sequence[float] | None = None,
    **kw
) -> dict:
    argv = [
        "calib-build", src, out,
        "--shape", shape, "--layout", layout, "--color", color, "--dtype", dtype,
    ]
    if only:
        # 名单优先于采样：它是用户明确的意图，而 count 只是起点
        argv += ["--only-file", workspace.rel(write_selection(workspace, only))]
    elif count:
        argv += ["--count", str(int(count))]
    if mean:
        argv += ["--mean", " ".join(str(v) for v in mean)]
    if scale:
        argv += ["--scale", " ".join(str(v) for v in scale)]
    return run_probe(runner, workspace, argv, **kw)


def probe_thumbs(runner, workspace, src: str, out: str, *, size: int = 96,
                 count: int = 0, only: Sequence[str] = (), **kw) -> dict:
    argv = ["thumbs", src, out, "--size", str(size)]
    if only:
        # 与 calib-build 共用同一份名单 —— 预览里看到的就是实际会用到的那几张
        argv += ["--only-file", workspace.rel(write_selection(workspace, only))]
    elif count:
        argv += ["--count", str(int(count))]
    return run_probe(runner, workspace, argv, **kw)


def probe_result_ok(payload: dict) -> tuple[bool, str]:
    """把探针回包的 `ok` 与 `error` 变成 (是否可用, 可读原因)。"""
    if payload.get("ok"):
        return True, ""
    return False, str(payload.get("error") or "探针报告了未知错误")
