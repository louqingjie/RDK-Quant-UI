# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""程序入口：组装并显示外壳。

`run()` 的签名刻意保持不变 —— cli.py 的 `ui` 子命令、tools/ 下的截图工具
都依赖它。
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from .palette import APP_NAME, ORG_NAME, ThemeMode, apply_theme, cjk_available
from .shell import AppShell, load_theme

#: 让解释器周期性回到字节码的间隔。见 `_install_interrupt_handler`。
_INTERRUPT_TICK_MS = 200


def _install_interrupt_handler(app: QApplication) -> QTimer:
    """让 Ctrl+C 真的能中断。

    **为什么光装 signal handler 没用**：`QApplication.exec()` 的循环跑在 C++ 里，
    而 Python 的信号处理器只在解释器执行字节码时才会被调用。事件循环长时间不回
    到 Python，SIGINT 就一直挂着，直到某个**随机**回调（比如 `QWidget::event`）
    里才抛出 `KeyboardInterrupt`——而 PySide 捕获它之后只打印一行
    "Error calling Python override of ..."，事件循环照常跑下去。
    实测现象就是连按四次 Ctrl+C 毫无反应。

    所以必须两件事配合：

    1. 一个空转的定时器（200ms）——定期把控制权交还解释器，让挂起的信号有机会
       被真正处理。这一条是关键，缺了它 handler 永远不会被调用。
    2. 一个 SIGINT 处理器——收到就整洁退出；**第二次**则恢复默认行为直接杀掉
       进程，免得界面卡住时连退都退不出来。

    返回的定时器挂在 app 上，不用调用方持有（否则会被 GC 掉，症状是"偶尔又不灵了"）。
    """
    presses = {"count": 0}

    def handler(_signum, _frame):
        presses["count"] += 1
        if presses["count"] >= 2:
            # 第二次：用户显然等不及了，恢复默认行为硬退出
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
            return
        sys.stderr.write("\n收到 Ctrl+C，正在退出（再按一次强制结束）…\n")
        sys.stderr.flush()
        app.quit()

    signal.signal(signal.SIGINT, handler)

    timer = QTimer(app)
    timer.setInterval(_INTERRUPT_TICK_MS)
    timer.timeout.connect(lambda: None)   # 空转即可，目的只是回到 Python
    timer.start()
    return timer


def run(
    workspace: Path | str = "./runs/current",
    *,
    screenshot: str | None = None,
    theme: ThemeMode | None = None,
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)

    # 必须在任何 setPalette / setStyleSheet 之前快照：那之后 app.palette() 拿到的
    # 就是我们自己设进去的调色板，平台原生配色就丢了。
    platform_palette = QPalette(app.palette())

    mode = theme or load_theme()
    apply_theme(app, mode, platform_palette=platform_palette)

    shell = AppShell(
        Path(workspace),
        platform_palette=platform_palette,
        theme=mode,
        with_remote=bool(screenshot),
    )
    shell.show()

    if not cjk_available():
        shell.status_note.setText("未检测到中文字体，界面可能显示为方块")

    for _ in range(8):
        app.processEvents()

    if screenshot:
        saved = shell.grab().save(screenshot)
        print(
            f"screenshot={screenshot} saved={saved} "
            f"size={shell.width()}x{shell.height()} theme={mode.value}"
        )
        shell.env_page.tasks.wait_all()
        return 0 if saved else 1

    interrupt_timer = _install_interrupt_handler(app)
    try:
        return app.exec()
    finally:
        interrupt_timer.stop()
