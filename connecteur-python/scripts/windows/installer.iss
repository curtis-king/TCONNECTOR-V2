; T-CONNECTOR SFEC - Inno Setup Script
; Compile avec Inno Setup 6+ (https://jrsoftware.org/isinfo.php)

#define MyAppName "T-CONNECTOR SFEC"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "TopInfo"
#define MyAppURL "https://topinfo.cg"
#define MyAppExeName "TConnector.exe"
#define ServiceName "TConnectorSFEC"

[Setup]
AppId={{TCONNECTOR-SFEC-2026}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\TConnector
DefaultGroupName={#MyAppName}
OutputDir=..\..\installer
OutputBaseFilename=TConnector-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=
SetupIconFile=
UninstallDisplayIcon={app}\TConnector.exe

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Files]
Source: "..\..\dist\TConnector\TConnector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\TConnector\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\TConnector\config.json"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Dashboard SFEC"; Filename: "http://localhost:3000"
Name: "{group}\Desinstaller {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Creer un raccourci sur le Bureau"; GroupDescription: "Raccourcis:"
Name: "installservice"; Description: "Installer en tant que service Windows"; GroupDescription: "Service Windows:"; Flags: checkedonce
Name: "starthttps"; Description: "Demarrer le dashboard apres installation"; GroupDescription: "Demarrage:"; Flags: checkedonce

[Run]
Filename: "net"; Parameters: "stop {#ServiceName}"; Flags: runhidden; Check: IsServiceInstalled
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-service"; Tasks: installservice; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "--start-service"; Tasks: installservice; Flags: runhidden waituntilterminated
Filename: "http://localhost:3000"; Tasks: starthttps; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "net"; Parameters: "stop {#ServiceName}"; Flags: runhidden
Filename: "sc"; Parameters: "delete {#ServiceName}"; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-service"; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
function IsServiceInstalled: Boolean;
begin
  Result := Exec('sc', 'query ' + '{#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, 0) and 
            (GetMAJORVERSION >= 0);
end;
