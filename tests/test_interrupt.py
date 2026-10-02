# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""Ctrl+C 真的能中断界面。

这个文件**故意不碰 Qt**（不导入 PySide6、不用 qapp），因为真正的断言是
"把真程序当子进程起起来、发 SIGINT、看它退不退"——那才是用户遇到的现象。

背景：`QApplication.exec()` 的循环跑在 C++ 里，Python 的信号处理器只在解释器
执行字节码时才被调用。只装 handler 是不够的，SIGINT 会一直挂着，直到某个随机
回调里抛出 KeyboardInterrupt，而 PySide 捕获后只打印一行
"Error calling Python override of ..."，事件循环继续跑——用户连按四次都没反应。
所以还需要一个空转定时器把控制权定期交还解释器。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import rdkquant

REPO = Path(rdkquant.__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 的 SIGINT 语义不同（需要 CREATE_NEW_PROCESS_GROUP），"
           "这条测的是 POSIX 行为",
)


def _spawn_ui(tmp_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.Popen(
        [sys.executable, "-m", "rdkquant", "ui", "--workspace", str(tmp_path / "run")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_exit(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_sigint_terminates_the_gui(tmp_path: Path):
    """一次 Ctrl+C 必须能退出，而且要在几秒内。"""
    proc = _spawn_ui(tmp_path)
    try:
        time.sleep(4.0)
        assert proc.poll() is None, "界面没能启动起来"

        proc.send_signal(signal.SIGINT)
        exited = _wait_exit(proc, 8.0)

        if not exited:
            proc.kill()
            proc.wait()
            pytest.fail(
                "发了 SIGINT 之后 8 秒仍未退出 —— 这正是用户报的问题："
                "Ctrl+C 完全无法中断"
            )
        assert proc.returncode == 0, f"应当整洁退出，实际退出码 {proc.returncode}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_second_sigint_force_kills(tmp_path: Path):
    """连按两次必须强退——界面卡住时不能连退都退不出来。"""
    proc = _spawn_ui(tmp_path)
    try:
        time.sleep(4.0)
        assert proc.poll() is None

        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)

        if not _wait_exit(proc, 6.0):
            proc.kill()
            proc.wait()
            pytest.fail("连按两次 SIGINT 仍未退出")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
