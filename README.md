# ClipMaker PC

A real desktop app for Windows: paste a YouTube link, get viral-ready 9:16 clips
with burned-in word-highlight captions — all rendered on your own PC.

**Pipeline:** yt-dlp (download) → faster-whisper (local transcription) →
Google Gemini API (**free** key, you paste it in Settings) for viral-moment
detection + 0–100 virality scores → ffmpeg (9:16 reframe, karaoke captions,
hook overlay) → MP4 files in a local folder.

## Quick start (Windows 10/11)

1. Install **Python 3.10+** from https://www.python.org/downloads/
   (tick **"Add python.exe to PATH"** during setup).
2. Copy this whole `clipmaker-pc` folder onto your PC.
3. Double-click **`Install.bat`**. It installs ffmpeg (via winget) and all
   Python packages, then launches the app. First run takes a few minutes.
4. In the app, open **⚙ Settings** and paste your free Gemini API key from
   https://aistudio.google.com/app/apikey — this powers the AI moment
   detection. Without it, you can still import and transcribe, but the
   "Find moments" button will ask for the key instead of guessing.
5. Paste a YouTube link → **Import** → wait for transcription →
   **🔥 Find moments with AI** → tick clips → **Render selected**.

Clips land in `work/<job>/clips/` — use **📁 Open clips folder** in the app.

## Download the ready-made .exe (easiest)

Grab **ClipMakerPC-win64.zip** from the
[GitHub Releases page](https://github.com/JESANUDDIN554/clipmaker-pc/releases).
Unzip it anywhere, open the `ClipMakerPC` folder and double-click
**ClipMakerPC.exe** — no Python, no installs. ffmpeg/ffprobe are bundled
inside, so nothing else is needed. (Windows SmartScreen may warn on first
launch because the exe is unsigned — click "More info" → "Run anyway".)

## Build the .exe yourself (optional)

The release above is built automatically by GitHub Actions
(`.github/workflows/build-exe.yml`) on every push to `main`. To build
locally on your own Windows PC instead:

```
pip install -r requirements.txt
pip install pyinstaller
pyinstaller ClipMakerPC.spec
```

The program is `dist/ClipMakerPC/ClipMakerPC.exe` — ship the whole
`ClipMakerPC` folder (the exe needs its `_internal` folder next to it),
plus `ffmpeg.exe`/`ffprobe.exe` next to the exe (the release build does
this automatically).

## What's new in v2

- **Face-tracked 9:16 reframe** — the crop window follows the speaker's face
  (YuNet DNN, bundled model, smoothed motion). Toggle in Settings; falls back
  to center crop when no face is found.
- **Caption editor** — fix misheard words per clip before rendering
  (✏️ Edit captions on any moment; re-render bakes in corrections).
- **Remove filler words** toggle — cuts um/uh/like/you know from the audio,
  video AND captions using the word timestamps.
- **3 new caption styles** — Neon Glow, Hollow Outline, Boxed Pop
  (6 styles total).
- **Virality breakdown** — hook / flow / value sub-scores under every badge.
- **SRT export** per rendered clip + **merge** selected clips into one video.

## What's inside

| File | What it does |
|---|---|
| `app.py` | Desktop window (pywebview) + job management |
| `pipeline.py` | Download, transcribe, detect moments, render |
| `facetrack.py` | Face detection + smoothed crop trajectory |
| `models/` | Bundled YuNet face-detection model (no download needed) |
| `ui/` | Local web interface (index.html, style.css, app.js) |
| `requirements.txt` | Python dependencies |
| `ClipMakerPC.spec` | PyInstaller build recipe |
| `Install.bat` | One-click installer/launcher |

## Notes & limits

- The transcription model (`base`, ~150 MB) downloads automatically on first
  use and is cached afterwards.
- First launch also downloads nothing else big — everything else is pip.
  The face-detection model ships inside the `models/` folder (no download).
- Long videos take a while: YouTube throttles downloads, and transcription
  runs at ~0.3× realtime on CPU. Face tracking adds ~1-2 min per clip on CPU
  (it scans the video for faces first) — turn it off in Settings for speed.
- The 9:16 reframe follows faces in v2 (toggle in Settings); with the toggle
  off, or when no face is found, it uses a smart center crop.
- Filler-word removal cuts the filler segments from audio, video and captions;
  the clip gets shorter by exactly the removed amount.
- Your Gemini key is stored only in `%USERPROFILE%\.clipmaker_pc_settings.json`
  on your PC — it never leaves your machine except to call Google's API.
- All code in this folder is original and written for this project.
