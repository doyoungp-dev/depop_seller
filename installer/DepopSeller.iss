; Depop Seller - the Windows installer (DepopSellerSetup.exe).
;
; Built by `python -m depop_seller release` through depop_seller/installer.py, which stages
;   <StageDir>\runtime   Python's official embeddable build plus the app's libraries
;   <StageDir>\app       exactly what DepopSeller.zip holds - the folder the in-app updater keeps current
; and passes AppVersion, StageDir and OutputDir on the ISCC command line.
;
; It installs for the current user only (no administrator password) into
; %LOCALAPPDATA%\Programs\Depop Seller, with Start menu and Desktop icons and an entry in
; Settings > Apps. The seller's data folder (photos, listings, style, key) lives elsewhere and is
; never touched - not by installing, not by upgrading, not by uninstalling.

#ifndef AppVersion
  #error AppVersion must be passed by installer.py
#endif
#ifndef StageDir
  #error StageDir must be passed by installer.py
#endif
#ifndef OutputDir
  #error OutputDir must be passed by installer.py
#endif

#define AppName "Depop Seller"
#define Launch "-m depop_seller hub"

[Setup]
; Never change AppId: it is how Windows knows a newer installer upgrades this app rather than
; installing a second copy next to it.
AppId={{D3ACB89F-C80D-4399-93C8-32AC74F40CB5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=doyoungp-dev
AppPublisherURL=https://github.com/doyoungp-dev/depop_seller
AppSupportURL=https://github.com/doyoungp-dev/depop_seller/issues
AppUpdatesURL=https://github.com/doyoungp-dev/depop_seller/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} installer
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=DepopSellerSetup
SetupIconFile={#StageDir}\app\depop_seller\static\icon.ico
UninstallDisplayIcon={app}\app\depop_seller\static\icon.ico
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[InstallDelete]
; An upgrade starts from a clean copy, so nothing an older version left behind lingers.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#StageDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "{#Launch}"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\depop_seller\static\icon.ico"; Comment: "Turn clothing photos into Depop listings"
; /desktopicon=no leaves the Desktop alone (used when testing an installer on a working machine).
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "{#Launch}"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\depop_seller\static\icon.ico"; Comment: "Turn clothing photos into Depop listings"; Check: WantDesktopIcon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "{#Launch}"; WorkingDir: "{app}\app"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Files the app or its updater wrote after installing (compiled code, newer app files).
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"

[Messages]
FinishedLabel=Depop Seller is installed. Open it any time from the Depop Seller icon on your Desktop or in the Start menu.

[Code]
function WantDesktopIcon(): Boolean;
begin
  Result := ExpandConstant('{param:desktopicon|yes}') <> 'no';
end;

{ A copy of the app that is open holds its files. Ask it to close, the way its own Close button
  does: the app answers POST /quit on 127.0.0.1:8765. Nothing is running? Nothing happens. }
procedure CloseRunningApp();
var
  Http: Variant;
begin
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.SetTimeouts(1000, 1000, 2000, 2000);
    Http.Open('POST', 'http://127.0.0.1:8765/quit', False);
    Http.SetRequestHeader('Content-Type', 'application/json');
    Http.Send('{}');
    Sleep(2000);
  except
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningApp();
  Result := True;
end;
