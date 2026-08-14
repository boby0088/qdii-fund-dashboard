@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  start "" http://127.0.0.1:8765
  python run.py serve 8765
) else (
  echo 未找到 python，请先安装 Python 3 并加入 PATH
  pause
)
