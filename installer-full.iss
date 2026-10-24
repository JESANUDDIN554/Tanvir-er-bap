; Itraji Cliper - FULL Inno Setup installer script.
; Builds a single ItrajiCliper-Setup-Full.exe that contains the whole
; app PLUS the built-in AI: the ~9 GB Qwen2.5-14B-Instruct Q4_K_M GGUF
; and the llama.cpp engine, so after install the Built-in AI works with
; zero first-run downloads. Same AppId / install dir as the light
; installer (installer.iss), so it installs over it cleanly.
; Compiled in CI by .github/workflows/build-full.yml, which downloads
; the model + engine into a staging dir first (never stored in git).
;
; Placement matches local_ai.py exactly: DATA_DIR is the install dir,
; the model must land at {app}\models\Qwen2.5-14B-Instruct-Q4_K_M.gguf
; (exact size 8988110976 bytes) and llama-server.exe under {app}\llama\
; for model_ready() to be true on first launch.
;
; ISPP defines (CI overrides them with absolute staging paths):
;   FullPayloadDir - dir holding models\*.gguf and llama\* (default
;                    "fullpayload", relative to this script)
;   FullOutDir     - where the .exe is written (default ".")

#ifndef FullPayloadDir
#define FullPayloadDir "fullpayload"
#endif
#ifndef FullOutDir
#define FullOutDir "."
#endif

[Setup]
AppId={{7C4A1E62-3F5B-4A9D-9C21-5E8D6B0A2F41}
AppName=Itraji Cliper
AppVersion=2.0.0
AppPublisher=Itraji
DefaultDirName={localappdata}\Programs\Itraji Cliper
DisableProgramGroupPage=yes
OutputDir={#FullOutDir}
OutputBaseFilename=ItrajiCliper-Setup-Full
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
; The app itself - identical payload to the light installer.
Source: "dist\ClipMakerPC\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; The built-in AI model (~9 GB). GGUF data does not compress, so store
; it raw (nocompression) in its own solid block (solidbreak) instead of
; burning CI time in lzma2 for zero gain.
Source: "{#FullPayloadDir}\models\Qwen2.5-14B-Instruct-Q4_K_M.gguf"; DestDir: "{app}\models"; Flags: ignoreversion nocompression solidbreak
; The llama.cpp engine, pre-extracted exactly like local_ai.py's own
; first-run extraction would lay it down.
Source: "{#FullPayloadDir}\llama\*"; DestDir: "{app}\llama"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Itraji Cliper"; Filename: "{app}\ClipMakerPC.exe"
Name: "{autodesktop}\Itraji Cliper"; Filename: "{app}\ClipMakerPC.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipMakerPC.exe"; Description: "Launch Itraji Cliper"; Flags: nowait postinstall skipifsilent
