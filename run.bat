@echo off
if "%1"=="--run" goto run

rem Apply medium font (Consolas 18pt) to the CollMgm console window before opening it
reg add "HKCU\Console\CollMgm - Collection Management" /v FontSize   /t REG_DWORD /d 1310720 /f >nul 2>&1
reg add "HKCU\Console\CollMgm - Collection Management" /v FaceName   /t REG_SZ    /d "Consolas" /f >nul 2>&1
reg add "HKCU\Console\CollMgm - Collection Management" /v FontFamily /t REG_DWORD /d 54 /f >nul 2>&1
reg add "HKCU\Console\CollMgm - Collection Management" /v FontWeight /t REG_DWORD /d 400 /f >nul 2>&1

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
