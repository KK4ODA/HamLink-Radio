@echo off
setlocal EnableDelayedExpansion
title HomeLink Radio — Launcher
color 0A

echo.
echo  ========================================
echo    HomeLink Radio
echo    Dependency Check and Launch
echo  ========================================
echo.

:: -------------------------------------------
:: Check for Python
:: -------------------------------------------
echo [1/4] Checking for Python...

where python >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  ERROR: Python is not installed or not in your PATH.
    echo.
    echo  Please install Python 3.8+ from:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo         Found: !PYVER!

python -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)" 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  ERROR: Python 3.8 or higher is required.
    echo  You have: !PYVER!
    echo  Please update from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo         OK

:: -------------------------------------------
:: Check pip is available
:: -------------------------------------------
echo.
echo [2/4] Checking pip...
python -m pip --version >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo         pip not found. Attempting to install...
    python -m ensurepip --default-pip >nul 2>&1
    python -m pip --version >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo.
        echo  ERROR: pip is not available and could not be installed.
        echo  Try reinstalling Python with pip included.
        echo.
        pause
        exit /b 1
    )
)
echo         OK

:: -------------------------------------------
:: Install/check Flask
:: -------------------------------------------
echo.
echo [3/4] Checking for Flask...
python -c "import flask" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo         Flask not found. Installing...
    python -m pip install flask 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo         Trying with --user flag...
        python -m pip install flask --user 2>&1
    )
    python -c "import flask" >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo.
        echo  ERROR: Failed to install Flask.
        echo.
        echo  Try running one of these manually:
        echo    python -m pip install flask
        echo    python -m pip install flask --user
        echo    pip install flask
        echo.
        pause
        exit /b 1
    )
    echo         Flask installed successfully.
) else (
    echo         OK
)

:: -------------------------------------------
:: Install/check aprslib (optional)
:: -------------------------------------------
echo.
echo [3.5/4] Checking for aprslib (APRS support)...
python -c "import aprslib" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo         aprslib not found. Installing...
    python -m pip install aprslib >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        python -m pip install aprslib --user >nul 2>&1
    )
    python -c "import aprslib" >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo         WARNING: Could not install aprslib.
        echo         APRS features will be disabled.
        echo         You can install manually later: pip install aprslib
    ) else (
        echo         aprslib installed successfully.
    )
) else (
    echo         OK
)

:: -------------------------------------------
:: Check that monitor.py exists
:: -------------------------------------------
echo.
echo [4/4] Checking for monitor.py...
if not exist "%~dp0monitor.py" (
    echo.
    echo  ERROR: monitor.py not found!
    echo  Make sure this .bat file is in the same folder as monitor.py
    echo.
    pause
    exit /b 1
)
echo         Found in: %~dp0
echo         OK

:: -------------------------------------------
:: Check for Pat (optional)
:: -------------------------------------------
echo.
echo [4.5] Checking for Pat Winlink client (optional)...
where pat >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    if exist "C:\Pat\pat.exe" (
        echo         Found: C:\Pat\pat.exe
    ) else (
        echo         Pat not found in PATH or C:\Pat\
        echo         Winlink features will be unavailable.
        echo         Download Pat from https://getpat.io if needed.
    )
) else (
    echo         OK
)

:: -------------------------------------------
:: Show installed versions
:: -------------------------------------------
echo.
echo  ----------------------------------------
for /f "tokens=*" %%v in ('python -c "from importlib.metadata import version; print(version('flask'))" 2^>^&1') do echo    Flask: %%v
python -c "import aprslib; print('  aprslib: available')" 2>nul || echo    aprslib: not installed (APRS disabled)
echo  ----------------------------------------

:: -------------------------------------------
:: Launch
:: -------------------------------------------
echo.
echo  ========================================
echo    All checks passed! Starting...
echo  ========================================
echo.
echo  Monitor will be available at:
echo    http://127.0.0.1:5000
echo.
echo  Press Ctrl+C to stop the monitor.
echo.

:: Open browser after a short delay (hidden window)
start /min "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5000 && exit"

:: Run the monitor from the script's directory
cd /d "%~dp0"
python monitor.py

:: If we get here, monitor exited
echo.
echo  Monitor has stopped.
echo.
pause
endlocal
