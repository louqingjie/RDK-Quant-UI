@echo off
REM ============================================================================
REM RDK Quantization UI -- native Windows launcher (Docker Desktop)
REM
REM KEEP THIS FILE PURE ASCII. cmd.exe decodes .cmd using the OEM code page
REM (936/GBK on Chinese Windows), not UTF-8; Chinese here corrupts parsing of
REM the following lines. tests/test_repo_hygiene.py enforces this.
REM
REM Requires:
REM   * Docker Desktop running, with the OpenExplorer image loaded.
REM     Get it with:  .venv\Scripts\rdkquant.exe images fetch x5-1.2.8-cpu
REM   * A virtualenv at .venv with the ui extra installed.
REM
REM Note: the image must be loaded into DOCKER DESKTOP's daemon. The docker
REM inside WSL is a SEPARATE daemon -- an image loaded there is invisible here.
REM ============================================================================

setlocal
cd /d "%~dp0.."

set "PYW=.venv\Scripts\pythonw.exe"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] No virtualenv at .venv
    echo.
    echo   Create it first:
    echo       py -3 -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -e ".[ui]"
    echo.
    pause
    exit /b 1
)

REM Sanity check BEFORE launching windowless -- otherwise a failure would be
REM completely silent (pythonw has no console to print to).
"%PY%" -c "import PySide6, rdkquant.ui.app" 2>nul
if errorlevel 1 (
    echo [ERROR] The UI dependencies are not importable.
    echo.
    echo   Try reinstalling:
    echo       .venv\Scripts\python.exe -m pip install -e ".[ui]"
    echo.
    pause
    exit /b 1
)

if exist "%PYW%" (
    start "" "%PYW%" -m rdkquant ui
) else (
    "%PY%" -m rdkquant ui
)
