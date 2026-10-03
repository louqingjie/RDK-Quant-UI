# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""截图矩阵：每个页面 × 每个主题 × 每个缩放档，各出一张 PNG。

存在的意义是让 UI 的验收可执行、可复现，而不是靠"我看着还行"。
矩阵里必须包含 **Fusion + 无平台主题** 这一档——那是 Linux 上最差的配置
（裸 WSL、没装桌面主题的环境），在这里过关，在 KDE/Breeze 上只会更好。

用法：

    python tools/shots.py --matrix                 # 全部组合，各自起一个进程
    python tools/shots.py --theme dark --out shots # 单次，用当前环境

`--matrix` 之所以要起子进程：QT_QPA_PLATFORM / QT_SCALE_FACTOR / -style 都
必须在 QApplication 构造之前生效，一个进程里换不了。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _seed_calib_images(workspace: Path) -> Path:
    """给截图工作区造几张真图。

    不造的话校准页永远是「尚未扫描」，网格一行都拍不到 —— **懒加载的改动
    就没被截图矩阵覆盖**，评审看不出网格是好的还是坏的。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage

    target = workspace / "cal_images"
    target.mkdir(parents=True, exist_ok=True)
    if any(target.glob("*.jpg")):
        return target
    for index in range(12):
        image = QImage(320, 240, QImage.Format.Format_RGB32)
        image.fill(QColor.fromHsv(index * 28 % 360, 150, 200))
        image.save(str(target / f"demo{index:02d}.jpg"), "JPEG")
    return target


def _populate_calib_grid(shell, workspace: Path) -> None:
    """让校准页的网格真的有内容。

    正常流程要走"选目录 → 扫描"，那需要容器。截图只关心**渲染**，
    所以这里直接把扫描的结论注进去 —— 走的是和真实路径**同一个**
    `grid.set_images()`，所以懒加载、按需解码全都是真的在跑。
    """
    from rdkquant.core.calibfilter import CalibStat

    page = next(
        (p for p in shell.stage_pages if type(p).__name__ == "CalibPage"), None
    )
    if page is None:
        return
    source = _seed_calib_images(workspace)
    names = sorted(p.name for p in source.glob("*.jpg"))
    if not names:
        return
    page._staged = source
    page._stats = [
        CalibStat(name=name, size=(source / name).stat().st_size,
                  width=320, height=240, mode="RGB")
        for name in names
    ]
    page.grid.set_images(source, names=names)
    page._displayed = list(names)
    page.filter_section.setEnabled(True)
    page._refresh_funnel()
    # 网格固定 4 行高，卡片栈到它上面已经有 500 多像素 —— 不滚的话截图里
    # 根本拍不到网格（评审会以为坏了）。真实路径也做同一件事，见 `_on_apply_filter`。
    page.scroll.ensureWidgetVisible(page.grid, 0, 0)


def _settle(app, predicate, timeout_ms: int = 2000) -> bool:
    """泵事件循环直到 `predicate()` 成立或超时。**有上限**，不会挂住截图。

    缩略图是**按需、异步**解码的（见 `ui/widgets/thumbnailgrid.py`）：
    光调几次 `processEvents()` 是不够的 —— 工作线程还没跑完，
    截出来的网格就是一片空白。
    """
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _single(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT / "src"))

    from PySide6.QtWidgets import QApplication  # noqa: E402

    from rdkquant.ui import icons  # noqa: E402
    from rdkquant.ui.palette import (  # noqa: E402
        APP_NAME,
        ORG_NAME,
        ThemeMode,
        apply_theme,
        cjk_available,
    )
    from rdkquant.ui.shell import AppShell  # noqa: E402
    from PySide6.QtGui import QPalette  # noqa: E402

    app = QApplication([sys.argv[0]])
    if args.style:
        app.setStyle(args.style)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)

    platform_palette = QPalette(app.palette())
    base_style = app.style().objectName()  # 设过样式表之后就拿不到了，先存下来

    mode = ThemeMode(args.theme)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 必须先 apply_theme 再构造界面 —— 顺序和 app.py 的 run() 一致。
    # apply_theme 内部会按当前字体重算尺寸令牌（configure），而控件是在构造时
    # 读取这些令牌的。顺序反了的话，控件用的是默认尺寸、而后面按行数定高用的是
    # 重算后的尺寸，两边对不上，表格就会莫名其妙地裁掉最后一行。
    apply_theme(app, mode, platform_palette=platform_palette)

    shell = AppShell(Path(args.workspace), platform_palette=platform_palette, theme=mode)
    shell.show()
    for _ in range(6):
        app.processEvents()

    scale_tag = f"x{args.scale:g}".replace(".", "")
    written = []
    for row in range(shell.nav.count()):
        shell.nav.setCurrentRow(row)
        for _ in range(4):
            app.processEvents()
        icons.clear_cache()
        apply_theme(app, mode, platform_palette=platform_palette)
        shell._reload_nav_icons()
        for _ in range(3):
            app.processEvents()

        key = shell.nav.item(row).data(0x0100)  # Qt.UserRole

        # 校准页要有内容，否则拍的是"尚未扫描"，网格一行都看不见
        if key == "calib":
            _populate_calib_grid(shell, Path(args.workspace))
            grid = getattr(
                next((p for p in shell.stage_pages
                      if type(p).__name__ == "CalibPage"), None), "grid", None
            )
            if grid is not None:
                # 等首屏缩略图**真的解码回来**再截图
                _settle(app, lambda g=grid: g.decoded_rows() > 0)
            for _ in range(3):
                app.processEvents()

        name = f"{base_style or 'default'}_{mode.value}_{scale_tag}_{row + 1:02d}_{key}.png"
        target = out_dir / name
        if shell.grab().save(str(target)):
            written.append(name)

    print(
        f"[{base_style or 'default'} / {mode.value} / scale {args.scale:g}] "
        f"{len(written)} 张，中文字体={'有' if cjk_available() else '缺失'}"
    )
    for name in written:
        print("   ", name)
    return 0


def _matrix(args: argparse.Namespace) -> int:
    combos = []
    for style in args.styles:
        for theme in args.themes:
            for scale in args.scales:
                combos.append((style, theme, scale))

    print(f"共 {len(combos)} 个组合，每个组合起一个子进程\n")
    failures = 0
    for style, theme, scale in combos:
        env = dict(os.environ)
        env["QT_SCALE_FACTOR"] = str(scale)
        if args.platform:
            env["QT_QPA_PLATFORM"] = args.platform
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--theme", theme,
            "--out", args.out,
            "--workspace", args.workspace,
            "--scale", str(scale),
        ]
        if style:
            cmd += ["--style", style]
        result = subprocess.run(cmd, env=env, cwd=str(ROOT))
        if result.returncode != 0:
            failures += 1
            print(f"!! 组合失败: style={style} theme={theme} scale={scale}")
    print(f"\n完成：{len(combos) - failures}/{len(combos)} 个组合成功")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shots", description="UI 截图矩阵")
    parser.add_argument("--out", default="shots", help="输出目录")
    parser.add_argument("--workspace", default="runs/shot_demo", help="截图用的工作区")
    parser.add_argument(
        "--theme", default="system",
        choices=["system", "light", "dark"], help="主题",
    )
    parser.add_argument("--style", default="", help="Qt 基础样式，如 Fusion")
    parser.add_argument("--scale", type=float, default=1.0, help="QT_SCALE_FACTOR")
    parser.add_argument("--platform", default="", help="QT_QPA_PLATFORM，如 offscreen")
    parser.add_argument("--matrix", action="store_true", help="跑完整矩阵（起子进程）")
    parser.add_argument(
        "--styles", nargs="*", default=["Fusion", "default"],
        help="矩阵里的基础样式（default = 不带 -style）",
    )
    parser.add_argument(
        "--themes", nargs="*", default=["dark", "light"], help="矩阵里的主题",
    )
    parser.add_argument(
        "--scales", nargs="*", type=float, default=[1.0, 1.5], help="矩阵里的缩放档",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.styles == ["default"]:
        args.styles = [""]
    return _matrix(args) if args.matrix else _single(args)


if __name__ == "__main__":
    raise SystemExit(main())
