@echo off
title HamLink Radio — Build Standalone EXE
color 0A

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║  HamLink Radio — Build Standalone   ║
echo  ║  Creates a single .exe file              ║
echo  ╚══════════════════════════════════════════╝
echo.

:: Check Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: Python not found. Install Python 3.8+
    pause
    exit /b 1
)
echo  [OK] Python found

:: Check/install PyInstaller
python -c "import PyInstaller" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  Installing PyInstaller...
    python -m pip install pyinstaller --quiet
    if %ERRORLEVEL% NEQ 0 (
        echo  ERROR: Failed to install PyInstaller
        pause
        exit /b 1
    )
)
echo  [OK] PyInstaller ready

:: Check/install Flask
python -c "import flask" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  Installing Flask...
    python -m pip install flask --quiet
)
echo  [OK] Flask ready

:: Check/install aprslib
python -c "import aprslib" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  Installing aprslib...
    python -m pip install aprslib --quiet
)
echo  [OK] aprslib ready

:: Check/install pywin32 (for VarAC broadcast automation)
python -c "import win32gui" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  Installing pywin32...
    python -m pip install pywin32 --quiet
)
echo  [OK] pywin32 ready

echo.
echo  Building standalone executable...
echo  This may take 1-2 minutes...
echo.

cd /d "%~dp0"

python -m PyInstaller ^
    --onefile ^
    --name "HamLink_Radio" ^
    --icon NONE ^
    --hidden-import=flask ^
    --hidden-import=sqlite3 ^
    --hidden-import=uuid ^
    --hidden-import=csv ^
    --hidden-import=winsound ^
    --hidden-import=aprslib ^
    --hidden-import=win32gui ^
    --hidden-import=win32api ^
    --hidden-import=win32con ^
    --hidden-import=ctypes ^
    --hidden-import=configparser ^
    --noconfirm ^
    --clean ^
    monitor.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║  Build complete!                         ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  Your standalone executable is at:
echo    dist\HamLink_Radio.exe
echo.
echo  To deploy:
echo    1. Copy HamLink_Radio.exe to any folder
echo    2. Double-click to run (no Python needed!)
echo    3. A config.json will be created on first run
echo    4. Open http://127.0.0.1:5000 in your browser
echo.
echo  NOTE: On first run, Windows Defender may scan
echo  the .exe — this is normal and takes a moment.
echo.
pause
