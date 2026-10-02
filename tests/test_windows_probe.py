# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""Windows 真机验证时揪出来的两个问题。

这两条都**只在真机上才暴露**，在 Linux 或 mock 环境里永远不会出现——
所以它们值得单独的回归测试。

背景：在用户的 Windows 机器上跑 `rdkquant doctor`，得到两份互相矛盾的报告：

  第一次（修复前）：
      Docker : 未安装              ← 误报
      磁盘   : 剩余 1.8 GB
      结论   : 可以执行量化         ← 错的，1.8 GB 连镜像都装不下

  实际环境：Docker Desktop 4.93.0 装着、守护进程 29.8.1 在跑、docker.exe 可用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rdkquant.core import proc
from rdkquant.core.probe import (
    GB,
    WORKABLE_DISK_BYTES,
    HostReport,
    probe_host,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用行为")


# --------------------------------------------------------------------------
# 1. 装了 Docker Desktop 却报"未安装"
# --------------------------------------------------------------------------
def test_windows_docker_dirs_cover_the_modern_per_user_location():
    """Docker Desktop 4.x 起改成**按用户安装**，不再是 Program Files。

    实测位置：`%LOCALAPPDATA%\\Programs\\DockerDesktop\\resources\\bin\\docker.exe`
    ——只找 `C:\\Program Files\\Docker\\Docker` 会漏掉它。
    """
    joined = " ".join(proc._WINDOWS_DOCKER_DIRS).lower()
    assert "localappdata" in joined, "4.x 的按用户安装位置必须在内"
    assert "programfiles" in joined, "老式安装位置也要保留"


@WINDOWS_ONLY
def test_refresh_windows_path_merges_registry_into_environ():
    """PATH 必须能从注册表重新读一遍。

    Windows 的用户 PATH 存在注册表里，进程只在启动时拿一次。Docker Desktop 若
    是在本进程启动之后装好的，`os.environ["PATH"]` 里就没有它。
    """
    proc._path_refreshed = False
    before = os.environ.get("PATH", "")
    proc.refresh_windows_path()
    after = os.environ.get("PATH", "")
    assert after, "PATH 不能变空"
    assert before in after, "原来的 PATH 必须保留在末尾（合并而不是替换）"
    # 只做一次
    proc.refresh_windows_path()


@WINDOWS_ONLY
def test_which_finds_docker_desktop_on_this_machine():
    """这台机器上装了 Docker Desktop，which 就该找得到。

    这条在没装 Docker Desktop 的机器上会失败——那是**正确**的失败，提醒我们
    这条路径没有被真机覆盖。
    """
    refresh = os.environ.get("PATH", "")
    os.environ["PATH"] = ""          # 把 PATH 清空，模拟"装了但 PATH 是旧的"
    try:
        proc._path_refreshed = False
        found = proc.which("docker")
    finally:
        os.environ["PATH"] = refresh
        proc._path_refreshed = False
    assert found, (
        "PATH 里没有 docker 时，which 应当去已知的 Docker Desktop 目录兜底找一遍"
    )
    assert found.lower().endswith("docker.exe")


def test_which_returns_none_for_unknown_tools():
    assert proc.which("绝对不存在的可执行文件-xyz") is None


def test_which_still_works_on_posix():
    assert proc.which("sh") in (None, "/bin/sh", "/usr/bin/sh")


# --------------------------------------------------------------------------
# 2. 磁盘只剩 1.8 GB 却说"可以执行量化"
# --------------------------------------------------------------------------
def _report(tmp_path: Path, free_bytes: int, *, docker_ok: bool = True) -> HostReport:
    report = HostReport()
    report.platform = "test"
    report.arch = "x86_64"
    report.disk = type(report.disk)(str(tmp_path), free_bytes + 10 * GB, free_bytes)
    report.docker.daemon_ok = docker_ok
    report.docker.cli_path = "/usr/bin/docker"
    return report


def test_workable_disk_floor_is_at_least_one_image(tmp_path: Path, monkeypatch):
    """底线必须大于"最小的镜像解压后"所需 —— 否则就是允许一个做不了的配置。"""
    assert WORKABLE_DISK_BYTES >= 4 * GB, (
        "最小的 CPU 镜像 1.69 GB，下载 + 解压经验上要 2.2 倍 ≈ 3.7 GB"
    )


def test_tiny_disk_blocks_the_conclusion(tmp_path: Path, monkeypatch):
    """不指定目标镜像时，磁盘过小也必须判 ERROR。

    原来的写法在这个分支只给 WARN，于是真机上 C: 只剩 1.8 GB 时结论仍是
    「可以执行量化」——用户会以为可以开工。
    """
    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: shutil._ntuple_diskusage(500 * GB, 498 * GB, 2 * GB),
    )
    report = probe_host(tmp_path)
    codes = {f.code for f in report.findings}
    assert "DISK_INSUFFICIENT" in codes
    assert not report.can_quantize, "磁盘不够就不能说可以量化"


def test_roomy_disk_does_not_block(tmp_path: Path, monkeypatch):
    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: shutil._ntuple_diskusage(1000 * GB, 500 * GB, 500 * GB),
    )
    report = probe_host(tmp_path)
    codes = {f.code for f in report.findings}
    assert "DISK_INSUFFICIENT" not in codes
    # 没有指定目标镜像时，磁盘充足就不产生任何磁盘类结论——这是有意的
    # （磁盘容量已经在汇总表里显示了，不必再占一条 finding）
    assert "DISK_TIGHT" not in codes


def test_docker_down_alone_blocks_the_conclusion():
    from rdkquant.core.probe import DockerStatus

    report = HostReport()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=False)
    assert not report.can_quantize


def test_docker_ready_and_no_errors_allows_the_conclusion():
    from rdkquant.core.probe import DockerStatus

    report = HostReport()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    assert report.can_quantize, "只有 Docker 就绪且没有 ERROR 时才说可以量化"
