# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""子进程不能弹控制台窗口。

**现象**：双击 `scripts/run-windows-native.cmd` 会连着闪出一堆黑框。
启动脚本用的是 `pythonw`（`start "" "%PYW%" -m rdkquant ui`），
进程**自身没有控制台**；这种情况下每 spawn 一个控制台子进程，
Windows 就给它新建一个窗口。体检一次跑 4 条命令，`docker run` 那条还会一直挂着。

**为什么在终端里复现不了**：从 `python.exe` 启动时子进程继承终端的控制台，
不会新建窗口。所以这个 bug 只在"双击 .cmd"这条路径上出现，
开发者在自己的终端里跑 `python -m rdkquant ui` 永远看不到。

**实测**（pythonw 下枚举新增可见顶层窗口，Windows 11 + Windows Terminal）：

    裸 subprocess.run（不传 creationflags）   新出现 2 个
        CASCADIA_HOSTING_WINDOW_CLASS | C:\\WINDOWS\\SYSTEM32\\ping.exe
        PseudoConsoleWindow
    proc.run_capture()                        新出现 0 个

⚠️ 注意窗口类名是 `CASCADIA_HOSTING_WINDOW_CLASS` 而**不是** `ConsoleWindowClass` ——
测这件事的时候按老类名去数会数出 0，误以为"本来就没有窗口"。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from rdkquant.core import proc


def test_flag_matches_the_platform():
    if os.name == "nt":
        assert proc._NO_WINDOW == subprocess.CREATE_NO_WINDOW
        assert proc._NO_WINDOW != 0, "Windows 上必须真的带上这个标志"
    else:
        assert proc._NO_WINDOW == 0, "POSIX 下 creationflags 只能是 0"


def test_run_capture_suppresses_the_console(monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    proc.run_capture(["docker", "--version"])

    assert captured["creationflags"] == proc._NO_WINDOW
    # stdin 也要断开：pythonw 下没有可继承的 stdin
    assert captured["stdin"] is subprocess.DEVNULL


def test_stream_lines_suppresses_the_console(monkeypatch):
    captured: dict = {}

    class FakeStdout:
        def __iter__(self):
            return iter(["line\n"])

        def close(self):
            pass

    class FakeProc:
        stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)
    proc.stream_lines(["docker", "run", "img"], lambda _line: None)

    assert captured["creationflags"] == proc._NO_WINDOW
    assert captured["stdin"] is subprocess.DEVNULL


@pytest.mark.skipif(os.name != "nt", reason="窗口是 Windows 的事")
def test_flag_is_the_documented_constant():
    """把常量的值也钉住 —— 0x08000000 是 CREATE_NO_WINDOW。

    写错成别的标志（比如 DETACHED_PROCESS）不会报错，但行为不同：
    后者连控制台都不分配，某些程序会因此表现异常。
    """
    assert proc._NO_WINDOW == 0x08000000
