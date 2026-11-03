@echo off
if exist "%~dp0常用版本\AbyssLauncher.exe" (
    start "" "%~dp0常用版本\AbyssLauncher.exe"
    exit /b 0
)
if not exist "%~dp0dist\Abyss\AbyssLauncher.exe" (
    echo Launcher not found: 常用版本\AbyssLauncher.exe
    pause
    exit /b 1
)
start "" "%~dp0dist\Abyss\AbyssLauncher.exe"
