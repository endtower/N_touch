@echo off
rem commandcode API 卡顿时双击本文件: 自动体检并重钉最优 Cloudflare 边缘 IP
cd /d "%~dp0"
python cc_fix_edge.py fix
echo.
pause
