@echo off
if not exist "%~dp0dist\Abyss\AbyssLauncher.exe" (
    echo Launcher not found: dist\Abyss\AbyssLauncher.exe
    pause
    exit /b 1
)
start "" "%~dp0dist\Abyss\AbyssLauncher.exe"
