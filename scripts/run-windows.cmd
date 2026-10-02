@echo off
REM ============================================================================
REM RDK Quantization UI -- Windows launcher
REM
REM IMPORTANT: keep this file PURE ASCII.
REM   cmd.exe decodes .bat/.cmd files using the OEM code page (936/GBK on a
REM   Chinese Windows), not UTF-8. Chinese text in comments gets mis-decoded
REM   into garbage bytes that cmd then tries to execute as commands -- and it
REM   eats characters from the FOLLOWING lines too, so the real code breaks.
REM   Observed: "%RDKQUANT_WSL_DIR%" became "QUANT_WSL_DIR", "wsl.exe -e"
REM   became just "-e". Both reported as "not a command".
REM   tests/test_repo_hygiene.py enforces this.
REM
REM Why WSL is the only route (all measured, not assumed):
REM   * The official compiler is x86 Linux only; quantization runs in Docker.
REM   * Docker Desktop and the docker inside WSL are TWO SEPARATE DAEMONS.
REM     The 1.69 GB image already loaded into WSL is invisible to Docker Desktop.
REM   * Drive C: has ~3.4 GB free; the image needs ~3.7 GB (tar + extracted).
REM   * WSLg shows the GUI directly on the Windows desktop -- no X server needed.
REM
REM Override the repo location if it is not at the default:
REM     set RDKQUANT_WSL_DIR=/home/yourname/src/rdk-quant
REM ============================================================================

setlocal
if "%RDKQUANT_WSL_DIR%"=="" set "RDKQUANT_WSL_DIR=~/src/rdk-quant"

echo Starting RDK Quantization UI inside WSL...
echo   repo : %RDKQUANT_WSL_DIR%
echo   The window will appear on your Windows desktop (WSLg).
echo.

wsl.exe -e bash -lc "cd %RDKQUANT_WSL_DIR% || { echo '[ERROR] repo not found: %RDKQUANT_WSL_DIR%'; echo '        Set RDKQUANT_WSL_DIR, or run scripts/dev-setup.sh first.'; exit 1; }; exec .venv/bin/rdkquant ui"

if errorlevel 1 (
    echo.
    echo [FAILED] The UI did not start. Common causes:
    echo   1. Environment not prepared yet:
    echo        wsl -e bash -lc "cd ~/src/rdk-quant && bash ./scripts/dev-setup.sh"
    echo   2. Repo is not at the default location:
    echo        set RDKQUANT_WSL_DIR=/home/yourname/src/rdk-quant
    echo.
    pause
)
