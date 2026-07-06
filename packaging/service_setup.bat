@echo off
rem CollMgm — install and start the Windows Service.
rem Run this from {app}\ after installation (called by the Inno Setup installer).
rem Requires administrator privileges.

setlocal
set "APP=%~dp0"
set "NSSM=%APP%tools\nssm.exe"
set "PYTHON=%APP%python\python.exe"
set "SCRIPT=%APP%scripts\start_server.py"

echo Installing CollMgm web service...

rem Remove any previous installation (idempotent)
"%NSSM%" stop   collmgm-server 2>nul
"%NSSM%" remove collmgm-server confirm 2>nul

rem Install service
"%NSSM%" install collmgm-server "%PYTHON%"
if %ERRORLEVEL% neq 0 (
  echo ERROR: Failed to install service.
  exit /b 1
)

rem Configure service
"%NSSM%" set collmgm-server AppParameters      "\"%SCRIPT%\""
"%NSSM%" set collmgm-server AppDirectory       "%APP:~0,-1%"
"%NSSM%" set collmgm-server DisplayName        "CollMgm Web Server"
"%NSSM%" set collmgm-server Description        "CollMgm LAN collection management web server"
"%NSSM%" set collmgm-server Start              SERVICE_AUTO_START
"%NSSM%" set collmgm-server AppStdout          "%APP%logs\server.log"
"%NSSM%" set collmgm-server AppStderr          "%APP%logs\server-error.log"
"%NSSM%" set collmgm-server AppRotateFiles     1
"%NSSM%" set collmgm-server AppRotateBytes     1048576
"%NSSM%" set collmgm-server AppRotateSeconds   86400
"%NSSM%" set collmgm-server AppRestartDelay    5000

rem Start service
"%NSSM%" start collmgm-server
if %ERRORLEVEL% neq 0 (
  echo WARNING: Service installed but failed to start immediately.
  echo Check logs at %APP%logs\
)

rem Add firewall inbound rule (LAN profiles only)
netsh advfirewall firewall delete rule name="CollMgm Web Server" >nul 2>&1
netsh advfirewall firewall add rule ^
  name="CollMgm Web Server" ^
  protocol=TCP ^
  dir=in ^
  localport=8100 ^
  action=allow ^
  profile=domain,private

echo.
echo Done.  CollMgm is accessible on the local network:
echo   http://%COMPUTERNAME%:8100
endlocal
