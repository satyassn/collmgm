@echo off
cd /d "%~dp0"
echo Starting CollMgm web server...
echo Access on this PC:  http://localhost:8100
echo Access on the LAN:  http://%COMPUTERNAME%:8100
echo.
echo Press Ctrl+C to stop.
echo.
if exist "%~dp0python\python.exe" (
  rem Embedded runtime (installed build)
  "%~dp0python\python.exe" "%~dp0scripts\start_server.py"
) else (
  rem System Python (developer machine)
  python scripts\start_server.py
)
pause
