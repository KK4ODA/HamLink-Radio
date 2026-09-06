@echo off
setlocal
title HamLink Radio - Build Standalone EXE
color 0A

echo.
echo  ========================================
echo    HamLink Radio - build standalone .exe
echo  ========================================
echo.

cd /d "%~dp0"

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: Python not found. Install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)
echo  [OK] Python found

echo  Installing build dependencies...
python -m pip install --quiet --upgrade -r requirements.txt pyinstaller
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo  [OK] Dependencies ready

echo.
echo  Building (this takes 1-2 minutes)...
echo.

:: --add-data bundles static/ (Leaflet + marker icons) inside the exe.
:: At runtime monitor.py resolves bundled files via sys._MEIPASS and keeps
:: config.json / logs / tiles next to the .exe itself.
python -m PyInstaller ^
    --onefile ^
    --name "HamLink_Radio" ^
    --icon "docs\icon.ico" ^
    --add-data "static;static" ^
    --hidden-import=flask ^
    --hidden-import=aprslib ^
    --hidden-import=win32gui ^
    --hidden-import=win32api ^
    --hidden-import=win32con ^
    --collect-all comtypes ^
    --noconfirm ^
    --clean ^
    monitor.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  ERROR: Build failed - see the output above.
    pause
    exit /b 1
)

echo.
echo  ========================================
echo    Build complete
echo  ========================================
echo.
echo  Your standalone executable is at:
echo    dist\HamLink_Radio.exe
echo.
echo  To deploy:
echo    1. Copy HamLink_Radio.exe to any folder (add a tiles\ folder for offline maps)
echo    2. Double-click it - no Python needed
echo    3. config.json is created next to the exe on first run
echo    4. Open http://127.0.0.1:5000 in your browser
echo.
echo  NOTE: Windows Defender may scan the .exe on first run - this is normal.
echo.
pause
endlocal
