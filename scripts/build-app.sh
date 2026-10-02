#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors
#
# 把本工具打包成一个**自包含的应用程序**。
#
#   使用者拿到 dist/rdkquant/ 之后直接跑 ./rdkquant 即可：
#   不需要 Python、不需要 pip、不需要 venv、不需要知道 PySide6 是什么。
#
#   ./scripts/build-app.sh              构建
#   ./scripts/build-app.sh --appimage   构建并尝试打成一个 AppImage
#
# 关于 venv：脚本内部**确实**会建一个虚拟环境，但它是构建期的实现细节，
# 放在 .build/ 下、对使用者完全不可见。这就是"把 venv 从用户路径里去掉"
# 的具体做法——不是删掉 venv，而是不让用户碰它。
#
# 为什么不走系统包那条路：Ubuntu 22.04 的仓库里根本没有 PySide6（只有
# PySide2/Qt5），而 Ubuntu 23.04+ / Debian 12+ 又用 PEP 668 锁掉了系统 pip。
# 所以"声明依赖让系统去装"在这个目标平台上不存在，只能自己带。

set -euo pipefail

WITH_APPIMAGE=0
for arg in "$@"; do
    case "$arg" in
        --appimage) WITH_APPIMAGE=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
PY="${PY:-python3}"
BUILD_DIR="$REPO_ROOT/.build"
BUILD_VENV="$BUILD_DIR/venv"

[ "$(uname -s)" = "Linux" ] || die "只支持在 Linux 上打包（PySide6 是平台相关的）"

# ---------------------------------------------------------------------------
# 构建环境（用户永远看不到这一层）
# ---------------------------------------------------------------------------
if [ ! -x "$BUILD_VENV/bin/python" ] || ! "$BUILD_VENV/bin/python" -c "import pip" 2>/dev/null; then
    info "准备构建环境（$BUILD_DIR/venv）"
    rm -rf -- "$BUILD_VENV"
    "$PY" -m venv "$BUILD_VENV" || die "建不了构建环境。缺 python3-venv 的话：
     sudo apt-get install -y python3-venv"
fi
BUILD_PY="$BUILD_VENV/bin/python"

info "安装构建依赖与本体"
"$BUILD_PY" -m pip install --disable-pip-version-check -q --upgrade pip
"$BUILD_PY" -m pip install --disable-pip-version-check -q -e ".[ui]"
"$BUILD_PY" -m pip install --disable-pip-version-check -q pyinstaller

# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
info "用 PyInstaller 打包"
rm -rf "$REPO_ROOT/build" "$REPO_ROOT/dist"
"$BUILD_PY" -m PyInstaller \
    --noconfirm --clean \
    --onedir \
    --windowed \
    --name rdkquant \
    --collect-data rdkquant \
    --exclude-module tkinter \
    --exclude-module PySide6.QtQml \
    --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.Qt3DCore \
    --exclude-module PySide6.QtWebEngineCore \
    packaging/entry.py 2>&1 | tail -6

APP="$REPO_ROOT/dist/rdkquant/rdkquant"
[ -x "$APP" ] || die "打包没产出可执行文件：$APP"

# ---------------------------------------------------------------------------
# 自检：不需要显示器就能验证二进制是否真的能用
# ---------------------------------------------------------------------------
info "自检（不依赖显示器）"
if "$APP" doctor >/dev/null 2>&1; then
    ok "CLI 路径可用"
else
    # doctor 在环境不就绪时返回非 0，那是正常的；这里只要求它能跑起来
    if "$APP" --version >/dev/null 2>&1; then
        ok "CLI 路径可用（doctor 返回非 0 是环境结论，不是崩溃）"
    else
        die "打包产物跑不起来"
    fi
fi

if QT_QPA_PLATFORM=offscreen "$APP" ui --help >/dev/null 2>&1 || true; then :; fi

INTERNAL="$REPO_ROOT/dist/rdkquant/_internal"
[ -d "$INTERNAL" ] || INTERNAL="$REPO_ROOT/dist/rdkquant"
if find "$INTERNAL" -maxdepth 3 \( -name "libQt6Core*" -o -name "Qt6Core*" \) -print -quit 2>/dev/null | grep -q .; then
    ok "Qt 运行库已打入产物"
else
    warn "没在产物里找到 Qt 运行库，界面可能起不来"
fi

SIZE="$(du -sh "$REPO_ROOT/dist/rdkquant" | cut -f1)"
ok "产物：$REPO_ROOT/dist/rdkquant  ($SIZE)"
echo "     直接运行：$APP"
echo "     分发方式：把整个 dist/rdkquant 目录打包给对方，解压后运行其中的 rdkquant"

# ---------------------------------------------------------------------------
if [ "$WITH_APPIMAGE" = 1 ]; then
    info "尝试打成 AppImage"
    TOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
    if [ ! -x "$TOOL" ]; then
        URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL -o "$TOOL" "$URL" && chmod +x "$TOOL" || warn "下载 appimagetool 失败，跳过"
        else
            warn "没有 curl，跳过 AppImage"
        fi
    fi
    if [ -x "$TOOL" ]; then
        APPDIR="$BUILD_DIR/rdkquant.AppDir"
        rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications"
        cp -r "$REPO_ROOT/dist/rdkquant/." "$APPDIR/usr/bin/"
        cat > "$APPDIR/rdkquant.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=RDK Quantization UI
Comment=Model quantization tool for D-Robotics RDK X3/X5
Exec=rdkquant
Icon=rdkquant
Categories=Development;Engineering;
Terminal=false
DESKTOP
        cp "$APPDIR/rdkquant.desktop" "$APPDIR/usr/share/applications/"
        # AppImage 需要一个图标；没有就用一个占位
        if [ -f "$REPO_ROOT/packaging/rdkquant.png" ]; then
            cp "$REPO_ROOT/packaging/rdkquant.png" "$APPDIR/rdkquant.png"
        fi
        ARCH=x86_64 "$TOOL" "$APPDIR" "$REPO_ROOT/dist/rdkquant-x86_64.AppImage" 2>&1 | tail -3 \
            && ok "AppImage：dist/rdkquant-x86_64.AppImage" \
            || warn "appimagetool 失败了（WSL 里常因 FUSE 不可用）。dist/rdkquant/ 仍然可用"
    fi
fi
