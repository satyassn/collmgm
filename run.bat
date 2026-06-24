@echo off
if "%1"=="--run" goto run

start "CollMgm - Collection Management" cmd /k "%~f0" --run
exit /b

:run
title CollMgm - Collection Management
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"
) else (
    python -c "import sys; sys.path.insert(0, 'scripts'); import collmenu; collmenu.main()"
)
echo.
echo Press any key to close...
pause >nul
