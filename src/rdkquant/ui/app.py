# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""程序入口：组装并显示外壳。

`run()` 的签名刻意保持不变 —— cli.py 的 `ui` 子命令、tools/ 下的截图工具
都依赖它。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from .palette import APP_NAME, ORG_NAME, ThemeMode, apply_theme, cjk_available
from .shell import AppShell, load_theme


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

    return app.exec()
