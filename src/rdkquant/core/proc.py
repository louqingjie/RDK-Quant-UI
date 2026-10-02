# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""子进程与流式输出的公共封装。

刻意只用标准库：core 要能在 OE 容器（Ubuntu 20.04 / Python 3.8-3.10）里跑，
不能引入 requests / psutil 之类的额外依赖。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

LineCallback = Callable[[str], None]


class Cancelled(Exception):
    """用户主动取消。"""


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


#: Docker Desktop 可能装在哪些地方（按新到旧）。
#: 4.x 起改成**按用户安装**到 `%LOCALAPPDATA%\Programs\DockerDesktop`，
#: 不再是经典的 `C:\Program Files\Docker\Docker`。实测踩到：装好了、守护进程
#: 在跑、`docker.exe` 可用，但我们的体检报告说"未安装 Docker 命令行"。
_WINDOWS_DOCKER_DIRS = (
    r"%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin",
    r"%ProgramFiles%\Docker\Docker\resources\bin",
    r"%ProgramFiles(x86)%\Docker\Docker\resources\bin",
)

_path_refreshed = False


def refresh_windows_path() -> None:
    """把 PATH 从注册表重新读一遍。

    **为什么需要**：Windows 的用户 PATH 存在注册表里，而一个进程只在**启动时**
    拿到当时的 PATH。如果 Docker Desktop 是在本进程启动之后才装好（或者 PATH 是
    那时才更新的），`os.environ["PATH"]` 里就没有它，`shutil.which("docker")`
    会报"未安装"——实测正是如此：Docker Desktop 4.93 装着、守护进程 29.8.1 在跑、
    docker.exe 直接调用可用，但体检说未安装。

    这是个通用问题（任何"装完即刻用"的程序都会遇到），所以放在这里而不是
    特判 docker。只做一次，之后靠缓存。
    """
    global _path_refreshed
    if _path_refreshed or os.name != "nt":
        return
    _path_refreshed = True
    try:
        import winreg
    except ImportError:
        return

    parts: list[str] = []
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
                parts.append(str(value))
        except OSError:
            continue

    existing = os.environ.get("PATH", "")
    if not parts:
        return
    # 注册表里的值可能含 %VAR%，要展开
    merged = os.pathsep.join([*parts, existing])
    os.environ["PATH"] = os.path.expandvars(merged)


def which(exe: str) -> str | None:
    """查找可执行文件；Windows 上也会命中 docker.exe。

    Windows 上先刷新一次 PATH（见 `refresh_windows_path`），再在几个已知位置
    兜底找一遍 Docker Desktop。
    """
    refresh_windows_path()
    found = shutil.which(exe)
    if found or os.name != "nt":
        return found

    if os.path.splitext(exe)[0].lower() != "docker":
        return None
    for pattern in _WINDOWS_DOCKER_DIRS:
        directory = os.path.expandvars(pattern)
        candidate = os.path.join(directory, "docker.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def run_capture(argv: Sequence[str], *, timeout: float = 20.0) -> ProcResult:
    """跑一条命令并捕获输出。永不抛异常——环境探测必须能在任何机器上跑完。"""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return ProcResult(proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip())
    except FileNotFoundError:
        return ProcResult(127, "", f"未找到可执行文件: {argv[0]}")
    except subprocess.TimeoutExpired:
        return ProcResult(124, "", f"超时（>{timeout}s）: {' '.join(argv)}")
    except OSError as exc:  # 权限、路径等
        return ProcResult(1, "", str(exc))


def stream_lines(
    argv: Sequence[str],
    on_line: LineCallback,
    *,
    cancel: threading.Event | None = None,
    cwd: str | None = None,
) -> int:
    """逐行流式执行命令，支持取消。

    返回进程退出码。取消时返回 130（SIGINT 约定）。
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
        )
    except FileNotFoundError:
        on_line(f"[错误] 未找到可执行文件: {argv[0]}")
        return 127
    except OSError as exc:
        on_line(f"[错误] 无法启动进程: {exc}")
        return 1

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                _terminate(proc)
                on_line("[已取消] 进程已被终止")
                return 130
            on_line(line.rstrip("\r\n"))
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def join_lines(lines: Iterable[str], prefix: str = "") -> str:
    return "\n".join(prefix + line for line in lines)
