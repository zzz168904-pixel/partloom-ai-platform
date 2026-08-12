#define AppName "PartLoom AI Platform"
#define AppVersion "0.1.0 Beta.3"
#define AppPublisher "PartLoom AI Platform contributors"
#define AppExeName "PartLoomAI.exe"

[Setup]
AppId={{D0C3F998-5286-46F3-BBC5-92A68D7B0D5C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\PartLoom AI Platform
DefaultGroupName=PartLoom AI Platform
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\artifacts
OutputBaseFilename=PartLoom-AI-Platform-0.1.0-Beta.3-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion=0.1.0.0
VersionInfoProductName={#AppName}
VersionInfoDescription=Deterministic AI CAD orchestration workbench

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\PartLoomAI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PartLoom AI Platform"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\PartLoom AI Platform"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch PartLoom AI Platform"; Flags: nowait postinstall skipifsilent
