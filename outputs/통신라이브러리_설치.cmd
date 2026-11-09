@echo off
cd /d "%~dp0"
py -3 -m pip install -r "%~dp0requirements.txt"
pause
