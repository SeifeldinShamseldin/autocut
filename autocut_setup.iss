[Setup]
AppName=AutoCut
AppVersion=1.0
DefaultDirName={autopf}\AutoCut
DefaultGroupName=AutoCut
OutputDir=dist
OutputBaseFilename=AutoCut-Windows-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\AutoCut.exe

[Files]
Source: "dist\AutoCut\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\AutoCut"; Filename: "{app}\AutoCut.exe"
Name: "{commondesktop}\AutoCut"; Filename: "{app}\AutoCut.exe"

[Run]
Filename: "{app}\AutoCut.exe"; Description: "Launch AutoCut"; Flags: nowait postinstall skipifsilent
