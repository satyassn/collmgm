@echo off
title CollMgm - Collection Management
cd /d "%~dp0"
python\python.exe scripts\collmenu.py
echo.
echo Press any key to close...
pause >nul
