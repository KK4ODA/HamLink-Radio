@echo off
setlocal EnableDelayedExpansion
title HamLink Radio
color 0A

echo.
echo  ========================================
echo    HamLink Radio
echo  ========================================
echo.

cd /d "%~dp0"

:: "--no-browser" is passed by HamLink's self-updater when it relaunches
:: itself; the dashboard tab is already open in that case.
set "OPEN_BROWSER=1"
if /I "%~1"=="--no-browser" set "OPEN_BROWSER=0"

:: -------------------------------------------
:: 1. Python
:: -------------------------------------------
echo [1/4] Checking for Python...
where python >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  ERROR: Python is not installed or not in your PATH.
    echo  Install Python 3.8+ from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
python -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)" 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  ERROR: Python 3.8 or higher is required. You have: !PYVER!
    echo.
    pause
    exit /b 1
)
echo         !PYVER! OK

:: -------------------------------------------
:: 2. pip
:: -------------------------------------------
echo [2/4] Checking pip...
python -m pip --version >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    python -m ensurepip --default-pip >nul 2>&1
    python -m pip --version >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo.
        echo  ERROR: pip is not available. Reinstall Python with pip included.
        echo.
        pause
        exit /b 1
    )
)
echo         OK

:: -------------------------------------------
:: 3. Dependencies (requirements.txt)
:: -------------------------------------------
echo [3/4] Checking dependencies...
python -c "import flask, aprslib" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo         Installing from requirements.txt...
    python -m pip install -r requirements.txt
    if !ERRORLEVEL! NEQ 0 (
        echo         Retrying with --user...
        python -m pip install --user -r requirements.txt
    )
)
python -c "import flask" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo  ERROR: Flask could not be installed. Try running manually:
    echo    python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
python -c "import aprslib" >nul 2>&1
if !ERRORLEVEL! NEQ 0 echo         WARNING: aprslib missing - APRS features disabled.
python -c "import win32gui, comtypes" >nul 2>&1
if !ERRORLEVEL! NEQ 0 echo         NOTE: pywin32/comtypes missing - VarAC broadcast and relay automation disabled.
echo         OK

:: -------------------------------------------
:: 4. Files
:: -------------------------------------------
echo [4/4] Checking files...
if not exist "monitor.py" (
    echo.
    echo  ERROR: monitor.py not found next to this launcher.
    echo.
    pause
    exit /b 1
)
where pat >nul 2>&1
if !ERRORLEVEL! NEQ 0 if not exist "C:\Pat\pat.exe" echo         Pat not found - Winlink is optional ^(https://getpat.io^)
echo         OK

:: -------------------------------------------
:: Launch
:: -------------------------------------------
echo.
echo  Starting HamLink Radio at http://127.0.0.1:5000
echo  Press Ctrl+C here, or use "Stop HamLink" in the browser, to quit.
echo.

:: Open the browser after a short delay (hidden helper window)
if "%OPEN_BROWSER%"=="1" start /min "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5000 && exit"

python monitor.py
set EXIT_CODE=%ERRORLEVEL%

:: Exit code 0 = clean shutdown from the UI: close the window silently
if %EXIT_CODE%==0 (
  endlocal
  exit
)

echo.
echo  HamLink stopped unexpectedly (exit code %EXIT_CODE%). See the messages above.
echo.
pause
endlocal
