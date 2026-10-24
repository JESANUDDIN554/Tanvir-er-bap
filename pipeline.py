"""Itraji Cliper - local video processing pipeline.

Stages: yt-dlp download -> faster-whisper transcription ->
Built-in AI (local model) viral-moment detection -> ffmpeg 9:16 render
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
# Windows: never let a child tool flash a console window. The app itself is
# windowed (PyInstaller console=False), but ffmpeg / ffprobe / yt-dlp are
# console programs - spawned from a windowed parent, Windows gives each one
# its own black terminal window unless CREATE_NO_WINDOW is passed. Wrap
# subprocess once, here, so EVERY call in the process (this module and
# app.py, which imports it) renders and downloads fully in the background.
# ---------------------------------------------------------------------------
if os.name == "nt":
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _sp_run = subprocess.run
    _sp_popen = subprocess.Popen

    def _force_no_window(kwargs):
        # OR the flag in - never just a default. A caller (or a library
        # like yt-dlp) that passes its OWN creationflags must not be able
        # to lose the no-window bit, or a console flashes on Windows.
        kwargs["creationflags"] = \
            int(kwargs.get("creationflags") or 0) | _CREATE_NO_WINDOW

    def _run_no_window(*args, **kwargs):
        _force_no_window(kwargs)
        return _sp_run(*args, **kwargs)

    class _PopenNoWindow(_sp_popen):
        def __init__(self, *args, **kwargs):
            _force_no_window(kwargs)
            super().__init__(*args, **kwargs)

    subprocess.run = _run_no_window
    subprocess.Popen = _PopenNoWindow


# ---------------------------------------------------------------------------
# Encode quality tiers (Step 2 "Render quality"). The user's standing
# preference is maximum picture quality over render speed, so "maximum"
# is the default everywhere.
# ---------------------------------------------------------------------------
_ENCODE_TIERS = {
    "maximum": ("slow", "16"),
    "balanced": ("medium", "20"),
    "fast": ("veryfast", "23"),
}

# ---------------------------------------------------------------------------
# yt-dlp download (with YouTube bot-check retry chain)
# ---------------------------------------------------------------------------

# Markers that identify YouTube's "Sign in to confirm you're not a bot" wall.
BOTCHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "http error 403",
)


def _is_botcheck_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in BOTCHECK_MARKERS)


def _download_once(url, out_dir, progress_cb, extra_opts, attempt_label):
    """Single yt-dlp attempt. Returns (video_path, title, duration_sec)."""
    import yt_dlp

    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, "source.%(ext)s")

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            progress_cb(min(99, pct), "Downloading video"
                        + (f" [{attempt_label}]" if attempt_label else ""))

    opts = {
        "format": "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[height<=2160][ext=mp4]/b[height<=2160]/b",
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 3,
    }
    opts.update(extra_opts or {})
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
    return path, title, duration


def download_video(url, out_dir, progress_cb=None, use_cookies=False):
    """Download best <=720p mp4. Returns (video_path, title, duration_sec).

    Retry chain for YouTube's bot check: normal -> android player client
    (usually bypasses the check) -> browser cookies (chrome -> edge).
    When use_cookies is set, cookie attempts go first (auto-detect
    chrome -> edge -> firefox; missing browser profiles are skipped).
    Raises RuntimeError("youtube_bot_check") when YouTube's bot wall
    blocks every attempt.
    """
    attempts = []  # (label, extra_opts)
    if use_cookies:
        for browser in ("chrome", "edge", "firefox"):
            attempts.append((f"cookies:{browser}",
                             {"cookiesfrombrowser": (browser,)}))
    attempts.append(("default", {}))
    attempts.append(("android", {"extractor_args":
                                 {"youtube": {"player_client": ["android"]}}}))
    if not use_cookies:
        for browser in ("chrome", "edge"):
            attempts.append((f"cookies:{browser}",
                             {"cookiesfrombrowser": (browser,)}))

    last_err, botcheck_seen = None, False
    for label, extra in attempts:
        if progress_cb:
            progress_cb(2, "Downloading video"
                        + (f" [{label}]" if label != "default" else ""))
        try:
            path, title, duration = _download_once(
                url, out_dir, progress_cb, extra, label)
            if progress_cb:
                progress_cb(100, "Download complete")
            return path, title, duration
        except Exception as e:  # noqa: BLE001 - part of the retry chain
            msg = str(e)
            if _is_botcheck_error(msg):
                botcheck_seen = True
            if label.startswith("cookies:") and "cookie" in msg.lower() and (
                    "could not" in msg.lower() or "not found" in msg.lower()
                    or "no such" in msg.lower()):
                continue  # no usable profile for this browser; try next
            last_err = e
            continue

    if botcheck_seen:
        raise RuntimeError("youtube_bot_check")
    if last_err is not None:
        raise RuntimeError(str(last_err)[:300])
    raise RuntimeError("download failed")


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


def _whisper_cache_dir(model_name):
    """The huggingface hub cache folder for the faster-whisper model,
    or None if it cannot be located."""
    repo = (model_name if "/" in model_name
            else "Systran/faster-whisper-" + model_name)
    folder = "models--" + repo.replace("/", "--")
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return os.path.join(HF_HUB_CACHE, folder)
    except Exception:  # noqa: BLE001
        home = os.path.expanduser("~")
        return os.path.join(home, ".cache", "huggingface", "hub", folder)


def _load_whisper_model(model_name, progress_cb=None):
    """Load the faster-whisper model, self-healing a broken download.

    On Windows a first download can end up half-written (interrupted,
    or snapshot entries that are symlinks the OS cannot resolve), and
    every later load then dies with "Unable to open file 'model.bin'".
    If the first load fails, wipe the cached model and download it
    fresh, exactly once, then load again.
    """
    # Real files instead of symlinks in the HF cache: symlink entries
    # are the classic Windows failure behind the model.bin error.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception:  # noqa: BLE001 - fall through to the repair path
        pass
    cache_dir = _whisper_cache_dir(model_name)
    if cache_dir and os.path.isdir(cache_dir):
        if progress_cb:
            progress_cb(6, "Repairing transcription model download")
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def transcribe(video_path, model_name="base", progress_cb=None,
               language="auto"):
    """Transcribe with word-level timestamps. Returns list of word dicts.

    language: faster-whisper language code ("en", "es", ...) or "auto"."""
    if progress_cb:
        progress_cb(5, "Loading transcription model")
    model = _load_whisper_model(model_name, progress_cb)
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
        lang = None if language in (None, "auto", "") else language
        segments, _info = model.transcribe(
            audio, word_timestamps=True, vad_filter=True, language=lang)
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
# Moment-detection prompt (shared by the Built-in AI detector below)
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = None  # @corepack (loaded at unlock)

# ---------------------------------------------------------------------------
# Core pack: the AI prompts, caption styles and hashtag sets live in
# corepack.enc, sealed with a key only a genuine license carries (see
# licensing.py). Until unlock, detection and rendering refuse honestly
# instead of running - patching the activation screen cannot help,
# because the content itself is not in the clear anywhere.
# ---------------------------------------------------------------------------

_COREPACK_LOADED = False

_LOCKED_MSG = ("This copy is locked - activate it with your license "
               "key first.")


def load_corepack(pack):
    global PROMPT_TEMPLATE, POSTING_PROMPT_TEMPLATE, STYLES
    global GENRE_HASHTAGS, _COREPACK_LOADED
    PROMPT_TEMPLATE = pack["prompt_template"]
    POSTING_PROMPT_TEMPLATE = pack["posting_prompt_template"]
    STYLES = pack["styles"]
    GENRE_HASHTAGS = pack["genre_hashtags"]
    _COREPACK_LOADED = True


def corepack_loaded():
    return _COREPACK_LOADED


# Built-in AI moment detection (local LLM via llama.cpp)
# ---------------------------------------------------------------------------
# Same contract as the moment detectors: same prompt, same JSON
# schema, same validation. The model runs on this machine instead -
# see local_ai.py. Long transcripts are split into time-ordered word
# chunks whose rendered transcript keeps the whole prompt < ~55k
# chars, detected per chunk, then merged + overlap-deduped.

_LOCAL_TRANSCRIPT_BUDGET = 48000  # rendered transcript chars per chunk


def _local_transcript_chunks(words, max_chars=_LOCAL_TRANSCRIPT_BUDGET):
    """Split words into consecutive spans whose transcript_text()
    rendering stays within max_chars. Splits only on the same 12-word
    line boundaries transcript_text() itself uses, so the [mm:ss]
    markers stay exact."""
    chunks, cur, cur_len = [], [], 0
    for i in range(0, len(words), 12):
        group = words[i:i + 12]
        start = group[0]["start"]
        line = (f"[{int(start // 60):02d}:{int(start % 60):02d}] "
                + " ".join(w["word"] for w in group))
        add = len(line) + 1
        if cur and cur_len + add > max_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.extend(group)
        cur_len += add
    if cur:
        chunks.append(cur)
    return chunks


def _parse_local_moments(data, clip_max_s):
    """Validate/normalize one reply's moments - same field rules as
    detect_moments(); virality capped at 99 like the offline path."""
    out = []
    for m in data.get("moments", []):
        try:
            start = float(m["start"])
            end = float(m["end"])
            if end <= start or start < 0:
                continue
            # clamp to clip-length bounds
            if end - start > clip_max_s:
                end = start + clip_max_s
            v = max(0, min(99, int(m.get("virality", 50))))

            def _sub(key):
                try:
                    return max(0, min(100, int(m.get(key, v))))
                except (TypeError, ValueError):
                    return v

            out.append({
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
    return out


def detect_moments_local(words, clip_min_s, clip_max_s,
                         max_clips=8, progress_cb=None):
    """Find viral moments with the built-in local AI.

    Mirrors detect_moments() inputs and output shape, but the model
    (Qwen2.5-14B via llama.cpp, see local_ai.py) runs on this machine -
    no key. Raises RuntimeError with a plain message when the model
    is not downloaded yet or the engine fails; never invents moments.
    """
    import local_ai

    if not PROMPT_TEMPLATE:
        raise RuntimeError(_LOCKED_MSG)
    if not words:
        return []
    if not local_ai.model_ready():
        raise RuntimeError(
            "Built-in AI model is not downloaded yet. In Step 2 pick "
            "'Built-in AI (no key)' and click 'Download Built-in AI' "
            "(one-time download, ~9 GB), then try again.")
    if progress_cb:
        progress_cb(2, "Starting Built-in AI")
    local_ai.ensure_server(progress_cb)

    chunks = _local_transcript_chunks(words)
    moments = []
    for i, cwords in enumerate(chunks):
        if progress_cb:
            progress_cb(int(10 + i / max(len(chunks), 1) * 85),
                        f"Built-in AI analyzing transcript "
                        f"({i + 1}/{len(chunks)})")
        prompt = PROMPT_TEMPLATE.format(
            min_s=clip_min_s, max_s=clip_max_s,
            max_clips=max_clips, transcript=transcript_text(cwords))
        reply = local_ai.chat(
            [{"role": "user", "content": prompt}], progress_cb)
        try:
            data = local_ai.extract_json(reply)
        except ValueError as e:
            raise RuntimeError(
                "Built-in AI returned an answer that could not be "
                f"read ({e}). Please run detection again.") from e
        moments.extend(_parse_local_moments(data, clip_max_s))

    # merge: best first, drop moments overlapping a picked one by
    # >30% of the shorter clip (same rule as the offline detector)
    moments.sort(key=lambda m: m["virality"], reverse=True)
    picked = []
    for m in moments:
        s, e = m["start"], m["end"]
        overlap = False
        for p in picked:
            inter = max(0.0, min(e, p["end"]) - max(s, p["start"]))
            if inter > 0.3 * min(e - s, p["end"] - p["start"]):
                overlap = True
                break
        if not overlap:
            picked.append(m)
        if len(picked) >= max_clips:
            break
    if progress_cb:
        progress_cb(100, f"Found {len(picked)} moments")
    return picked


# ---------------------------------------------------------------------------
# OFFLINE heuristic moment detection (no model needed)
# ---------------------------------------------------------------------------
# Finds energetic, dense, surprising segments of the transcript using only
# local signals. Scores are labeled heuristic - they find high-energy
# segments, not deep topic understanding. All titles/hooks are extracted
# verbatim from the transcript; nothing is ever invented.

OFFLINE_ENERGY_WORDS = {
    "wow", "insane", "crazy", "breaking", "amazing", "unbelievable",
    "incredible", "shocking", "shocked", "wild", "nuts", "huge",
    "massive", "suddenly", "instantly", "watch", "look", "listen",
    "never", "ever", "best", "worst", "secret", "exposed", "caught",
    "busted", "exploded", "explosion", "shots", "fired", "chase",
    "sirens", "arrested", "arrest", "gun", "police", "officer",
    "suspect", "haha", "lol", "hilarious", "terrifying", "scary",
    "dangerous", "stunning", "jaw-dropping",
}

_OFFLINE_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "to", "of", "in", "on", "at", "for", "with", "as",
    "by", "from", "about", "into", "over", "after", "it", "its",
    "this", "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "me", "him", "her", "us", "them", "my", "your", "his",
    "our", "their", "so", "if", "not", "no", "yes", "just", "very",
    "really", "have", "has", "had", "do", "does", "did", "done",
    "can", "will", "would", "could", "should", "there", "here",
    "what", "when", "where", "who", "how", "why", "get", "got",
    "go", "going", "come", "came", "said", "say", "like", "know",
    "think", "want", "see", "one", "two", "all", "up", "out", "too",
    "then", "than", "also", "well", "now", "oh", "uh", "um",
}


def _clean_word(w):
    return re.sub(r"[^a-z']", "", w.lower())


def _content_words(win_words):
    return {_clean_word(w["word"]) for w in win_words
            if len(_clean_word(w["word"])) > 2
            and _clean_word(w["word"]) not in _OFFLINE_STOPWORDS}


def _score_offline_window(win_words, prev_content, win_dur):
    """Score one candidate window. Returns (virality, hook, flow, value,
    reason_parts)."""
    n = len(win_words)
    density = n / max(win_dur, 0.1)  # words per second
    # ~1.5 wps is calm speech, ~2.8+ is rapid/excited
    density_score = min(100, max(0, (density - 1.2) / 2.4 * 100))

    punct = sum(w["word"].count("!") + w["word"].count("?")
                for w in win_words)
    punct_score = min(100, punct * 25)

    energy_hits = [w["word"].strip(",.!?;:") for w in win_words
                   if _clean_word(w["word"]) in OFFLINE_ENERGY_WORDS]
    energy_score = min(100, len(energy_hits) * 22)

    caps = sum(1 for w in win_words
               if len(w["word"]) > 2 and w["word"].isupper())
    caps_score = min(100, caps * 20)

    content = _content_words(win_words)
    if prev_content and content:
        union = content | prev_content
        jaccard = len(content & prev_content) / len(union) if union else 0
        novelty_score = (1 - jaccard) * 100
    else:
        novelty_score = 60.0  # neutral for the first window

    # penalties: long silences and filler-word density
    gaps = [b["start"] - a["end"]
            for a, b in zip(win_words, win_words[1:])]
    silence = sum(g for g in gaps if g > 1.5)
    silence_pen = min(40, silence * 6)

    filler_dur = sum(e - s for s, e in find_filler_ranges(win_words))
    filler_pen = min(25, filler_dur / max(win_dur, 0.1) * 120)

    virality = (0.30 * density_score + 0.22 * energy_score
                + 0.18 * punct_score + 0.15 * novelty_score
                + 0.15 * caps_score
                - 0.5 * silence_pen - 0.3 * filler_pen)
    virality = max(5, min(99, round(virality)))

    hook = round(0.5 * energy_score + 0.3 * punct_score
                 + 0.2 * caps_score)
    flow = round(max(0, 0.6 * density_score + 0.4 * novelty_score
                     - 0.4 * silence_pen))
    value = round(0.5 * energy_score + 0.5 * novelty_score)

    parts = [f"{density:.1f} words/sec",
             f"{len(energy_hits)} energy words"
             + (": " + ", ".join(sorted(set(energy_hits))[:4])
                if energy_hits else ""),
             f"{punct} exclamations/questions",
             f"{novelty_score:.0f}% topic novelty"]
    if silence > 1.5:
        parts.append(f"{silence:.1f}s silence (penalty)")
    if filler_dur > 1.0:
        parts.append(f"{filler_dur:.1f}s fillers (penalty)")
    return virality, hook, flow, value, "; ".join(parts)


def _extract_title_hook(win_words):
    """Extractive title + hook: verbatim text from the window only."""
    text = " ".join(w["word"] for w in win_words)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
             if s.strip()]
    punchy = next((s for s in sents
                   if s.endswith("!") or s.endswith("?")), None)
    title = punchy or (sents[0] if sents else "")
    twords = title.split()
    if len(twords) > 9:
        title = " ".join(twords[:9])
    title = title[:70].strip() or "Clip"
    hook = " ".join(text.split()[:8])[:60].strip()
    return title, hook


def detect_moments_offline(words, clip_min_s, clip_max_s, max_clips=8):
    """Heuristic moment detection from the local transcript. No model needed.

    Returns list of {start, end, title, hook, virality, hook_score,
    flow_score, value_score, reason, offline: True}. Titles and hooks are
    extracted verbatim from the transcript - never invented.
    """
    if not words or clip_max_s <= 0 or max_clips <= 0:
        return []
    dur = words[-1]["end"]
    # Candidate window lengths spanning the requested RANGE - the old
    # code used one fixed midpoint length, so every moment came out
    # the same size. Windows at min / mid / max lengths are scored
    # together and compete in the single NMS pass below, so picked
    # durations genuinely vary inside [clip_min_s, clip_max_s].
    raw_lengths = {float(clip_min_s), (clip_min_s + clip_max_s) / 2.0,
                   float(clip_max_s)}
    lengths = sorted(L for L in raw_lengths if 5.0 <= L <= dur)
    if not lengths:
        if dur < max(5.0, clip_min_s * 0.5):
            return []
        lengths = [dur]

    scored = []
    for win_len in lengths:
        step = max(3.0, win_len / 6.0)
        candidates = []
        t = 0.0
        while t + win_len <= dur + 0.001:
            candidates.append((t, t + win_len))
            t += step
        if dur > win_len and (not candidates
                              or candidates[-1][0] < dur - win_len - 0.5):
            candidates.append((max(0.0, dur - win_len), dur))
        prev_content = set()
        for ws, we in candidates:
            win_words = [w for w in words
                         if w["end"] > ws and w["start"] < we]
            if len(win_words) < 12:
                prev_content = _content_words(win_words)
                continue
            # trim leading/trailing silence inside the window
            s = max(ws, win_words[0]["start"])
            e = min(we, win_words[-1]["end"])
            if e - s < clip_min_s:
                # speech fills only part of the window: stretch back
                # toward the window bounds so the moment still lands
                # inside the requested range when the window allows it
                need = clip_min_s - (e - s)
                grow_l = min(need / 2.0, s - ws)
                s -= grow_l
                e = min(we, e + (need - grow_l))
            if e - s < 5:
                prev_content = _content_words(win_words)
                continue
            virality, hook_s, flow_s, value_s, parts = _score_offline_window(
                win_words, prev_content, e - s)
            prev_content = _content_words(win_words)
            scored.append((virality, s, e, win_words, hook_s, flow_s,
                           value_s, parts))

    # non-max suppression: keep best-first, drop heavy overlaps
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = []
    for item in scored:
        _, s, e, *_rest = item
        overlap = False
        for ps, pe in [(p[1], p[2]) for p in picked]:
            inter = max(0.0, min(e, pe) - max(s, ps))
            if inter > 0.3 * min(e - s, pe - ps):
                overlap = True
                break
        if not overlap:
            picked.append(item)
        if len(picked) >= max_clips:
            break

    moments = []
    for (virality, s, e, win_words, hook_s, flow_s, value_s,
            parts) in picked:
        title, hook = _extract_title_hook(win_words)
        moments.append({
            "start": round(s, 2),
            "end": round(e, 2),
            "title": title,
            "hook": hook,
            "virality": virality,
            "hook_score": hook_s,
            "flow_score": flow_s,
            "value_score": value_s,
            "reason": "Heuristic (offline): " + parts,
            "offline": True,
        })
    moments.sort(key=lambda m: m["virality"], reverse=True)
    return moments


# ---------------------------------------------------------------------------
# Heuristic speaker split (offline diarization-lite) + AI posting packages
# ---------------------------------------------------------------------------

def diarize_heuristic(words):
    """Split words into 2 speakers using local pause cues only.

    A gap > 1.0s between words starts a new turn, and turns alternate
    between Speaker 1 and Speaker 2. This is a heuristic for chaotic
    multi-speaker audio (e.g. bodycam) - labels are honest
    ("Speaker 1"/"Speaker 2"): no attempt is made to identify WHO is
    speaking (officer vs suspect), which needs real diarization.
    Returns a NEW word list with a "speaker" field (0/1) on each word.
    """
    out = []
    speaker = 0
    prev_end = None
    for w in words:
        nw = dict(w)
        if prev_end is not None and w["start"] - prev_end > 1.0:
            speaker = 1 - speaker  # new turn -> other speaker
        nw["speaker"] = speaker
        out.append(nw)
        prev_end = w["end"]
    return out


SPEAKER_LABELS = ("Speaker 1", "Speaker 2")

# Hashtag seeds per genre for posting packages (extractive fallback).
GENRE_HASHTAGS = {"": []}  # @corepack (loaded at unlock)
POSTING_PROMPT_TEMPLATE = None  # @corepack (loaded at unlock)


def _extractive_posting_package(words, moment, genre=""):
    """Posting package without any AI: hook line as title base, template
    description, genre + extracted-keyword hashtags. All text is verbatim
    from the transcript or fixed templates - nothing invented."""
    text = " ".join(w["word"] for w in words)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
             if len(s.strip().split()) >= 3]

    def _sent_score(s):
        toks = [_clean_word(t) for t in s.split()]
        energy = sum(1 for t in toks if t in OFFLINE_ENERGY_WORDS)
        bang = 2 if s.endswith("!") else 1 if s.endswith("?") else 0
        return energy * 3 + bang * 2 + min(len(toks), 20) * 0.1

    ranked = sorted(sents, key=_sent_score, reverse=True)

    def _trim(s, n=58):
        s = s.strip()
        return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

    # candidate titles: best verbatim sentences, then the moment title,
    # then the window opener - deduped AFTER trimming (what the user sees)
    cands = [_trim(s) for s in ranked[:3]]
    mt = (moment.get("title") or "").strip()
    if mt:
        cands.append(_trim(mt))
    opener = _trim(" ".join(text.split()[:10]))
    if opener:
        cands.append(opener)
    titles = []
    for c in cands:
        if c and c not in titles:
            titles.append(c)
        if len(titles) == 3:
            break
    while len(titles) < 3:
        base = titles[0] if titles else "Must-watch moment"
        titles.append(base if base.endswith("🔥") else base + " 🔥")
    titles = titles[:3]

    hook_line = ranked[0] if ranked else text[:80]
    genre_word = {"news": "bodycam", "comedy": "comedy",
                  "gaming": "gaming", "podcast": "podcast",
                  "business": "business", "fitness": "fitness"}.get(
                      genre, "viral")
    description = (
        f'"{_trim(hook_line, 120)}"\n\n'
        f"This {genre_word} moment had us speechless. "
        f"What would YOU have done in this situation? 👇")

    tags = list(GENRE_HASHTAGS.get(genre, GENRE_HASHTAGS[""]))
    seen_words = set()
    for w in words:
        cw = _clean_word(w["word"])
        if (cw in OFFLINE_ENERGY_WORDS and cw not in seen_words
                and len(cw) > 2):
            seen_words.add(cw)
            tags.append("#" + cw)
        if len(tags) >= 8:
            break
    # de-dupe, keep order, cap at 8
    hashtags = list(dict.fromkeys(tags))[:8]

    return {
        "titles": titles,
        "description": description,
        "hashtags": hashtags,
        "mode": "extractive",
        "note": ("Auto-generated on this PC (no AI key): titles are "
                 "verbatim lines from the clip."),
    }


def generate_posting_package(words, moment, genre=""):
    """Per-clip posting package: 3 titles, FB description, hashtags.

    Written by the Built-in AI when its model is downloaded ("ai"
    mode). Otherwise -> extractive fallback on this PC ("extractive"
    mode). If the AI call fails, falls back to extractive and says so
    honestly. "mode" always reports which produced the copy.
    """
    moment = moment or {}
    try:
        import local_ai
        ready = bool(POSTING_PROMPT_TEMPLATE) \
            and local_ai.model_ready()
    except Exception:  # noqa: BLE001 - local AI unavailable
        ready = False
    if ready:
        try:
            text = " ".join(w["word"] for w in words)[:6000]
            raw = local_ai.chat([{
                "role": "user",
                "content": POSTING_PROMPT_TEMPLATE.format(
                    transcript=text)}])
            data = local_ai.extract_json(raw)
            titles = [str(t)[:70] for t in data.get("titles", [])[:3]]
            desc = str(data.get("description", ""))[:600]
            tags = []
            for h in data.get("hashtags", [])[:8]:
                h = str(h).strip()
                if not h.startswith("#"):
                    h = "#" + h
                h = re.sub(r"[^#a-zA-Z0-9_]", "", h)
                if len(h) > 1:
                    tags.append(h.lower())
            if titles and desc:
                return {
                    "titles": (titles + ["Clip"] * 3)[:3],
                    "description": desc,
                    "hashtags": tags or GENRE_HASHTAGS.get(
                        genre, GENRE_HASHTAGS[""])[:8],
                    "mode": "ai",
                    "note": "Written by the Built-in AI from the clip "
                            "transcript.",
                }
            raise RuntimeError("the model returned no usable copy")
        except Exception as e:  # noqa: BLE001 - honest fallback
            fb = _extractive_posting_package(words, moment, genre)
            fb["note"] = ("Built-in AI request failed (%s) - fell back "
                          "to the on-PC extractive version."
                          % str(e)[:80])
            return fb
    return _extractive_posting_package(words, moment, genre)


# ---------------------------------------------------------------------------
# Studio tools: Title & Hook Generator, AI Scriptwriter, Video Enhancer.
# The prompts are plain templates in THIS file (deliberately NOT in the
# encrypted corepack): they are product copy, not secrets, and keeping
# them here means Studio features never need a corepack rebuild.
# ---------------------------------------------------------------------------

TITLES_PROMPT_TEMPLATE = """You are an expert short-form video title \
writer for Facebook Reels / TikTok / YouTube Shorts.

