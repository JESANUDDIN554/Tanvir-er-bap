@echo off
REM ============================================================
REM  ClipMaker PC - one-click installer / launcher for Windows
REM  1. Checks Python 3.10+
REM  2. Installs ffmpeg (via winget) if missing
REM  3. Installs Python dependencies
REM  4. Launches the app
REM ============================================================
setlocal

echo [1/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  Python was not found. Please install Python 3.10 or newer from:
    echo  https://www.python.org/downloads/
    echo  IMPORTANT: tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)
python --version

echo.
echo [2/4] Checking ffmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo  ffmpeg not found - installing via winget (needs admin)...
    winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo  Automatic ffmpeg install failed. Install it manually from:
        echo  https://www.gyan.dev/ffmpeg/builds/  (ffmpeg-release-essentials.zip)
        echo  and add the bin folder to your PATH, then run this file again.
        echo.
        pause
        exit /b 1
    )
    echo  ffmpeg installed. If 'ffmpeg' is still not found, close this window,
    echo  open a NEW command prompt once, then run Install.bat again.
)
ffmpeg -version 2>nul | findstr "ffmpeg version" >nul
if errorlevel 1 (
    echo.
    echo  ffmpeg was just installed but is not on PATH yet.
    echo  Close this window, open it again, and run Install.bat once more.
    echo.
    pause
    exit /b 1
)

echo.
echo [3/4] Installing Python packages (first run takes a few minutes)...
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  Package install failed. Check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo [4/4] Starting ClipMaker PC...
python app.py
if errorlevel 1 (
    echo.
    echo  The app closed with an error. Copy the message above and ask for help.
    echo.
    pause
)
