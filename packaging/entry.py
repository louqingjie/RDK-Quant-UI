# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""打包成自包含程序时的入口。

设计要点：**同一个可执行文件既能开图形界面，也能当命令行工具用。**

    ./rdkquant             → 图形界面
    ./rdkquant doctor      → 环境体检
    ./rdkquant images …    → 镜像管理

这么做不只是顺手，它有两个实际好处：

1. 用户只需要记住一个名字，不用担心"我装的是 CLI 还是 GUI"；
2. 打包产物**可以无显示环境自检**（`./rdkquant doctor` 不需要 DISPLAY），
   否则在 CI 或服务器上根本没法验证二进制是否可用。
"""

from __future__ import annotations

import sys

#: 这些子命令直接交给 CLI，不启动图形界面
CLI_COMMANDS = {"doctor", "images", "mock", "ui", "--help", "-h", "--version"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and (args[0] in CLI_COMMANDS or args[0].startswith("-")):
        from rdkquant.cli import main as cli_main

        return int(cli_main(args) or 0)

    from rdkquant.ui.app import run

    return int(run() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
