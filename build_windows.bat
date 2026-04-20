@echo off
REM ─────────────────────────────────────────────
REM  AutoCut — Windows build script
REM  Produces AutoCut-Windows-Setup.exe installer
REM ─────────────────────────────────────────────

echo Installing dependencies...
pip install -r requirements.txt pyinstaller

echo Building AutoCut.exe...
pyinstaller --noconfirm --onedir --windowed --collect-data tkinterdnd2 --name "AutoCut" main.py

REM ── Try to build installer with Inno Setup ──
where iscc >nul 2>&1
if %errorlevel% == 0 (
    echo Building installer with Inno Setup...
    iscc autocut_setup.iss
    echo.
    echo Done! Installer: dist\AutoCut-Windows-Setup.exe
) else (
    echo Inno Setup not found - creating zip instead...
    powershell -Command "Compress-Archive -Path 'dist\AutoCut' -DestinationPath 'dist\AutoCut-Windows.zip' -Force"
    echo.
    echo Done! dist\AutoCut-Windows.zip
    echo.
    echo TIP: Install Inno Setup from https://jrsoftware.org/isinfo.php
    echo      to build a proper one-click installer next time.
)
pause
