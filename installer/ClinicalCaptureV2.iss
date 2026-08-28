#define AppName "Clinical Capture V2"
#define AppVersion "0.8.0"
#define AppPublisher "Clinical Capture"
#define AppExeName "ClinicalCaptureV2.exe"

[Setup]
AppId={{C3C2C258-1D32-4C23-BE0B-2D0C7E6A2B0A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\ClinicalCaptureV2
DefaultGroupName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename=ClinicalCaptureV2-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\ClinicalCaptureV2\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{localappdata}\ClinicalCapture\data"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："
