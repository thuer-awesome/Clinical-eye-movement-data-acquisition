#define AppName "Clinical Capture"
#define AppVersion "0.5.0"
#define AppPublisher "Clinical Capture"
#define AppExeName "ClinicalCapture.exe"

[Setup]
AppId={{9F9B3D0E-30B3-4F66-9FC8-6EA4BDAE6B1B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\ClinicalCapture
DefaultGroupName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename=ClinicalCapture-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\ClinicalCapture\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{localappdata}\ClinicalCapture\data"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--data-dir ""{localappdata}\ClinicalCapture\data"""
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--data-dir ""{localappdata}\ClinicalCapture\data"""; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："
