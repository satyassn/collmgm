; CollMgm Beta Installer
; Build with Inno Setup 6.x: https://jrsoftware.org/isinfo.php

#define MyAppName "CollMgm"
#define MyAppVersion "0.1"
#ifndef MyAppRelease
  #define MyAppRelease "alpha"
#endif
#ifndef MyAppBuild
  #define MyAppBuild "0"
#endif
#define MyAppPublisher "Your Company Name"
#define MyAppExeName "run.bat"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}-{#MyAppRelease}-build{#MyAppBuild}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CollMgm-{#MyAppRelease}-Build{#MyAppBuild}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Require no admin rights — installs to user's AppData
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"

[Files]
; Python embeddable runtime — place python-3.12.x-embed-amd64 contents here before build
Source: "python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; Application scripts
Source: "..\scripts\collmenu.py";        DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_cli.py";        DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_workflow.py";   DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_data.py";       DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\coll_store.py";      DestDir: "{app}\scripts"; Flags: ignoreversion

; Test data (pre-seeded CSVs)
Source: "..\data\users.csv";                    DestDir: "{app}\data"; Flags: ignoreversion
Source: "..\data\beats.csv";                    DestDir: "{app}\data"; Flags: ignoreversion
Source: "..\data\vouchers.csv";                 DestDir: "{app}\data"; Flags: ignoreversion
Source: "..\data\installments.csv";             DestDir: "{app}\data"; Flags: ignoreversion
Source: "..\data\completed_vouchers.csv";       DestDir: "{app}\data"; Flags: ignoreversion
Source: "..\data\completed_installments.csv";   DestDir: "{app}\data"; Flags: ignoreversion

; Launcher and guide
Source: "..\run.bat";        DestDir: "{app}"; Flags: ignoreversion
Source: "BETA_GUIDE.txt";    DestDir: "{app}"; Flags: ignoreversion isreadme

[Dirs]
; Create staging and archive dirs so the app can write to them immediately
Name: "{app}\staging"
Name: "{app}\archive"
Name: "{app}\prints"

[Icons]
Name: "{group}\CollMgm";              Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run.bat"" --run"; WorkingDir: "{app}"; IconFilename: "{sys}\cmd.exe"
Name: "{group}\Uninstall CollMgm";    Filename: "{uninstallexe}"
Name: "{commondesktop}\CollMgm";      Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run.bat"" --run"; WorkingDir: "{app}"; IconFilename: "{sys}\cmd.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\cmd.exe"; Parameters: "/k ""{app}\run.bat"" --run"; Description: "Launch CollMgm now"; Flags: nowait postinstall skipifsilent
