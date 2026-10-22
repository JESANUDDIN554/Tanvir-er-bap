; Itraji Cliper - Inno Setup installer script.
; Builds a single ItrajiCliper-Setup.exe: download one file, run it,
; it installs the app per-user (no admin prompt), adds Start Menu +
; desktop shortcuts, and offers to launch the app at the end.
; Compiled in CI right after the PyInstaller build (see
; .github/workflows/build-exe.yml). Source payload: dist\ClipMakerPC.

[Setup]
AppId={{7C4A1E62-3F5B-4A9D-9C21-5E8D6B0A2F41}
AppName=Itraji Cliper
AppVersion=2.0.0
AppPublisher=Itraji
DefaultDirName={localappdata}\Programs\Itraji Cliper
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=ItrajiCliper-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\ClipMakerPC.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\ClipMakerPC\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Itraji Cliper"; Filename: "{app}\ClipMakerPC.exe"
Name: "{autodesktop}\Itraji Cliper"; Filename: "{app}\ClipMakerPC.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipMakerPC.exe"; Description: "Launch Itraji Cliper"; Flags: nowait postinstall skipifsilent
