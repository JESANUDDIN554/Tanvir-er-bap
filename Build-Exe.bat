@echo off
REM ============================================================
REM  Build ClipMakerPC.exe on this PC - just double-click this.
REM  Run Install.bat first (one time) so Python and libraries
REM  are ready. Building takes a few minutes.
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Run Install.bat first, then try again.
    pause
    exit /b 1
)

echo Installing builder (one time, ~1 min)...
python -m pip install --quiet pyinstaller
if errorlevel 1 (
    echo Could not install pyinstaller. Check your internet and try again.
    pause
    exit /b 1
)

echo Building ClipMakerPC.exe (takes a few minutes)...
python -m PyInstaller ClipMakerPC.spec
if errorlevel 1 (
    echo Build failed. Copy any red error text and send it to Muse.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done! Your program is here:
echo  dist\ClipMakerPC\ClipMakerPC.exe
echo.
echo  Copy the whole ClipMakerPC folder anywhere you like.
echo  First run may show a Windows SmartScreen warning
echo  (unsigned app) - click "More info" then "Run anyway".
echo ============================================================
pause
