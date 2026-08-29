@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_app.ps1"
if errorlevel 1 (
    echo.
    echo App startup failed. See the error message above.
    pause
)
