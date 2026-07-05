@echo off
rem CollMgm — stop and remove the Windows Service.
rem Called by the Inno Setup uninstaller.  Requires administrator privileges.

setlocal
set "APP=%~dp0"
set "NSSM=%APP%tools\nssm.exe"

echo Removing CollMgm web service...

"%NSSM%" stop   collmgm-server 2>nul
"%NSSM%" remove collmgm-server confirm 2>nul

netsh advfirewall firewall delete rule name="CollMgm Web Server" >nul 2>&1

echo Service removed.
endlocal
