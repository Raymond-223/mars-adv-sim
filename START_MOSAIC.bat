@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MOSAIC-Omega v1.9.0

echo ===============================================
echo  MOSAIC-Omega v1.9.0 - Windows Launcher
echo ===============================================
echo.
echo [MOSAIC] Zero-install startup. No pip. No PyPI. No venv setup.
echo [MOSAIC] The bundled src\ tree is imported directly.
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "MOSAIC_PY="

rem This BAT only locates a Python 3.10-3.13 interpreter. Everything else --
rem capability detection, random loopback port, backend health check and
rem browser launch -- is owned by scripts\windows_launcher.py.

rem 1. Python launcher: probe each selector. Having py.exe on the machine does
rem    not mean any particular minor version is actually installed, so every
rem    candidate is executed once before it is trusted.
where py.exe >nul 2>nul
if not errorlevel 1 (
    call :try_py -3.11
    if defined MOSAIC_PY goto run
    call :try_py -3.12
    if defined MOSAIC_PY goto run
    call :try_py -3.10
    if defined MOSAIC_PY goto run
    call :try_py -3.13
    if defined MOSAIC_PY goto run
)

rem 2. Fall back to whatever python.exe is first on PATH.
where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "MOSAIC_PY=python.exe"
        goto run
    )
)

echo.
echo [MOSAIC] No suitable Python found.
echo Please install Python 3.10 - 3.13 ^(x64^) from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during installation.
set RC=20
goto fail

:try_py
py %1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "MOSAIC_PY=py %1"
goto :eof

:run
echo [MOSAIC] Using interpreter: %MOSAIC_PY%
echo.
%MOSAIC_PY% scripts\windows_launcher.py
set RC=%ERRORLEVEL%
if "%RC%"=="0" goto success

:fail
echo.
echo [MOSAIC] Startup failed. Exit code: %RC%
echo Check .mosaic_logs\launcher.log and .mosaic_logs\server-stderr.log for details.
pause
exit /b %RC%

:success
echo.
echo [MOSAIC] Started successfully.
exit /b 0
