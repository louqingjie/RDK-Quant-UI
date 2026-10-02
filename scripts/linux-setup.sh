#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors
#
# 在 Linux 上把本工具的图形界面跑起来。
#
#   ./scripts/linux-setup.sh          安装依赖 + 建虚拟环境 + 装包 + 离屏自检
#   ./scripts/linux-setup.sh --run    装好之后直接启动界面
#   ./scripts/linux-setup.sh --deps   只补系统依赖
#   ./scripts/linux-setup.sh --check  只做诊断，不改动任何东西
#
# 为什么要有这个脚本：在 Linux 上跑 Qt 程序，十次失败有九次是同一个原因——
# xcb 平台插件缺系统库，报错却是 "Could not load the Qt platform plugin xcb"，
# 完全不告诉你缺哪一个。这个脚本会直接把缺失的 .so 名字打出来。

set -euo pipefail

PY="${PY:-python3}"
VENV="${VENV:-.venv}"
RUN_AFTER=0
DEPS_ONLY=0
CHECK_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --run)   RUN_AFTER=1 ;;
        --deps)  DEPS_ONLY=1 ;;
        --check) CHECK_ONLY=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $arg（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# ---------------------------------------------------------------------------
# 1. 平台与基础约束
# ---------------------------------------------------------------------------
[ "$(uname -s)" = "Linux" ] || die "这个脚本只用于 Linux。Windows 上请走 WSL2（见 README）。"

# PySide6 6.11 的 Linux 轮子是 manylinux_2_34，也就是要求 glibc >= 2.34。
# Ubuntu 22.04 = 2.35 ✓   Debian 12 = 2.36 ✓   Ubuntu 20.04 = 2.31 ✗
# 顺带说明为什么 GUI 不能塞进 OpenExplorer 容器（那是 Ubuntu 20.04）。
GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
if [ -z "$GLIBC" ]; then
    GLIBC="$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}' || echo 0)"
fi
if [ "$(printf '%s\n2.34\n' "$GLIBC" | sort -V | head -1)" != "2.34" ]; then
    die "glibc ${GLIBC} 太旧，PySide6 需要 >= 2.34。
     Ubuntu 22.04 及以上、Debian 12 及以上都可以。
     Ubuntu 20.04 请升级系统，或改用「UI 跑在宿主机、量化在容器里」的分离部署。"
fi
ok "glibc ${GLIBC}"

"$PY" - <<'PY' || die "Python 版本不满足要求"
import sys
v = sys.version_info
if not ((3, 10) <= (v.major, v.minor) < (3, 15)):
    raise SystemExit(f"    需要 Python 3.10 ~ 3.14，当前是 {v.major}.{v.minor}")
print(f"    Python {v.major}.{v.minor}.{v.micro}")
PY
ok "Python 版本可用（$PY）"

# ---------------------------------------------------------------------------
# 2. Qt 运行所需的系统库
# ---------------------------------------------------------------------------
# 这些是 Qt 6 的 xcb 平台插件在 Debian/Ubuntu 上的常规依赖集合。
# 缺 libxcb-cursor0 是最常见的一种（Qt 6.5 起成为硬依赖），
# 症状就是那句信息量极低的 "Could not load the Qt platform plugin xcb"。
QT_LIBS=(
    libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0
    libdbus-1-3 libfontconfig1
)

have_apt=0
command -v apt-get >/dev/null 2>&1 && have_apt=1

missing_libs=()
if [ "$have_apt" = 1 ]; then
    for pkg in "${QT_LIBS[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" \
            || missing_libs+=("$pkg")
    done
fi

if [ "$have_apt" = 1 ] && [ "${#missing_libs[@]}" -gt 0 ]; then
    warn "缺少 ${#missing_libs[@]} 个系统库：${missing_libs[*]}"
    if [ "$CHECK_ONLY" = 1 ]; then
        warn "（--check 模式，不安装）"
    else
        info "用 apt 安装（需要 sudo）"
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends "${missing_libs[@]}"
        ok "系统库已就绪"
    fi
elif [ "$have_apt" = 0 ]; then
    warn "没有 apt-get，跳过系统库检查。若界面起不来，请用 --check 看缺哪些 .so。"
else
    ok "Qt 所需的系统库看起来齐全"
fi

# 中文字体：缺了会把界面渲染成一堆方块。这个包比较大，只提示不强制装。
if command -v fc-list >/dev/null 2>&1; then
    if fc-list :lang=zh 2>/dev/null | head -1 | grep -q .; then
        ok "系统中文字体可用"
    else
        warn "没检测到中文字体，界面里的中文会显示成方块。建议："
        warn "    sudo apt-get install -y fonts-noto-cjk"
    fi
fi

