#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors
#
# 把 Windows 侧的仓库同步到 WSL 里的可运行副本。
#
#   ./scripts/wsl-sync.sh              同步
#   ./scripts/wsl-sync.sh --delete     同步并删除目标端多余的文件
#
# 为什么需要它：WSL 的 /mnt/c 挂载是 uid=0;gid=0，文件在 Linux 看来都归 root，
# 非 root 用户 chmod 会 EPERM，而 `python3 -m venv` 必须给 activate 脚本 chmod
# —— 所以虚拟环境**只能**建在 Linux 文件系统上。于是就有了「在 Windows 上编辑、
# 在 WSL 里运行」这个组合，也就需要一条单向同步。
#
# 更好的替代方案（如果你不想同步）：
#   * 用 VS Code 的 Remote-WSL 直接打开 WSL 里的目录（推荐，零同步）
#   * 或者从 Windows 资源管理器打开 \\wsl$\Ubuntu-22.04\home\<你>\src\rdk-quant
#
# 从 Windows 侧调用：
#   wsl -e bash /mnt/c/Users/<你>/Desktop/RDK-Quantization-UI/scripts/wsl-sync.sh
# 从 WSL 侧调用（脚本默认就知道 Windows 仓库在哪）：
#   cd /mnt/c/Users/<你>/Desktop/RDK-Quantization-UI && ./scripts/wsl-sync.sh

set -euo pipefail

DELETE=0
for arg in "$@"; do
    case "$arg" in
        --delete) DELETE=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# 源：脚本自己所在的仓库（可能在 /mnt/c 下）
cd "$(dirname "$0")/.."
SRC="$(pwd)"

# 目标：Linux 文件系统里的副本
DST="${RDKQUANT_WSL_DST:-$HOME/src/rdk-quant}"

case "$SRC" in
    /mnt/*) ;;
    *) die "这个脚本是给「Windows 侧编辑 → WSL 侧运行」用的，当前源目录不在 /mnt 下：$SRC
     如果你的仓库本来就在 Linux 文件系统里，直接在原地开发即可，不需要同步。" ;;
esac

[ -d "$DST" ] || die "目标目录还不存在：$DST
     先建一次：
         mkdir -p \"$DST\" && cd \"$SRC\" && git archive --format=tar HEAD | tar x -C \"$DST\""

info "同步 $SRC  →  $DST"

# 只同步源码与配置文件。**绝不**同步 .venv / .build / dist：
# 那些是平台相关的构建产物，Windows 侧的拿到 Linux 上用不了，
# 而且带着 --delete 同步它们会把好好的 Linux venv 删掉。
ITEMS=(src tests tools packaging scripts pyproject.toml README.md LICENSE .gitignore)
RSYNC_ARGS=(-a --info=stats2)
[ "$DELETE" = 1 ] && RSYNC_ARGS+=(--delete)

if command -v rsync >/dev/null 2>&1; then
    for item in "${ITEMS[@]}"; do
        [ -e "$SRC/$item" ] || continue
        rsync "${RSYNC_ARGS[@]}" "$SRC/$item" "$DST/" >/dev/null
    done
else
    # 没有 rsync 就退回 cp。此时 --delete 无法实现，直接告知而不是假装成功。
    [ "$DELETE" = 1 ] && die "没有 rsync，--delete 无法实现。装一下：sudo apt-get install -y rsync"
    for item in "${ITEMS[@]}"; do
        [ -e "$SRC/$item" ] || continue
        if [ -d "$SRC/$item" ]; then
            mkdir -p "$DST/$item"
            cp -r "$SRC/$item/." "$DST/$item/"
        else
            cp "$SRC/$item" "$DST/$item"
        fi
    done
fi
chmod +x "$DST"/scripts/*.sh 2>/dev/null || true

ok "同步完成"

# 顺手确认目标端的 venv 还在、还能用——这些才是真正能跑起来的东西
if [ -x "$DST/.venv/bin/python" ] && "$DST/.venv/bin/python" -c "import pip" 2>/dev/null; then
    ok "运行环境就绪：$DST/.venv"
    echo "     开界面：cd $DST && .venv/bin/rdkquant ui"
else
    echo
    echo "  [!] $DST/.venv 还不可用，先建一次："
    echo "      cd $DST && ./scripts/dev-setup.sh"
fi
