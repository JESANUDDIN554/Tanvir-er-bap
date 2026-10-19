# ClipMaker PC

A real desktop app for Windows: paste a video link, get viral-ready clips
with burned-in word-highlight captions — all rendered on your own PC.
Same 3-step workflow as the Clip Maker web app: **Import → Customize → Clips**.

**Pipeline:** yt-dlp (download, with YouTube bot-check retries) →
faster-whisper (local transcription, 25 languages) → viral-moment detection
(**Offline** heuristic mode needs no key, or **AI** mode with your free
Gemini key) + 0–99 virality scores → ffmpeg (9:16 / 16:9 / 1:1 / 4:5 reframe,
720p/1080p, karaoke captions, hook overlay) → MP4 files in a local folder.

## Quick start (Windows 10/11)

1. Unzip **ClipMakerPC-win64.zip** (from
   [Releases](https://github.com/JESANUDDIN554/Tanvir-er-bap/releases)),
   double-click **ClipMakerPC.exe**. (SmartScreen may warn because the exe
   is unsigned — "More info" → "Run anyway".)
2. **Step 1 — Import:** paste a YouTube/Vimeo/Rumble/Twitch/Drive/direct link,
   or use a video file already on your PC. If YouTube shows a bot check,
   turn on **"Use my browser's YouTube login"** (log into YouTube in
   Chrome/Edge first).
3. **Step 2 — Customize:** pick detection mode (Offline needs no API key),
   aspect ratio, clip length, genre template, caption style, and toggles
   (emojis, keyword highlight, silence/filler removal, auto-censor).
   Use **Find clip moment** to jump to a described moment.
4. **Step 3 — Clips:** generate, preview, trim + re-render, export
   SRT/VTT, download everything as ZIP, or merge clips in Clip Composer.

No API key is needed for Offline mode. For AI detection, paste your free
Gemini key from https://aistudio.google.com/app/apikey in ⚙ Settings.

## What's new in v3 (web-app parity)

- **3-step wizard** (Import → Customize → Clips) mirroring the web app.
- **Aspect ratios** 9:16 / 16:9 / 1:1 / 4:5 with visual picker, **720p/1080p**.
- **Clip length presets** 15s–15min + custom seconds; **clip count** 4/8/12/16/Max (40).
- **6 genre templates** (Podcast, Comedy, Gaming, News/Bodycam, Business, Fitness).
- **Caption magic toggles**: add emojis, highlight keywords, remove silences,
  remove filler words, auto-censor beep.
- **Custom caption styles** — design and save your own, persisted.
- **Find clip moment** — natural-language search over the transcript
  (each hit stamped 🎯 Matched; misses reported honestly).
- **Batch import** up to 10 URLs, **local file** import, **SRT-as-transcript**.
- **Per-clip trim + re-render**, captions-only preview, **Download-all ZIP**,
  **SRT/VTT export**, **Premiere/DaVinci XML timeline**, **Clip Composer**
  (merge + intro/outro + cut/crossfade).
- **YouTube bot-check resilience**: android-client + browser-cookie retry
  chain, one-click cookie toggle, auto yt-dlp updates at startup.
- 25 transcription languages; 2h+ long-video warning; thumbnail previews.

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

## What's inside

| File | What it does |
|---|---|
| `app.py` | Localhost server + desktop-style window (Chrome/Edge --app) + job management |
| `pipeline.py` | Download (bot-check retry chain), transcribe, detect moments, render |
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
