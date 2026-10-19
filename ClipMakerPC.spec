# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ClipMaker PC.
Build the .exe ON YOUR WINDOWS PC (PyInstaller cannot cross-compile
from Linux/macOS to Windows):

    pip install -r requirements.txt
    pip install pyinstaller
    pyinstaller ClipMakerPC.spec

The finished program is in dist/ClipMakerPC/.
Ship the whole ClipMakerPC folder (the .exe needs its _internal folder).
Requires ffmpeg on PATH (Install.bat handles this via winget).
"""

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("ui", "ui"),
        ("models", "models"),  # YuNet face-detection model for face tracking
    ],
    hiddenimports=[
        "faster_whisper",
        "ctranslate2",
        "onnxruntime",
        "tokenizers",
        "yt_dlp",
        "webview",
        "cv2",
        "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipMakerPC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed app, no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ClipMakerPC",
)
