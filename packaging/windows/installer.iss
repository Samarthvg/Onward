; Inno Setup 6 script for the Windows installer. packaging/build.py passes the
; /D values below; to build by hand from the repository root:
;   iscc /DAppVersion=0.1.0 "/DSourceDir=dist\It gets easier" packaging\windows\installer.iss
;
; Installs for the current user only (no administrator prompt) into
; %LOCALAPPDATA%\Programs\It gets easier, adds a Start menu entry, and starts
; the app, which turns on Start at login itself. Run again over an older
; version, it upgrades in place; the app's updater runs it with
; /SILENT /RELAUNCH=1 so the new version starts by itself afterwards.
;
; Uninstalling removes the program, its Start menu entry and its login
; item. Your jobs, resumes and settings (%LOCALAPPDATA%\JobRadar) stay
; unless you choose to delete them when asked.
;
; Code signing: set SignTool below to the owner's own certificate, e.g. in
; Inno Setup's Tools > Configure Sign Tools, a tool named "mysign" with
;   signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f "C:\path\cert.pfx" /p $p $f
; then uncomment the SignTool line. packaging/README.md has the details.

#ifndef AppName
  #define AppName "It gets easier"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\It gets easier"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputName
  #define OutputName "it-gets-easier-" + AppVersion + "-windows-setup"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\icon.ico"
#endif
#define AppExe AppName + ".exe"
#define DataDir "{localappdata}\JobRadar"

[Setup]
; Never change AppId: it is how an upgrade finds the installed copy.
AppId={{6C1C6B2E-4F0B-4B8E-9C52-3D3F1E2A7A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
; SignTool=mysign

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files of the previous version that this one no longer has.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: Relaunch

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""{#AppExe}"""; Flags: runhidden; RunOnceId: "StopApp"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, '{#AppName}');
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent and DirExists(ExpandConstant('{#DataDir}')) then
    if MsgBox('Also delete your saved jobs, resumes and settings?' #13#10#13#10 +
              ExpandConstant('{#DataDir}') + #13#10#13#10 +
              'Choose No to keep them for a later install.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{#DataDir}'), True, True, True);
end;
