# 把 Windows 侧的改动同步到 WSL 并启动界面。
#
# 为什么需要同步：仓库放在 Windows 文件系统上，而虚拟环境必须建在 Linux 文件系统上
# （/mnt/c 是 uid=0;gid=0，非 root 用户 chmod 会 EPERM，而 python3 -m venv 必须给
# activate 脚本 chmod）。所以"在 Windows 编辑、在 WSL 运行"是这个项目的常态。
#
# 用法（在 Windows 的 PowerShell 里）：
#     wsl -e bash /mnt/c/Users/ccron/Desktop/RDK-Quantization-UI/scripts/sync-and-run.sh

set -u

WIN_REPO=/mnt/c/Users/ccron/Desktop/RDK-Quantization-UI
WSL_REPO=${RDKQUANT_WSL_DIR:-$HOME/src/rdk-quant}

echo "==> 同步 $WIN_REPO → $WSL_REPO"
cd "$WIN_REPO" || { echo "找不到 Windows 侧仓库：$WIN_REPO"; exit 1; }
bash ./scripts/wsl-sync.sh || exit 1

echo
echo "==> 启动界面"
cd "$WSL_REPO" || exit 1
exec .venv/bin/rdkquant ui "$@"
