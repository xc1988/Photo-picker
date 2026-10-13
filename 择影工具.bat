@echo off
rem 摄影师择影工具 — 双击启动；也可把照片文件夹拖到本图标上
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 ( set PY=python ) else ( set PY=py )
start "" %PY% photopicker.py %*
