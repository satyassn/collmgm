@echo off
title CollMgm - Collection Management
cd /d "%~dp0"
python\python.exe -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"
echo.
echo Press any key to close...
pause >nul
