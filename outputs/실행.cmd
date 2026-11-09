@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0Fluke 교정기.exe"
exit /b