if [ "$DEPS_ONLY" = 1 ] || [ "$CHECK_ONLY" = 1 ]; then
    info "完成（仅依赖检查）"
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. 虚拟环境与包安装
# ---------------------------------------------------------------------------
if [ ! -d "$VENV" ]; then
    info "创建虚拟环境 $VENV"
    "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
[ -x "$VPY" ] || die "虚拟环境里没有 $VPY"

info "安装本工具（含 UI 依赖）"
"$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$VPY" -m pip install --disable-pip-version-check --quiet -e ".[ui,dev]"
ok "安装完成"

# ---------------------------------------------------------------------------
# 4. 离屏自检：不需要显示器就能验证整条链路
# ---------------------------------------------------------------------------
# 这一步刻意用 offscreen 平台。先把"代码和依赖是否正常"与"图形环境是否配好"
# 这两件事分开，否则 xcb 一报错你就不知道到底是哪个坏了。
info "离屏自检（QT_QPA_PLATFORM=offscreen）"
if QT_QPA_PLATFORM=offscreen "$VPY" - <<'PY'
import sys
from PySide6.QtWidgets import QApplication
from rdkquant.ui.palette import ThemeMode, apply_theme, cjk_available
from rdkquant.ui.shell import AppShell
from PySide6.QtGui import QPalette

app = QApplication([sys.argv[0]])
app.setOrganizationName("RDKQuant")
app.setApplicationName("rdkquant")
platform_palette = QPalette(app.palette())
apply_theme(app, ThemeMode.DARK, platform_palette=platform_palette)
shell = AppShell("runs/setup-check", platform_palette=platform_palette, theme=ThemeMode.DARK)
shell.show()
for _ in range(6):
    app.processEvents()
pages = shell.nav.count()
shot = shell.grab()
print(f"    页面数 {pages}，渲染 {'成功' if not shot.isNull() else '失败'}，"
      f"中文字体 {'有' if cjk_available() else '缺失'}")
raise SystemExit(0 if (not shot.isNull() and pages >= 8) else 1)
PY
then
    ok "离屏渲染通过 —— 代码和依赖都没问题，剩下的只可能是图形环境"
else
    die "离屏渲染失败。这跟图形环境无关，是安装本身的问题，请把上面的报错发出来。"
fi

# ---------------------------------------------------------------------------
# 5. 真正的图形环境诊断
# ---------------------------------------------------------------------------
info "检查图形环境"
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    ok "检测到 Wayland（$WAYLAND_DISPLAY）"
elif [ -n "${DISPLAY:-}" ]; then
    ok "检测到 X11（DISPLAY=$DISPLAY）"
else
    warn "既没有 DISPLAY 也没有 WAYLAND_DISPLAY —— 当前是纯命令行环境。"
    warn "在你自己的桌面会话里跑，或者："
    warn "  * Windows：用 WSL2，WSLg 会自动提供图形支持（见 README）"
    warn "  * 远程服务器：用 ssh -X，或先跑 Xvfb 再用 VNC"
fi

# 真起一次窗口，失败就直接把缺失的 .so 打出来
if "$VPY" - <<'PY' 2>/dev/null
import sys
from PySide6.QtWidgets import QApplication
app = QApplication([sys.argv[0]])
raise SystemExit(0)
PY
then
    ok "Qt 平台插件加载正常"
else
    warn "Qt 平台插件加载失败。下面是真正缺的库："
    QXCB="$("$VPY" - <<'PY'
import pathlib
try:
    import PySide6
except Exception:
    raise SystemExit(0)
p = pathlib.Path(PySide6.__file__).parent / "Qt" / "plugins" / "platforms" / "libqxcb.so"
print(p if p.exists() else "")
PY
)"
    if [ -n "$QXCB" ]; then
        ldd "$QXCB" 2>/dev/null | grep "not found" || echo "    （ldd 没报缺库，可能是别的原因）"
        echo
        echo "    → 把上面 'not found' 前面的库名去掉 lib 前缀和 .so 版本号，"
        echo "      用 apt-file search 或直接 apt-get install 对应包即可。"
        echo "    → 或直接用 QT_DEBUG_PLUGINS=1 重跑，Qt 会打印它找插件的全过程："
        echo "      QT_DEBUG_PLUGINS=1 $VPY -m rdkquant ui"
    else
        echo "    没找到 libqxcb.so —— 安装可能不完整，试试重装 PySide6。"
    fi
fi

# ---------------------------------------------------------------------------
echo
ok "准备就绪。启动界面："
echo "      $VENV/bin/rdkquant ui"
echo "   或者："
echo "      ./scripts/linux-setup.sh --run"

if [ "$RUN_AFTER" = 1 ]; then
    info "启动界面"
    exec "$VENV/bin/rdkquant" ui
fi
