# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""忙碌指示：跑起来时界面必须看得出来。

**起因**：`docker run` 正在跑，而底部进度条一直写着「就绪」——
`set_progress` 只有环境页（镜像下载）在调，`StagePage` 从来不碰进度条。
用户盯着一个不动的界面，不知道在不在跑、要等多久。

这里测的是"看得见"这件事本身，不涉及容器。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rdkquant.ui.widgets.logpanel import LogPanel, format_duration  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def panel(qapp):
    return LogPanel()


# --------------------------------------------------------------------------
# 时长文案
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seconds,expected", [
    (0, "0 秒"),
    (45, "45 秒"),
    (60, "1 分"),
    (125, "2 分 5 秒"),
    (3600, "1 小时 0 分"),
    (3725, "1 小时 2 分"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# --------------------------------------------------------------------------
# 忙碌状态
# --------------------------------------------------------------------------
def test_idle_starts_ready(panel):
    assert not panel.busy
    assert panel.progress.format() == "就绪"
    assert (panel.progress.minimum(), panel.progress.maximum()) == (0, 100)


def test_begin_busy_switches_to_indeterminate(panel):
    """`docker run` 给不出真实百分比 —— 用跑马灯，不要编一个假进度。

    Qt 里 `setRange(0, 0)` 就是"不确定进度"，会一直滚动。
    """
    panel.begin_busy("模型解析")
    assert panel.busy
    assert (panel.progress.minimum(), panel.progress.maximum()) == (0, 0)
    assert "模型解析" in panel.progress.format()


def test_busy_text_shows_elapsed_time(panel):
    """**已用时间是关键**：`docker run` 起来后可能几分钟不吐一行日志，
    没有这个计时，用户没法区分"在跑"和"卡死"。

    有了它，用户还能逐渐建立"这类任务大概要多久"的预期。
    """
    panel.begin_busy("生成校准数据")
    assert "0 秒" in panel.progress.format()

    # 把起点往前拨，免得测试真的要等 2 秒
    panel._busy_since -= 125
    panel._tick_busy()
    assert "2 分 5 秒" in panel.progress.format()


def test_end_busy_restores_a_determinate_bar(panel):
    panel.begin_busy("模型解析")
    panel.end_busy("模型解析完成")
    assert not panel.busy
    assert (panel.progress.minimum(), panel.progress.maximum()) == (0, 100)
    assert panel.progress.format() == "模型解析完成"


def test_end_busy_default_says_ready(panel):
    panel.begin_busy("模型解析")
    panel.end_busy()
    assert panel.progress.format() == "就绪"


def test_busy_label_defaults_when_empty(panel):
    panel.begin_busy("")
    assert panel.progress.format().startswith("正在运行")


def test_timer_actually_ticks(qapp, panel):
    """光设一次文案不算数 —— 计时器得真的在跑。"""
    panel.begin_busy("跑着")
    panel._busy_since -= 3
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        qapp.processEvents()
        if "3 秒" in panel.progress.format() or "4 秒" in panel.progress.format():
            break
        time.sleep(0.05)
    assert "秒" in panel.progress.format()
    assert "已用" in panel.progress.format()
