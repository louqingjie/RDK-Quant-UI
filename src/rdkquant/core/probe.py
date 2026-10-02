# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""宿主机环境体检。

这个模块存在的理由很实际：UI 在宿主机、量化在容器里，所以「为什么跑不起来」
的原因有九成落在环境上（Docker 没装 / daemon 没起 / 磁盘不够 / 内存不够 /
WSL 没装）。与其让用户对着报错猜，不如一次性给出结论和下一步动作。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .proc import run_capture, which

GB = 1024 ** 3

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class DockerStatus:
    cli_path: str | None = None
    cli_version: str | None = None
    daemon_ok: bool = False
    daemon_version: str | None = None
    error: str = ""


@dataclass(frozen=True)
class GpuStatus:
    nvidia_smi_path: str | None = None
    devices: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.devices)


@dataclass(frozen=True)
class DiskStatus:
    path: str = ""
    total_bytes: int = 0
    free_bytes: int = 0


@dataclass(frozen=True)
class RamStatus:
    total_bytes: int = 0
    available_bytes: int = 0


@dataclass
class HostReport:
    platform: str = ""
    is_windows: bool = False
    is_linux: bool = False
    arch: str = ""
    python: str = ""
    docker: DockerStatus = field(default_factory=DockerStatus)
    gpu: GpuStatus = field(default_factory=GpuStatus)
    disk: DiskStatus = field(default_factory=DiskStatus)
    ram: RamStatus = field(default_factory=RamStatus)
    wsl_installed: bool | None = None
    wsl_distros: tuple[str, ...] = ()
    recommended_shm_bytes: int = 4 * GB
    findings: list[Finding] = field(default_factory=list)

    # ---- 便捷判定 ----
    @property
    def can_quantize(self) -> bool:
        """能否真正跑通量化闭环。"""
        return self.docker.daemon_ok and not any(
            f.level == LEVEL_ERROR for f in self.findings
        )

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == LEVEL_ERROR]

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "arch": self.arch,
            "python": self.python,
            "docker": {
                "cli_path": self.docker.cli_path,
                "cli_version": self.docker.cli_version,
                "daemon_ok": self.docker.daemon_ok,
                "daemon_version": self.docker.daemon_version,
                "error": self.docker.error,
            },
            "gpu": {"devices": list(self.gpu.devices)},
            "disk": {
                "path": self.disk.path,
                "total_gb": round(self.disk.total_bytes / GB, 1),
                "free_gb": round(self.disk.free_bytes / GB, 1),
            },
            "ram": {
                "total_gb": round(self.ram.total_bytes / GB, 1),
                "available_gb": round(self.ram.available_bytes / GB, 1),
            },
            "wsl": {"installed": self.wsl_installed, "distros": list(self.wsl_distros)},
            "recommended_shm_gb": round(self.recommended_shm_bytes / GB, 1),
            "can_quantize": self.can_quantize,
            "findings": [f.as_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# 单项探测
# --------------------------------------------------------------------------
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")

#: `docker run --workdir` 需要 API 1.35，也就是 Docker 17.09+
DOCKER_MIN_SUPPORTED = (17, 9)
#: `docker run --gpus` 需要 Docker 19.03+
DOCKER_MIN_FOR_GPU = (19, 3)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """把 "24.0.7" 或 "Docker version 20.10.24, build 5d6db84" 解析成 (24, 0, 7)。"""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    parts: list[int] = []
    for chunk in match.group(1).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return None
    return tuple(parts) or None


def version_lt(version: tuple[int, ...] | None, target: tuple[int, ...]) -> bool:
    """版本比较。先补齐长度再比，否则 (19,) 会被判成小于 (19, 3)。"""
    if version is None:
        return False
    size = max(len(version), len(target))
    left = version + (0,) * (size - len(version))
    right = target + (0,) * (size - len(target))
    return left < right


def probe_docker() -> DockerStatus:
    exe = which("docker")
    if not exe:
        return DockerStatus(error="未找到 docker 命令")

    cli_version = None
    res = run_capture([exe, "--version"], timeout=15)
    if res.ok:
        m = _VERSION_RE.search(res.stdout)
        cli_version = m.group(1) if m else res.stdout

    # 这一条是唯一能证明「daemon 活着」的检查
    srv = run_capture([exe, "version", "--format", "{{.Server.Version}}"], timeout=25)
    if srv.ok and srv.stdout:
        return DockerStatus(exe, cli_version, True, srv.stdout.splitlines()[0].strip())
    return DockerStatus(exe, cli_version, False, None, srv.stderr or srv.stdout or "docker daemon 无响应")


def probe_gpu() -> GpuStatus:
    exe = which("nvidia-smi")
    if not exe:
        return GpuStatus()
    res = run_capture(
        [exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], timeout=20
    )
    if not res.ok:
        return GpuStatus(exe)
    devices = tuple(line.strip() for line in res.stdout.splitlines() if line.strip())
    return GpuStatus(exe, devices)


def probe_disk(path: os.PathLike | str) -> DiskStatus:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
        return DiskStatus(str(target), usage.total, usage.free)
    except OSError:
        return DiskStatus(str(target))


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def probe_ram() -> RamStatus:
    if sys.platform == "win32":
        try:
            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return RamStatus(int(stat.ullTotalPhys), int(stat.ullAvailPhys))
        except Exception:  # noqa: BLE001 - 探测失败不应影响主流程
            pass
        return RamStatus()
    # Linux / macOS
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            total = avail = 0
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
            return RamStatus(total, avail)
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page
        return RamStatus(total, 0)
    except (OSError, ValueError):
        return RamStatus()


def probe_wsl() -> tuple[bool | None, tuple[str, ...]]:
    """仅 Windows 有意义。"""
    if sys.platform != "win32":
        return None, ()
    res = run_capture(["wsl.exe", "-l", "-q"], timeout=20)
    if not res.ok:
        return False, ()
    # wsl.exe 输出是 UTF-16LE，走 text 解码可能带 \x00
    names = tuple(
        n.replace("\x00", "").strip()
        for n in res.stdout.splitlines()
        if n.replace("\x00", "").strip()
    )
    return True, names


def recommended_shm_bytes(total_ram: int) -> int:
    """官方示例建议 --shm-size=15g，但那是给大内存工作站的。

    这里取总内存的 1/4，夹在 1G ~ 8G 之间——在 16G 机器上给出 4G，
    既能编译常见模型，又不会把机器拖死。
    """
    if total_ram <= 0:
        return 4 * GB
    return max(1 * GB, min(8 * GB, total_ram // 4))


# --------------------------------------------------------------------------
# 组合体检
# --------------------------------------------------------------------------
def probe_host(
    workspace: os.PathLike | str = ".",
    *,
    required_image_bytes: int | None = None,
    required_image_name: str = "",
) -> HostReport:
    report = HostReport()
    report.platform = f"{sys.platform} {os.name}"
    report.is_windows = sys.platform == "win32"
    report.is_linux = sys.platform.startswith("linux")
    report.arch = os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "?")
    report.python = sys.version.split()[0]

    report.docker = probe_docker()
    report.gpu = probe_gpu()
    report.disk = probe_disk(workspace)
    report.ram = probe_ram()
    report.wsl_installed, report.wsl_distros = probe_wsl()
    report.recommended_shm_bytes = recommended_shm_bytes(report.ram.total_bytes)

    f: list[Finding] = []

    # --- Docker ---
    if report.docker.cli_path is None:
        hint = (
            "Windows：安装 Docker Desktop（会自动装好 WSL2），或先 `wsl --install` "
            "再在发行版里装 docker engine。"
            if report.is_windows
            else "Linux：sudo apt install docker.io 或按官方文档装 docker-ce，并把当前用户加入 docker 组。"
        )
        f.append(Finding(LEVEL_ERROR, "DOCKER_CLI_MISSING", "未安装 Docker 命令行", hint))
    elif not report.docker.daemon_ok:
        hint = (
            "启动 Docker Desktop，确认右下角鲸鱼图标变绿；若刚装完 WSL2，需要重启一次。"
            if report.is_windows
            else "sudo systemctl start docker；另外确认当前用户有权限访问 /var/run/docker.sock。"
        )
        f.append(
            Finding(
                LEVEL_ERROR,
                "DOCKER_DAEMON_DOWN",
                f"Docker 命令行存在（{report.docker.cli_version}）但守护进程无响应",
                hint,
            )
        )
    else:
        f.append(
            Finding(
                LEVEL_OK,
                "DOCKER_READY",
                f"Docker 就绪：客户端 {report.docker.cli_version}，服务端 {report.docker.daemon_version}",
            )
        )
        # 版本下限：我们只用到 docker load / run / image ls / version，
        # 这些在发行版的 docker.io 和官方 docker-ce 上行为一致。
        # 真正需要卡的是 --workdir 和 --gpus 的引入版本。
        version = parse_version(report.docker.cli_version)
        if version_lt(version, DOCKER_MIN_SUPPORTED):
            f.append(
                Finding(
                    LEVEL_ERROR,
                    "DOCKER_TOO_OLD",
                    f"Docker {report.docker.cli_version} 太旧："
                    f"容器运行需要 --workdir，要求 {'.'.join(map(str, DOCKER_MIN_SUPPORTED))} 以上",
                    "Ubuntu 22.04+ 可直接 apt install docker.io；"
                    "或按 Docker 官方文档装 docker-ce。两者都可以，见 README。",
                )
            )
        elif report.gpu.available and version_lt(version, DOCKER_MIN_FOR_GPU):
            f.append(
                Finding(
                    LEVEL_WARN,
                    "DOCKER_OLD_FOR_GPU",
                    f"Docker {report.docker.cli_version}：--gpus 需要 "
                    f"{'.'.join(map(str, DOCKER_MIN_FOR_GPU))} 以上，用不了 GPU 版镜像",
                    "改用 CPU 版镜像，或升级 Docker。",
                )
            )

    # --- WSL（只对 Windows 有意义）---
    if report.is_windows and report.wsl_installed is False:
        f.append(
            Finding(
                LEVEL_WARN,
                "WSL_MISSING",
                "未检测到 WSL2 发行版",
                "管理员 PowerShell 执行 `wsl --install -d Ubuntu-22.04` 后重启。"
                "本机实测 Hyper-V 已启用，无需进 BIOS 开虚拟化。",
            )
        )
    elif report.is_windows and report.wsl_distros:
        f.append(
            Finding(LEVEL_OK, "WSL_READY", "WSL 发行版：" + "、".join(report.wsl_distros))
        )

    # --- GPU ---
    if report.gpu.available:
        f.append(
            Finding(
                LEVEL_OK,
                "GPU_READY",
                "NVIDIA GPU：" + "；".join(report.gpu.devices),
                "可以选用 GPU 版镜像，但需确认已装 nvidia-container-toolkit，否则容器里看不到显卡。",
            )
        )
    else:
        f.append(
            Finding(
                LEVEL_OK,
                "NO_GPU",
                "未检测到可用 NVIDIA GPU",
                "改用 CPU 版镜像即可（体积小很多）。量化仍可完成，只是校准会慢一些。",
            )
        )

    # --- 磁盘 ---
    free = report.disk.free_bytes
    need = required_image_bytes
    if need:
        # 压缩包 + 解压后的镜像层，经验上要 2~2.5 倍
        estimate = int(need * 2.2)
        if free < estimate:
            f.append(
                Finding(
                    LEVEL_ERROR,
                    "DISK_INSUFFICIENT",
                    f"磁盘空间不足：{report.disk.path} 剩余 {free / GB:.1f} GB，"
                    f"而 {required_image_name or '所选镜像'} 约需 {estimate / GB:.1f} GB"
                    f"（下载 {need / GB:.1f} GB + 导入解压）",
                    "清理磁盘、把 Docker 的镜像存储目录改到大容量分区，或改用 CPU 版镜像（1.7 GB）。",
                )
            )
        elif free < estimate + 10 * GB:
            f.append(
                Finding(
                    LEVEL_WARN,
                    "DISK_TIGHT",
                    f"磁盘偏紧：剩余 {free / GB:.1f} GB，本次约需 {estimate / GB:.1f} GB",
                    "建议预留额外的 10 GB 给中间产物（校准缓存、量化模型、日志）。",
                )
            )
        else:
            f.append(
                Finding(LEVEL_OK, "DISK_OK", f"磁盘充足，剩余 {free / GB:.1f} GB")
            )
    elif free < 20 * GB:
        f.append(
            Finding(
                LEVEL_WARN,
                "DISK_TIGHT",
                f"磁盘剩余仅 {free / GB:.1f} GB",
                "GPU 版镜像解压后可能超过 20 GB，请预留空间或选用 CPU 版。",
            )
        )

    # --- 内存 ---
    if 0 < report.ram.total_bytes < 8 * GB:
        f.append(
            Finding(
                LEVEL_WARN,
                "RAM_LOW",
                f"物理内存仅 {report.ram.total_bytes / GB:.1f} GB",
                f"共享内存已自动下调到 {report.recommended_shm_bytes / GB:.1f} GB；"
                "编译大模型时请把 jobs 调小，否则可能 OOM。",
            )
        )
    elif report.ram.total_bytes:
        f.append(
            Finding(
                LEVEL_OK,
                "RAM_OK",
                f"内存 {report.ram.total_bytes / GB:.1f} GB，"
                f"建议 --shm-size={report.recommended_shm_bytes / GB:.1f}g",
            )
        )

    report.findings = f
    return report
