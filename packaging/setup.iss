; CollMgm Installer — alpha (LAN web server edition)
; Build with Inno Setup 6.x: https://jrsoftware.org/isinfo.php
; Pre-requisites before building — see BUILD_STEPS.md:
;   1. packaging\python\  — Python 3.x Windows embeddable package
;   2. packaging\nssm\nssm.exe — NSSM 2.24 win64 binary
;   3. run setup_build_env.bat to install web packages into embedded Python

#define MyAppName "CollMgm"
#define MyAppVersion "0.1"
#ifndef MyAppRelease
  #define MyAppRelease "alpha"
#endif
#ifndef MyAppBuild
  #define MyAppBuild "0"
#endif
#define MyAppPublisher "Your Company Name"
#define ServiceName "collmgm-server"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}-{#MyAppRelease}-{#MyAppBuild}
AppPublisher={#MyAppPublisher}
DefaultDirName={commonpf64}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CollMgm-{#MyAppRelease}-{#MyAppBuild}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Admin rights required for Windows Service registration and firewall rule
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut for the CLI launcher"

; ---------------------------------------------------------------------------
; Files
; ---------------------------------------------------------------------------

[Files]

; ---- Embedded Python runtime -----------------------------------------------
Source: "python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; ---- NSSM service manager --------------------------------------------------
Source: "nssm\nssm.exe"; DestDir: "{app}\tools"; Flags: ignoreversion

; ---- Core application scripts ----------------------------------------------
Source: "..\scripts\collmenu.py";       DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_cli.py";       DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_workflow.py";  DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_orchestrate.py"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_data.py";      DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_store.py";     DestDir: "{app}\scripts"; Flags: ignoreversion

; ---- Web server scripts ----------------------------------------------------
Source: "..\scripts\coll_api.py";      DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\start_server.py";  DestDir: "{app}\scripts"; Flags: ignoreversion

; ---- Web templates and static assets ---------------------------------------
Source: "..\templates\*"; DestDir: "{app}\templates"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\static\*";    DestDir: "{app}\static";    Flags: ignoreversion recursesubdirs createallsubdirs

; ---- Service management scripts (installed to app root) --------------------
Source: "service_setup.bat";  DestDir: "{app}"; Flags: ignoreversion
Source: "service_remove.bat"; DestDir: "{app}"; Flags: ignoreversion

; ---- Launchers and docs ----------------------------------------------------
Source: "..\run.bat";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\run_server.bat";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "BETA_GUIDE.txt";     DestDir: "{app}"; Flags: ignoreversion isreadme

; ---------------------------------------------------------------------------
; Directories created on first install; never touched on upgrade
; ---------------------------------------------------------------------------

[Dirs]
Name: "{app}\data"
Name: "{app}\staging"
Name: "{app}\archive"
Name: "{app}\prints"
Name: "{app}\logs"
Name: "{app}\config"

; ---------------------------------------------------------------------------
; Shortcuts
; ---------------------------------------------------------------------------

[Icons]
Name: "{group}\CollMgm (CLI)";       Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run.bat""";        WorkingDir: "{app}"; IconFilename: "{sys}\cmd.exe"
Name: "{group}\CollMgm Web Server";  Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run_server.bat"""; WorkingDir: "{app}"; IconFilename: "{sys}\cmd.exe"
Name: "{group}\Uninstall CollMgm";   Filename: "{uninstallexe}"
Name: "{commondesktop}\CollMgm (CLI)"; Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run.bat"""; WorkingDir: "{app}"; IconFilename: "{sys}\cmd.exe"; Tasks: desktopicon

; ---------------------------------------------------------------------------
; Post-install: set up Windows Service and firewall rule
; ---------------------------------------------------------------------------

[Run]
; Run service_setup.bat (which calls NSSM and netsh) — needs admin
Filename: "{sys}\cmd.exe"; Parameters: "/c ""{app}\service_setup.bat"""; WorkingDir: "{app}"; StatusMsg: "Installing CollMgm web service..."; Flags: runhidden waituntilterminated

; Offer to open the web app in the default browser
Filename: "{sys}\cmd.exe"; Parameters: "/c start http://localhost:8100"; Description: "Open CollMgm in browser now"; Flags: nowait postinstall skipifsilent shellexec

; ---------------------------------------------------------------------------
; Pre-uninstall: remove Windows Service and firewall rule
; ---------------------------------------------------------------------------

[UninstallRun]
Filename: "{sys}\cmd.exe"; Parameters: "/c ""{app}\service_remove.bat"""; WorkingDir: "{app}"; StatusMsg: "Removing CollMgm web service..."; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"

; ---------------------------------------------------------------------------
; Code: stop and remove service BEFORE files are overwritten (upgrade safety)
; ---------------------------------------------------------------------------

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  NssmPath: String;
begin
  if CurStep = ssInstall then
  begin
    NssmPath := ExpandConstant('{app}\tools\nssm.exe');
    if FileExists(NssmPath) then
    begin
      // Upgrade path: gracefully stop then deregister the existing service
      // before any files are overwritten.  Errors are ignored — the service
      // may not be running or may not exist yet.
      Exec(NssmPath, 'stop ' + '{#ServiceName}', ExpandConstant('{app}'),
           SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(NssmPath, 'remove ' + '{#ServiceName}' + ' confirm', ExpandConstant('{app}'),
           SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end
    else
    begin
      // First install or NSSM not yet present: attempt sc stop/delete as fallback
      Exec(ExpandConstant('{sys}\net.exe'), 'stop ' + '{#ServiceName}', '',
           SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{sys}\sc.exe'), 'delete ' + '{#ServiceName}', '',
           SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
    // Brief pause to let the service process exit before files are overwritten
    Sleep(1500);
  end;
end;
