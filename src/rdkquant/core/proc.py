# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""子进程与流式输出的公共封装。

刻意只用标准库：core 要能在 OE 容器（Ubuntu 20.04 / Python 3.8-3.10）里跑，
不能引入 requests / psutil 之类的额外依赖。
"""

from __future__ import annotations

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


def which(exe: str) -> str | None:
    """查找可执行文件；Windows 上也会命中 docker.exe。"""
    return shutil.which(exe)


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
