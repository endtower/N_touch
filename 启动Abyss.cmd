@echo off
if not exist "%~dp0app\AbyssLauncher.exe" (
    echo Launcher not found: app\AbyssLauncher.exe
    pause
    exit /b 1
)
start "" /D "%~dp0app" "%~dp0app\AbyssLauncher.exe"
