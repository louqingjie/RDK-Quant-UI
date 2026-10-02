@echo off
REM RDK 模型量化工具 —— Windows 启动器
REM
REM 为什么必须走 WSL：官方编译器只支持 x86 Linux，量化必须在容器里跑。
REM 而 Docker Desktop 和 WSL 里的 docker 是**两个独立的守护进程**——
REM 已经 `docker load` 进 WSL 的 1.69 GB 镜像，在 Docker Desktop 那边是看不到的，
REM 走 Windows 原生路线得重新导一遍。
REM
REM 界面由 WSLg 直接显示在 Windows 桌面上，不需要 X 服务器。
REM
REM 如果仓库不在默认位置，先设置：
REM     set RDKQUANT_WSL_DIR=/home/你的用户名/src/rdk-quant

setlocal
if "%RDKQUANT_WSL_DIR%"=="" set "RDKQUANT_WSL_DIR=~/src/rdk-quant"

echo 正在 WSL 里启动 RDK 模型量化工具...
echo   仓库：%RDKQUANT_WSL_DIR%
echo   界面会显示在 Windows 桌面上（WSLg）。
echo.

wsl.exe -e bash -lc "cd %RDKQUANT_WSL_DIR% || { echo '找不到仓库：%RDKQUANT_WSL_DIR%'; echo '请先设置 RDKQUANT_WSL_DIR，或执行 scripts/dev-setup.sh 准备环境。'; exit 1; }; exec .venv/bin/rdkquant ui"

if errorlevel 1 (
    echo.
    echo [失败] 界面没能启动。常见原因：
    echo   1. 环境还没准备 → 在 WSL 里跑：bash scripts/dev-setup.sh
    echo   2. 仓库不在默认位置 → set RDKQUANT_WSL_DIR=/home/你/src/rdk-quant
    echo.
    pause
)
