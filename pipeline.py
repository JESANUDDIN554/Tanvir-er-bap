"""ClipMaker PC - local video processing pipeline.

Stages: yt-dlp download -> faster-whisper transcription ->
Gemini (free API key) viral-moment detection -> ffmpeg 9:16 render
with karaoke captions and hook overlay.

All code is original, written for this project.
"""

import json
import os
import re
import subprocess
import tempfile

import requests

# ---------------------------------------------------------------------------
# yt-dlp download
# ---------------------------------------------------------------------------

def download_video(url, out_dir, progress_cb=None):
    """Download best <=720p mp4. Returns (video_path, title, duration_sec)."""
    import yt_dlp

    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, "source.%(ext)s")

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            progress_cb(min(99, pct), "Downloading video")

    opts = {
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 3,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        # merged file may have .mp4 extension instead of original ext
        if not os.path.exists(path):
            base = os.path.splitext(path)[0]
            for ext in (".mp4", ".mkv", ".webm"):
                if os.path.exists(base + ext):
                    path = base + ext
                    break
    title = info.get("title") or "video"
    duration = int(info.get("duration") or 0)
    if progress_cb:
        progress_cb(100, "Download complete")
    return path, title, duration


# ---------------------------------------------------------------------------
# faster-whisper transcription
# ---------------------------------------------------------------------------

def _extract_wav(video_path, wav_path):
    """Extract 16kHz mono WAV with ffmpeg (avoids PyAV version issues)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav_path]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError("audio extraction failed")


def _read_wav_mono16k(wav_path):
    import wave
    import numpy as np
    with wave.open(wav_path, "rb") as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
    audio = np.frombuffer(raw, dtype=np.int16 if sw == 2 else np.int32)
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        # simple linear resample
        import numpy as np2  # noqa: F811
        old_idx = np2.linspace(0, len(audio), int(len(audio) * 16000 / sr))
        audio = np2.interp(np2.arange(int(old_idx[-1]) + 1),
                           np2.arange(len(audio)), audio)
    return (audio.astype("float32") / 32768.0)


def transcribe(video_path, model_name="base", progress_cb=None):
    """Transcribe with word-level timestamps. Returns list of word dicts."""
    from faster_whisper import WhisperModel

    if progress_cb:
        progress_cb(5, "Loading transcription model")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    if progress_cb:
        progress_cb(10, "Extracting audio")
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="trans_")
    os.close(wav_fd)
    try:
        _extract_wav(video_path, wav_path)
        audio = _read_wav_mono16k(wav_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass
    if progress_cb:
        progress_cb(15, "Transcribing audio")

    try:
        segments, _info = model.transcribe(
            audio, word_timestamps=True, vad_filter=True)
    except Exception:
        segments, _info = model.transcribe(audio, word_timestamps=True)

    words = []
    seg_list = list(segments)
    total = max(len(seg_list), 1)
    for i, seg in enumerate(seg_list):
        if progress_cb and i % 5 == 0:
            progress_cb(10 + int(i / total * 85), "Transcribing audio")
        if seg.words:
            for w in seg.words:
                words.append(
                    {"start": float(w.start), "end": float(w.end),
                     "word": w.word.strip()}
                )
        else:
            # fall back to segment-level timing split evenly across tokens
            tokens = seg.text.strip().split()
            if not tokens:
                continue
            dur = (seg.end - seg.start) / len(tokens)
            for j, tok in enumerate(tokens):
                words.append({
                    "start": seg.start + j * dur,
                    "end": seg.start + (j + 1) * dur,
                    "word": tok,
                })
    words = [w for w in words if w["word"]]
    if progress_cb:
        progress_cb(100, f"Transcription complete ({len(words)} words)")
    return words


def transcript_text(words):
    """Plain text with [mm:ss] markers, for the moment-detection prompt."""
    lines, cur, start = [], [], None
    for w in words:
        if start is None:
            start = w["start"]
        cur.append(w["word"])
        # break line roughly every 12 words
        if len(cur) >= 12:
            lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] "
                         + " ".join(cur))
            cur, start = [], None
    if cur:
        lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] "
                     + " ".join(cur))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini viral-moment detection (free API key)
# ---------------------------------------------------------------------------

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-2.0-flash:generateContent")

PROMPT_TEMPLATE = """You are a viral short-form video editor. Below is a transcript of a video,
with [mm:ss] timestamps marking when each line starts.

RULES - follow strictly:
- Only pick moments from things ACTUALLY said in the transcript. Never invent events, quotes, or timestamps.
- Each clip must be between {min_s} and {max_s} seconds long.
- start/end are seconds (numbers) matching the timestamp markers.
- Score virality 0-100: hook strength of the first 3 seconds, emotional peak, quotability.
- Also score three sub-dimensions 0-100: "hook" (do the first 3 seconds stop
  the scroll?), "flow" (does the moment hold attention start to finish?),
  "value" (does the viewer leave with something: laugh, shock, knowledge?).
- Return at most {max_clips} moments, best first.
- Return ONLY valid JSON, no other text.

Transcript:
{transcript}

JSON format:
{{"moments": [{{"start": 12, "end": 45, "title": "Short punchy title",
"hook": "Opening on-screen text, under 8 words",
"virality": 87, "hook_score": 90, "flow_score": 84, "value_score": 85,
"reason": "One sentence why this will perform"}}]}}"""


def _gemini_call(prompt, api_key):
    r = requests.post(
        GEMINI_URL,
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.7},
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    # strip possible markdown fences
    text = re.sub(r"^```[a-z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text.strip())
    return json.loads(text)


def detect_moments(words, api_key, clip_min_s, clip_max_s,
                   max_clips=8, progress_cb=None):
    """Use Gemini to find viral moments. Returns list of moment dicts."""
    if not api_key:
        raise RuntimeError("no_api_key")
    full = transcript_text(words)
    # chunk long transcripts so each request stays in context
    chunks, cur = [], []
    for line in full.split("\n"):
        cur.append(line)
        if sum(len(x) for x in cur) > 20000:
            chunks.append("\n".join(cur))
            cur = []
    if cur:
        chunks.append("\n".join(cur))

    moments = []
    for i, chunk in enumerate(chunks):
        if progress_cb:
            progress_cb(int(i / max(len(chunks), 1) * 90),
                        f"AI analyzing transcript ({i + 1}/{len(chunks)})")
        prompt = PROMPT_TEMPLATE.format(
            min_s=clip_min_s, max_s=clip_max_s,
            max_clips=max_clips, transcript=chunk)
        try:
            data = _gemini_call(prompt, api_key)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 403):
                raise RuntimeError("bad_api_key")
            raise RuntimeError("gemini_error")
        for m in data.get("moments", []):
            try:
                start = float(m["start"])
                end = float(m["end"])
                if end <= start or start < 0:
                    continue
                # clamp to clip-length bounds
                if end - start > clip_max_s:
                    end = start + clip_max_s
                v = max(0, min(100, int(m.get("virality", 50))))

                def _sub(key):
                    try:
                        return max(0, min(100, int(m.get(key, v))))
                    except (TypeError, ValueError):
                        return v

                moments.append({
                    "start": start,
                    "end": end,
                    "title": str(m.get("title", "Untitled"))[:80],
                    "hook": str(m.get("hook", ""))[:60],
                    "virality": v,
                    "hook_score": _sub("hook_score"),
                    "flow_score": _sub("flow_score"),
                    "value_score": _sub("value_score"),
                    "reason": str(m.get("reason", ""))[:160],
                })
            except (KeyError, TypeError, ValueError):
                continue

    moments.sort(key=lambda m: m["virality"], reverse=True)
    if progress_cb:
        progress_cb(100, f"Found {len(moments[:max_clips])} moments")
    return moments[:max_clips]


# ---------------------------------------------------------------------------
# ASS caption builder (karaoke word highlight + hook overlay)
# ---------------------------------------------------------------------------

STYLES = {
    "bold-pop": {
        "label": "Bold Pop",
        "primary": "&H0000FFFF",    # yellow
        "secondary": "&H00FFFFFF",  # white sweep base
        "outline": "&H00000000",
        "bold": 1,
    },
    "lime-punch": {
        "label": "Lime Punch",
        "primary": "&H002EFFC8",    # lime
        "secondary": "&H00FFFFFF",
        "outline": "&H00000000",
        "bold": 1,
    },
    "clean": {
        "label": "Clean White",
        "primary": "&H00FFFFFF",    # white
        "secondary": "&H00FFE500",  # cyan sweep
        "outline": "&H80000000",
        "bold": 0,
    },
    "neon-glow": {
        "label": "Neon Glow",
        "primary": "&H00FFFF00",    # neon cyan
        "secondary": "&H00FFFFFF",  # white sweep
        "outline": "&H00000000",
        "bold": 1,
        "glow": 1,                 # thick outline + shadow = glow feel
    },
    "hollow": {
        "label": "Hollow Outline",
        "primary": "&HFF000000",    # fully transparent fill
        "secondary": "&HFF000000",  # sweep stays invisible too
        "outline": "&H00FFFFFF",    # solid white outline
        "bold": 1,
        "hollow": 1,               # extra-thick outline, no shadow
    },
    "boxed": {
        "label": "Boxed Pop",
        "primary": "&H00FFFFFF",    # white text
        "secondary": "&H0000FFFF",  # yellow sweep
        "outline": "&HFF000000",    # opaque black box behind text
        "bold": 1,
        "box": 1,                  # BorderStyle 3 = opaque box
    },
}


def _esc(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _group_lines(words):
    """Group words into caption lines."""
    lines, cur, chars = [], [], 0
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.8 or chars > 34
                    or len(cur) >= 7):
            lines.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += len(w["word"]) + 1
    if cur:
        lines.append(cur)
    return lines


def build_ass(words, start_s, end_s, style_name, hook_text, out_path):
    """Write an ASS subtitle file for one clip window."""
    st = STYLES.get(style_name, STYLES["bold-pop"])
    win = [w for w in words if w["end"] > start_s and w["start"] < end_s]
    win.sort(key=lambda w: w["start"])

    def ts(s):
        s = max(0, s)
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}.{int((s % 1) * 100):02d}"

    body = []
    box_events = []
    use_box = bool(st.get("box"))
    # hook overlay at the top, first 4 seconds
    if hook_text:
        hook_end = min(start_s + 4, end_s)
        layer = 1 if use_box else 0
        body.append(
            f"Dialogue: {layer},{ts(start_s)},{ts(hook_end)},Hook,,0,0,0,,"
            f"{_esc(hook_text.upper())}")

    for line in _group_lines(win):
        parts = []
        for w in line:
            cs = max(1, int((w["end"] - w["start"]) * 100))
            parts.append(f"{{\\k{cs}}}{_esc(w['word'])}")
        layer = 1 if use_box else 0
        body.append(
            f"Dialogue: {layer},{ts(line[0]['start'])},{ts(line[-1]['end'])},"
            f"Caption,,0,0,0,,{' '.join(parts)}")
        if use_box:
            # opaque rounded-ish box behind the line, drawn with vector
            # graphics at absolute script coords (top-left origin)
            text = " ".join(w["word"] for w in line)
            est_w = sum(20 if c == " " else
                        24 if c in "ilj.,!;:'\"" else 36 for c in text)
            est_w = est_w * 60 / 60 + 56  # padding
            x0, x1 = 540 - est_w / 2, 540 + est_w / 2
            y0, y1 = 1592, 1700
            box_events.append(
                f"Dialogue: 0,{ts(line[0]['start'])},{ts(line[-1]['end'])},"
                f"Box,,0,0,0,,{{\\an7\\pos(0,0)\\p1}}"
                f"m {x0:.0f} {y0} l {x1:.0f} {y0} "
                f"l {x1:.0f} {y1} l {x0:.0f} {y1}{{\\p0}}")

    # style variants: glow = thick outline + shadow, hollow = thick
    # outline only, box = opaque box drawn behind the text via {\p1}
    # (this libass build ignores BorderStyle 3, so the box is vector-drawn)
    if st.get("glow"):
        border_style, outline_w, shadow_w = 1, 4, 2
    elif st.get("hollow"):
        border_style, outline_w, shadow_w = 1, 6, 0
    elif st.get("box"):
        border_style, outline_w, shadow_w = 1, 2, 0
    else:
        border_style, outline_w, shadow_w = 1, 3, 1

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{60},{st['primary']},{st['secondary']},{st['outline']},&H99000000,{st['bold']},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_w},2,40,40,240,1
Style: Hook,Arial,{54},&H0000FFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,3,2,0,8,40,40,150,1
Style: Box,Arial,10,&HCC000000,&HCC000000,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(box_events + body) + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass)
    return out_path


def _ffmpeg_ass_path(path):
    # escape for the subtitles filter on Windows and Linux
    p = path.replace("\\", "/")
    return p.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


# ---------------------------------------------------------------------------
# v2: filler-word removal
# ---------------------------------------------------------------------------

# Single-word fillers plus multi-word phrases, matched case-insensitively
# against the word timestamps. Toggleable in Settings (default off).
FILLERS = {"um", "uh", "uhm", "er", "erm", "ah", "ahh", "hmm", "mm",
           "like", "basically", "actually", "literally"}
FILLER_PHRASES = (("you", "know"), ("i", "mean"), ("sort", "of"),
                  ("kind", "of"))


def find_filler_ranges(words, pad=0.08):
    """Return sorted [(start, end)] ranges covering filler words/phrases."""
    ranges = []
    lowered = [w["word"].strip().lower().strip(",.!?;:") for w in words]
    i = 0
    while i < len(words):
        matched = False
        for phrase in FILLER_PHRASES:
            n = len(phrase)
            if (i + n <= len(words)
                    and tuple(lowered[i:i + n]) == phrase):
                ranges.append((words[i]["start"], words[i + n - 1]["end"]))
                i += n
                matched = True
                break
        if matched:
            continue
        if lowered[i] in FILLERS:
            ranges.append((words[i]["start"], words[i]["end"]))
        i += 1
    # pad + merge overlaps
    padded = [(max(0, s - pad), e + pad) for s, e in ranges]
    padded.sort()
    merged = []
    for s, e in padded:
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def compute_keep_segments(start, end, removed):
    """Complement of removed ranges inside [start, end)."""
    segs, cur = [], start
    for s, e in sorted(removed):
        s, e = max(s, start), min(e, end)
        if e <= cur or s >= end:
            continue
        if s > cur:
            segs.append((cur, s))
        cur = max(cur, e)
    if cur < end:
        segs.append((cur, end))
    return [(s, e) for s, e in segs if e - s > 0.05]


def remap_words_to_kept(words, removed):
    """Shift word timings onto the filler-free timeline.

    new_t = t - (total removed duration before t). Words inside removed
    ranges are dropped.
    """
    removed = sorted(removed)
    out = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2.0
        if any(s <= mid <= e for s, e in removed):
            continue
        shift = sum(e - s for s, e in removed if e <= mid)
        out.append({"start": w["start"] - shift,
                    "end": w["end"] - shift, "word": w["word"]})
    return out


def kept_duration(start, end, removed):
    return sum(e - s for s, e in compute_keep_segments(start, end, removed))


# ---------------------------------------------------------------------------
# v2: caption editor re-timing
# ---------------------------------------------------------------------------

def retime_words(orig_words, new_text, start, end):
    """Fit edited caption text back onto word timings.

    Same word count -> keep original timings, swap text.
    Different count -> distribute across the original time span,
    weighted by character length so pacing stays natural.
    """
    new_tokens = new_text.split()
    if not new_tokens:
        return []
    span_s = orig_words[0]["start"] if orig_words else start
    span_e = orig_words[-1]["end"] if orig_words else end
    if len(new_tokens) == len(orig_words):
        return [{"start": w["start"], "end": w["end"], "word": t}
                for w, t in zip(orig_words, new_tokens)]
    weights = [max(len(t), 1) for t in new_tokens]
    total_w = sum(weights)
    span = max(span_e - span_s, 0.1)
    out, cur = [], span_s
    for tok, wt in zip(new_tokens, weights):
        dur = span * wt / total_w
        out.append({"start": cur, "end": cur + dur, "word": tok})
        cur += dur
    return out


# ---------------------------------------------------------------------------
# v2: SRT export
# ---------------------------------------------------------------------------

def write_srt(words, start, end, out_path):
    """Write an SRT subtitle file for one clip window."""
    win = [w for w in words if w["end"] > start and w["start"] < end]
    win.sort(key=lambda w: w["start"])

    def ts(s):
        s = max(0, s)
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    blocks = []
    for n, line in enumerate(_group_lines(win), 1):
        text = " ".join(w["word"] for w in line)
        blocks.append(f"{n}\n{ts(line[0]['start'])} --> "
                      f"{ts(line[-1]['end'])}\n{text}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return out_path


# ---------------------------------------------------------------------------
# v2: multi-clip merge
# ---------------------------------------------------------------------------

def merge_clips(clip_paths, out_path):
    """Concatenate rendered MP4s (same codec/params) into one file."""
    if len(clip_paths) < 2:
        raise RuntimeError("pick at least 2 clips to merge")
    for p in clip_paths:
        if not os.path.exists(p):
            raise RuntimeError("missing clip file: " + p)
    lst_fd, lst_path = tempfile.mkstemp(suffix=".txt", prefix="merge_")
    os.close(lst_fd)
    try:
        with open(lst_path, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write("file '" + p.replace("'", "'\\''") + "'\n")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
               "-i", lst_path, "-c", "copy", "-movflags", "+faststart",
               out_path]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("merge failed: " + r.stderr[-1500:])
    finally:
        try:
            os.remove(lst_path)
        except OSError:
            pass
    return out_path


# ---------------------------------------------------------------------------
# v2: probing + face-tracked / filler-cut piped render
# ---------------------------------------------------------------------------

def probe_video(video_path):
    """Return (width, height, fps) via ffprobe (ffmpeg fallback)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "default=noprint_wrappers=1", video_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        vals = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
        w, h = int(vals.get("width", 0)), int(vals.get("height", 0))
        num, den = vals.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
        if w and h and fps:
            return w, h, fps
    except Exception:  # noqa: BLE001 - fall through to ffmpeg probe
        pass
    # fallback: parse ffmpeg -i output
    r = subprocess.run(["ffmpeg", "-i", video_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True)
    import re as _re
    m = _re.search(r"(\d{2,5})x(\d{2,5})", r.stderr)
    f = _re.search(r"(\d+(?:\.\d+)?)\s*fps", r.stderr)
    w = int(m.group(1)) if m else 1280
    h = int(m.group(2)) if m else 720
    fps = float(f.group(1)) if f else 30.0
    return w, h, fps


def _even(n):
    return int(n // 2 * 2)


def _kept_audio_filter(start, end, removed):
    """ffmpeg -filter_complex for the filler-free audio of one clip."""
    segs = compute_keep_segments(start, end, removed)
    if not segs:
        raise RuntimeError("filler removal would delete the whole clip")
    parts, labels = [], []
    for i, (s, e) in enumerate(segs):
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},"
                     f"asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[a{i}]")
    if len(labels) == 1:
        return "".join(parts).replace("[a0]", "[aout]", 1), "[aout]"
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    return ";".join(parts), "[aout]"


def render_clip_tracked(video_path, moment, words, style_name, out_path,
                        face_track=True, remove_fillers=False,
                        progress_cb=None):
    """Face-tracked 9:16 render with optional filler removal.

    Frames are cropped per-frame in Python (OpenCV) following the
    smoothed face trajectory, then piped to ffmpeg for encoding with
    burned-in captions. Falls back to center crop when no face is found.
    """
    try:
        import cv2  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "Face tracking needs opencv-python - reinstall with Install.bat")
    from facetrack import (FaceTracker, sample_face_centers,
                           smooth_trajectory, interpolate_x)

    start, end = moment["start"], moment["end"]
    src_w, src_h, fps = probe_video(video_path)
    crop_w = min(_even(src_h * 9 / 16), src_w)
    crop_h = src_h
    out_w, out_h = 1080, 1920

    win_words = [w for w in words if w["end"] > start and w["start"] < end]
    win_words.sort(key=lambda w: w["start"])

    # filler removal -> keep segments + remapped caption timings
    removed = find_filler_ranges(win_words) if remove_fillers else []
    removed = [(max(s, start), min(e, end)) for s, e in removed]
    removed = [(s, e) for s, e in removed if e > s]
    keep_segs = compute_keep_segments(start, end, removed)
    if not keep_segs:
        raise RuntimeError("filler removal would delete the whole clip")
    cap_words = remap_words_to_kept(win_words, removed)

    # face trajectory (clip-relative times)
    trajectory = None
    if face_track:
        try:
            tracker = FaceTracker()
            if tracker.backend == "none":
                raise RuntimeError("no_face_backend")
            if progress_cb:
                progress_cb(3, "Detecting faces")

            def _tp(pct, stage, stage_hint=None):
                if progress_cb and stage_hint:
                    progress_cb(3 + int(pct or 0) * 0.1
                                if pct else 3, stage_hint)

            samples = sample_face_centers(
                video_path, start, end, tracker, progress_cb=_tp)
            # make times clip-relative
            samples = [(t - start, cx) for t, cx in samples]
            trajectory = smooth_trajectory(samples, src_w, crop_w)
            if progress_cb:
                n_found = sum(1 for _, cx in samples if cx is not None)
                progress_cb(12, f"Face tracking: {n_found} face samples")
        except Exception as e:  # noqa: BLE001 - never break a render
            if "no_face_backend" in str(e):
                raise RuntimeError(
                    "Face tracking needs opencv-python - "
                    "reinstall with Install.bat")
            trajectory = None  # center crop fallback

    center_left = (src_w - crop_w) / 2.0

    # audio: kept segments only
    audio_tmp = tempfile.mktemp(suffix=".m4a", prefix="kept_")
    afilter, alabel = _kept_audio_filter(start, end, removed)
    acmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
            "-filter_complex", afilter, "-map", alabel,
            "-c:a", "aac", "-b:a", "128k", audio_tmp]
    r = subprocess.run(acmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(audio_tmp):
        raise RuntimeError("audio cut failed: " + r.stderr[-1200:])

    # captions on the filler-free timeline
    ass_fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix="clip_")
    os.close(ass_fd)
    build_ass(cap_words, 0, kept_duration(start, end, removed),
              style_name, moment.get("hook", ""), ass_path)

    # encode: pipe cropped frames -> ffmpeg + subtitles + audio
    enc = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{out_w}x{out_h}", "-framerate", f"{fps:.3f}",
           "-i", "pipe:0",
           "-i", audio_tmp,
           "-vf", f"subtitles={_ffmpeg_ass_path(ass_path)}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-shortest", out_path]
    proc = subprocess.Popen(enc, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)

    def in_keep(t):
        return any(s <= t < e for s, e in
                   [(a - start, b - start) for a, b in keep_segs])

    cap = cv2.VideoCapture(video_path)
    total_frames = int((end - start) * fps)
    start_frame = int(start * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    written, failed = 0, False
    try:
        for i in range(total_frames):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            t = i / fps
            if not in_keep(t):
                continue
            x_left = (interpolate_x(trajectory, t) if trajectory
                      else center_left)
            x_left = int(max(0, min(src_w - crop_w, x_left)))
            crop = frame[0:crop_h, x_left:x_left + crop_w]
            small = cv2.resize(crop, (out_w, out_h),
                               interpolation=cv2.INTER_AREA)
            try:
                proc.stdin.write(small.tobytes())
            except BrokenPipeError:
                failed = True
                break
            written += 1
            if progress_cb and i % 60 == 0:
                progress_cb(12 + int(i / max(total_frames, 1) * 85),
                            f"Rendering clip ({written} frames)")
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode("utf-8", "ignore")
        proc.wait()
    for p in (ass_path, audio_tmp):
        try:
            os.remove(p)
        except OSError:
            pass
    if failed or proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg encode failed: " + err[-2000:])
    if progress_cb:
        progress_cb(100, "Render complete")
    return out_path


# ---------------------------------------------------------------------------
# ffmpeg render
# ---------------------------------------------------------------------------

def render_clip(video_path, moment, words, style_name, out_path,
                progress_cb=None, face_track=False, remove_fillers=False,
                edited_words=None):
    """Cut + 9:16 reframe + burned captions + hook. Returns out_path.

    face_track=True -> per-frame face-following crop (OpenCV pipe).
    remove_fillers=True -> cuts filler words from audio/video/captions.
    edited_words -> caption word list from the caption editor (replaces
    the transcript words for this clip).
    The plain v1 path (direct ffmpeg center crop) is kept when neither
    upgrade is active.
    """
    if face_track or remove_fillers:
        use_words = (edited_words if edited_words is not None else words)
        return render_clip_tracked(
            video_path, moment, use_words, style_name, out_path,
            face_track=face_track, remove_fillers=remove_fillers,
            progress_cb=progress_cb)

    start, end = moment["start"], moment["end"]

    ass_fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix="clip_")
    os.close(ass_fd)
    cap_words = edited_words if edited_words is not None else words
    build_ass(cap_words, start, end, style_name, moment.get("hook", ""),
              ass_path)

    vf = (f"crop=trunc(ih*9/16/2)*2:trunc(ih/2)*2,"
          f"scale=1080:1920,"
          f"subtitles={_ffmpeg_ass_path(ass_path)}")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end),
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path,
    ]
    if progress_cb:
        progress_cb(5, "Rendering clip")
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True)
    try:
        os.remove(ass_path)
    except OSError:
        pass
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg failed: " + proc.stderr[-2000:])
    if progress_cb:
        progress_cb(100, "Render complete")
    return out_path
