; Inno Setup script for FPSConv.
; Built by .github/workflows/release.yml:  iscc /DAppVersion=1.0.42 packaging\installer.iss
; Per-user install (no admin prompt), so silent auto-updates just work.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "FPSConv"
#define AppPublisher "AdkHex"
#define AppURL "https://github.com/AdkHex/FPSConv"
#define AppExe "FPSConv.exe"

[Setup]
AppId={{7284D80D-2905-47F8-8B51-90BC7795CAD7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\FPSConv\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} command line"; Filename: "{app}\fpsconv-cli.exe"; Parameters: "doctor"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Runs after a normal install (checkbox) AND after a silent auto-update (no skipifsilent),
; so the app comes back by itself once the updater has replaced the files.
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