Source material:
{source}

Write 10 scroll-stopping titles and 5 opening hooks for a clip from \
this material. Rules:
- Titles: max 60 characters, punchy, curiosity-driven, no clickbait lies.
- Hooks: the first spoken line / on-screen text, max 12 words each.
- Score every item 0-100 for viral potential (honest, spread the scores).
- Reply with ONLY a JSON object, no prose:
{{"titles": [{{"text": "...", "score": 87}}], \
"hooks": [{{"text": "...", "score": 82}}]}}"""

SCRIPT_PROMPT_TEMPLATE = """You are a short-form video scriptwriter.

Topic: {topic}
Tone: {tone}
Target length: about {seconds} seconds spoken (~{nwords} words total).

Write the script with exactly these three section headers on their \
own lines: HOOK, BODY, CTA.
- HOOK: the first 3 seconds - one or two lines that stop the scroll.
- BODY: the main content, spoken naturally, short sentences.
- CTA: one line telling the viewer what to do next.
Reply with ONLY the script text (with the three headers), no notes."""


def _words_text(words, cap=6000):
    return " ".join(w["word"] for w in words or [])[:cap]


def _basic_titles(words):
    """Extractive titles/hooks straight from the transcript (no AI).

    Every string is verbatim transcript text; scores are a simple
    energy heuristic, normalized by rank. Labeled "basic" by callers -
    never presented as AI work."""
    text = _words_text(words, cap=20000)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
             if len(s.strip().split()) >= 3]

    def _score(s):
        toks = [_clean_word(t) for t in s.split()]
        energy = sum(1 for t in toks if t in OFFLINE_ENERGY_WORDS)
        bang = 2 if s.endswith("!") else 1 if s.endswith("?") else 0
        return energy * 3 + bang * 2 + min(len(toks), 20) * 0.1

    def _trim(s, n=58):
        s = s.strip()
        return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

    ranked = sorted(sents, key=_score, reverse=True)
    titles, seen = [], set()
    for s in ranked:
        t = _trim(s)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            titles.append(t)
        if len(titles) == 10:
            break
    if len(titles) < 10:
        # Short transcripts have few full sentences: pad with more
        # verbatim fragments (comma clauses, sentence openers, then
        # plain word windows) - still 100% transcript text.
        frags = []
        for s in ranked:
            frags.extend(p.strip() for p in s.split(",")
                         if len(p.strip().split()) >= 4)
            frags.append(" ".join(s.split()[:7]))
        words_all = text.split()
        for off in range(0, max(0, len(words_all) - 5), 5):
            frags.append(" ".join(words_all[off:off + 9]))
        for fr in frags:
            t = _trim(fr)
            if t and len(t.split()) >= 3 and t.lower() not in seen:
                seen.add(t.lower())
                titles.append(t)
            if len(titles) == 10:
                break
    hooks = []
    for s in ranked:
        if s.endswith(("!", "?")):
            hooks.append(_trim(" ".join(s.split()[:12]), 70))
        if len(hooks) == 5:
            break
    if len(hooks) < 5 and ranked:
        opener = _trim(" ".join(text.split()[:10]), 60)
        if opener and opener not in hooks:
            hooks.insert(0, opener)

    def _pack(items, top):
        return [{"text": t, "score": max(35, top - 6 * i)}
                for i, t in enumerate(items)]

    return _pack(titles, 88), _pack(hooks, 84)


def generate_titles(words=None, topic="", genre=""):
    """10 titles + 5 hooks, each with a 0-100 score.

    Written by the Built-in AI when its model is downloaded ("ai"
    mode). Without the model, a transcript still yields honest
    extractive titles ("basic" mode). With neither a model nor a
    transcript there is nothing truthful to generate from, so the
    result is mode "needs_model" and the UI offers the download.
    """
    words = words or []
    topic = (topic or "").strip()
    try:
        import local_ai
        ready = local_ai.model_ready()
    except Exception:  # noqa: BLE001 - local AI unavailable
        ready = False
    if ready:
        try:
            if words:
                source = ("Transcript:\n" + _words_text(words))
                if topic:
                    source += "\n\nCreator's angle: " + topic[:300]
            else:
                source = "Topic: " + topic[:500]
            raw = local_ai.chat([{
                "role": "user",
                "content": TITLES_PROMPT_TEMPLATE.format(source=source)}])
            data = local_ai.extract_json(raw)

            def _clean(items, limit):
                out = []
                for it in items or []:
                    if isinstance(it, dict):
                        t = str(it.get("text", "")).strip()
                        try:
                            sc = int(float(it.get("score", 50)))
                        except (TypeError, ValueError):
                            sc = 50
                    else:
                        t, sc = str(it).strip(), 50
                    if t:
                        out.append({"text": t[:90],
                                    "score": max(0, min(100, sc))})
                    if len(out) == limit:
                        break
                return out

            titles = _clean(data.get("titles"), 10)
            hooks = _clean(data.get("hooks"), 5)
            if titles:
                return {"titles": titles, "hooks": hooks, "mode": "ai",
                        "note": "Written by the Built-in AI."}
            raise RuntimeError("the model returned no usable titles")
        except Exception as e:  # noqa: BLE001 - honest fallback
            if words:
                t, h = _basic_titles(words)
                return {"titles": t, "hooks": h, "mode": "basic",
                        "note": ("Built-in AI request failed (%s) - "
                                 "basic titles taken straight from the "
                                 "transcript instead." % str(e)[:80])}
            return {"titles": [], "hooks": [], "mode": "needs_model",
                    "note": ("The Built-in AI didn't answer (%s). Try "
                             "again in a moment." % str(e)[:80])}
    if words:
        t, h = _basic_titles(words)
        return {"titles": t, "hooks": h, "mode": "basic",
                "note": ("Basic mode (Built-in AI not downloaded): "
                         "titles are verbatim lines from the "
                         "transcript, scored by a simple heuristic.")}
    return {"titles": [], "hooks": [], "mode": "needs_model",
            "note": ("Download the Built-in AI (~9 GB, one time) to "
                     "write titles from a topic - or pick a Library "
                     "project to get basic titles from its transcript.")}


def generate_script(topic, tone="Energetic", seconds=60):
    """A Hook / Body / CTA short-form script from the Built-in AI.

    There is deliberately NO extractive fallback: a script invented
    by templates would be fake AI work. Without the model the result
    is an honest needs_model state and the UI offers the download."""
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "empty_topic",
                "message": "Describe what the video is about first."}
    try:
        import local_ai
        ready = local_ai.model_ready()
    except Exception:  # noqa: BLE001
        ready = False
    if not ready:
        return {"ok": False, "error": "needs_model",
                "message": ("The AI Scriptwriter needs the Built-in "
                            "AI model. Download it once (~9 GB) and "
                            "it works fully offline, forever.")}
    try:
        seconds = max(15, min(300, int(seconds)))
    except (TypeError, ValueError):
        seconds = 60
    try:
        raw = local_ai.chat([{
            "role": "user",
            "content": SCRIPT_PROMPT_TEMPLATE.format(
                topic=topic[:600], tone=(tone or "Energetic")[:40],
                seconds=seconds, nwords=int(seconds * 2.5))}])
    except Exception as e:  # noqa: BLE001 - surfaced, never faked
        return {"ok": False, "error": "ai_failed",
                "message": "Built-in AI request failed: " + str(e)[:200]}
    script = (raw or "").strip()
    if len(script) < 40:
        return {"ok": False, "error": "ai_failed",
                "message": "The Built-in AI returned an empty script - "
                           "try again."}
    return {"ok": True, "script": script, "mode": "ai"}


def enhance_video(video_path, out_path, progress_cb=None,
                  quality_tier="maximum"):
    """Basic enhancer: upscale to a 1080 short side (Lanczos) when the
    source is smaller, then an unsharp pass. Honestly a sharpen +
    upscale - NOT an AI upscaler. Returns out_path."""
    if not os.path.isfile(video_path):
        raise RuntimeError("Video file not found: " + str(video_path))
    W, H, _fps = probe_video(video_path)
    if not W or not H:
        raise RuntimeError("Could not read the video's dimensions.")
    duration = probe_duration(video_path) or 0.0
    short = min(W, H)
    if short < 1080:
        f = 1080.0 / short
        tw = int(round(W * f / 2.0)) * 2
        th = int(round(H * f / 2.0)) * 2
        vf = (f"scale={tw}:{th}:flags=lanczos,"
              "unsharp=5:5:0.8:5:5:0.0")
    else:
        vf = "unsharp=5:5:0.8:5:5:0.0"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)),
                exist_ok=True)
    cmd = (["ffmpeg", "-y", "-i", video_path, "-vf", vf,
            "-map", "0:v:0", "-map", "0:a?"]
           + _encode_args(quality_tier)
           + ["-c:a", "aac", "-b:a", "192k", out_path])
    _run_ffmpeg_progress(cmd, duration, progress_cb, 3, 97,
                         "Enhancing (upscale + sharpen)")
    if not os.path.isfile(out_path) \
            or os.path.getsize(out_path) < 1024:
        raise RuntimeError("Enhance produced no output file.")
    if progress_cb:
        progress_cb(100, "Done")
    return out_path


# ---------------------------------------------------------------------------
# ASS caption builder (karaoke word highlight + hook overlay)
# ---------------------------------------------------------------------------

STYLES = {}  # @corepack (loaded at unlock)


CUSTOM_STYLES = {}


def set_custom_styles(styles):
    """Register user-saved styles: [{name, primary, secondary, outline,
    bold}]. Colours are ASS &HAABBGGRR strings."""
    CUSTOM_STYLES.clear()
    for s in styles or []:
        key = str(s.get("name", "")).strip().lower().replace(" ", "-")
        if not key:
            continue
        CUSTOM_STYLES[key] = {
            "label": str(s.get("name", "Custom"))[:40],
            "primary": str(s.get("primary", "&H00FFFFFF")),
            "secondary": str(s.get("secondary", "&H0000FFFF")),
            "outline": str(s.get("outline", "&H00000000")),
            "bold": int(s.get("bold", 1)),
        }


def _esc(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _get_style(name):
    return (CUSTOM_STYLES.get(name)
            or STYLES.get(name, STYLES["bold-pop"]))


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


# ---------------------------------------------------------------------------
# Caption Studio parameters: captions generalized from named styles to
# a parameter bundle. The named Step-2 styles stay as-is (legacy path);
# the studio's presets below are bundles of these params, and a render
# carrying explicit caption_params renders through them instead.
# ---------------------------------------------------------------------------

FONTS_DIR = ""  # app.py points this at the app's fonts/ folder

CAPTION_FONT_FILES = {
    "anton": "Anton-Regular.ttf",
    "bebas-neue": "BebasNeue-Regular.ttf",
    "montserrat": "Montserrat-ExtraBold.ttf",
    "poppins": "Poppins-Bold.ttf",
    "archivo-black": "ArchivoBlack-Regular.ttf",
}
CAPTION_FONTS = [
    {"id": "default", "name": "Default (Arial)", "font_name": "Arial"},
    {"id": "anton", "name": "Anton", "font_name": "Anton"},
    {"id": "bebas-neue", "name": "Bebas Neue", "font_name": "Bebas Neue"},
    {"id": "montserrat", "name": "Montserrat", "font_name": "Montserrat"},
    {"id": "poppins", "name": "Poppins", "font_name": "Poppins"},
    {"id": "archivo-black", "name": "Archivo Black",
     "font_name": "Archivo Black"},
]
CAPTION_SIZES = {"xs": 0.7, "s": 0.85, "m": 1.0, "l": 1.2,
                 "xl": 1.45, "xxl": 1.7}


def _preset(**kw):
    base = {"uppercase": True, "dim_inactive": 55, "position": "bottom",
            "size": "l", "one_word": False, "animation": "none",
            "font": "default", "text_color": "#FFFFFF",
            "highlight_color": "#C8FF2E", "border": 3,
            "outline_color": "#000000", "bold": 1}
    base.update(kw)
    return base


CAPTION_PRESETS = {
    "default": _preset(),
    "one-word": _preset(one_word=True, size="xl", animation="pop"),
    "clean": _preset(uppercase=False, dim_inactive=0, size="m", border=2,
                     highlight_color="#FFFFFF"),
    "pill": _preset(animation="box", size="m", dim_inactive=35),
    "lime-box": _preset(animation="box"),
    "hormozi": _preset(font="montserrat", size="xl", border=4,
                       highlight_color="#FFE135", dim_inactive=60),
    "karaoke": _preset(uppercase=False, dim_inactive=45, size="m"),
    "beast": _preset(font="anton", size="xxl", border=5,
                     highlight_color="#FF3131"),
    "neon": _preset(animation="glow", border=2, text_color="#E8FFFF",
                    highlight_color="#00E5FF"),
    "gold-glow": _preset(animation="glow", text_color="#FFF6D8",
                         highlight_color="#FFC400"),
    "cyber": _preset(font="bebas-neue", size="xl", dim_inactive=65,
                     highlight_color="#00FF9D"),
    "boxed": _preset(animation="box", size="m", dim_inactive=30,
                     highlight_color="#FFFFFF"),
    "minimal": _preset(uppercase=False, dim_inactive=0, size="s",
                       border=1, highlight_color="#FFFFFF"),
    "classic": _preset(uppercase=False, dim_inactive=20, size="m",
                       text_color="#FFFF00", highlight_color="#FFFF00"),
    "royal-blue": _preset(animation="pop",
                          highlight_color="#4D7CFE"),
    "mint": _preset(animation="glow", highlight_color="#3DFFB0"),
    "fire": _preset(font="archivo-black", size="xl", animation="pop",
                    border=4, highlight_color="#FF6A00"),
    "bubble": _preset(font="poppins", animation="pop", dim_inactive=40,
                      highlight_color="#FF8FDC"),
    "ghost": _preset(dim_inactive=75, border=1, size="m",
                     text_color="#EDEDED", highlight_color="#FFFFFF"),
    "shorts-pop": _preset(font="poppins", animation="pop", size="xl",
                          dim_inactive=50),
}

_STYLE_TO_PRESET = {"bold-pop": "default", "lime-punch": "lime-box",
                    "clean": "clean", "neon-glow": "neon",
                    "hollow": "ghost", "boxed": "boxed"}


def hex_to_ass(hex_color, alpha="00"):
    """'#RRGGBB' -> ASS '&HAABBGGRR'."""
    h = str(hex_color or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        h = "FFFFFF"
    return ("&H" + alpha + h[4:6] + h[2:4] + h[0:2]).upper()


def _dim_ass(ass_color, dim_pct):
    """Dim an ASS color toward black by dim_pct (0-100)."""
    f = 1.0 - 0.8 * (max(0, min(100, dim_pct)) / 100.0)
    bb = int(int(ass_color[4:6], 16) * f)
    gg = int(int(ass_color[6:8], 16) * f)
    rr = int(int(ass_color[8:10], 16) * f)
    return "&H00%02X%02X%02X" % (bb, gg, rr)


def _font_available(font_id):
    if font_id == "default":
        return True
    fname = CAPTION_FONT_FILES.get(font_id)
    return bool(fname and FONTS_DIR
                and os.path.isfile(os.path.join(FONTS_DIR, fname)))


def _font_name(font_id):
    """ASS FontName for a font id; falls back to Arial when the named
    font's file isn't bundled (never fail a render over a font)."""
    if font_id != "default" and _font_available(font_id):
        for f in CAPTION_FONTS:
            if f["id"] == font_id:
                return f["font_name"]
    return "Arial"


def _fontsdir_opt():
    """ffmpeg subtitles-filter fontsdir option for the bundled fonts,
    escaped for the filtergraph's double parse (same scheme as
    _ffmpeg_ass_path). Empty when no fonts folder is wired."""
    if not FONTS_DIR or not os.path.isdir(FONTS_DIR):
        return ""
    p = FONTS_DIR.replace("\\", "/")
    for ch in (":", "'", ",", "[", "]", ";"):
        p = p.replace(ch, "\\\\\\" + ch)
    return ":fontsdir=" + p


def normalize_caption_params(params=None, style_name=None):
    """Effective caption params: a preset bundle (chosen by
    params['preset'] or seeded from the legacy style name) overlaid
    with the explicit params, sanitized. Returns None when params is
    None - the caller then uses the legacy named-style path."""
    if params is None:
        return None
    params = dict(params or {})
    preset_id = params.get("preset") or _STYLE_TO_PRESET.get(
        style_name or "", "default")
    if preset_id not in CAPTION_PRESETS:
        preset_id = "default"
    eff = dict(CAPTION_PRESETS[preset_id])
    eff["preset"] = preset_id
    for k, v in params.items():
        if v is not None and k in eff:
            eff[k] = v
    try:
        eff["dim_inactive"] = max(0, min(100, int(eff["dim_inactive"])))
    except (TypeError, ValueError):
        eff["dim_inactive"] = 0
    try:
        eff["border"] = max(0, min(8, int(eff["border"])))
    except (TypeError, ValueError):
        eff["border"] = 3
    if eff["size"] not in CAPTION_SIZES:
        eff["size"] = "m"
    if eff["position"] not in ("top", "middle", "bottom"):
        eff["position"] = "bottom"
    if eff["animation"] not in ("pop", "glow", "box", "none"):
        eff["animation"] = "none"
    if eff["font"] != "default" and eff["font"] not in CAPTION_FONT_FILES:
        eff["font"] = "default"
    eff["uppercase"] = bool(eff["uppercase"])
    eff["one_word"] = bool(eff["one_word"])
    eff["bold"] = 1 if eff.get("bold", 1) else 0
    for ck, dflt in (("text_color", "#FFFFFF"),
                     ("highlight_color", "#C8FF2E"),
                     ("outline_color", "#000000")):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(eff.get(ck, ""))):
            eff[ck] = dflt
    return eff


def build_ass(words, start_s, end_s, style_name, hook_text, out_path,
              res=(1080, 1920), emojis=False, highlight=False,
              speaker_colors=None, hook_overlay=True, cta_text=None,
              safe_zone=False, caption_params=None):
    """Write an ASS subtitle file for one clip window.

    res: (width, height) of the rendered clip - fonts and positions scale.
    emojis: append a relevant emoji after high-energy words.
    highlight: paint keyword/capitalized words in lime green.
    speaker_colors: (color1, color2) ASS colors - when set, each word is
        painted by its "speaker" field (Speaker 1/2), overriding the
        highlight color. Words without a speaker field count as Speaker 1.
    hook_overlay: show the hook text as an animated overlay in the first
        ~2.5s (top safe zone). False suppresses it entirely.
    cta_text: when set, shown as a faded overlay in the last 2 seconds
        (top safe zone).
    safe_zone: nudge captions/hook inside Reels UI safe areas (captions
        above the bottom caption zone, hook below the top bar).
    """
    if not STYLES:
        raise RuntimeError(_LOCKED_MSG)
    st = _get_style(style_name)
    # "none": no word captions at all. The hook and CTA overlays above
    # are separate features with their own toggles and still apply.
    no_captions = (style_name == "none")
    # Caption Studio: explicit params take over caption rendering
    # entirely (including reviving captions on a "none"-styled clip -
    # applying studio params IS the user's intent to have captions).
    eff = normalize_caption_params(caption_params, style_name)
    if eff is not None:
        no_captions = False
    W, H = int(res[0]), int(res[1])
    sx, sy = W / 1080.0, H / 1920.0
    cap_font = max(24, int(60 * sx))
    hook_font = max(22, int(54 * sx))
    # safe-zone: captions clear the Reels bottom UI (~20% of height),
    # hook clears the top bar
    cap_margin_v = int((360 if safe_zone else 240) * sy)
    hook_margin_v = int((230 if safe_zone else 150) * sy)
    win = [w for w in words if w["end"] > start_s and w["start"] < end_s]
    win.sort(key=lambda w: w["start"])

    # sanitize speaker colors: must be 2 ASS &HAABBGGRR colors
    spk_cols = None
    if speaker_colors:
        try:
            c1, c2 = list(speaker_colors)[:2]

            def _ok(c):
                return isinstance(c, str) and re.fullmatch(
                    r"&H[0-9A-Fa-f]{8}", c) is not None
            if _ok(c1) and _ok(c2):
                spk_cols = (c1.upper(), c2.upper())
        except (TypeError, ValueError):
            spk_cols = None

    def ts(s):
        s = max(0, s)
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}.{int((s % 1) * 100):02d}"

    body = []
    box_events = []
    use_box = bool(st.get("box"))
    # hook overlay at the top, first ~2.5s (animated fade)
    if hook_text and hook_overlay:
        hook_end = min(start_s + 2.5, end_s)
        layer = 1 if use_box else 0
        body.append(
            f"Dialogue: {layer},{ts(start_s)},{ts(hook_end)},Hook,,0,0,0,,"
            f"{{\\fad(250,250)}}{_esc(hook_text.upper())}")
    # CTA overlay: last 2 seconds, top safe zone, faded
    if cta_text:
        cta_start = max(start_s, end_s - 2.0)
        if cta_start < end_s - 0.2:
            layer = 1 if use_box else 0
            body.append(
                f"Dialogue: {layer},{ts(cta_start)},{ts(end_s)},"
                f"CTA,,0,0,0,,{{\\fad(300,300)}}{_esc(cta_text)}")

    if eff is not None:
        # ---- Caption Studio params mode --------------------------
        p_font = _font_name(eff["font"])
        p_size = max(20, int(cap_font * CAPTION_SIZES[eff["size"]]))
        p_prim = hex_to_ass(eff["text_color"])
        p_hl = hex_to_ass(eff["highlight_color"])
        p_dim = _dim_ass(p_prim, eff["dim_inactive"])
        p_out = hex_to_ass(eff["outline_color"])
        if eff["position"] == "top":
            p_align, p_marginv = 8, int(150 * sy)
        elif eff["position"] == "middle":
            p_align, p_marginv = 5, 0
        else:
            p_align, p_marginv = 2, cap_margin_v
        p_shadow = 3 if eff["animation"] == "glow" else 1
        p_box = eff["animation"] == "box"

        def _p_word(w):
            t = w["word"].upper() if eff["uppercase"] else w["word"]
            t = _esc(t)
            if emojis:
                em = EMOJI_MAP.get(_clean_word(w["word"]))
                if em:
                    t += em
            return t

        def _p_anim_tags():
            if eff["animation"] == "pop":
                return ("{\\fscx118\\fscy118"
                        "\\t(0,140,\\fscx100\\fscy100)}")
            if eff["animation"] == "glow":
                return "{\\shad5\\4c" + p_hl + "&}"
            return ""

        def _p_box_event(line_words, active_idx, ev_start, ev_end):
            u = p_size / 60.0

            def ew(t):
                return sum(20 if c == " " else 24
                           if c in "ilj.,!;:'\"" else 36
                           for c in t) * u
            texts = [_p_word(w) for w in line_words]
            line_w = ew(" ".join(texts)) + 56 * u
            off = 0.0
            for jx in range(active_idx):
                off += ew(texts[jx]) + 20 * u
            word_w = ew(texts[active_idx])
            cx = W / 2.0
            x0 = max(4.0, cx - line_w / 2 + off - 14 * u)
            x1 = min(W - 4.0, x0 + word_w + 28 * u)
            bh = int(p_size * 1.7)
            if eff["position"] == "top":
                y0 = max(0, p_marginv - int(p_size * 0.2))
                y1 = y0 + bh
            elif eff["position"] == "middle":
                y0 = int(H / 2 - bh / 2 + p_size * 0.35)
                y1 = y0 + bh
            else:
                y1 = H - p_marginv - int(p_size * 0.1)
                y0 = y1 - bh
            return (
                f"Dialogue: 0,{ts(ev_start)},{ts(ev_end)},Box,,0,0,0,,"
                f"{{\\an7\\pos(0,0)\\p1}}"
                f"m {x0:.0f} {y0:.0f} l {x1:.0f} {y0:.0f} "
                f"l {x1:.0f} {y1:.0f} l {x0:.0f} {y1:.0f}{{\\p0}}")

        if eff["one_word"]:
            for w in win:
                body.append(
                    f"Dialogue: {1 if p_box else 0},"
                    f"{ts(w['start'])},{ts(w['end'])},Caption,,0,0,0,,"
                    f"{{\\1c{p_prim}&}}{_p_anim_tags()}{_p_word(w)}")
                if p_box:
                    box_events.append(
                        _p_box_event([w], 0, w["start"], w["end"]))
        else:
            for line in _group_lines(win):
                for k, w in enumerate(line):
                    ev_end = (line[k + 1]["start"] if k + 1 < len(line)
                              else line[-1]["end"])
                    parts = []
                    for jx, wj in enumerate(line):
                        if jx == k:
                            parts.append(
                                "{\\1c" + p_hl + "&}" + _p_anim_tags()
                                + _p_word(wj)
                                + ("{\\fscx100\\fscy100}"
                                   if eff["animation"] == "pop" else ""))
                        else:
                            parts.append(
                                "{\\1c" + p_dim + "&}" + _p_word(wj))
                    body.append(
                        f"Dialogue: {1 if p_box else 0},"
                        f"{ts(w['start'])},{ts(ev_end)},Caption,,0,0,0,,"
                        + " ".join(parts))
                    if p_box:
                        box_events.append(
                            _p_box_event(line, k, w["start"], ev_end))
        cap_style_line = (
            f"Style: Caption,{p_font},{p_size},{p_prim},{p_hl},{p_out},"
            f"&H99000000,{eff['bold']},0,0,0,100,100,0,0,1,"
            f"{eff['border']},{p_shadow},{p_align},40,40,{p_marginv},1")
        _box_fill = hex_to_ass(eff["highlight_color"], "40")
        box_style_line = (
            f"Style: Box,Arial,10,{_box_fill},{_box_fill},"
            "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")
    else:
        prim_col = st["primary"]  # ASS &HAABBGGRR, e.g. &H0000FFFF
        for line in ([] if no_captions else _group_lines(win)):
            parts = []
            for w in line:
                cs = max(1, int((w["end"] - w["start"]) * 100))
                txt = _esc(w["word"])
                if emojis:
                    em = EMOJI_MAP.get(_clean_word(w["word"]))
                    if em:
                        txt += em
                if spk_cols is not None:
                    # speaker colors win over highlight - that IS the feature
                    col = spk_cols[w.get("speaker", 0) % 2]
                elif highlight and _is_highlight_word(w["word"]):
                    col = "&H002EFFC8"
                else:
                    col = prim_col
                parts.append(f"{{\\k{cs}\\1c{col}&}}{txt}")
            layer = 1 if use_box else 0
            body.append(
                f"Dialogue: {layer},{ts(line[0]['start'])},{ts(line[-1]['end'])},"
                f"Caption,,0,0,0,,{' '.join(parts)}")
            if use_box:
                # opaque rounded-ish box behind the line, drawn with vector
                # graphics at absolute script coords (top-left origin)
                text = " ".join(w["word"] for w in line)
                est_w = (sum(20 if c == " " else
                             24 if c in "ilj.,!;:'\"" else 36 for c in text)
                         + 56) * sx
                cx = W / 2.0
                x0, x1 = cx - est_w / 2, cx + est_w / 2
                y0, y1 = int(1592 * sy), int(1700 * sy)
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
        cap_style_line = (
            f"Style: Caption,Arial,{cap_font},{st['primary']},"
            f"{st['secondary']},{st['outline']},&H99000000,{st['bold']},"
            f"0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_w},"
            f"2,40,40,{cap_margin_v},1")
        box_style_line = (
            "Style: Box,Arial,10,&HCC000000,&HCC000000,&H00000000,"
            "&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{cap_style_line}
Style: Hook,Arial,{hook_font},&H0000FFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,3,2,0,8,40,40,{hook_margin_v},1
Style: CTA,Arial,{hook_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,3,2,0,8,40,40,{hook_margin_v},1
{box_style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(box_events + body) + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass)
    return out_path


def _ffmpeg_ass_path(path):
    # Escape for the subtitles filter. ffmpeg parses the -vf string TWICE:
    # first the filtergraph pass, then the subtitles filter's own option
    # parser, which splits options on ':'. A single '\:' is eaten by the
    # first pass, so on Windows the drive colon (C:) splits the path and
    # the remainder lands in the next option slot (original_size) - the
    # "Unable to parse original_size" render failure. Escape each special
    # char for BOTH passes so the option parser still sees '\:' etc.
    p = path.replace("\\", "/")
    for ch in (":", "'", ",", "[", "]", ";"):
        p = p.replace(ch, "\\\\\\" + ch)
    return p


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
        nw = dict(w)  # keep extra keys (e.g. "speaker") through the remap
        nw["start"] = w["start"] - shift
        nw["end"] = w["end"] - shift
        out.append(nw)
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
# v3: ratios, languages, genres, caption magic, silence detect, censor,
# VTT + XML timeline export, SRT parsing, keyword moment search
# ---------------------------------------------------------------------------

RATIOS = {
    "9:16": {"rw": 9, "rh": 16,
             "1080p": (1080, 1920), "720p": (720, 1280)},
    "16:9": {"rw": 16, "rh": 9,
             "1080p": (1920, 1080), "720p": (1280, 720)},
    "1:1": {"rw": 1, "rh": 1,
            "1080p": (1080, 1080), "720p": (720, 720)},
    "4:5": {"rw": 4, "rh": 5,
            "1080p": (1080, 1350), "720p": (720, 900)},
}


def ratio_dims(ratio, quality):
    """(width, height) for a ratio + quality choice."""
    r = RATIOS.get(ratio, RATIOS["9:16"])
    return r.get(quality, r["1080p"])


def crop_filter(ratio):
    """ffmpeg crop expression that cuts the source to the target ratio."""
    r = RATIOS.get(ratio, RATIOS["9:16"])
    rw, rh = r["rw"], r["rh"]
    if rw == 16 and rh == 9:
        return "crop=trunc(iw/2)*2:trunc(iw*9/16/2)*2"
    if rw == 1 and rh == 1:
        return r"crop=trunc(min(iw\,ih)/2)*2:trunc(min(iw\,ih)/2)*2"
    return f"crop=trunc(ih*{rw}/{rh}/2)*2:trunc(ih/2)*2"


# 25 transcription languages (faster-whisper codes)
LANGUAGES = [
    ("auto", "Auto-detect"), ("en", "English"), ("es", "Spanish"),
    ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("pt", "Portuguese"), ("ru", "Russian"), ("ja", "Japanese"),
    ("ko", "Korean"), ("zh", "Chinese"), ("hi", "Hindi"),
    ("bn", "Bengali"), ("ar", "Arabic"), ("tr", "Turkish"),
    ("nl", "Dutch"), ("pl", "Polish"), ("sv", "Swedish"),
    ("no", "Norwegian"), ("da", "Danish"), ("fi", "Finnish"),
    ("el", "Greek"), ("he", "Hebrew"), ("id", "Indonesian"),
    ("th", "Thai"), ("vi", "Vietnamese"),
]

# Genre templates: preset defaults for detection + caption options.
GENRES = {
    "podcast": {"label": "Podcast", "style": "clean",
                "emojis": False, "highlight": True, "censor": False,
                "silences": True, "fillers": True},
    "comedy": {"label": "Comedy", "style": "bold-pop",
               "emojis": True, "highlight": True, "censor": True,
               "silences": False, "fillers": False},
    "gaming": {"label": "Gaming", "style": "neon-glow",
               "emojis": True, "highlight": True, "censor": True,
               "silences": False, "fillers": False},
    "news": {"label": "News/Bodycam", "style": "boxed",
             "emojis": False, "highlight": True, "censor": True,
             "silences": False, "fillers": False},
    "business": {"label": "Business", "style": "clean",
                 "emojis": False, "highlight": True, "censor": False,
                 "silences": True, "fillers": True},
    "fitness": {"label": "Fitness", "style": "lime-punch",
                "emojis": True, "highlight": True, "censor": False,
                "silences": False, "fillers": False},
}

# Word -> emoji for the "Add emojis" caption toggle (small built-in map).
EMOJI_MAP = {
    "wow": "😮", "insane": "🤯", "crazy": "🤪", "amazing": "😲",
    "unbelievable": "😱", "incredible": "🤩", "shocking": "😨",
    "stunning": "😍", "beautiful": "😍", "fire": "🔥", "lit": "🔥",
    "explosion": "💥", "exploded": "💥", "police": "🚔", "officer": "👮",
    "cops": "🚔", "arrest": "🚨", "arrested": "🚨", "siren": "🚨",
    "sirens": "🚨", "gun": "🔫", "money": "💰", "cash": "💵",
    "rich": "💰", "win": "🏆", "winner": "🏆", "love": "❤️",
    "funny": "😂", "hilarious": "🤣", "lol": "😂", "haha": "😂",
    "scary": "😱", "terrifying": "😱", "secret": "🤫", "exposed": "📸",
    "breaking": "🚨", "exclusive": "🔥", "fight": "🥊", "run": "🏃",
    "car": "🚗", "dog": "🐶", "cat": "🐱", "goal": "⚽",
}

# Small built-in blocklist for the auto-censor beep (separate block, keep
# it short - strong profanity only).
CENSOR_WORDS = {
    "fuck", "fucking", "fucker", "fucked", "shit", "shitty",
    "bitch", "bitches", "asshole", "dick", "dickhead", "pussy",
    "cunt", "bastard", "whore", "slut", "nigga", "nigger",
    "faggot", "retard",
}


def _is_highlight_word(word):
    """POS-free keyword heuristic: capitalized words + energy words."""
    w = word.strip().strip(",.!?;:\"'()")
    if len(w) < 3:
        return False
    if w[0].isupper():
        return True
    return _clean_word(w) in OFFLINE_ENERGY_WORDS


def find_censor_ranges(words):
    """Source/clip-relative [(start, end)] ranges of profane words."""
    ranges = []
    for w in words:
        t = w["word"].strip().lower().strip(",.!?;:\"'()")
        if t in CENSOR_WORDS or (t.endswith("s") and t[:-1] in CENSOR_WORDS):
            ranges.append((w["start"], w["end"]))
    return ranges


def probe_duration(video_path):
    """Duration in seconds via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def apply_censor_beep(clip_path, ranges, out_path, freq=1000):
    """Duck the audio and mix a beep over profanity ranges (clip-relative).

    Timing-neutral: the beep replaces the word 1:1, so captions stay in
    sync. Re-encodes audio only (video is copied).
    """
    if not ranges:
        raise RuntimeError("no censor ranges")
    dur = probe_duration(clip_path) or sum(e - s for s, e in ranges)
    cond = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in ranges)
    fc = (f"[0:a]volume=enable='{cond}':volume=0[muted];"
          f"sine=frequency={freq}:duration={dur:.3f}[beep];"
          "[muted][beep]amix=inputs=2:duration=first:"
          "dropout_transition=0[aout]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip_path,
           "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("censor failed: " + r.stderr[-800:])
    return out_path


def detect_silences(video_path, start, end, min_dur=0.6, threshold_db=-38):
    """Find silent ranges inside [start, end) via ffmpeg silencedetect.

    Returns [(s, e)] in source-relative seconds."""
    cmd = ["ffmpeg", "-v", "info", "-ss", str(max(0, start)),
           "-to", str(end), "-i", video_path,
           "-af", f"silencedetect=noise={threshold_db}dB:d={min_dur}",
           "-f", "null", "-"]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    ranges, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            cur = float(m.group(1)) + max(0, start)
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and cur is not None:
            s = max(cur, start)
            e = min(float(m.group(1)) + max(0, start), end)
            if e - s >= min_dur * 0.8:
                ranges.append((s, e))
            cur = None
    if cur is not None and end - max(cur, start) >= min_dur:
        ranges.append((max(cur, start), end))
    return ranges


def write_vtt(words, start, end, out_path):
    """Write a WebVTT subtitle file for one clip window."""
    win = [w for w in words if w["end"] > start and w["start"] < end]
    win.sort(key=lambda w: w["start"])

    def ts(s):
        s = max(0, s)
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"

    blocks = ["WEBVTT\n"]
    for line in _group_lines(win):
        text = " ".join(w["word"] for w in line)
        blocks.append(f"{ts(line[0]['start'])} --> {ts(line[-1]['end'])}"
                      f"\n{text}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return out_path


def write_xml_timeline(clips, out_path, fps=30):
    """FCPXML 1.9 timeline of the rendered clips.

    Imports into DaVinci Resolve and Premiere Pro. clips is a list of
    {path, title, duration} with duration in seconds.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    def stamp(s):
        return f"{max(1, int(round(s * fps)))}/{fps}s"

    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(resources, "format", id="f1",
                  name="FFVideoFormatRateUndefined",
                  frameDuration=f"1/{fps}s", width="1080", height="1920")
    for i, c in enumerate(clips, 1):
        a = ET.SubElement(resources, "asset", id=f"r{i}",
                          name=str(c.get("title", f"clip{i}"))[:80],
                          hasVideo="1", hasAudio="1",
                          duration=stamp(c["duration"]))
        ET.SubElement(a, "media-rep", kind="original-media",
                      src="file://" + str(c["path"]).replace("\\", "/"))
    total = sum(c["duration"] for c in clips) or 1
    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name="Itraji Cliper")
    project = ET.SubElement(event, "project", name="Itraji Cliper Timeline")
    seq = ET.SubElement(project, "sequence", format="f1",
                        duration=stamp(total), tcStart="0s", tcFormat="NDF",
                        audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    offset = 0.0
    for i, c in enumerate(clips, 1):
        d, o = stamp(c["duration"]), stamp(offset)
        clip = ET.SubElement(spine, "clip",
                             name=str(c.get("title", f"clip{i}"))[:80],
                             offset=o, duration=d, start="0s")
        ET.SubElement(clip, "video", offset=o, ref=f"r{i}", duration=d)
        offset += c["duration"]
    pretty = minidom.parseString(
        ET.tostring(fcpxml, encoding="utf-8")).toprettyxml(
            indent="  ", encoding="utf-8")
    with open(out_path, "wb") as f:
        f.write(pretty)
    return out_path


def parse_srt_file(path):
    """Parse an SRT file into word dicts (cue time split evenly per word)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()

    def ts(t):
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", t.strip())
        if not m:
            return None
        h, mi, s, ms = map(int, m.groups())
        return h * 3600 + mi * 60 + s + ms / 1000.0

    words = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.strip().splitlines()
                 if ln.strip()]
        if len(lines) < 2:
            continue
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        a, b = [p.strip() for p in lines[ti].split("-->")]
        s, e = ts(a), ts(b)
        if s is None or e is None or e <= s:
            continue
        toks = " ".join(lines[ti + 1:]).split()
        if not toks:
            continue
        per = (e - s) / len(toks)
        for j, tok in enumerate(toks):
            words.append({"start": s + j * per, "end": s + (j + 1) * per,
                          "word": tok})
    words.sort(key=lambda w: w["start"])
    return words


def find_moments_by_keywords(words, queries, clip_min_s, clip_max_s,
                             max_clips=8):
    """Offline keyword/phrase search over the transcript.

    Returns (moments, unmatched). Each moment carries
    'matched': <query> (the UI stamps it "Matched: <query>").
    Queries with zero hits are returned in unmatched - reported honestly,
    never invented.
    """
    queries = [str(q or "").strip() for q in (queries or []) if str(q or "").strip()]
    if not words:
        return [], queries
    dur = words[-1]["end"]
    win_len = min(max((clip_min_s + clip_max_s) / 2.0, 10.0), dur)
    if win_len < 5:
        return [], queries
    step = max(3.0, win_len / 6.0)
    lowers = [_clean_word(w["word"]) for w in words]

    moments, unmatched = [], []
    for q in queries:
        terms = [_clean_word(t) for t in re.findall(r"[a-zA-Z']+", q.lower())]
        terms = [t for t in terms
                 if t and t not in _OFFLINE_STOPWORDS]
        if not terms:
            unmatched.append(q)
            continue
        best = None  # (score, s, e)
        t = 0.0
        while t + win_len <= dur + 0.001:
            s, e = t, t + win_len
            bag = {lowers[i] for i in range(len(words))
                   if words[i]["end"] > s and words[i]["start"] < e}
            hits = sum(1 for term in terms if term in bag)
            if hits:
                score = hits * 10 + (25 if hits == len(terms) else 0)
                if best is None or score > best[0]:
                    best = (score, s, e)
            t += step
        if best is None:
            unmatched.append(q)
            continue
        score, s, e = best
        win_words = [w for w in words if w["end"] > s and w["start"] < e]
        title, hook = _extract_title_hook(win_words)
        v = max(40, min(95, 45 + score))
        moments.append({
            "start": round(s, 2), "end": round(e, 2),
            "title": title, "hook": hook, "virality": v,
            "hook_score": v, "flow_score": max(30, v - 10),
            "value_score": max(30, v - 5),
            "reason": f"Keyword match for: {q}",
            "matched": q, "offline": True,
        })
    return moments[:max_clips], unmatched


# ---------------------------------------------------------------------------
# v2: multi-clip merge
# ---------------------------------------------------------------------------

def _merge_xfade(paths, out_path, d=0.5):
    """Crossfade-merge clips (re-encode). All clips must share WxH."""
    infos = [probe_video(p) for p in paths]
    w, h = infos[0][0], infos[0][1]
    fps = infos[0][2] or 30.0
    durs = [probe_duration(p) for p in paths]
    vparts, aparts = [], []
    for i, p in enumerate(paths):
        vparts.append(
            f"[{i}:v]fps={fps:.3f},format=yuv420p,"
            f"setpts=PTS-STARTPTS[v{i}]")
        aparts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
    # video xfade chain
    expr = ";".join(vparts + aparts)
    last_v, last_a = "v0", "a0"
    acc = 0.0
    for i in range(1, len(paths)):
        acc += durs[i - 1]
        off = max(0.0, acc - d * i)
        expr += (f";[{last_v}][v{i}]xfade=transition=fade:"
                 f"duration={d}:offset={off:.3f}[xv{i}]")
        expr += (f";[{last_a}][a{i}]acrossfade=d={d}[ac{i}]")
        last_v, last_a = f"xv{i}", f"ac{i}"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in paths:
        cmd += ["-i", p]
    cmd += ["-filter_complex", expr,
            "-map", f"[{last_v}]", "-map", f"[{last_a}]",
            "-c:v", "libx264", "-preset", "slow", "-crf", "17",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("crossfade merge failed: " + r.stderr[-1200:])
    return out_path


def merge_clips(clip_paths, out_path, transition="cut",
                intro_path=None, outro_path=None):
    """Concatenate rendered MP4s into one file.

    transition: "cut" (fast concat) or "crossfade" (0.5s video+audio
    fades, re-encodes; falls back to cut when clips differ in size).
    intro_path/outro_path: optional clips prepended/appended.
    """
    seq = [p for p in [intro_path] + list(clip_paths) + [outro_path] if p]
    if len(seq) < 2:
        raise RuntimeError("pick at least 2 clips to merge")
    for p in seq:
        if not os.path.exists(p):
            raise RuntimeError("missing clip file: " + p)
    if transition == "crossfade":
        try:
            infos = [probe_video(p) for p in seq]
            same = all(i[0] == infos[0][0] and i[1] == infos[0][1]
                       for i in infos)
        except Exception:  # noqa: BLE001 - fall back to cut
            same = False
        if same:
            return _merge_xfade(seq, out_path)
    lst_fd, lst_path = tempfile.mkstemp(suffix=".txt", prefix="merge_")
    os.close(lst_fd)
    try:
        with open(lst_path, "w", encoding="utf-8") as f:
            for p in seq:
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


# ---------------------------------------------------------------------------
# v3: generalized render engine (ratios, quality, caption magic)
# ---------------------------------------------------------------------------

def _render_tracked(video_path, start, end, keep_segs, removed, ass_path,
                    W, H, ratio, out_path, progress_cb=None,
                    punch_zoom=False, enc_preset="slow", enc_crf="16"):
    """Face-tracked render: per-frame crop follows the smoothed face
    trajectory (or stays centered), frames piped to ffmpeg."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "Face tracking needs opencv-python - "
            "reinstall with Install.bat")
    from facetrack import (FaceTracker, sample_face_centers,
                           smooth_trajectory, interpolate_x)

    src_w, src_h, fps = probe_video(video_path)
    r = RATIOS.get(ratio, RATIOS["9:16"])
    rw, rh = r["rw"], r["rh"]
    crop_w = min(_even(src_h * rw / rh), src_w)
    crop_h = src_h
    if crop_w >= src_w:
        crop_w = src_w
        crop_h = _even(src_w * rh / rw)
    y_top = int((src_h - crop_h) / 2)

    trajectory = None
    try:
        tracker = FaceTracker()
        if tracker.backend == "none":
            raise RuntimeError("no_face_backend")
        if progress_cb:
            progress_cb(3, "Detecting faces")
        samples = sample_face_centers(
            video_path, start, end, tracker, progress_cb=None)
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
            "-c:a", "aac", "-b:a", "192k", audio_tmp]
    r_ = subprocess.run(acmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True)
    if r_.returncode != 0 or not os.path.exists(audio_tmp):
        raise RuntimeError("audio cut failed: " + r_.stderr[-1200:])

    # encode: pipe cropped frames -> ffmpeg + subtitles + audio
    vf_parts = []
    if punch_zoom:
        vf_parts.append(
            "zoompan=z='min(1+0.0008*min(on\\,90)"
            "+0.00004*max(0\\,on-90)\\,1.16)'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={fps:.3f}")
    vf_parts.append(f"subtitles={_ffmpeg_ass_path(ass_path)}"
                         f"{_fontsdir_opt()}")
    enc = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-framerate", f"{fps:.3f}",
           "-i", "pipe:0",
           "-i", audio_tmp,
           "-vf", ",".join(vf_parts),
           "-c:v", "libx264", "-preset", enc_preset, "-crf", enc_crf,
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
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
            crop = frame[y_top:y_top + crop_h, x_left:x_left + crop_w]
            # INTER_AREA smears when ENLARGING (the usual case: the
            # crop is smaller than the 1080x1920 output); Lanczos is the
            # sharp choice for upscale, AREA stays best for downscale.
            _interp = (cv2.INTER_LANCZOS4
                       if crop.shape[1] < W or crop.shape[0] < H
                       else cv2.INTER_AREA)
            small = cv2.resize(crop, (W, H), interpolation=_interp)
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
    try:
        os.remove(audio_tmp)
    except OSError:
        pass
    if failed or proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg encode failed: " + err[-2000:])
    return out_path


def _render_plain(video_path, start, end, keep_segs, ass_path,
                  W, H, ratio, out_path, progress_cb=None,
                  punch_zoom=False, enhance_clarity=False,
                  enc_preset="slow", enc_crf="16"):
    """ffmpeg-only render: crop/scale/captions, optional silence/filler
    cuts via filter_complex segment concat.

    punch_zoom: subtle zoom-punch on the hook (~1.0x -> ~1.07x over the
        first 3s) then a slow drift, capped at 1.16x (zoompan, 1:1 frames).
    enhance_clarity: light denoise + unsharp ("enhance" - NOT an AI
        upscale; honest naming is deliberate).
    """
    crop = crop_filter(ratio)
    vf = [crop, f"scale={W}:{H}:flags=lanczos"]
    if punch_zoom:
        # zoompan d=1 keeps a 1:1 frame mapping; fps is pinned to the
        # source fps so A/V stays in sync.
        _pw, _ph, _fps = probe_video(video_path)
        _fps = _fps or 30.0
        vf.append(
            "zoompan=z='min(1+0.0008*min(on\\,90)"
            "+0.00004*max(0\\,on-90)\\,1.16)'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={_fps:.3f}")
    if enhance_clarity:
        vf.append("hqdn3d=4:3:6:4.5,unsharp=5:5:0.7:5:5:0.0")
    vf.append(f"subtitles={_ffmpeg_ass_path(ass_path)}"
                    f"{_fontsdir_opt()}")
    vf_tail = ",".join(vf)
    single = (len(keep_segs) == 1
              and abs(keep_segs[0][0] - start) < 0.01
              and abs(keep_segs[0][1] - end) < 0.01)
    if single:
        # fast path: one straight cut
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-ss", str(start), "-to", str(end),
               "-i", video_path,
               "-vf", vf_tail,
               "-c:v", "libx264", "-preset", enc_preset, "-crf", enc_crf,
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", out_path]
    else:
        vparts, aparts, vl, al = [], [], [], []
        for i, (s, e) in enumerate(keep_segs):
            vparts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},"
                          f"setpts=PTS-STARTPTS[v{i}]")
            aparts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},"
                          f"asetpts=PTS-STARTPTS[a{i}]")
            vl.append(f"[v{i}]")
            al.append(f"[a{i}]")
        n = len(keep_segs)
        vparts.append("".join(vl) + f"concat=n={n}:v=1:a=0,"
                      + vf_tail + "[vout]")
        aparts.append("".join(al) + f"concat=n={n}:v=0:a=1[aout]")
        fc = ";".join(vparts + aparts)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-filter_complex", fc,
               "-map", "[vout]", "-map", "[aout]",
               "-c:v", "libx264", "-preset", enc_preset, "-crf", enc_crf,
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", out_path]
    if progress_cb:
        progress_cb(5, "Rendering clip")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg failed: " + r.stderr[-2000:])
    return out_path


def _overlay_logo(clip_path, logo_path):
    """Corner logo watermark (top-left, ~14% of clip width).

    Timing-neutral post-pass: video re-encoded once, audio copied.
    Best-effort: raises RuntimeError on failure so the caller can decide
    whether to keep the unbranded clip."""
    if not logo_path or not os.path.isfile(logo_path):
        raise RuntimeError("logo file not found: " + str(logo_path)[:120])
    cw, _ch, _fps = probe_video(clip_path)
    lw = max(48, int((cw or 540) * 0.14))
    tmp = clip_path + ".logo.mp4"
    fc = (f"[1:v]scale={lw}:-2:flags=lanczos[logo];"
          f"[0:v][logo]overlay=28:28:format=auto[vout]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip_path, "-i", logo_path,
           "-filter_complex", fc, "-map", "[vout]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "slow", "-crf", "17",
           "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", "-shortest", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("logo overlay failed: " + r.stderr[-600:])
    os.replace(tmp, clip_path)
    return clip_path


def render_clip(video_path, moment, words, style_name, out_path,
                progress_cb=None, face_track=False, remove_fillers=False,
                remove_silences=False, auto_censor=False, emojis=False,
                highlight=False, ratio="9:16", quality="1080p",
                custom_styles=None, edited_words=None,
                speaker_colors=None, hook_overlay=True, cta_text=None,
                safe_zone=False, punch_zoom=False, enhance_clarity=False,
                logo_path=None, quality_tier="maximum",
                caption_params=None):
    """Cut + reframe + burned captions + hook. Returns out_path.

    face_track=True -> per-frame face-following crop (OpenCV pipe).
    remove_fillers / remove_silences -> cut those ranges from
    audio/video/captions (captions re-timed onto the kept timeline).
    auto_censor -> beep over profanity (timing-neutral second pass).
    emojis / highlight -> caption magic. ratio/quality pick output size.
    edited_words -> caption word list from the caption editor.
    speaker_colors -> (color1, color2) ASS colors: each caption word is
        painted by its heuristic speaker (Speaker 1/2 labels, honest -
        no identity claims). Overrides the highlight color.
    hook_overlay -> show the hook text overlay in the first ~2.5s.
    cta_text -> when set, shown as a faded overlay in the last 2s.
    safe_zone -> nudge captions/hook inside Reels UI safe areas.
    punch_zoom -> subtle zoom-punch on the hook + slow drift.
    enhance_clarity -> light denoise + sharpen (honest "enhance",
        never "AI upscale").
    logo_path -> corner logo PNG watermarked top-left (brand kit).
    """
    start, end = float(moment["start"]), float(moment["end"])
    W, H = ratio_dims(ratio, quality)
    enc_preset, enc_crf = _ENCODE_TIERS.get(
        quality_tier, _ENCODE_TIERS["maximum"])
    if custom_styles:
        set_custom_styles(custom_styles)

    cap_src = edited_words if edited_words is not None else words
    win_words = [w for w in cap_src if w["end"] > start and w["start"] < end]
    win_words.sort(key=lambda w: w["start"])
    if speaker_colors:
        # heuristic 2-speaker split before any cutting so colors follow
        # words through the filler/silence remap
        win_words = diarize_heuristic(win_words)

    # removed ranges (source-relative), merged
    removed = []
    if remove_fillers:
        removed += find_filler_ranges(win_words)
    if remove_silences:
        try:
            removed += detect_silences(video_path, start, end)
        except Exception:  # noqa: BLE001 - silence cut is best-effort
            pass
    removed = [(max(s, start), min(e, end)) for s, e in removed]
    removed = [(s, e) for s, e in removed if e > s]
    removed.sort()
    merged = []
    for s, e in removed:
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    removed = merged

    keep_segs = compute_keep_segments(start, end, removed)
    if not keep_segs:
        raise RuntimeError("silence/filler removal would delete the whole clip")
    cap_words = remap_words_to_kept(win_words, removed)
    # Rebase onto the clip's 0-based output timeline. remap only strips
    # removed filler/silence ranges - it keeps source-relative timestamps,
    # but the rendered clip starts at t=0 and build_ass is called with the
    # window [0, total_dur). Without this shift every word falls outside
    # the window, the .ass gets zero caption events, and the render comes
    # out fine but captionless for any moment past the video's start.
    cap_words = [{**w, "start": w["start"] - start,
                  "end": w["end"] - start} for w in cap_words]
    total_dur = kept_duration(start, end, removed)

    ass_fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix="clip_")
    os.close(ass_fd)
    build_ass(cap_words, 0, total_dur, style_name, moment.get("hook", ""),
              ass_path, res=(W, H), emojis=emojis, highlight=highlight,
              speaker_colors=speaker_colors, hook_overlay=hook_overlay,
              cta_text=cta_text, safe_zone=safe_zone,
              caption_params=caption_params)
    try:
        if face_track:
            _render_tracked(video_path, start, end, keep_segs, removed,
                            ass_path, W, H, ratio, out_path, progress_cb,
                            punch_zoom=punch_zoom,
                            enc_preset=enc_preset, enc_crf=enc_crf)
        else:
            _render_plain(video_path, start, end, keep_segs, ass_path,
                          W, H, ratio, out_path, progress_cb,
                          punch_zoom=punch_zoom,
                          enhance_clarity=enhance_clarity,
                          enc_preset=enc_preset, enc_crf=enc_crf)
    finally:
        try:
            os.remove(ass_path)
        except OSError:
            pass

    if logo_path:
        # brand-kit corner logo: best-effort - a failed overlay must not
        # lose the finished clip
        try:
            _overlay_logo(out_path, logo_path)
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(100, "Logo skipped: " + str(e)[:80])

    if auto_censor:
        ranges = find_censor_ranges(cap_words)
        if ranges:
            tmp = out_path + ".censored.mp4"
            try:
                apply_censor_beep(out_path, ranges, tmp)
                os.replace(tmp, out_path)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    if progress_cb:
        progress_cb(100, "Render complete")
    return out_path


# Backwards-compatible alias (old positional-arg callers).
def render_clip_tracked(video_path, moment, words, style_name, out_path,
                        face_track=True, remove_fillers=False,
                        progress_cb=None, **kwargs):
    return render_clip(video_path, moment, words, style_name, out_path,
                       progress_cb=progress_cb, face_track=face_track,
                       remove_fillers=remove_fillers, **kwargs)


# ---------------------------------------------------------------------------
# Caption Remover - strip captions already burned into the pixels
# ---------------------------------------------------------------------------
# Three honest methods:
#   crop - cut the band out and scale back to the original frame size
#   fill - replace the band with a strong blur + grain sampled from the
#          band itself (texture-filled, never a flat bar)
#   ai   - LaMa inpainting (ONNX, via the bundled onnxruntime) applied
#          to the band only; model downloads once on first use

LAMA_MODEL_URL = ("https://huggingface.co/Carve/LaMa-ONNX/resolve/main/"
                  "lama_fp32.onnx")
LAMA_MODEL_FILE = "lama_fp32.onnx"
LAMA_MODEL_SIZE = 208_044_816  # bytes, per the HuggingFace blob listing


def _remover_band(region, height):
    """Validate a region dict -> (top_px, bottom_px) inside the frame.

    region = {"top_pct": float, "bottom_pct": float}: the caption band
    spans top_pct%..bottom_pct% of the frame height, full width.
    """
    try:
        top = float((region or {}).get("top_pct", 82.0))
        bot = float((region or {}).get("bottom_pct", 100.0))
    except (TypeError, ValueError):
        raise ValueError("Caption region needs numeric top/bottom "
                         "percentages.")
    top = min(max(top, 0.0), 100.0)
    bot = min(max(bot, 0.0), 100.0)
    if bot - top < 2.0:
        raise ValueError("Caption region is too thin (under 2% of the "
                         "frame height) - widen the band.")
    y0 = int(round(height * top / 100.0))
    y1 = int(round(height * bot / 100.0))
    y0 = max(0, min(y0, height - 2))
    y1 = max(y0 + 2, min(y1, height))
    return y0, y1


def _encode_args(quality_tier):
    preset, crf = _ENCODE_TIERS.get(quality_tier or "maximum",
                                    _ENCODE_TIERS["maximum"])
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p"]


def lama_model_path():
    import local_ai
    os.makedirs(local_ai.MODELS_DIR, exist_ok=True)
    return os.path.join(local_ai.MODELS_DIR, LAMA_MODEL_FILE)


def lama_ready():
    try:
        return os.path.getsize(lama_model_path()) == LAMA_MODEL_SIZE
    except OSError:
        return False


def ensure_lama(progress_cb=None):
    """Download the LaMa ONNX model once (~198 MB), like the other
    brains. Returns the model path; RuntimeError on failure."""
    import urllib.request
    path = lama_model_path()
    if lama_ready():
        return path
    tmp = path + ".part"
    try:
        req = urllib.request.Request(
            LAMA_MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length")
                        or LAMA_MODEL_SIZE)
            got = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress_cb and total:
                    progress_cb(2 + int(13 * got / total),
                                "Downloading AI inpainting model "
                                "(one-time, ~198 MB)")
        if got != LAMA_MODEL_SIZE:
            raise RuntimeError(
                f"AI model download incomplete ({got} of "
                f"{LAMA_MODEL_SIZE} bytes) - check your connection "
                "and retry.")
        os.replace(tmp, path)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not download the AI inpainting "
                           f"model: {e}")
    return path


# ---------------------------------------------------------------------------
# OCR text detection (PP-OCRv4 DBNet, ONNX) - finds the actual text
# boxes instead of guessing a band from edges. The model is tiny
# (~4.7 MB, Apache-2.0: PaddleOCR via the RapidOCR project) and
# self-downloads once on first use, like the other brains. Used for
# (a) auto caption-band detection and (b) tight per-frame text masks,
# so inpainting only touches the letters (+ outline/shadow), not a
# whole rectangular slab of picture.
# ---------------------------------------------------------------------------
OCR_DET_MODEL_URL = ("https://huggingface.co/SWHL/RapidOCR/resolve/main/"
                     "PP-OCRv4/ch_PP-OCRv4_det_infer.onnx")
OCR_DET_MODEL_FILE = "ppocrv4_det.onnx"
OCR_DET_MODEL_SIZE = 4_745_517
OCR_DET_MODEL_SHA256 = ("d2a7720d45a54257208b1e13e36a8479894cb74155"
                        "a5efe29462512d42f49da9")


def ocr_model_path():
    import local_ai
    os.makedirs(local_ai.MODELS_DIR, exist_ok=True)
    return os.path.join(local_ai.MODELS_DIR, OCR_DET_MODEL_FILE)


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ocr_ready():
    try:
        return os.path.getsize(ocr_model_path()) == OCR_DET_MODEL_SIZE
    except OSError:
        return False


def ensure_text_detector(progress_cb=None):
    """Download the OCR text-detection model once (~4.7 MB), verified
    by size AND sha256 (unlike a bare size check, a corrupted or
    substituted file can not slip through). Returns the model path;
    RuntimeError on failure."""
    import urllib.request
    path = ocr_model_path()
    if ocr_ready() and _sha256_file(path) == OCR_DET_MODEL_SHA256:
        return path
    tmp = path + ".part"
    try:
        req = urllib.request.Request(
            OCR_DET_MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length")
                        or OCR_DET_MODEL_SIZE)
            got = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress_cb and total:
                    progress_cb(3, "Downloading text-detection model "
                                   "(one-time, ~5 MB)")
        if got != OCR_DET_MODEL_SIZE \
                or _sha256_file(tmp) != OCR_DET_MODEL_SHA256:
            raise RuntimeError(
                "Text-detection model download did not verify "
                f"({got} bytes) - check your connection and retry.")
        os.replace(tmp, path)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not download the text-detection "
                           f"model: {e}")
    return path


class _TextDetector:
    """DBNet text detector (PP-OCRv4 det, ONNX). detect() returns a
    list of (x0, y0, x1, y1, score) boxes in source-image pixels."""

    def __init__(self, model_path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = os.cpu_count() or 2
        self.sess = ort.InferenceSession(
            model_path, sess_options=opts,
            providers=["CPUExecutionProvider"])
        self.iname = self.sess.get_inputs()[0].name

    def detect(self, img_bgr, max_side=960, thresh=0.3, box_thresh=0.55):
        import cv2
        import numpy as np
        h, w = img_bgr.shape[:2]
        if not h or not w:
            return []
        scale = min(1.0, float(max_side) / max(h, w))
        nw = max(32, int(round(w * scale)) // 32 * 32)
        nh = max(32, int(round(h * scale)) // 32 * 32)
        rgb = cv2.cvtColor(cv2.resize(img_bgr, (nw, nh)),
                           cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], np.float32)
        std = np.array([0.229, 0.224, 0.225], np.float32)
        x = ((x - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)
        prob = self.sess.run(None, {self.iname: x})[0][0, 0]
        binary = (prob > thresh).astype(np.uint8)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / float(nw), h / float(nh)
        boxes = []
        for cnt in contours:
            if cv2.contourArea(cnt) < 12:
                continue
            rect = cv2.minAreaRect(cnt)
            (cx, cy), (bw, bh), ang = rect
            if min(bw, bh) < 3:
                continue
            m = np.zeros(prob.shape, np.uint8)
            cv2.drawContours(m, [cnt], -1, 1, -1)
            sel = m > 0
            score = float(prob[sel].mean()) if sel.any() else 0.0
            if score < box_thresh:
                continue
            # DBNet unclip: grow the box by area*1.5/perimeter so the
            # mask covers the whole glyph plus its dark outline.
            d = cv2.contourArea(cnt) * 1.5 / max(cv2.arcLength(cnt, True),
                                                 1.0)
            pts = cv2.boxPoints(((cx, cy), (bw + 2 * d, bh + 2 * d), ang))
            xs, ys = pts[:, 0] * sx, pts[:, 1] * sy
            boxes.append((float(max(0, xs.min())),
                          float(max(0, ys.min())),
                          float(min(w, xs.max())),
                          float(min(h, ys.max())), score))
        return boxes


_OCR_SINGLETON = {"det": None, "failed": False}


def _get_text_detector(progress_cb=None):
    """Lazy singleton; raises RuntimeError when the OCR model cannot
    be downloaded/loaded (callers fall back honestly)."""
    if _OCR_SINGLETON["det"] is not None:
        return _OCR_SINGLETON["det"]
    if _OCR_SINGLETON["failed"]:
        raise RuntimeError("Text detection unavailable (earlier load "
                           "failed).")
    try:
        det = _TextDetector(ensure_text_detector(progress_cb))
    except Exception:
        _OCR_SINGLETON["failed"] = True
        raise
    _OCR_SINGLETON["det"] = det
    return det


def _tophat_text_mask(gray):
    """Cheap per-frame text-pixel mask from a grayscale crop: white
    top-hat (bright strokes) + black top-hat (dark strokes), the
    classic caption-text signature. Returns float32 0/1, same shape.
    Used for caption-change detection and to top up the OCR mask
    between detector refreshes. Large inputs are processed at half
    resolution (caption strokes are big there, and the full-res
    morphology costs ~0.9 s/frame at 1080p - the mask is dilated and
    blurred by callers anyway, so the softer edges cost nothing)."""
    import cv2
    import numpy as np
    h, w = gray.shape[:2]
    if h > 420 and w > 420:
        small = cv2.resize(gray, (w // 2, h // 2),
                           interpolation=cv2.INTER_AREA)
        m = _tophat_text_mask(small)
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
        return (m > 0.4).astype(np.float32)
    kw = max(9, (w // 28) | 1)
    kh = max(5, (h // 7) | 1)
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kw, kh))
    g = gray.astype(np.int16)
    white = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kern).astype(np.int16)
    black = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT,
                             kern).astype(np.int16)
    resp = np.maximum(white, black)
    # text strokes are also locally brighter/darker than their blur
    blur = cv2.GaussianBlur(gray, (0, 0), 3).astype(np.int16)
    resp = np.maximum(resp, np.abs(g - blur) * 2)
    return (resp > 20).astype(np.float32)


def _rasterize_boxes(boxes, shape, ox=0, oy=0, grow_x=0.30, grow_y=0.55):
    """Rasterize OCR boxes into a 0/1 float mask of `shape` (h, w),
    offset by (ox, oy) and grown so the mask also covers the caption
    box/shadow hugging the text."""
    import numpy as np
    h, w = shape
    mask = np.zeros((h, w), np.float32)
    for (x0, y0, x1, y1, _s) in boxes:
        bh = max(2.0, y1 - y0)
        bw = max(2.0, x1 - x0)
        gx = bw * grow_x * 0.5 + 3
        gy = bh * grow_y * 0.5 + 3
        ix0 = int(max(0, x0 - ox - gx))
        iy0 = int(max(0, y0 - oy - gy))
        ix1 = int(min(w, x1 - ox + gx))
        iy1 = int(min(h, y1 - oy + gy))
        if ix1 > ix0 and iy1 > iy0:
            mask[iy0:iy1, ix0:ix1] = 1.0
    return mask


def _lama_session():
    import onnxruntime as ort
    model = ensure_lama()
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = os.cpu_count() or 2
    sess = ort.InferenceSession(model, sess_options=opts,
                                providers=["CPUExecutionProvider"])
    inputs = {i.name: i for i in sess.get_inputs()}
    img_name = next((n for n in inputs if "image" in n.lower()
                     or "img" in n.lower()), sess.get_inputs()[0].name)
    mask_name = next((n for n in inputs if "mask" in n.lower()),
                     sess.get_inputs()[-1].name)
    return sess, img_name, mask_name


def _lama_inpaint(sess, img_name, mask_name, crop_bgr, mask01):
    """Run LaMa (fixed 512x512) on a working crop with a tight 0/1
    mask; returns the inpainted crop at the crop's own resolution.

    (Native-resolution tiling was evaluated for large crops and
    measured on real bodycam frames: no visible gain - the footage
    behind captions is defocused/compressed, so there is little fine
    texture for the squeeze to lose - at ~6x the per-plate cost.
    Single call it is.)"""
    import cv2
    import numpy as np
    ch, cw = crop_bgr.shape[:2]
    small = cv2.resize(crop_bgr, (512, 512),
                       interpolation=cv2.INTER_AREA)
    m = cv2.resize(mask01, (512, 512), interpolation=cv2.INTER_AREA)
    m = (m > 0.2).astype(np.float32)[None, None]
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    inp = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    res = sess.run(None, {img_name: inp, mask_name: m})[0]
    o = np.asarray(res)[0].transpose(1, 2, 0)
    if float(o.max()) <= 1.5:
        o = o * 255.0
    o = np.clip(o, 0, 255).astype(np.uint8)
    return cv2.resize(cv2.cvtColor(o, cv2.COLOR_RGB2BGR), (cw, ch),
                      interpolation=cv2.INTER_LINEAR)


def _crop_text_mask(det, crop_bgr, band_y0, band_y1):
    """Tight text mask for a working crop (0/1 float, crop shape).

    OCR boxes (when the detector is available) unioned with the
    per-frame top-hat signal, dilated a touch. When neither finds
    anything, falls back to the plain band rectangle (the historical
    behaviour) so removal never silently does nothing."""
    import cv2
    import numpy as np
    ch, cw = crop_bgr.shape[:2]
    mask = np.zeros((ch, cw), np.float32)
    if det is not None:
        try:
            boxes = det.detect(crop_bgr, max_side=768)
            # Only text whose centre sits in the caption rows counts
            # as caption text - scene text that merely shares the
            # working crop (signs, documents) is not a caption.
            boxes = [b for b in boxes
                     if band_y0 - 12 <= (b[1] + b[3]) / 2.0
                     <= band_y1 + 12]
            mask = np.maximum(mask, _rasterize_boxes(boxes, (ch, cw)))
        except Exception:
            pass
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    th = _tophat_text_mask(gray)
    # only trust top-hat inside/near the band rows (scene texture
    # elsewhere in the context crop would over-mask)
    near = np.zeros((ch, cw), np.float32)
    ny0 = max(0, band_y0 - (band_y1 - band_y0) // 2)
    ny1 = min(ch, band_y1 + (band_y1 - band_y0) // 2)
    near[ny0:ny1] = 1.0
    mask = np.maximum(mask, th * near)
    if float(mask.sum()) < 0.0005 * ch * cw:
        mask = np.zeros((ch, cw), np.float32)
        mask[band_y0:band_y1] = 1.0
        return mask
    return cv2.dilate(mask, np.ones((5, 5), np.float32))


def _run_ffmpeg_progress(cmd, duration, progress_cb, lo, hi, stage):
    """Run ffmpeg, mapping its -progress output onto [lo, hi] percent."""
    full = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    tail = []
    try:
        for line in proc.stdout:
            tail.append(line.rstrip())
            if line.startswith("out_time_ms=") and duration:
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = min(1.0, max(0.0, t / duration))
                if progress_cb:
                    progress_cb(lo + int((hi - lo) * frac), stage)
    finally:
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: "
                           + " | ".join(tail[-6:])[:400])


def remove_captions(video_path, region, mode, out_path,
                    progress_cb=None, quality_tier="maximum"):
    """Remove burned-in captions from a band of the frame.

    region: {"top_pct": .., "bottom_pct": ..}; mode: crop|fill|ai|
    temporal (auto == temporal: the app's smart default). Returns
    out_path. Progress via progress_cb(pct, stage_text)."""
    if not os.path.isfile(video_path):
        raise RuntimeError("Video file not found: " + str(video_path))
    if mode == "auto":
        mode = "temporal"
    if mode not in ("crop", "fill", "ai", "temporal"):
        raise ValueError("Unknown caption-removal mode: " + str(mode))
    W, H, _fps = probe_video(video_path)
    if not W or not H:
        raise RuntimeError("Could not read the video's dimensions.")
    duration = probe_duration(video_path) or 0.0
    y0, y1 = _remover_band(region, H)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)),
                exist_ok=True)
    if progress_cb:
        progress_cb(2, "Preparing")
    if mode == "crop":
        _remove_captions_crop(video_path, out_path, W, H, y0, y1,
                              duration, progress_cb, quality_tier)
    elif mode == "fill":
        _remove_captions_fill(video_path, out_path, W, H, y0, y1,
                              duration, progress_cb, quality_tier)
    elif mode == "temporal":
        _remove_captions_temporal(video_path, out_path, W, H, y0, y1,
                                  duration, progress_cb, quality_tier)
    else:
        _remove_captions_ai(video_path, out_path, W, H, y0, y1,
                            duration, progress_cb, quality_tier)
    if not os.path.isfile(out_path) \
            or os.path.getsize(out_path) < 1024:
        raise RuntimeError("Caption removal produced no output file.")
    if progress_cb:
        progress_cb(100, "Done")
    return out_path


def _remove_captions_crop(src, out, W, H, y0, y1, duration, cb, tier):
    parts = []
    if y0 > 0:
        parts.append((0, y0))
    if y1 < H:
        parts.append((y1, H - y1))
    if not parts:
        raise ValueError("The caption region covers the whole frame - "
                         "there is nothing left to keep in Crop mode. "
                         "Use Smart Fill or AI Inpaint instead.")
    enc = _encode_args(tier)
    if len(parts) == 1:
        py, ph = parts[0]
        vf = (f"crop={W}:{ph}:0:{py},"
              f"scale={W}:{H}:flags=lanczos")
        cmd = (["ffmpeg", "-y", "-i", src, "-vf", vf]
               + enc + ["-c:a", "aac", "-b:a", "192k", out])
    else:
        (ay, ah), (by, bh) = parts
        fc = (f"[0:v]split[a][b];"
              f"[a]crop={W}:{ah}:0:{ay}[t];"
              f"[b]crop={W}:{bh}:0:{by}[bt];"
              f"[t][bt]vstack=inputs=2,"
              f"scale={W}:{H}:flags=lanczos[v]")
        cmd = (["ffmpeg", "-y", "-i", src, "-filter_complex", fc,
                "-map", "[v]", "-map", "0:a?"]
               + enc + ["-c:a", "aac", "-b:a", "192k", out])
    _run_ffmpeg_progress(cmd, duration, cb, 5, 97,
                         "Cropping the caption band out")


def _remove_captions_fill(src, out, W, H, y0, y1, duration, cb, tier):
    bh = y1 - y0
    radius = max(10, min(60, bh // 3))
    fc = (f"[0:v]split[base][tmp];"
          f"[tmp]crop={W}:{bh}:0:{y0},"
          f"boxblur=lr={radius}:lp=3:cr={max(6, radius // 2)}:cp=2,"
          f"noise=alls=6:allf=t+u[band];"
          f"[base][band]overlay=0:{y0}[v]")
    cmd = (["ffmpeg", "-y", "-i", src, "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a?"]
           + _encode_args(tier) + ["-c:a", "aac", "-b:a", "192k", out])
    _run_ffmpeg_progress(cmd, duration, cb, 5, 97,
                         "Filling the caption band")


def _remove_captions_ai(src, out, W, H, y0, y1, duration, cb, tier):
    import cv2
    import numpy as np

    if cb:
        cb(14, "Loading AI inpainting model")
    sess, img_name, mask_name = _lama_session()
    try:
        det = _get_text_detector(cb)
    except Exception:
        det = None  # tight masks degrade to the band rectangle

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError("Could not open the video for AI inpainting.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    # Working crop: the band plus an equal amount of clean context.
    pad = max(16, y1 - y0)
    cy0 = max(0, y0 - pad)
    cy1 = min(H, y1 + pad)
    ch = cy1 - cy0
    # band rows inside the working crop
    by0 = y0 - cy0
    by1 = y1 - cy0

    log_path = out + ".ffmpeg.log"
    logf = open(log_path, "wb")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{W}x{H}", "-r", f"{fps:.3f}", "-i", "-",
         "-i", src, "-map", "0:v", "-map", "1:a?",
         ] + _encode_args(tier) + [
         "-c:a", "aac", "-b:a", "192k", "-shortest", out],
        stdin=subprocess.PIPE, stdout=logf, stderr=logf)
    n = 0
    inpainted = 0
    last_small = None   # last working-crop frame that was inpainted
    last_band = None    # its inpainted band, at full resolution
    since_inpaint = 0
    pipe_broken = False
    try:
        if cb:
            cb(18, "AI inpainting frames")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[cy0:cy1]
            small = cv2.resize(crop, (512, 512),
                               interpolation=cv2.INTER_AREA)
            # Temporal reuse: burned captions (and the scene behind
            # them) usually sit still for stretches. If the working
            # crop barely changed since the last inpainted frame,
            # reuse that inpaint; force a fresh one every 30 frames.
            reuse = False
            if last_small is not None and since_inpaint < 30:
                diff = float(np.abs(
                    small.astype(np.int16)
                    - last_small.astype(np.int16)).mean())
                reuse = diff < 2.0
            if reuse:
                band = last_band
                since_inpaint += 1
            else:
                m = _crop_text_mask(det, crop, by0, by1)
                back = _lama_inpaint(sess, img_name, mask_name, crop, m)
                band = back[y0 - cy0:y1 - cy0]
                last_small = small
                last_band = band
                since_inpaint = 0
                inpainted += 1
            frame[y0:y1] = band
            try:
                proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                pipe_broken = True
                break
            n += 1
            if cb and total and n % 5 == 0:
                cb(18 + int(77 * n / total),
                   f"AI inpainting frames ({n}/{total})")
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=120)
        except Exception:
            proc.kill()
        logf.close()
    if pipe_broken:
        raise RuntimeError("AI inpainting failed: the video encoder "
                           "stopped early.")
    if proc.returncode != 0 or n == 0:
        tail = ""
        try:
            with open(log_path, errors="replace") as f:
                tail = f.read()[-300:]
        except OSError:
            pass
        raise RuntimeError("AI inpainting failed. " + tail)
    if cb:
        cb(97, f"AI inpainting done ({inpainted} of {n} frames "
               f"newly inpainted)")
    try:
        os.remove(log_path)
    except OSError:
        pass


def _remove_captions_temporal(src, out, W, H, y0, y1, duration, cb, tier):
    """Temporal inpainting (the smart default).

    Per-frame LaMa flickers because every frame is rebuilt from
    scratch, and it is far too slow to run on every frame anyway.
    Instead this engine builds ONE clean "plate" of the caption area
    with LaMa, then TRACKS it through the following frames with dense
    optical flow: the plate is warped along with the moving scene, so
    the fill is temporally stable (no shimmer, no ghost letters when
    the words change - the plate is text-free everywhere, so new
    words sit on the same clean background). Caption changes do NOT
    force a new plate; only background motion does: when the
    forward-backward flow check says the warp can no longer be
    trusted (scene cut, violent motion, occlusion), the plate is
    rebuilt from the current frame. Static shots ride one plate for
    a long stretch, so LaMa runs a handful of times per minute of
    video instead of on every frame.
    """
    import cv2
    import numpy as np

    if cb:
        cb(14, "Loading AI inpainting model")
    sess, img_name, mask_name = _lama_session()
    try:
        det = _get_text_detector(cb)
    except Exception:
        det = None  # masks degrade to top-hat / band rectangle

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError("Could not open the video for AI "
                           "inpainting.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    pad = max(16, y1 - y0)
    cy0 = max(0, y0 - pad)
    cy1 = min(H, y1 + pad)
    ch = cy1 - cy0
    by0, by1 = y0 - cy0, y1 - cy0

    # Optical flow runs on a downscaled copy of the working crop.
    fw = min(W, 480)
    fs = fw / float(W)
    fh = max(2, int(round(ch * fs)))
    grid_x, grid_y = np.meshgrid(
        np.arange(W, dtype=np.float32),
        np.arange(ch, dtype=np.float32))
    max_age_fast = int(6 * fps)      # plate life while the scene moves
    max_age_slow = int(10 * fps)     # ... and while it barely does
    disp_budget = 0.07 * fw          # total scene travel a plate may
    #                                  follow before it is rebuilt -
    #                                  bounds zoom/pan warp drift

    log_path = out + ".ffmpeg.log"
    logf = open(log_path, "wb")
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{W}x{H}", "-r", f"{fps:.3f}", "-i", "-",
         "-i", src, "-map", "0:v", "-map", "1:a?",
         ] + _encode_args(tier) + [
         "-c:a", "aac", "-b:a", "192k", "-shortest", out],
        stdin=subprocess.PIPE, stdout=logf, stderr=logf)

    plate = None          # clean (text-free) working crop, full res
    prev_small = None     # previous frame's crop, flow resolution
    prev_mask = None      # previous frame's text mask (crop coords)
    prev_had_text = False
    plate_age = 0
    cum_disp = 0.0        # scene travel since the plate was built
    motion_hist = []      # recent mean |flow| values (flow pixels)
    mask_ocr = None       # latest OCR mask, crop coordinates
    since_ocr = 10 ** 9
    ocr_every = max(15, min(45, int(round(fps))))  # ~1 s windows
    # Per-window text verdict from the OCR refresh: "wide" = text
    # spread across the band (captions / overlay strip - full mask
    # logic), "narrow" = one localised patch (a corner logo - paint
    # only at the OCR-confirmed boxes), "none" = OCR saw no text in
    # the band at all (pass the frames through untouched; texture
    # alone must never trigger painting - see the mask logic below).
    win_mode = "wide"
    n = plates = warps = 0
    pipe_broken = False
    try:
        if cb:
            cb(18, "AI temporal inpainting frames")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[cy0:cy1]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (fw, fh),
                               interpolation=cv2.INTER_AREA)

            # ---- text mask for this frame --------------------------
            if det is not None and since_ocr >= ocr_every:
                try:
                    boxes = det.detect(frame, max_side=768)
                    # caption text only: boxes centred in the band
                    # rows (scene text elsewhere is not a caption)
                    boxes = [b for b in boxes
                             if y0 - 12 <= (b[1] + b[3]) / 2.0
                             <= y1 + 12]
                    mask_ocr = _rasterize_boxes(boxes, (ch, W),
                                                oy=cy0)
                    # Classify the window by how the OCR evidence
                    # spreads over the band: coverage of the band
                    # area and of its columns. A corner logo covers
                    # ~0.30 of the columns; captions and the bodycam
                    # strip span nearly all of them.
                    band_area = float(max(1, (by1 - by0) * W))
                    cov = (float(mask_ocr[by0:by1].sum())
                           / band_area)
                    cols = float((mask_ocr[by0:by1].sum(axis=0)
                                  > 0).mean())
                    if not boxes:
                        win_mode = "none"
                    elif cols >= 0.38 or cov >= 0.45:
                        win_mode = "wide"
                    else:
                        win_mode = "narrow"
                except Exception:
                    mask_ocr = None
                since_ocr = 0
            since_ocr += 1
            gate_closed = det is not None and win_mode == "none"
            th = _tophat_text_mask(gray)
            near = np.zeros((ch, W), np.float32)
            ny0 = max(0, by0 - (by1 - by0) // 2)
            ny1 = min(ch, by1 + (by1 - by0) // 2)
            near[ny0:ny1] = 1.0
            th = th * near
            if gate_closed:
                # OCR found no text in the band this window: any
                # mask would come from texture alone (wood grain,
                # photo detail fires the top-hat hard), and painting
                # the plate over a text-free stretch leaves a
                # smeared strip behind. No mask, no paint.
                mask = np.zeros((ch, W), np.float32)
            elif det is not None and win_mode == "narrow" \
                    and mask_ocr is not None:
                # Only localised text this window (e.g. a corner
                # logo in a photo segment): paint only at/near the
                # OCR-confirmed boxes instead of wherever the
                # top-hat signal fires across the band.
                gate = cv2.dilate(mask_ocr,
                                  np.ones((41, 41), np.float32))
                mask = np.maximum(mask_ocr, th) * gate
            elif mask_ocr is not None:
                a_th = float(th[by0:by1].sum())
                a_ocr = float(mask_ocr[by0:by1].sum())
                if a_ocr <= 1.0 or a_th > 1.4 * a_ocr:
                    mask = np.maximum(mask_ocr, th)
                elif a_th < 0.3 * a_ocr:
                    # caption shrank/vanished: keep OCR pixels only
                    # near the text that is actually there now
                    gate = cv2.dilate(th, np.ones((31, 31),
                                                  np.float32))
                    mask = np.maximum(th, mask_ocr * gate)
                else:
                    mask = np.maximum(mask_ocr, th)
            else:
                mask = th
            if float(mask.sum()) < 0.0005 * ch * W:
                mask = np.zeros((ch, W), np.float32)
                mask[by0:by1] = 1.0
                have_text = False
            else:
                mask = cv2.dilate(mask, np.ones((5, 5), np.float32))
                have_text = True

            # ---- plate: build or track -----------------------------
            rebuild = plate is None
            flow = None
            if prev_small is not None and not rebuild \
                    and not gate_closed:
                flow = cv2.calcOpticalFlowFarneback(
                    prev_small, small, None, 0.5, 3, 21, 3, 5, 1.2, 0)
                mag = float(np.abs(flow).mean())
                cum_disp += mag
                motion_hist.append(mag)
                if len(motion_hist) > 30:
                    motion_hist.pop(0)
                # forward-backward consistency: warp the backward
                # flow by the forward flow; a trustworthy warp nearly
                # cancels out.
                flow_b = cv2.calcOpticalFlowFarneback(
                    small, prev_small, None, 0.5, 3, 21, 3, 5, 1.2, 0)
                yy, xx = np.mgrid[0:fh, 0:fw].astype(np.float32)
                mx = (xx + flow[:, :, 0]).astype(np.float32)
                my = (yy + flow[:, :, 1]).astype(np.float32)
                bw = cv2.remap(flow_b, mx, my, cv2.INTER_LINEAR)
                fb_err = float(np.median(np.abs(flow + bw).reshape(-1)))
                hist = cv2.calcHist([small], [0], None, [64],
                                    [0, 256]).flatten()
                hist_p = cv2.calcHist([prev_small], [0], None, [64],
                                      [0, 256]).flatten()
                denom = float(np.linalg.norm(hist)
                              * np.linalg.norm(hist_p)) or 1.0
                hist_corr = float(hist @ hist_p / denom)
                low_motion = (float(np.mean(motion_hist)) < 0.2
                              if motion_hist else False)
                age_limit = max_age_slow if low_motion else max_age_fast
                if fb_err > 1.2 or mag > 0.16 * fw or hist_corr < 0.55 \
                        or plate_age > age_limit \
                        or cum_disp > disp_budget:
                    rebuild = True
            if gate_closed:
                # Text-free window: no plate work and no painting -
                # the frame passes through untouched. Drop the plate
                # so text returning later is filled from a fresh
                # plate built on a text-bearing frame, never from a
                # stale plate carried across a scene change.
                plate = None
            elif rebuild:
                # A plate must be text-free EVERYWHERE text could
                # appear, not just where this frame's mask looked:
                # build it from the union of this frame's and the
                # previous frame's masks, dilated wide. A partial
                # mask would bake the missed letters INTO the plate
                # and ghost them into every following frame.
                build_mask = mask
                if prev_mask is not None:
                    build_mask = np.maximum(build_mask, prev_mask)
                build_mask = cv2.dilate(
                    build_mask, np.ones((9, 9), np.float32))
                plate = _lama_inpaint(sess, img_name, mask_name,
                                      crop, build_mask)
                plates += 1
                # Verify the plate really is text-free; if letters
                # survived the inpaint, widen the mask with what is
                # still text-like in the plate and inpaint once more.
                pgray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
                pth = _tophat_text_mask(pgray) * near
                if float(pth[by0:by1].sum()) > 0.35 * max(
                        1.0, float(build_mask[by0:by1].sum())):
                    build_mask = cv2.dilate(
                        np.maximum(build_mask, pth),
                        np.ones((9, 9), np.float32))
                    plate = _lama_inpaint(sess, img_name, mask_name,
                                          crop, build_mask)
                    plates += 1
                plate_age = 0
                cum_disp = 0.0
            else:
                fup = cv2.resize(flow, (W, ch),
                                   interpolation=cv2.INTER_LINEAR)
                fup[:, :, 0] *= (W / float(fw))
                fup[:, :, 1] *= (ch / float(fh))
                plate = cv2.remap(
                    plate,
                    (grid_x - fup[:, :, 0]).astype(np.float32),
                    (grid_y - fup[:, :, 1]).astype(np.float32),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                warps += 1
                plate_age += 1
            prev_small = small
            prev_mask = mask if have_text else None
            prev_had_text = have_text

            # ---- composite the clean plate under the text ----------
            # Burned-in captions are a screen-fixed overlay, so the
            # previous frame's real mask stays valid for this frame
            # too: unioning it patches the frames where motion blur
            # makes the text itself hard to detect (the classic
            # ghost-letter moment is a caption change mid-whip-pan).
            eff = mask
            if prev_mask is not None and (have_text or prev_had_text):
                eff = np.maximum(mask, prev_mask) if have_text \
                    else prev_mask
            if have_text or prev_had_text:
                alpha = eff[by0:by1]
                k = 9 if (motion_hist and motion_hist[-1] > 1.2) else 5
                alpha = cv2.dilate(alpha, np.ones((k, k), np.float32))
                alpha = cv2.GaussianBlur(alpha, (9, 9), 0)
                # saturate: weakly-covered pixels (faint italic
                # lines, anti-aliased edges) must still be FULLY
                # replaced, or they survive as readable fragments
                alpha = np.clip(alpha * 1.5, 0.0, 1.0)[:, :, None]
                band = frame[y0:y1].astype(np.float32)
                fill = plate[by0:by1].astype(np.float32)
                frame[y0:y1] = (alpha * fill
                                + (1.0 - alpha) * band).astype(np.uint8)
            try:
                proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                pipe_broken = True
                break
            n += 1
            if cb and total and n % 10 == 0:
                cb(18 + int(77 * n / total),
                   f"AI temporal inpainting frames ({n}/{total})")
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=120)
        except Exception:
            proc.kill()
        logf.close()
    if pipe_broken:
        raise RuntimeError("AI temporal inpainting failed: the video "
                           "encoder stopped early.")
    if proc.returncode != 0 or n == 0:
        tail = ""
        try:
            with open(log_path, errors="replace") as f:
                tail = f.read()[-300:]
        except OSError:
            pass
        raise RuntimeError("AI temporal inpainting failed. " + tail)
    if cb:
        cb(97, f"AI temporal done ({plates} clean plates rebuilt, "
               f"{warps} frames tracked by motion)")
    try:
        os.remove(log_path)
    except OSError:
        pass


def _detect_band_ocr(video_path, samples=18):
    """OCR caption-band detection: run the text detector over frames
    sampled across the video (decoded sequentially - random seeks
    are far slower), cluster the boxes by vertical proximity, and
    keep every cluster that behaves like captions: WIDE, confident
    text in a THIN strip in the lower frame. Presence share does NOT
    gate qualification - dialogue captions come in bursts (a few
    seconds in a long clip), so a caption cluster may appear in only
    a couple of samples while an always-on overlay appears in all of
    them; gating on persistence picks the overlay and leaves the
    dialogue behind. All qualifying clusters are unioned into one
    band. Returns the detect dict, or None when sampling failed."""
    import cv2
    import numpy as np
    det = _get_text_detector()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not total or not height:
        cap.release()
        return None
    targets = set(np.linspace(int(total * 0.04), int(total * 0.96),
                              samples).astype(int).tolist())
    per_sample = []
    idx = -1
    while True:
        if not cap.grab():
            break
        idx += 1
        if idx not in targets:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            continue
        boxes = det.detect(frame, max_side=768)
        per_sample.append([(x0 / width, y0 / height, x1 / width,
                            y1 / height, s)
                           for (x0, y0, x1, y1, s) in boxes])
    cap.release()
    if len(per_sample) < 5:
        return None
    all_boxes = [b for bs in per_sample for b in bs]
    if not all_boxes:
        return {"found": False, "method": "ocr", "boxes": 0}
    # Caption-likeness clustering: group the boxes themselves by
    # vertical proximity (boxes whose rows overlap or nearly touch
    # chain together - the lines of one caption block, or a logo
    # sitting on the metadata strip), then judge each cluster the
    # way a person would: captions are WIDE, confident text in a
    # THIN strip (a line or two, never a third of the frame) in the
    # lower half. Presence share is only a small ranking bonus: the
    # previous scoring required a cluster in a third of all samples,
    # which dialogue bursts (2 of 14 samples in the QA bodycam clip)
    # can never reach, while the permanent overlay sailed through -
    # auto mode erased the bodycam clock strip and left both spoken
    # caption lines fully legible. A stack of scene text (a document
    # shown on screen) chains into one TALL cluster and is rejected
    # by the height gate; top-of-frame titles and watermarks fall
    # outside the caption zone.
    cand = []
    for si, bs in enumerate(per_sample):
        for b in bs:
            if (b[2] - b[0]) >= 0.20 and b[4] >= 0.60:
                cand.append((si, b))
    if not cand:
        return {"found": False, "method": "ocr",
                "boxes": len(all_boxes)}
    cand.sort(key=lambda sb: (sb[1][1] + sb[1][3]) / 2.0)
    clusters = []
    for si, b in cand:
        if clusters and b[1] - clusters[-1]["bot"] <= 0.030:
            c = clusters[-1]
        else:
            c = {"top": b[1], "bot": b[3], "boxes": []}
            clusters.append(c)
        c["top"] = min(c["top"], b[1])
        c["bot"] = max(c["bot"], b[3])
        c["boxes"].append((si, b))
    n_samples = float(len(per_sample))
    scored = []
    for c in clusters:
        boxes = c["boxes"]
        cheight = c["bot"] - c["top"]
        center = (c["top"] + c["bot"]) / 2.0
        present = sorted({si for si, _b in boxes})
        n_present = len(present)
        width_med = float(np.median([b[2] - b[0] for _s, b in boxes]))
        score_med = float(np.median([b[4] for _s, b in boxes]))
        areas = [sum((b[2] - b[0]) * (b[3] - b[1])
                     for s2, b in boxes if s2 == si)
                 for si in present]
        area_med = float(np.median(areas))
        density = area_med / max(cheight, 1e-6)
        qualifies = (
            cheight <= 0.30
            and center >= 0.45
            and width_med >= 0.22
            and score_med >= 0.70
            and density >= 0.10
            and (n_present >= 2
                 or (width_med >= 0.40 and score_med >= 0.90
                     and area_med >= 0.015)))
        if not qualifies:
            continue
        share = n_present / n_samples
        score = (width_med * score_med * float(np.sqrt(area_med))
                 * (0.55 + 0.45 * share))
        scored.append((score, c["top"], c["bot"]))
    if not scored:
        return {"found": False, "method": "ocr",
                "boxes": len(all_boxes)}
    # Union every qualifying cluster into ONE band (the dialogue
    # lines AND the overlay strip beneath them - the band the manual
    # control run proved ghost-free). Clusters join in score order
    # only while the union stays under ~half the frame height, so
    # one stray cluster cannot stretch the band over the picture.
    scored.sort(key=lambda s: -s[0])
    best_score = scored[0][0]
    top_u, bot_u = scored[0][1], scored[0][2]
    for s in scored[1:]:
        nt = min(top_u, s[1])
        nb = max(bot_u, s[2])
        if nb - nt <= 0.52:
            top_u, bot_u = nt, nb
    m = 0.022
    top = max(0.0, top_u - m)
    bot = min(1.0, bot_u + m)
    return {"found": True,
            "top_pct": round(100.0 * top, 1),
            "bottom_pct": round(100.0 * bot, 1),
            "method": "ocr",
            "score": round(float(best_score), 3),
            "boxes": len(all_boxes)}


def detect_caption_band(video_path, samples=24):
    """Find where burned-in captions sit, by itself. OCR text
    detection first (it reads the actual letters); the older
    edge-persistence heuristic is only a second opinion for when the
    OCR model cannot be loaded or sees text it cannot cluster.
    found=False means 'no caption band we trust' - callers fall back
    to the standard bottom band and say so."""
    try:
        r = _detect_band_ocr(video_path, samples=min(samples, 18))
        if r is not None:
            if r.get("found") or not r.get("boxes"):
                return r
            edge = _detect_band_edges(video_path, samples=samples)
            return edge if edge.get("found") else r
    except Exception:
        pass
    return _detect_band_edges(video_path, samples=samples)


def _detect_band_edges(video_path, samples=24):
    """Find where burned-in captions sit in the frame, by itself.

    Samples frames across the video and looks for the horizontal band
    whose rows carry dense, persistent, center-spread edges - the
    signature of caption text (scene edges move around; caption rows
    stay put while the words change). Returns {"found": bool, ...};
    when found, top_pct/bottom_pct give the band (with a safety
    margin). Conservative gates: a video with no captions should come
    back found=False so callers can fall back honestly.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"found": False}
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not total or not height:
        cap.release()
        return {"found": False}
    idxs = np.linspace(int(total * 0.04), int(total * 0.96),
                       samples).astype(int)
    edge_frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if width > 320:
            gray = cv2.resize(gray,
                              (320, max(2, int(height * 320 / width))))
        edge_frames.append(cv2.Canny(gray, 50, 150) > 0)
    cap.release()
    if len(edge_frames) < 5:
        return {"found": False}
    E = np.stack(edge_frames).astype(np.float32)
    _n, h, w = E.shape
    prof = E.mean(axis=2)                       # per-frame row density
    mean_prof = prof.mean(axis=0)
    persist = (prof > 0.03).mean(axis=0)        # rows edgy in many frames
    score = mean_prof * (0.35 + 0.65 * persist)
    k = max(3, int(h * 0.015)) | 1
    ss = np.convolve(score, np.ones(k) / k, mode="same")
    peak = float(ss.max())
    if peak < 0.16:
        return {"found": False, "peak": round(peak, 3)}
    # Candidate bands = clusters of strong rows. A busy static scene
    # region can outscore the captions, so EVERY candidate must pass
    # the text gates on its own; the best passing one wins.
    above = ss > 0.45 * peak
    clusters = []
    i = 0
    while i < h:
        if above[i]:
            j = i
            while j < h and above[j]:
                j += 1
            clusters.append((i, j))
            i = j
        else:
            i += 1
    ctx = int(h * 0.03)
    good = []
    for a, b in clusters:
        y0 = max(0, a - ctx)
        y1 = min(h, b + ctx)
        if y1 - y0 < int(h * 0.035):
            c = (y0 + y1) // 2
            half = int(h * 0.02)
            y0 = max(0, c - half)
            y1 = min(h, c + half + 1)
        if y1 - y0 > int(h * 0.30):
            c = (y0 + y1) // 2
            half = int(h * 0.15)
            y0 = max(0, c - half)
            y1 = min(h, c + half)
        band = E[:, y0:y1, int(w * 0.1):int(w * 0.9)]
        if band.size == 0:
            continue
        colprof = band.mean(axis=(0, 1))
        mid_share = float(
            colprof[int(len(colprof) * 0.2):int(len(colprof) * 0.8)].sum()
            / max(float(colprof.sum()), 1e-9))
        bpeak = float(ss[y0:y1].max())
        if bpeak >= 0.16 and mid_share >= 0.72:
            good.append((bpeak * mid_share, y0, y1, mid_share))
    if not good:
        return {"found": False, "peak": round(peak, 3)}
    good.sort(reverse=True)
    _s, y0, y1, mid_share = good[0]
    # trim quiet rows at the band edges, then add a safety margin
    thr = 0.45 * float(ss[y0:y1].mean())
    while y0 < y1 - 2 and ss[y0] < thr:
        y0 += 1
    while y1 > y0 + 2 and ss[y1 - 1] < thr:
        y1 -= 1
    m = int(h * 0.022)
    y0 = max(0, y0 - m)
    y1 = min(h, y1 + m)
    return {"found": True,
            "top_pct": round(100.0 * y0 / h, 1),
            "bottom_pct": round(100.0 * y1 / h, 1),
            "peak": round(peak, 3),
            "mid_share": round(mid_share, 3)}


def find_preview_section(video_path, region, seconds=5.0):
    """Pick the most honest few seconds to preview: a caption CHANGE
    on a busy background - exactly where ghosting and flicker would
    show. Scans ~1 frame per second; returns (start_sec, end_sec)."""
    import cv2
    import numpy as np
    W, H, _f = probe_video(video_path)
    duration = probe_duration(video_path) or 0.0
    if not W or not H or not duration:
        return (0.0, min(seconds, duration or seconds))
    y0, y1 = _remover_band(region, H)
    band_h = max(2, y1 - y0)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return (0.0, min(seconds, duration))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps)))
    try:
        det = _get_text_detector()
    except Exception:
        det = None  # picker falls back to the top-hat score alone
    secs = []
    idx = -1
    while True:
        if not cap.grab():
            break
        idx += 1
        if idx % step:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            continue
        t = idx / fps
        gray = cv2.cvtColor(frame[y0:y1], cv2.COLOR_BGR2GRAY)
        m = _tophat_text_mask(gray)
        ctx = frame[max(0, y0 - band_h):y0]
        busy = 0.0
        if ctx.size:
            busy = float((cv2.Canny(cv2.cvtColor(
                ctx, cv2.COLOR_BGR2GRAY), 50, 150) > 0).mean())
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                           (64, 36))
        hist = cv2.calcHist([small], [0], None, [64],
                            [0, 256]).flatten()
        bmask = None
        if det is not None:
            try:
                boxes = det.detect(frame, max_side=768)
                boxes = [b for b in boxes
                         if y0 - 12 <= (b[1] + b[3]) / 2.0 <= y1 + 12]
                bmask = _rasterize_boxes(boxes, (band_h, W), oy=y0)
            except Exception:
                bmask = None
        secs.append((t, m, busy, hist, bmask))
    cap.release()
    if not secs:
        return (0.0, min(seconds, duration))
    # OCR-guided change: pixels covered by text boxes in ~every
    # sampled second are permanent overlay furniture (logo, clock
    # strip); what is left is the DYNAMIC text - the dialogue
    # captions. A caption change is the top-hat signal changing
    # inside the dynamic boxes; camera motion elsewhere in the band
    # (the old score's downfall on bodycam footage: a pan through a
    # caption-free stretch outscored the actual line change) never
    # creates OCR boxes, so it cannot win here.
    dyn = None
    if det is not None and all(s[4] is not None for s in secs):
        occ = np.zeros((band_h, W), np.float32)
        for s in secs:
            # dilate before accumulating: detector boxes jitter by a
            # few pixels, and a permanent logo's jittered edges must
            # still count as permanent, not as dynamic text
            occ += (cv2.dilate(s[4], np.ones((9, 9), np.float32))
                    > 0)
        occ /= float(len(secs))
        dyn = [s[4] * (occ < 0.8) for s in secs]
        if not any(float(d.sum()) > 0 for d in dyn):
            dyn = None
    band_area = float(max(1, band_h * W))
    best_t, best_score = 0.0, -1.0
    for i, (t, m, busy, hist, bmask) in enumerate(secs):
        change = 0.0
        if i > 0:
            pm, phist = secs[i - 1][1], secs[i - 1][3]
            if dyn is not None:
                gate = cv2.dilate(
                    np.maximum(dyn[i - 1], dyn[i]),
                    np.ones((15, 15), np.float32))
                a = pm * gate
                b = m * gate
                sa, sb = float(a.sum()), float(b.sum())
                if sa > 0 and sb > 0:
                    inter = float((np.minimum(a, b)).sum())
                    union = float((np.maximum(a, b)).sum()) or 1.0
                    change = 1.0 - inter / union
                elif sa + sb > 0:
                    change = 1.0  # text appeared / vanished
                # ...unless the whole frame changed with it: text
                # appearing/vanishing AT a scene cut is the cut,
                # not a caption change (histogram correlation - the
                # temporal engine's cut signal).
                denom = float(np.linalg.norm(hist)
                              * np.linalg.norm(phist)) or 1.0
                if float(hist @ phist / denom) < 0.55:
                    change = 0.0
                # Weight by how much text actually changed hands: a
                # caption line swapping involves thousands of text
                # pixels on both sides; a name strap flickering or
                # detector jitter involves a few hundred, and must
                # not outscore the real caption change.
                mass = min(sa, sb) / band_area
                change *= min(1.0, float(np.sqrt(mass / 0.06)))
                # ...and by how long the dynamic text has been on
                # screen around the change: a caption track runs for
                # seconds, a name strap or a stray graphic flickers
                # for one sampled second.
                present = [float(d.sum()) > 400.0 for d in dyn]
                run = 0
                j = i - 1
                while j >= 0 and present[j]:
                    run += 1
                    j -= 1
                j = i
                while j < len(present) and present[j]:
                    run += 1
                    j += 1
                change *= min(1.0, run / 3.0)
            elif pm.shape == m.shape:
                inter = float((m * pm).sum())
                union = float(((m + pm) > 0).sum()) or 1.0
                change = 1.0 - inter / union
                # A scene CUT changes the band mask completely too,
                # but it says nothing about caption-change quality.
                # Discount seconds whose whole frame changed
                # (histogram correlation - the temporal engine's cut
                # signal): a caption change leaves the scene's
                # histogram nearly untouched.
                denom = float(np.linalg.norm(hist)
                              * np.linalg.norm(phist)) or 1.0
                if float(hist @ phist / denom) < 0.55:
                    change = 0.0
        score = change * (0.4 + busy * 8.0)
        if float(m.sum()) > 0:
            score += 0.05
        if score > best_score:
            best_t, best_score = t, score
    start = max(0.0, best_t - 1.5)
    if start + seconds > duration:
        start = max(0.0, duration - seconds)
    return (start, min(duration, start + seconds))


def preview_caption_removal(video_path, region, mode, out_path,
                            progress_cb=None, quality_tier="maximum",
                            seconds=5.0):
    """Render only the worst ~5 s (see find_preview_section) with the
    chosen removal engine, so quality can be judged before a full
    run. Returns (out_path, start_sec, end_sec)."""
    import shutil
    import tempfile
    start, end = find_preview_section(video_path, region, seconds)
    workdir = tempfile.mkdtemp(prefix="crprev_")
    try:
        seg = os.path.join(workdir, "segment.mp4")
        cmd = (["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i",
                video_path, "-t", f"{max(0.5, end - start):.2f}"]
               + _encode_args(quality_tier)
               + ["-c:a", "aac", "-b:a", "192k", seg])
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        if r.returncode != 0 or not os.path.isfile(seg):
            raise RuntimeError("Could not cut the preview segment: "
                               + (r.stderr or "")[-200:])
        remove_captions(seg, region, mode, out_path,
                        progress_cb=progress_cb,
                        quality_tier=quality_tier)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out_path, start, end
