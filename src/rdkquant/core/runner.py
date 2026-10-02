# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""容器运行器：把「在 Docker 里跑 hb_mapper」这件事收敛成两个纯函数 + 一层薄封装。

`build_run_argv()` 刻意写成**不执行任何东西的纯函数**，因为它是整个项目最容易
出错、又最难端到端测试的地方（本机现在没装 Docker）。把 argv 拼装隔离出来，
就能用单元测试覆盖 Windows/Linux 两套路径规则、GPU、只读挂载、shm 等分支。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .proc import Cancelled, LineCallback, stream_lines, which

GB = 1024 ** 3
DEFAULT_WORKDIR = "/workspace"


@dataclass(frozen=True)
class Mount:
    """宿主机目录 → 容器目录的映射。"""

    host: Path
    container: str
    read_only: bool = False

    def to_arg(self, *, windows: bool | None = None) -> str:
        host = normalize_host_path(self.host, windows=windows)
        spec = f"{host}:{self.container}"
        if self.read_only:
            spec += ":ro"
        return spec


def normalize_host_path(path: os.PathLike | str, *, windows: bool | None = None) -> str:
    """转成 Docker 能接受的宿主机路径。

    Windows 上反斜杠要换成正斜杠（Docker Desktop 认 `C:/Users/x`，但对
    `C:\\Users\\x` 的支持在不同版本上并不一致）。

    `windows` 描述的是**目标 Docker 主机**的平台，不是当前解释器的平台。
    两者不一致只发生在测试里（在 Windows 上模拟 Linux），此时不能套用
    Windows 的路径解析规则——`Path("/opt/x").resolve()` 在 Windows 上会
    变成 `C:\\opt\\x`，那是个静默的错误。
    """
    if windows is None:
        windows = os.name == "nt"
    raw = str(path)
    if not windows:
        # POSIX 路径无需转换。只有在真的跑在 POSIX 上时才做 abspath，
        # 否则（Windows 上模拟 Linux 目标）Path 已经悄悄把 "/" 换成了 "\\"，
        # 这里必须转回来，且不能套用 Windows 的盘符解析。
        if os.name == "posix":
            return os.path.abspath(raw)
        return raw.replace("\\", "/")

    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(raw))
    return str(p).replace("\\", "/")


@dataclass
class RunSpec:
    image: str
    workspace: Path
    command: Sequence[str]
    workdir: str = DEFAULT_WORKDIR
    shm_size: str = "4g"
    gpus: bool = False
    extra_mounts: tuple[Mount, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    name: str | None = None
    remove: bool = True
    # 让容器以宿主机当前用户身份运行，避免挂载卷里的产物变成 root 所有。
    # 官方 FAQ 里「拷贝回来的文件权限不对」就是这个坑。
    match_host_user: bool = True
    # 显式覆盖 uid:gid。留空则从当前进程推导——单独抽出来是为了能跨平台单测。
    user: str | None = None
    network: str | None = None   # 默认不传：hb_mapper 不需要网络
    windows: bool | None = None  # 仅测试用


def build_run_argv(spec: RunSpec) -> list[str]:
    """拼出 `docker run ...` 的完整参数列表（纯函数，不执行）。"""
    windows = os.name == "nt" if spec.windows is None else spec.windows

    argv: list[str] = ["docker", "run"]
    if spec.remove:
        argv.append("--rm")
    if spec.name:
        argv += ["--name", spec.name]

    # 工作目录是必须的：容器内的相对路径（config.yaml 里的 ./xxx）都基于它解析
    argv += ["--workdir", spec.workdir]
    argv += ["--shm-size", spec.shm_size]

    if spec.gpus:
        argv += ["--gpus", "all"]

    if spec.network:
        argv += ["--network", spec.network]

    if spec.match_host_user and not windows:
        uid = spec.user
        if uid is None and hasattr(os, "getuid"):
            uid = f"{os.getuid()}:{os.getgid()}"
        if uid:
            argv += ["--user", uid]

    mounts = (Mount(spec.workspace, spec.workdir),) + tuple(spec.extra_mounts)
    for mount in mounts:
        argv += ["-v", mount.to_arg(windows=windows)]

    for key, value in spec.env:
        argv += ["-e", f"{key}={value}"]

    argv.append(spec.image)
    argv.extend(str(c) for c in spec.command)
    return argv


@dataclass
class ContainerRunner:
    """按固定配置反复执行容器命令。"""

    image: str
    workspace: Path
    shm_size: str = "4g"
    gpus: bool = False
    extra_mounts: tuple[Mount, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    match_host_user: bool = True

    def argv(self, command: Sequence[str], *, workdir: str = DEFAULT_WORKDIR) -> list[str]:
        return build_run_argv(
            RunSpec(
                image=self.image,
                workspace=self.workspace,
                command=command,
                workdir=workdir,
                shm_size=self.shm_size,
                gpus=self.gpus,
                extra_mounts=self.extra_mounts,
                env=self.env,
                match_host_user=self.match_host_user,
            )
        )

    def run(
        self,
        command: Sequence[str],
        *,
        on_line: LineCallback,
        cancel: threading.Event | None = None,
    ) -> int:
        if not which("docker"):
            on_line("[错误] 未找到 docker 命令。请先完成环境体检页里的 Docker 安装。")
            return 127
        argv = self.argv(command)
        on_line("$ " + " ".join(argv))
        return stream_lines(argv, on_line, cancel=cancel)


def default_shm_size(total_ram_bytes: int) -> str:
    """官方示例写 --shm-size=15g，但那是给大内存工作站的写法。

    在 16 GB 的机器上照抄会直接把机器拖死，所以按总内存的 1/4 夹逼。
    """
    if total_ram_bytes <= 0:
        return "4g"
    size = max(1 * GB, min(8 * GB, total_ram_bytes // 4))
    return f"{size // GB}g"
