; DiscordAnt installer (Inno Setup 6).
;
; Built by:  python build.py --installer
; which passes the version in (/DAppVersion=x.y.z) and expects the finished
; app folder in dist\DiscordAnt.
;
; It installs for the current user only, so it never asks for an
; administrator password.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DiscordAnt"
#define AppExe "DiscordAnt.exe"

[Setup]
; AppId ties updates and the uninstaller to earlier installs. Never change it.
AppId={{B39F4A9A-0706-4712-A408-C63E12FDC3AE}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=..\app\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
InfoBeforeFile=before_install.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The app holds this mark while it runs (see _hold_running_mark in gui.py),
; so setup and uninstall ask for it to be closed first.
AppMutex={#AppName}Running
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Program files of the version being replaced (an update may carry a
; different browser). Nothing you collected lives here.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\browser"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The browser writes a small "checked" mark beside itself on first use.
Type: filesandordirs; Name: "{app}\browser"
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// What you collected is kept on uninstall unless you say otherwise.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\{#AppName}');
  if UninstallSilent or not DirExists(DataDir) then
    exit;
  if MsgBox('Also delete everything {#AppName} collected on this PC?' + #13#10#13#10 +
            'That is the message archive, the saved pictures, your batches and ' +
            'the saved Discord login, in:' + #13#10 + DataDir + #13#10#13#10 +
            'Choose No to keep it, for example if you plan to install again.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
