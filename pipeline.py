"""Itraji Cliper - local video processing pipeline.

Stages: yt-dlp download -> faster-whisper transcription ->
Built-in AI (local model) viral-moment detection -> ffmpeg 9:16 render
with karaoke captions and hook overlay.

All code is original, written for this project.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time

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
# Linux/macOS: register the bundled fonts folder with fontconfig for
# child processes. ffmpeg's subtitles filter only uses fontsdir for
# NAMED fonts - glyph FALLBACK (the path caption emoji take, since the
# caption fonts contain no emoji) goes through fontconfig, which has
# never heard of the bundled folder, so on a machine whose only emoji
# font is a bitmap color font libass cannot rasterize, every caption
# emoji renders as a tofu box even with a good emoji font bundled.
# A generated config that includes the system config plus the bundled
# folder fixes fallback for every child call. FONTS_DIR is wired by
# app.py after import, so it is read lazily at call time.
# ---------------------------------------------------------------------------
if os.name == "posix":
    _sp_run_posix = subprocess.run
    _sp_popen_posix = subprocess.Popen
    _fc_conf_for = [None, None]  # [fonts_dir, config_path]

    def _fontconfig_file():
        fonts_dir = globals().get("FONTS_DIR") or ""
        if not fonts_dir or not os.path.isdir(fonts_dir):
            return None
        if not os.path.isfile("/etc/fonts/fonts.conf"):
            return None
        if _fc_conf_for[0] != fonts_dir:
            try:
                path = os.path.join(tempfile.gettempdir(),
                                    "itraji-fonts.conf")
                with open(path, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0"?>\n'
                            '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
                            "<fontconfig>\n"
                            '  <include ignore_missing="yes">'
                            "/etc/fonts/fonts.conf</include>\n"
                            "  <dir>" + fonts_dir + "</dir>\n"
                            "</fontconfig>\n")
                _fc_conf_for[0], _fc_conf_for[1] = fonts_dir, path
            except OSError:
                return None
        return _fc_conf_for[1]

    def _fontconfig_env(kwargs):
        # Respect an explicit caller env and a user's own config.
        if kwargs.get("env") is not None:
            return
        if os.environ.get("FONTCONFIG_FILE"):
            return
        conf = _fontconfig_file()
        if conf:
            env = dict(os.environ)
            env["FONTCONFIG_FILE"] = conf
            kwargs["env"] = env

    def _run_fontconfig(*args, **kwargs):
        _fontconfig_env(kwargs)
        return _sp_run_posix(*args, **kwargs)

    class _PopenFontconfig(_sp_popen_posix):
        def __init__(self, *args, **kwargs):
            _fontconfig_env(kwargs)
            super().__init__(*args, **kwargs)

    subprocess.run = _run_fontconfig
    subprocess.Popen = _PopenFontconfig


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
        # simple linear resample: the output grid must hold the
        # TARGET sample count (len * 16000 / sr) mapped onto input
        # indices. The old grid was built over the input count, so a
        # 48 kHz file came back with ~48001 "16 kHz" samples - 3x
        # stretched, with every downstream timestamp wrong. (Latent:
        # the only production caller forces 16 kHz via ffmpeg.)
        import numpy as np2  # noqa: F811
        n_out = int(len(audio) * 16000 / sr)
        if n_out > 0 and len(audio) > 1:
            audio = np2.interp(
                np2.linspace(0, len(audio) - 1, n_out),
                np2.arange(len(audio)), audio)
        elif n_out > 0:
            audio = np2.resize(audio, n_out)
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

    # Digital silence (a muted capture, a silent export) carries no
    # signal at all, yet the speech model still hallucinates words
    # over it - stamped minutes past the end of the file. No signal
    # means no speech: return an empty transcript, which every caller
    # already reports honestly.
    import numpy as np
    if not len(audio) or float(np.abs(audio).max()) < 1e-4:
        if progress_cb:
            progress_cb(100, "Transcription complete (0 words)")
        return []

    lang = None if language in (None, "auto", "") else language
    audio_dur = len(audio) / 16000.0

    # Progress is reported as segments ARRIVE. faster-whisper yields
    # segments lazily while it decodes, and each segment carries its
    # end timestamp, so seg.end / audio_dur is real decode progress on
    # the same 0-100 scale (15 -> 95 here, 100 at the end). The old
    # code materialised list(segments) BEFORE this loop, so the
    # callback sat at 15% for the entire decode - 843 s flat on a 2 h
    # file - and then jumped straight to 100%.
    _pct = {"last": 15}

    def _collect(segments):
        out = []
        for seg in segments:
            if progress_cb and audio_dur > 0:
                pct = 15 + int(min(seg.end, audio_dur) / audio_dur * 80)
                if pct > _pct["last"]:
                    _pct["last"] = pct
                    progress_cb(pct, "Transcribing audio")
            if seg.words:
                for w in seg.words:
                    out.append(
                        {"start": float(w.start), "end": float(w.end),
                         "word": w.word.strip()}
                    )
            else:
                # fall back to segment-level timing split evenly
                # across tokens
                tokens = seg.text.strip().split()
                if not tokens:
                    continue
                dur = (seg.end - seg.start) / len(tokens)
                for j, tok in enumerate(tokens):
                    out.append({
                        "start": seg.start + j * dur,
                        "end": seg.start + (j + 1) * dur,
                        "word": tok,
                    })
        return out

    try:
        segments, _info = model.transcribe(
            audio, word_timestamps=True, vad_filter=True, language=lang)
        words = _collect(segments)
    except Exception:
        # The fallback must cover DECODE-TIME failures too:
        # model.transcribe() returns a lazy generator, so most real
        # errors surface while _collect iterates it, not at the call.
        # The old try wrapped only the call (its fallback never
        # engaged for the common failure) and the retry silently
        # dropped BOTH the VAD filter and the requested language.
        # Retry once without VAD - the usual culprit - keeping the
        # language, and say so on the progress line. Partial words
        # from the failed attempt are discarded, never mixed in.
        if progress_cb:
            progress_cb(_pct["last"],
                        "Transcribing audio (retrying without "
                        "voice filtering)")
        segments, _info = model.transcribe(
            audio, word_timestamps=True, vad_filter=False,
            language=lang)
        words = _collect(segments)
    words = [w for w in words if w["word"]]
    # Word timings must live inside the audio they came from. On
    # near-silent or pathological input the decoder can emit words
    # stamped far past the end of the file, and every downstream
    # window (detection, captions, exports) trusts those numbers.
    audio_dur = len(audio) / 16000.0
    bounded = []
    for w in words:
        if w["start"] >= audio_dur:
            continue
        w["end"] = min(w["end"], audio_dur)
        bounded.append(w)
    words = bounded
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


def _grow_to_min(s, e, min_s, lo, hi):
    """Grow [s, e] toward min_s inside [lo, hi], allocating the
    growth in passes (half left, rest right, remainder back left) -
    the same allocation the offline detector's F3 fix uses, so no
    share is silently discarded at a bound. Returns (s, e); the
    result reaches min_s whenever [lo, hi] is at least min_s wide."""
    need = min_s - (e - s)
    if need <= 0:
        return s, e
    grow_l = min(need / 2.0, s - lo)
    s -= grow_l
    need -= grow_l
    grow_r = min(need, hi - e)
    e += grow_r
    need -= grow_r
    if need > 0:
        grow_l = min(need, s - lo)
        s -= grow_l
    return s, e


def _parse_local_moments(data, clip_max_s, clip_min_s=0.0,
                         span_lo=0.0, span_hi=None):
    """Validate/normalize one reply's moments - same field rules as
    detect_moments(); virality capped at 99 like the offline path.

    clip_min_s is enforced like the offline detector enforces it:
    the model was ASKED for [clip_min_s, clip_max_s] but its raw
    timestamps were previously trusted, so a 30-60s request could
    return 4s clips. Sub-minimum moments are grown inside the
    transcript span they were picked from ([span_lo, span_hi]);
    a moment that cannot reach clip_min_s even across the whole
    span is dropped - except when the whole span is shorter than
    clip_min_s, where the full span is the honest answer."""
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
            if clip_min_s and end - start < clip_min_s:
                hi = span_hi if span_hi is not None else end
                start, end = _grow_to_min(start, end, clip_min_s,
                                          span_lo, hi)
                if end - start < clip_min_s - 0.01:
                    if hi - span_lo < clip_min_s:
                        start, end = span_lo, hi
                    else:
                        continue
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
                         max_clips=8, progress_cb=None,
                         category="general", audio_profile=None):
    """Find viral moments with the built-in local AI.

    Mirrors detect_moments() inputs and output shape, but the model
    (Qwen2.5-14B via llama.cpp, see local_ai.py) runs on this machine -
    no key. Raises RuntimeError with a plain message when the model
    is not downloaded yet or the engine fails; never invents moments.

    A content category appends its focus line to the prompt; the
    Phase-1 subscores attached afterwards stay signal-derived (the
    model's own virality/hook/reason are untouched).
    """
    import local_ai
    category, _cat = category_info(category)

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
        if _cat["focus"]:
            prompt += ("\n\nContent category: " + _cat["label"]
                       + ". " + _cat["focus"])
        reply = local_ai.chat(
            [{"role": "user", "content": prompt}], progress_cb)
        try:
            data = local_ai.extract_json(reply)
        except ValueError as e:
            raise RuntimeError(
                "Built-in AI returned an answer that could not be "
                f"read ({e}). Please run detection again.") from e
        # Growth span for sub-minimum moments: the chunk the model
        # picked from - or the whole transcript when the chunk
        # itself is shorter than clip_min_s (a tiny final chunk
        # must not cap a moment below the requested minimum).
        if cwords and (cwords[-1]["end"] - cwords[0]["start"]
                       >= clip_min_s or not words):
            span = (cwords[0]["start"], cwords[-1]["end"])
        elif words:
            span = (words[0]["start"], words[-1]["end"])
        else:
            span = (0.0, None)
        moments.extend(_parse_local_moments(
            data, clip_max_s, clip_min_s,
            span_lo=span[0], span_hi=span[1]))

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
    # Explanation chips measured from each moment's own transcript
    # window - they accompany the model's scores and reason (which
    # stay the model's), they never replace or invent them.
    prev_content = set()
    for m in sorted(picked, key=lambda x: x["start"]):
        ww = [w for w in words
              if w["end"] > m["start"] and w["start"] < m["end"]]
        m["signals"] = window_signals(ww, prev_content,
                                      m["end"] - m["start"])
        _attach_subscores(
            m, ww, prev_content, m["end"] - m["start"],
            category=category,
            audio_level=_window_audio_level(audio_profile,
                                            m["start"], m["end"]))
        prev_content = _content_words(ww)
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


def _window_components(win_words, prev_content, win_dur, lexicon=None):
    """Raw signal components for one candidate window - shared by the
    offline scorer and the score-explanation chips so the explanation
    always matches the number it explains.

    lexicon: optional set of category words (content-category modes);
    hits are reported separately (lex_hits/lex_score) and never change
    the base components, so a general/no-category run is numerically
    identical to before."""
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

    lex_hits = []
    if lexicon:
        lex_hits = [w["word"].strip(",.!?;:")
                    for w in win_words
                    if _clean_word(w["word"]) in lexicon]
    lex_score = min(100, len(lex_hits) * 20)

    emo_hits = [w["word"].strip(",.!?;:") for w in win_words
                if _clean_word(w["word"]) in EMOTION_WORDS]
    emo_score = min(100, len(emo_hits) * 20)

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

    return {
        "density": density, "density_score": density_score,
        "punct": punct, "punct_score": punct_score,
        "energy_hits": energy_hits, "energy_score": energy_score,
        "lex_hits": lex_hits, "lex_score": lex_score,
        "emo_hits": emo_hits, "emo_score": emo_score,
        "caps": caps, "caps_score": caps_score,
        "novelty_score": novelty_score,
        "silence": silence, "silence_pen": silence_pen,
        "filler_dur": filler_dur, "filler_pen": filler_pen,
    }


def _signal_chips(c):
    """Explanation chips for a scored window (score-reason feature):
    the positive drivers always, penalties only when they bite."""
    chips = [
        {"key": "pace", "label": "Pace",
         "score": int(round(c["density_score"])),
         "detail": f"{c['density']:.1f} words/sec"},
        {"key": "energy", "label": "Energy words",
         "score": int(round(c["energy_score"])),
         "detail": (f"{len(c['energy_hits'])} hits: "
                    + ", ".join(sorted(set(c["energy_hits"]))[:4])
                    if c["energy_hits"] else "none in this window")},
        {"key": "hook", "label": "Questions & shouts",
         "score": int(round(c["punct_score"])),
         "detail": f"{c['punct']} ?/! marks"},
        {"key": "novelty", "label": "Fresh topic",
         "score": int(round(c["novelty_score"])),
         "detail": "new words vs earlier windows"},
    ]
    if c["silence"] > 1.5:
        chips.append({"key": "silence", "label": "Dead air",
                      "score": -int(round(c["silence_pen"])), "neg": True,
                      "detail": f"{c['silence']:.1f}s of silence"})
    if c["filler_dur"] > 1.0:
        chips.append({"key": "filler", "label": "Fillers",
                      "score": -int(round(c["filler_pen"])), "neg": True,
                      "detail": f"{c['filler_dur']:.1f}s of uh/um"})
    return chips


def window_signals(win_words, prev_content=None, win_dur=None):
    """Chips explaining a window's score, measured from the transcript
    alone. Attached to moments from EVERY detector (offline, built-in
    AI, keyword search) so the explanation is always a measurement of
    the actual words, never an invented justification."""
    if not win_words:
        return []
    if win_dur is None:
        win_dur = win_words[-1]["end"] - win_words[0]["start"]
    return _signal_chips(_window_components(win_words, prev_content,
                                            win_dur))


# ---------------------------------------------------------------------------
# Phase 1: multi-dimensional scoring + content-category modes
# ---------------------------------------------------------------------------
# Every detected moment gains named subscores (hook / retention /
# emotion / story) next to its existing overall virality, each with a
# one-line reason measured from the same window, plus a platform
# recommendation. All subscores are SIGNAL-DERIVED and deterministic:
# transcript measurements (pace, punctuation, lexical hits, novelty,
# sentence structure) and, when the caller supplies an audio energy
# profile, the window's voice level relative to the whole video.
# No model ever overrides a number; the Built-in AI's own overall
# virality/hook on its moments stays the model's, as before.

EMOTION_WORDS = {
    "afraid", "scared", "terrified", "terrifying", "panic", "panicked",
    "angry", "furious", "mad", "screaming", "screams", "scream",
    "crying", "cried", "tears", "laughing", "laughs", "hilarious",
    "shocked", "shocking", "surprised", "surprise", "desperate",
    "begging", "please", "help", "hurting", "pain", "afraid",
    "worried", "nervous", "excited", "thrilled", "amazed", "stunned",
    "speechless", "unbelievable", "incredible", "horrible", "terrible",
    "awful", "wonderful", "beautiful", "hate", "hated", "love",
    "loved", "scary", "frightened", "alarmed", "distraught",
}

# Category modes: a per-project content category re-weights detection
# and subscores toward what performs in that genre. "weights" scale
# the virality components (renormalised, so all-1.0 == the historical
# formula exactly); "lexicon" words are counted as category hits and
# feed "lex_bias" (max points added to a subscore at a full lexicon
# score); "focus" is the one line handed to the Built-in AI prompt.
_CAT_W = {"density": 1.0, "energy": 1.0, "punct": 1.0,
          "novelty": 1.0, "caps": 1.0}
CATEGORIES = {
    "general": {
        "label": "General",
        "focus": "",
        "lexicon": set(),
        "weights": dict(_CAT_W),
        "lex_bias": {"hook": 0, "retention": 0, "emotion": 0,
                     "story": 0},
    },
    "bodycam": {
        "label": "Bodycam / Police",
        "focus": ("Bodycam/police footage: prioritise escalation, "
                  "shouted commands, resistance, pursuits, weapons, "
                  "arrests and surprise reveals over calm talk."),
        "lexicon": {
            "stop", "hands", "police", "officer", "officers", "arrest",
            "arrested", "arresting", "chase", "chasing", "pursuit",
            "fleeing", "fled", "resist", "resisting", "resisted",
            "weapon", "gun", "knife", "shots", "fired", "taser",
            "tased", "backup", "suspect", "suspects", "driver",
            "license", "warrant", "detain", "detained", "custody",
            "handcuffs", "cuffs", "siren", "sirens", "radio",
            "dispatch", "vehicle", "pulled", "traffic", "dui",
            "intoxicated", "property", "trespassing",
        },
        "weights": {"density": 1.0, "energy": 1.25, "punct": 1.15,
                    "novelty": 0.85, "caps": 1.2},
        "lex_bias": {"hook": 10, "retention": 4, "emotion": 16,
                     "story": 6},
    },
    "podcast": {
        "label": "Podcast",
        "focus": ("Podcast conversation: prioritise personal stories, "
                  "disagreements, revelations, hot takes and laughter "
                  "over small talk."),
        "lexicon": {
            "story", "remember", "believe", "honestly", "honest",
            "actually", "opinion", "disagree", "secret", "learned",
            "mistake", "regret", "funny", "laughed", "laughing",
            "crazy", "realised", "realized", "truth", "admit",
            "confession", "experience", "growing", "childhood",
        },
        "weights": {"density": 0.9, "energy": 1.0, "punct": 1.0,
                    "novelty": 1.2, "caps": 0.8},
        "lex_bias": {"hook": 8, "retention": 8, "emotion": 8,
                     "story": 14},
    },
    "interview": {
        "label": "Interview",
        "focus": ("Interview: prioritise revealing answers, career "
                  "turning points, challenges, advice and surprising "
                  "admissions over introductions and small talk."),
        "lexicon": {
            "question", "answer", "career", "started", "challenge",
            "challenges", "advice", "success", "failure", "failed",
            "journey", "experience", "inspired", "decision",
            "difficult", "learned", "opportunity", "risk",
        },
        "weights": {"density": 0.9, "energy": 0.95, "punct": 1.05,
                    "novelty": 1.15, "caps": 0.8},
        "lex_bias": {"hook": 8, "retention": 8, "emotion": 6,
                     "story": 12},
    },
    "gaming": {
        "label": "Gaming",
        "focus": ("Gaming footage: prioritise clutch plays, wins and "
                  "losses, boss fights, rare loot, big reactions and "
                  "funny fails over quiet grinding."),
        "lexicon": {
            "kill", "kills", "killed", "clutch", "win", "winning",
            "won", "lose", "losing", "lost", "boss", "level",
            "headshot", "stream", "chat", "raid", "loot", "respawn",
            "insane", "clip", "ranked", "match", "round", "final",
        },
        "weights": {"density": 1.1, "energy": 1.2, "punct": 1.1,
                    "novelty": 0.95, "caps": 1.1},
        "lex_bias": {"hook": 10, "retention": 6, "emotion": 12,
                     "story": 4},
    },
    "education": {
        "label": "Education",
        "focus": ("Educational content: prioritise clear explanations, "
                  "key insights, common mistakes, worked examples and "
                  "memorable takeaways over introductions."),
        "lexicon": {
            "learn", "important", "remember", "example", "because",
            "understand", "concept", "step", "steps", "mistake",
            "tip", "tips", "explains", "explain", "means", "reason",
            "actually", "works", "formula", "method",
        },
        "weights": {"density": 0.95, "energy": 0.9, "punct": 0.9,
                    "novelty": 1.2, "caps": 0.7},
        "lex_bias": {"hook": 6, "retention": 12, "emotion": 3,
                     "story": 10},
    },
    "news": {
        "label": "News / Commentary",
        "focus": ("News and commentary: prioritise breaking "
                  "developments, official statements, confirmed facts, "
                  "witness accounts and strong reactions over "
                  "background recaps."),
        "lexicon": {
            "breaking", "report", "reported", "officials", "official",
            "confirmed", "incident", "statement", "investigation",
            "according", "witnesses", "witness", "update",
            "developing", "authorities", "announced", "scene",
        },
        "weights": {"density": 1.0, "energy": 1.05, "punct": 1.0,
                    "novelty": 1.15, "caps": 0.9},
        "lex_bias": {"hook": 8, "retention": 6, "emotion": 6,
                     "story": 10},
    },
}


def category_info(category):
    """(key, info) for a category key - unknown keys land on general."""
    key = str(category or "general").strip().lower()
    if key not in CATEGORIES:
        key = "general"
    return key, CATEGORIES[key]


def audio_energy_profile(video_path, bucket_s=1.0):
    """Per-bucket voice level of a video's audio, or None.

    Streams the audio through ffmpeg as 8 kHz mono PCM (no temp
    files, constant memory) and returns [{"t", "mean_db", "peak_db"}]
    plus the median mean level under key "_median_db" on the list's
    first element... (kept simple: returns (buckets, median_db)).
    Any failure (no audio stream, ffmpeg error) returns None - the
    scorers then simply drop their audio terms."""
    try:
        import numpy as np
    except ImportError:
        return None
    sr = 8000
    per = int(sr * bucket_s)
    cmd = ["ffmpeg", "-v", "error", "-i", str(video_path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None
    buckets = []
    try:
        idx = 0
        while True:
            raw = proc.stdout.read(per * 2)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if not len(a):
                break
            rms = float(np.sqrt(np.mean(a * a))) if len(a) else 0.0
            peak = float(np.max(np.abs(a))) if len(a) else 0.0
            mean_db = 20 * math.log10(rms / 32768.0) if rms > 0 else -80.0
            peak_db = 20 * math.log10(peak / 32768.0) if peak > 0 else -80.0
            buckets.append({"t": round(idx * bucket_s, 2),
                            "mean_db": round(mean_db, 1),
                            "peak_db": round(peak_db, 1)})
            idx += 1
    except Exception:  # noqa: BLE001 - profiling is best-effort
        buckets = []
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    if len(buckets) < 2:
        return None
    means = sorted(b["mean_db"] for b in buckets)
    median_db = means[len(means) // 2]
    return buckets, median_db


def _window_audio_level(profile, start, end):
    """(mean_db, peak_db, arousal 0-100) for [start, end] from an
    audio_energy_profile() result, or None. Arousal is the window's
    level ABOVE the video's own median - a shout in a quiet video
    counts more than the same level in a loud one."""
    if not profile:
        return None
    buckets, median_db = profile
    sel = [b for b in buckets if end > b["t"] >= start - 1.0]
    if not sel:
        return None
    import numpy as np
    lin = [10 ** (b["mean_db"] / 20.0) for b in sel]
    mean_db = 20 * math.log10(max(float(np.mean(lin)), 1e-6))
    peak_db = max(b["peak_db"] for b in sel)
    arousal = max(0.0, min(100.0, (mean_db - median_db) / 14.0 * 100.0))
    return mean_db, peak_db, arousal


_STORY_MARKERS = {
    "then", "suddenly", "but", "because", "until", "finally",
    "after", "before", "when", "so", "started", "began", "ended",
    "turns", "turned", "realised", "realized", "discovered",
}


def _subscores(win_words, prev_content, win_dur, category="general",
               audio_level=None, components=None):
    """Named subscores for one window: {hook, retention, emotion,
    story} (0-100 ints), a one-line reason per subscore, and a
    platform recommendation {name, why}.

    Deterministic and signal-derived. With category "general" and no
    audio the hook number equals the historical offline hook formula
    exactly; other categories shift weights/lexicon, never invent."""
    key, cat = category_info(category)
    c = components or _window_components(
        win_words, prev_content, win_dur, lexicon=cat["lexicon"])
    density = c["density"]
    energy = c["energy_score"]
    punct = c["punct_score"]
    novelty = c["novelty_score"]
    caps = c["caps_score"]

    def clamp(v):
        return int(max(2, min(99, round(v))))

    # -- hook: the historical formula, plus a capped category-lexicon
    # contribution when a category is active.
    hook = 0.5 * energy + 0.3 * punct + 0.2 * caps
    if key != "general":
        hook += cat["lex_bias"]["hook"] * c["lex_score"] / 100.0
    hook = clamp(hook)

    # -- retention: pace in the spoken sweet spot, fresh material,
    # open loops (questions), minus dead air / fillers.
    pace_fit = max(0.0, 100.0 - abs(density - 2.3) / 1.9 * 100.0)
    retention = (0.30 * pace_fit + 0.25 * novelty
                 + 0.20 * c["density_score"] + 0.15 * punct
                 + 0.10 * energy
                 - 0.6 * c["silence_pen"] - 0.4 * c["filler_pen"])
    if key != "general":
        retention += cat["lex_bias"]["retention"] * c["lex_score"] / 100.0
    retention = clamp(retention)

    # -- emotion: energy words + emotion lexicon + shouting, lifted
    # by measured voice arousal when an audio profile is available.
    emotion = (0.45 * energy + 0.25 * c["emo_score"]
               + 0.15 * punct + 0.15 * caps)
    if audio_level is not None:
        emotion = 0.75 * emotion + 0.25 * audio_level[2]
    if key != "general":
        emotion += cat["lex_bias"]["emotion"] * c["lex_score"] / 100.0
    emotion = clamp(emotion)

    # -- story: narrative markers, complete-sentence framing, topic
    # development (novelty), and enough words to hold an arc.
    toks = [_clean_word(w["word"]) for w in win_words]
    markers = [t for t in toks if t in _STORY_MARKERS]
    marker_score = min(100, len(markers) * 16)
    text = " ".join(w["word"] for w in win_words).strip()
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    completeness = 40.0
    if text[:1].isupper():
        completeness += 20
    if text[-1:] in ".!?":
        completeness += 25
    if len(sents) >= 3:
        completeness += 15
    story = (0.35 * marker_score + 0.25 * novelty
             + 0.20 * completeness
             + 0.20 * (0.5 * energy + 0.5 * novelty))
    if key != "general":
        story += cat["lex_bias"]["story"] * c["lex_score"] / 100.0
    story = clamp(story)

    # -- reasons: name the strongest measured driver of each number.
    def _hits(hits, n=3):
        return ", ".join(sorted(set(hits))[:n])

    hook_bits = []
    if c["punct"]:
        hook_bits.append(f"{c['punct']} question/exclamation marks")
    if c["energy_hits"]:
        hook_bits.append("energy words: " + _hits(c["energy_hits"]))
    if key != "general" and c["lex_hits"]:
        hook_bits.append(f"{cat['label']} words: "
                         + _hits(c["lex_hits"]))
    if c["caps"]:
        hook_bits.append(f"{c['caps']} shouted words")
    hook_reason = ("Strong opening signals - " + "; ".join(hook_bits)
                   if hook_bits else
                   "Steady spoken opening - no question, shout or "
                   "high-energy words at the start")

    ret_bits = [f"{density:.1f} words/sec pace",
                f"{novelty:.0f}% fresh topic"]
    if c["silence"] > 1.5:
        ret_bits.append(f"{c['silence']:.1f}s of dead air hurts it")
    elif c["filler_dur"] > 1.0:
        ret_bits.append(f"{c['filler_dur']:.1f}s of fillers hurts it")
    else:
        ret_bits.append("no dead air")
    retention_reason = "; ".join(ret_bits)

    emo_bits = []
    if c["emo_hits"]:
        emo_bits.append("emotion words: " + _hits(c["emo_hits"]))
    if c["energy_hits"]:
        emo_bits.append("high-energy words: "
                        + _hits(c["energy_hits"]))
    if audio_level is not None and audio_level[2] >= 40:
        emo_bits.append("voice level peaks well above the video's "
                        "average")
    if c["caps"]:
        emo_bits.append(f"{c['caps']} shouted words")
    emotion_reason = ("; ".join(emo_bits) if emo_bits else
                      "Calm delivery - low emotional charge in the "
                      "words and voice")

    story_bits = []
    if markers:
        story_bits.append("story-arc words: "
                          + ", ".join(sorted(set(markers))[:4]))
    if text[-1:] in ".!?":
        story_bits.append("lands on a complete sentence")
    if novelty >= 60:
        story_bits.append("topic keeps developing")
    story_reason = ("; ".join(story_bits) if story_bits else
                    "Loose segment - no clear setup-to-payoff arc in "
                    "the words")

    # -- platform: duration + the subscore profile pick the feed.
    dur = win_dur
    if dur <= 60 and hook >= 70:
        platform = {"name": "TikTok",
                    "why": "Under 60s with a strong opening hook - "
                           "TikTok's feed rewards exactly that."}
    elif story >= 70:
        platform = {"name": "Facebook Reels",
                    "why": "Story-driven clip - Facebook Reels "
                           "audiences watch narrative clips longest."}
    elif emotion >= 72:
        platform = {"name": "Instagram Reels",
                    "why": "High-emotion moment - Instagram Reels "
                           "favours shareable reactions."}
    elif dur <= 60:
        platform = {"name": "YouTube Shorts",
                    "why": "Punchy sub-60s cut - a safe Shorts length "
                           "with captions doing the work."}
    else:
        platform = {"name": "Facebook Reels",
                    "why": "Longer, steadier moment - fits Facebook "
                           "Reels' longer watch sessions."}

    return {"hook": hook, "retention": retention, "emotion": emotion,
            "story": story,
            "reasons": {"hook": hook_reason,
                        "retention": retention_reason,
                        "emotion": emotion_reason,
                        "story": story_reason},
            "platform": platform}


def _attach_subscores(moment, win_words, prev_content, win_dur,
                      category="general", audio_level=None,
                      components=None):
    """Attach the Phase-1 extras to a moment dict in place:
    scores{viral,hook,retention,emotion,story}, score_reasons,
    platform. viral/hook mirror the moment's existing overall and
    hook_score so every surface tells one story."""
    sub = _subscores(win_words, prev_content, win_dur,
                     category=category, audio_level=audio_level,
                     components=components)
    moment["scores"] = {
        "viral": int(moment.get("virality", 0) or 0),
        "hook": int(moment.get("hook_score",
                               sub["hook"]) or sub["hook"]),
        "retention": sub["retention"],
        "emotion": sub["emotion"],
        "story": sub["story"],
    }
    moment["score_reasons"] = sub["reasons"]
    moment["platform"] = sub["platform"]
    return moment


def _score_offline_window(win_words, prev_content, win_dur,
                          category="general"):
    """Score one candidate window. Returns (virality, hook, flow, value,
    reason_parts, signals).

    With a content category, the virality components are re-weighted
    (renormalised so all-weights-1.0 reproduces the historical number
    exactly) and category-lexicon hits join the reason text."""
    key, cat = category_info(category)
    c = _window_components(win_words, prev_content, win_dur,
                           lexicon=cat["lexicon"])
    density_score = c["density_score"]
    energy_score = c["energy_score"]
    punct_score = c["punct_score"]
    novelty_score = c["novelty_score"]
    caps_score = c["caps_score"]
    silence_pen = c["silence_pen"]
    filler_pen = c["filler_pen"]
    energy_hits = c["energy_hits"]

    w = cat["weights"]
    coefs = {"density": 0.30, "energy": 0.22, "punct": 0.18,
             "novelty": 0.15, "caps": 0.15}
    wsum = sum(coefs[k] * w[k] for k in coefs)
    virality = (coefs["density"] * w["density"] * density_score
                + coefs["energy"] * w["energy"] * energy_score
                + coefs["punct"] * w["punct"] * punct_score
                + coefs["novelty"] * w["novelty"] * novelty_score
                + coefs["caps"] * w["caps"] * caps_score) / wsum
    if key != "general":
        virality += 0.35 * c["lex_score"] * 0.30
    virality = (virality - 0.5 * silence_pen - 0.3 * filler_pen)
    virality = max(5, min(99, round(virality)))

    hook = round(0.5 * energy_score + 0.3 * punct_score
                 + 0.2 * caps_score)
    flow = round(max(0, 0.6 * density_score + 0.4 * novelty_score
                     - 0.4 * silence_pen))
    value = round(0.5 * energy_score + 0.5 * novelty_score)

    parts = [f"{c['density']:.1f} words/sec",
             f"{len(energy_hits)} energy words"
             + (": " + ", ".join(sorted(set(energy_hits))[:4])
                if energy_hits else ""),
             f"{c['punct']} exclamations/questions",
             f"{novelty_score:.0f}% topic novelty"]
    if key != "general" and c["lex_hits"]:
        parts.append(f"{len(c['lex_hits'])} {cat['label']} words"
                     + ": " + ", ".join(sorted(set(c["lex_hits"]))[:4]))
    if c["silence"] > 1.5:
        parts.append(f"{c['silence']:.1f}s silence (penalty)")
    if c["filler_dur"] > 1.0:
        parts.append(f"{c['filler_dur']:.1f}s fillers (penalty)")
    return virality, hook, flow, value, "; ".join(parts), _signal_chips(c)


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


def detect_moments_offline(words, clip_min_s, clip_max_s, max_clips=8,
                           category="general", audio_profile=None):
    """Heuristic moment detection from the local transcript. No model needed.

    Returns list of {start, end, title, hook, virality, hook_score,
    flow_score, value_score, reason, offline: True} - Phase 1 adds
    scores{...}/score_reasons/platform (see _subscores) and honours a
    content category. Titles and hooks are extracted verbatim from
    the transcript - never invented.
    """
    category, _cat = category_info(category)
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
                # Speech fills only part of the window: grow back
                # toward the window bounds so the moment still lands
                # inside the requested range. Growth is allocated in
                # passes - half to the left, then the rest to the
                # right, then any remainder the right side could not
                # take goes back to the left. The old one-pass split
                # capped the right side at `we` and silently DISCARDED
                # the unused share, so whenever speech ran flush to
                # the window end the moment came back up to need/2
                # shorter than clip_min_s (proven: a [30, 60] request
                # returning 25.5-29.0 s moments). Every candidate
                # window is at least clip_min_s long, so the room to
                # reach clip_min_s always exists inside the window;
                # a moment may only stay shorter than clip_min_s when
                # the whole transcript is shorter than clip_min_s
                # (the lengths == [dur] fallback above).
                need = clip_min_s - (e - s)
                grow_l = min(need / 2.0, s - ws)
                s -= grow_l
                need -= grow_l
                grow_r = min(need, we - e)
                e += grow_r
                need -= grow_r
                if need > 0:
                    s = max(ws, s - need)
            if e - s < 5:
                prev_content = _content_words(win_words)
                continue
            virality, hook_s, flow_s, value_s, parts, sigs = \
                _score_offline_window(win_words, prev_content, e - s,
                                      category=category)
            prev_content = _content_words(win_words)
            scored.append((virality, s, e, win_words, hook_s, flow_s,
                           value_s, parts, sigs))

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
            parts, sigs) in picked:
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
            "signals": sigs,
            "offline": True,
        })
    # Phase-1 subscores over the picked moments in time order (the
    # novelty chain runs across the moments the user actually gets).
    prev_content = set()
    for m in sorted(moments, key=lambda x: x["start"]):
        mw = [w for w in words
              if w["end"] > m["start"] and w["start"] < m["end"]]
        _attach_subscores(
            m, mw, prev_content, m["end"] - m["start"],
            category=category,
            audio_level=_window_audio_level(audio_profile,
                                            m["start"], m["end"]))
        prev_content = _content_words(mw)
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


def _posting_desc_is_echo(desc, words):
    """True when a model-written posting description is really the
    instruction parroted back ("Two to three sentences with a line
    break, ending in a question.") instead of copy: such text shares
    NO content words with the transcript, and often repeats itself
    verbatim across a blank line."""
    desc = desc or ""
    desc_words = {_clean_word(t) for t in
                  re.findall(r"[A-Za-z']+", desc.lower())}
    desc_words.discard("")
    tr_words = {_clean_word(w["word"]) for w in words or []}
    tr_words.discard("")
    halves = [h.strip() for h in re.split(r"\n\s*\n", desc) if h.strip()]
    if len(halves) >= 2 and halves[0] == halves[1]:
        return True
    return len(desc_words) >= 6 and not (desc_words & tr_words)


def posting_package_valid(pkg, words):
    """Re-validate a CACHED posting package before serving it: a
    package written by a weaker model build stays cached on the moment
    (and in job.json) forever, so the echo check must apply to cache
    reads too, not just fresh generations. Extractive packages are
    template-built from the transcript and always valid."""
    if not pkg or pkg.get("mode") != "ai":
        return True
    return not _posting_desc_is_echo(pkg.get("description", ""), words)


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
            # Echo guard (see _posting_desc_is_echo): reject parroted
            # instructions and take the honest extractive fallback.
            if _posting_desc_is_echo(desc, words):
                raise RuntimeError(
                    "the model echoed its instructions instead of "
                    "writing copy")
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


# ---------------------------------------------------------------------------
# Studio: Hook Maker. For videos whose own opening is too slow to stop
# the scroll: find the single most gripping moment in the video, write
# hook lines from what is actually said, and (in app.py) render a short
# hook clip from it. Same honesty rules as Titles: AI-written only when
# the Built-in AI is present; otherwise the hooks are the transcript's
# own lines, labeled "basic" - never presented as AI work.
# ---------------------------------------------------------------------------

HOOKS_PROMPT_TEMPLATE = """You are an expert short-form video hook \
writer for Facebook Reels / TikTok / YouTube Shorts. The video below \
has a weak, slow opening, so its hook must be MANUFACTURED from the \
most gripping thing that actually happens in it.

Transcript:
{transcript}
{genre_line}
Write 10 scroll-stopping hook lines for this video. A hook is the \
first on-screen text / spoken line of a clip. Rules:
- Every hook must be grounded in something that REALLY happens or is \
said in the transcript - no invented events, no clickbait lies.
- Max 12 words each. Punchy, specific, curiosity-driven: a question, \
a shock, a warning, or the most intense moment teased without \
spoiling it.
- Score every hook 0-100 for scroll-stopping power (honest, spread \
the scores).
- Reply with ONLY a JSON object, no prose:
{{"hooks": [{{"text": "...", "score": 87}}]}}"""

# Extra high-stakes vocabulary for hook scoring (bodycam / news heavy,
# on top of OFFLINE_ENERGY_WORDS).
_HOOK_STAKES_WORDS = {
    "dead", "died", "dies", "killed", "blood", "crash", "crashed",
    "fire", "knife", "attack", "attacked", "screaming", "screams",
    "help", "stop", "warning", "danger", "handcuffs", "tased",
    "taser", "ran", "running", "fight", "shot", "shooting",
    "stolen", "robbery", "hostage", "exploded", "trapped",
}

_HOOK_OPENERS = {
    "what", "why", "how", "who", "when", "watch", "look", "listen",
    "stop", "wait", "never", "imagine", "this",
}


def _transcript_sentences(words):
    """Group timed words into sentences.

    Returns [{start, end, text, words}]. A sentence closes on terminal
    punctuation or after 30 words (whisper pauses are unreliable)."""
    sents, cur = [], []
    for w in words or []:
        cur.append(w)
        t = (w.get("word") or "").strip()
        if t.endswith((".", "!", "?")) or len(cur) >= 30:
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    out = []
    for ws in sents:
        text = " ".join(w["word"] for w in ws).strip()
        if text:
            out.append({"start": ws[0]["start"], "end": ws[-1]["end"],
                        "text": text, "words": ws})
    return out


def _score_hook_sentence(sent):
    """Hook-likeness of one sentence, 0-100 (heuristic, honest).

    Rewards questions / exclamations, high-stakes and energy words,
    numbers, direct openers (what/why/watch/…) and a punchy length;
    calm filler scores low."""
    ws = sent["words"]
    toks = [_clean_word(w["word"]) for w in ws]
    n = max(len(toks), 1)
    energy = sum(1 for t in toks if t in OFFLINE_ENERGY_WORDS)
    stakes = sum(1 for t in toks if t in _HOOK_STAKES_WORDS)
    score = min(42, (energy * 2 + stakes * 3) * 7)
    text = sent["text"].rstrip()
    if text.endswith("?"):
        score += 22
    elif text.endswith("!"):
        score += 16
    if any(any(ch.isdigit() for ch in w["word"]) for w in ws):
        score += 10
    if toks and toks[0] in _HOOK_OPENERS:
        score += 8
    if 4 <= n <= 14:
        score += 12
    elif n <= 20:
        score += 5
    elif n > 26:
        score -= 8
    return max(5, min(97, round(score)))


def _basic_hooks(words):
    """Extractive hook lines: the transcript's own most gripping
    lines, ranked (plus a curiosity-cut opener variant of the best
    ones). Verbatim transcript text - labeled "basic" by callers,
    never presented as AI work."""
    sents = _transcript_sentences(words)
    if not sents:
        return []
    ranked = sorted(sents, key=_score_hook_sentence, reverse=True)

    def _trim(text, n=90):
        text = " ".join(text.split())
        return text if len(text) <= n else \
            text[:n].rsplit(" ", 1)[0] + "…"

    out, seen = [], set()

    def _add(text, score):
        t = _trim(text)
        key = t.lower().strip(" .…")
        if len(t.split()) >= 3 and key and key not in seen:
            seen.add(key)
            out.append({"text": t,
                        "score": max(25, min(97, int(score)))})

    for s in ranked:
        _add(s["text"], _score_hook_sentence(s))
        if len(out) >= 7:
            break
    # curiosity cuts: the opening words of the best sentences, cut
    # mid-thought so the viewer has to keep watching to finish it.
    for s in ranked:
        toks = s["text"].split()
        if len(toks) >= 9:
            _add(" ".join(toks[:7]) + "…",
                 _score_hook_sentence(s) - 4)
        if len(out) >= 10:
            break
    out.sort(key=lambda h: h["score"], reverse=True)
    return out[:10]


def generate_hooks(words, genre=""):
    """8-10 ranked hook lines written from the transcript.

    Built-in AI when its model is downloaded ("ai" mode); honest
    extractive lines otherwise ("basic" mode). With no transcript
    there is nothing truthful to hook from ("needs_transcript")."""
    words = words or []
    if not words:
        return {"hooks": [], "mode": "needs_transcript",
                "note": "A hook is built from what is said in the "
                        "video - this project has no transcript yet."}
    try:
        import local_ai
        ready = local_ai.model_ready()
    except Exception:  # noqa: BLE001 - local AI unavailable
        ready = False
    if ready:
        try:
            genre_line = ("\nContent type: " + genre.strip()[:60] + "\n"
                          if (genre or "").strip() else "")
            raw = local_ai.chat([{
                "role": "user",
                "content": HOOKS_PROMPT_TEMPLATE.format(
                    transcript=_words_text(words, cap=9000),
                    genre_line=genre_line)}])
            data = local_ai.extract_json(raw)
            hooks = []
            for it in data.get("hooks") or []:
                if isinstance(it, dict):
                    t = str(it.get("text", "")).strip()
                    try:
                        sc = int(float(it.get("score", 50)))
                    except (TypeError, ValueError):
                        sc = 50
                else:
                    t, sc = str(it).strip(), 50
                if t:
                    hooks.append({"text": t[:90],
                                  "score": max(0, min(100, sc))})
                if len(hooks) == 10:
                    break
            if len(hooks) >= 4:
                hooks.sort(key=lambda h: h["score"], reverse=True)
                return {"hooks": hooks, "mode": "ai",
                        "note": "Written by the Built-in AI from this "
                                "video's transcript."}
            raise RuntimeError("the model returned too few hooks")
        except Exception as e:  # noqa: BLE001 - honest fallback
            hooks = _basic_hooks(words)
            if hooks:
                return {"hooks": hooks, "mode": "basic",
                        "note": ("Built-in AI request failed (%s) - "
                                 "hooks are the transcript's own most "
                                 "gripping lines instead."
                                 % str(e)[:80])}
    hooks = _basic_hooks(words)
    if hooks:
        return {"hooks": hooks, "mode": "basic",
                "note": ("Basic mode (Built-in AI not downloaded): "
                         "hooks are the transcript's own most gripping "
                         "lines, lightly trimmed and ranked. Download "
                         "the Built-in AI for hooks written from "
                         "scratch.")}
    return {"hooks": [], "mode": "needs_transcript",
            "note": "No usable spoken lines were found in the "
                    "transcript to build a hook from."}


def hook_render_window(words, start, end, lo=3.0, hi=8.0,
                       target=6.0):
    """The window a hook clip should render: [start, end] unchanged
    when it already fits the 3-8s hook range; longer segments are
    trimmed to their most intense ~target-second sub-window, scored
    with the same heuristic the offline detector uses."""
    start, end = float(start), float(end)
    if end - start <= hi:
        return round(start, 2), round(end, 2)
    win = [w for w in words or []
           if w["end"] > start and w["start"] < end]
    best_s, best_v = start, -1.0
    dense_s, dense_n = start, -1
    t = start
    while t + target <= end + 0.001:
        ww = [w for w in win if w["end"] > t and w["start"] < t + target]
        if len(ww) > dense_n:
            dense_n, dense_s = len(ww), t
        if len(ww) >= 4:
            v, _h, _f, _val, _p, _sg = _score_offline_window(
                ww, None, target)
            if v > best_v:
                best_v, best_s = v, t
        t += 0.5
    if best_v < 0:
        # sparse speech: no window held enough words to score - take
        # the wordiest window instead of the segment's dead start
        best_s = dense_s
    return round(best_s, 2), round(best_s + target, 2)


def find_hook_moment(words, duration=0.0, moments=None):
    """The single strongest hook moment in a video.

    Preference order: the top-scoring detected moment (the job's own
    moments when given, else a fresh offline detection pass); when no
    moments exist at all - or detection only found a weak one and a
    transcript sentence is clearly stronger - the best transcript
    segment (top hook-scored sentence, widened toward ~5s). Returns
    {start, end, text, score, source, moment_index, segment_start,
    segment_end} or None. The start/end are the RENDER window
    (already trimmed to the hook range); segment_* keep the full
    picked segment for reference."""
    words = words or []
    if not words:
        return None
    dur = float(duration or 0) or words[-1]["end"]

    def _pack(m, source, index):
        rs, re_ = hook_render_window(words, m["start"], m["end"])
        win = [w for w in words
               if w["end"] > rs and w["start"] < re_]
        text = " ".join(w["word"] for w in win).strip()
        sigs = m.get("signals") or window_signals(win, None, re_ - rs)
        return {"start": rs, "end": re_, "text": text,
                "score": int(m.get("virality", 0) or 0),
                "source": source, "moment_index": index,
                "signals": sigs,
                "segment_start": round(float(m["start"]), 2),
                "segment_end": round(float(m["end"]), 2)}

    def _segment():
        """Best-sentence segment moment, or None."""
        sents = _transcript_sentences(words)
        if not sents:
            return None
        best = max(sents, key=_score_hook_sentence)
        s, e = best["start"], best["end"]
        # widen toward ~5s with neighbouring words so the hook has
        # room to breathe (a lone 1.5s sentence makes a jumpy clip).
        # The sentence's word dicts are the same objects as in
        # `words`, so locate the span by identity.
        ws = sorted(words, key=lambda w: w["start"])
        i0 = next((i for i, w in enumerate(ws)
                   if w is best["words"][0]), None)
        if i0 is not None:
            i1 = i0 + len(best["words"]) - 1
            while e - s < 4.5:
                grew = False
                if i1 + 1 < len(ws) and ws[i1 + 1]["start"] - e <= 1.2:
                    i1 += 1
                    e = ws[i1]["end"]
                    grew = True
                if e - s >= 4.5:
                    break
                if i0 - 1 >= 0 and s - ws[i0 - 1]["end"] <= 1.2:
                    i0 -= 1
                    s = ws[i0]["start"]
                    grew = True
                if not grew:
                    break
        seg = {"start": max(0.0, s), "end": min(dur, e),
               "virality": _score_hook_sentence(best)}
        out = _pack(seg, "segment", -1)
        out["score"] = int(seg["virality"])
        return out

    if moments:
        top = max(range(len(moments)),
                  key=lambda i: moments[i].get("virality", 0) or 0)
        return _pack(moments[top], "moment", top)
    try:
        cand = detect_moments_offline(words, 15, 60, max_clips=4)
    except Exception:  # noqa: BLE001 - fall through to segments
        cand = []
    if cand:
        top_v = int(cand[0].get("virality", 0) or 0)
        if top_v < 30:
            # detection only found a weak moment (long calm video):
            # a clearly stronger spoken line makes the better hook
            seg = _segment()
            if seg and seg["score"] > top_v + 15:
                return seg
        return _pack(cand[0], "detected", -1)
    return _segment()


def _hook_reason(sent, score):
    """One-line reason a sentence scored as a hook (measured features
    only - the same features _score_hook_sentence rewards)."""
    toks = [_clean_word(w["word"]) for w in sent["words"]]
    bits = []
    text = sent["text"].rstrip()
    if text.endswith("?"):
        bits.append("asks a question the viewer wants answered")
    elif text.endswith("!"):
        bits.append("lands as an exclamation")
    stakes = [t for t in toks if t in _HOOK_STAKES_WORDS]
    if stakes:
        bits.append("high-stakes words: "
                    + ", ".join(sorted(set(stakes))[:3]))
    energy = [t for t in toks if t in OFFLINE_ENERGY_WORDS]
    if energy:
        bits.append("energy words: "
                    + ", ".join(sorted(set(energy))[:3]))
    if any(any(ch.isdigit() for ch in w["word"]) for w in sent["words"]):
        bits.append("a concrete number makes it specific")
    if toks and toks[0] in _HOOK_OPENERS:
        bits.append(f"opens with '{toks[0]}'")
    if not bits:
        bits.append("punchy, self-contained line")
    return "; ".join(bits)


def rank_hook_candidates(words, moments=None, duration=0.0, limit=10):
    """Ranked hook candidates across the WHOLE video (Hook v2).

    Sources: the opening sentence of every detected moment (blended
    with that moment's own hook score) and the strongest standalone
    transcript sentences anywhere in the video. Each candidate is
    {start, end, text, score, reason, source, moment_index} where
    start/end are the RENDER window (already trimmed to the 3-8s
    hook range) and text is the verbatim sentence the window opens
    with. Deduped by window overlap; the top entry is the AI pick.
    Extractive only - nothing is rewritten or invented."""
    words = words or []
    if not words:
        return []
    dur = float(duration or 0) or words[-1]["end"]
    sents = _transcript_sentences(words)
    sent_by_start = {}
    for s in sents:
        sent_by_start.setdefault(round(s["start"], 2), s)

    cands = []

    def _add(start, end, text, score, reason, source, moment_index):
        rs, re_ = hook_render_window(words, start, end)
        if re_ - rs < 1.0:
            return
        cands.append({
            "start": rs, "end": re_,
            "text": " ".join(str(text).split())[:90],
            "score": int(max(5, min(98, round(score)))),
            "reason": reason, "source": source,
            "moment_index": moment_index,
        })

    # (a) detected moments: their opening line, blended with the
    # moment's own hook score so a proven moment outranks a stray line.
    for i, m in enumerate(moments or []):
        win = [w for w in words
               if w["end"] > m["start"] and w["start"] < m["end"]]
        msents = _transcript_sentences(win)
        if not msents:
            continue
        first = msents[0]
        base = _score_hook_sentence(first)
        own = m.get("hook_score")
        if own is None:
            own = m.get("virality", base)
        score = 0.6 * base + 0.4 * float(own or base)
        reason = (f"Opens detected moment #{i + 1} "
                  f"(viral {int(m.get('virality', 0) or 0)}) - "
                  + _hook_reason(first, base))
        _add(first["start"], first["end"], first["text"], score,
             reason, "moment", i)

    # (b) standalone sentences anywhere in the transcript.
    ranked = sorted(sents, key=_score_hook_sentence, reverse=True)
    for s in ranked[:limit * 2]:
        _add(s["start"], s["end"], s["text"],
             _score_hook_sentence(s), _hook_reason(s, 0),
             "sentence", -1)

    # dedupe: best-first, drop candidates whose render window
    # overlaps a kept one by >50% of the shorter window.
    cands.sort(key=lambda c: c["score"], reverse=True)
    kept = []
    for c in cands:
        overlap = False
        for k in kept:
            inter = max(0.0, min(c["end"], k["end"])
                        - max(c["start"], k["start"]))
            if inter > 0.5 * min(c["end"] - c["start"],
                                 k["end"] - k["start"]):
                overlap = True
                break
        if not overlap:
            kept.append(c)
        if len(kept) >= limit:
            break
    kept.sort(key=lambda c: c["score"], reverse=True)
    return kept


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
    # Encode to a .part sibling and rename on success (the batch
    # enhancer's pattern): writing straight to the final path meant
    # a failed re-enhance DESTROYED the previously good file, and a
    # render orphaned by an app kill kept writing the final name.
    final_path = out_path
    out_path = _part_sibling(final_path)
    cmd = (["ffmpeg", "-y", "-i", video_path, "-vf", vf,
            "-map", "0:v:0", "-map", "0:a?"]
           + _encode_args(quality_tier)
           + ["-c:a", "aac", "-b:a", "192k", out_path])
    try:
        _run_ffmpeg_progress(cmd, duration, progress_cb, 3, 97,
                             "Enhancing (upscale + sharpen)")
    except Exception:
        _remove_quietly(out_path)
        raise
    if not os.path.isfile(out_path) \
            or os.path.getsize(out_path) < 1024:
        _remove_quietly(out_path)
        raise RuntimeError("Enhance produced no output file.")
    os.replace(out_path, final_path)
    if progress_cb:
        progress_cb(100, "Done")
    return final_path


# ---------------------------------------------------------------------------
# Post-clipping batch enhancement (the Enhance-All queue in app.py)
#
# Everything here drives the SAME ffmpeg toolbox as enhance_video above
# (Lanczos scale, hqdn3d, unsharp, eq) - there is no second engine.
# A "plan" decides the filter chain:
#   {target: short-side px to upscale to (None = keep), sharpen:
#    off|light|medium, denoise: off|light|strong, colour: bool,
#    stabilize: bool, skip: bool, why: str}
# Frame interpolation is deliberately NOT offered: the bundled ffmpeg
# ships minterpolate, but motion-compensated interpolation at
# 1080x1920 measured >15x slower than realtime on CPU (an 8s clip
# took minutes), which would wedge a batch queue; the cheap modes
# (dup/blend) are not real interpolation and are not sold as such.
# ---------------------------------------------------------------------------

_ENHANCE_CAPS = None


def enhance_capabilities():
    """Which optional enhance filters the ACTUAL runtime ffmpeg has.
    The UI only offers what this returns True for, so a build whose
    ffmpeg lacks a filter simply never shows the option."""
    global _ENHANCE_CAPS
    if _ENHANCE_CAPS is not None:
        return dict(_ENHANCE_CAPS)
    caps = {"stabilize": False}
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=20)
        out = r.stdout or ""
        caps["stabilize"] = ("vidstabdetect" in out
                             and "vidstabtransform" in out)
    except Exception:  # noqa: BLE001 - no ffmpeg, no options
        pass
    _ENHANCE_CAPS = caps
    return dict(caps)


def _grab_gray_frame(video_path, t, sw=128):
    """One small grayscale frame as bytes (ffmpeg rawvideo), or None."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.2f}",
             "-i", video_path, "-frames:v", "1",
             "-vf", f"scale={sw}:-2,format=gray", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        out = r.stdout or b""
        if len(out) < sw * 8:
            return None
        return out
    except Exception:  # noqa: BLE001
        return None


def analyze_clip(video_path):
    """Cheap content probe for Smart enhance: dimensions/duration via
    ffprobe, plus luma / noise / contrast / motion from 3 sampled
    grayscale frames (pure Python over ~128px-wide frames, every 2nd
    pixel - about a second per clip).

    noise = mean |pixel - local 2x2 mean| (high-frequency residual):
    measured calibration on this machine - clean studio-style frames
    ~0.0-0.5, mild residual noise ~1-3, noisy broadcast ~8-12.
    Returns None when probing fails (callers use safe defaults)."""
    try:
        W, H, fps = probe_video(video_path)
        dur = probe_duration(video_path) or 0.0
        if not W or not H or not dur:
            return None
        f1 = _grab_gray_frame(video_path, dur * 0.25)
        f2 = _grab_gray_frame(video_path, dur * 0.60)
        f3 = _grab_gray_frame(video_path, dur * 0.60 + 0.5)
        frames = [f for f in (f1, f2) if f]
        if not frames:
            return None
        sw = 128
        sh = len(frames[0]) // sw
        npix = 0
        total = 0
        dev = 0.0
        for f in frames:
            for i in range(0, len(f), 2):
                v = f[i]
                total += v
                npix += 1
        luma = total / max(1, npix)
        for f in frames:
            for i in range(0, len(f), 2):
                d = f[i] - luma
                dev += d if d >= 0 else -d
        contrast = dev / max(1, npix)
        # noise: residual against the 2x2 neighbourhood mean
        acc = 0.0
        cnt = 0
        for f in frames:
            for y in range(0, sh - 1, 2):
                row = y * sw
                nxt = row + sw
                for x in range(0, sw - 1, 2):
                    p = f[row + x]
                    blur = (p + f[row + x + 1] + f[nxt + x]
                            + f[nxt + x + 1]) >> 2
                    d = p - blur
                    acc += d if d >= 0 else -d
                    cnt += 1
        noise = acc / max(1, cnt)
        motion = 0.0
        if f2 and f3 and len(f2) == len(f3):
            s = 0
            n = 0
            for i in range(0, len(f2), 2):
                d = f2[i] - f3[i]
                s += d if d >= 0 else -d
                n += 1
            motion = s / max(1, n)
        return {"w": W, "h": H, "fps": fps, "duration": dur,
                "short": min(W, H), "portrait": H > W,
                "luma": round(luma, 1), "noise": round(noise, 2),
                "contrast": round(contrast, 1),
                "motion": round(motion, 2)}
    except Exception:  # noqa: BLE001 - analysis is advisory only
        return None


def smart_enhance_plan(a):
    """Decide a plan from analyze_clip() output. Conservative by
    design: faces (portrait clips - this app's renders are talking-
    head verticals) never get more than light sharpening, heavy
    motion caps temporal denoise (smear guard), and a clip that is
    already clean at full size is honestly skipped, not re-encoded."""
    if not a:
        return {"target": None, "sharpen": "light", "denoise": "light",
                "colour": False, "stabilize": False, "skip": False,
                "why": "Analysis unavailable - safe defaults "
                       "(light denoise + light sharpen)."}
    short, noise, luma = a["short"], a["noise"], a["luma"]
    contrast, motion = a["contrast"], a["motion"]
    target = 1080 if short < 1080 else None
    if noise >= 5.0:
        denoise = "strong"
    elif noise >= 1.8:
        denoise = "light"
    else:
        denoise = "off"
    if motion > 20 and denoise == "strong":
        denoise = "light"  # fast motion + strong temporal denoise = smear
    colour = bool(luma < 60 or luma > 198 or contrast < 18)
    if noise >= 5.0:
        sharpen = "off"  # sharpening heavy noise makes it worse
    elif target:
        sharpen = "light"  # upscaling softens; restore a little bite
    elif noise < 1.2:
        sharpen = "off"  # already crisp and clean - nothing to gain,
        # and this is what lets a finished full-size clip be skipped
    elif a["portrait"]:
        sharpen = "light"  # face cap: never aggressive on portraits
    elif noise < 3.5:
        sharpen = "medium"
    else:
        sharpen = "light"
    skip = (target is None and denoise == "off" and not colour
            and sharpen == "off")
    bits = []
    if target:
        bits.append(f"upscale to {target}p")
    if denoise != "off":
        bits.append(f"{denoise} denoise (noise {noise})")
    if colour:
        bits.append(f"auto colour (luma {luma}, contrast {contrast})")
    if sharpen != "off":
        bits.append(f"{sharpen} sharpen")
    why = ("Already high quality - skipped."
           if skip else "Smart: " + (", ".join(bits) or "no change"))
    return {"target": target, "sharpen": sharpen, "denoise": denoise,
            "colour": colour, "stabilize": False, "skip": skip,
            "why": why}


def manual_enhance_plan(options, a):
    """Normalise the user's Custom options into a plan. Upscale
    targets never downscale; a target at/below the source size is
    dropped (and noted in the plan's why)."""
    options = options or {}
    short = (a or {}).get("short")
    target = options.get("target") or None
    if target:
        try:
            target = int(target)
        except (TypeError, ValueError):
            target = None
    note = ""
    if target and short and target <= short:
        note = f" (already >= {target}p, no upscale)"
        target = None
    sharpen = options.get("sharpen") or "off"
    if sharpen not in ("off", "light", "medium"):
        sharpen = "off"
    denoise = options.get("denoise") or "off"
    if denoise not in ("off", "light", "strong"):
        denoise = "off"
    caps = enhance_capabilities()
    stabilize = bool(options.get("stabilize")) and caps["stabilize"]
    bits = []
    if target:
        bits.append(f"upscale to {target}p")
    if denoise != "off":
        bits.append(f"{denoise} denoise")
    if options.get("colour"):
        bits.append("auto colour")
    if sharpen != "off":
        bits.append(f"{sharpen} sharpen")
    if stabilize:
        bits.append("stabilize")
    return {"target": target, "sharpen": sharpen, "denoise": denoise,
            "colour": bool(options.get("colour")),
            "stabilize": stabilize, "skip": False,
            "why": "Custom: " + (", ".join(bits) or "re-encode") + note}


def plan_is_empty(plan):
    """True when a plan would change nothing at all."""
    return (not plan.get("target") and plan.get("denoise") == "off"
            and not plan.get("colour") and plan.get("sharpen") == "off"
            and not plan.get("stabilize"))


def build_enhance_passes(src, out_path, plan, a, quality_tier="maximum",
                         transforms_path=None):
    """Build the ffmpeg pass list for a plan. Returns a list of
    {cmd, lo, hi, stage} with progress windows over the whole job.
    Stabilization (when planned) is a vidstabdetect pass (0-12%)
    followed by the main transform+enhance pass."""
    W = (a or {}).get("w") or 0
    H = (a or {}).get("h") or 0
    passes = []
    vf = []
    if plan.get("stabilize") and transforms_path:
        passes.append({
            "cmd": ["ffmpeg", "-y", "-v", "error", "-i", src,
                    "-vf", "vidstabdetect=shakiness=5:accuracy=15:"
                           f"result={transforms_path}",
                    "-f", "null", "-"],
            "lo": 0, "hi": 12, "stage": "Analysing camera motion"})
        vf.append(f"vidstabtransform=smoothing=30:"
                  f"input={transforms_path}")
    dn = plan.get("denoise")
    if dn == "light":
        vf.append("hqdn3d=3:2:4:3")
    elif dn == "strong":
        vf.append("hqdn3d=7:5:9:7")
    target = plan.get("target")
    if target and W and H and min(W, H) < target:
        f = target / float(min(W, H))
        tw = int(round(W * f / 2.0)) * 2
        th = int(round(H * f / 2.0)) * 2
        vf.append(f"scale={tw}:{th}:flags=lanczos")
    if plan.get("colour"):
        luma = (a or {}).get("luma", 115)
        contrast_v = (a or {}).get("contrast", 40)
        bright = max(-0.03, min(0.10, (115.0 - luma) * 0.0009))
        cont = 1.12 if contrast_v < 18 else 1.07
        vf.append(f"eq=contrast={cont:.2f}:brightness={bright:.3f}:"
                  "saturation=1.10")
    sh = plan.get("sharpen")
    if sh == "light":
        vf.append("unsharp=5:5:0.45:5:5:0.0")
    elif sh == "medium":
        vf.append("unsharp=5:5:0.8:5:5:0.0")
    cmd = (["ffmpeg", "-y", "-v", "error", "-i", src]
           + (["-vf", ",".join(vf)] if vf else [])
           + ["-map", "0:v:0", "-map", "0:a?"]
           + _encode_args(quality_tier)
           + ["-c:a", "aac", "-b:a", "192k", out_path])
    lo = 12 if passes else 0
    passes.append({"cmd": cmd, "lo": lo, "hi": 100,
                   "stage": "Enhancing"})
    return passes


class FfmpegCancelled(Exception):
    """Raised by run_ffmpeg_controlled when the caller cancels."""


def _suspend_process(pid):
    """Suspend a child process (POSIX SIGSTOP / Windows
    NtSuspendProcess). Returns True when the suspend took effect."""
    try:
        if os.name == "posix":
            import signal
            os.kill(pid, signal.SIGSTOP)
            return True
        if os.name == "nt":
            import ctypes
            ntdll = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.OpenProcess(0x0800, False, pid)
            if not handle:
                return False
            try:
                return ntdll.NtSuspendProcess(handle) == 0
            finally:
                kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return False


def _resume_process(pid):
    try:
        if os.name == "posix":
            import signal
            os.kill(pid, signal.SIGCONT)
            return True
        if os.name == "nt":
            import ctypes
            ntdll = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.OpenProcess(0x0800, False, pid)
            if not handle:
                return False
            try:
                return ntdll.NtResumeProcess(handle) == 0
            finally:
                kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return False


def run_ffmpeg_controlled(cmd, duration, progress_cb, lo, hi, stage,
                          control):
    """_run_ffmpeg_progress with a control channel: control is a
    dict the caller mutates - {"cancel": True} terminates the run
    (FfmpegCancelled), {"pause": True} suspends the ffmpeg process
    until cleared (falls back to a no-op flag the caller can read
    via control["pause_unsupported"] when the OS refuses)."""
    import threading as _th
    full = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    tail = []
    state = {"done": False, "suspended": False}

    def _reader():
        try:
            for line in proc.stdout:
                tail.append(line.rstrip())
                if len(tail) > 60:
                    del tail[:30]  # only the last lines are ever read
                if line.startswith("out_time_ms=") and duration:
                    try:
                        t = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    frac = min(1.0, max(0.0, t / duration))
                    if progress_cb:
                        progress_cb(lo + int((hi - lo) * frac), stage)
        except Exception:  # noqa: BLE001
            pass
        state["done"] = True

    reader = _th.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        while not state["done"]:
            if control.get("cancel"):
                if state["suspended"]:
                    _resume_process(proc.pid)  # a stopped process
                    state["suspended"] = False  # cannot take SIGTERM
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
                raise FfmpegCancelled()
            want_pause = bool(control.get("pause"))
            if want_pause and not state["suspended"]:
                if _suspend_process(proc.pid):
                    state["suspended"] = True
                else:
                    control["pause_unsupported"] = True
            elif not want_pause and state["suspended"]:
                _resume_process(proc.pid)
                state["suspended"] = False
            reader.join(timeout=0.2)
    finally:
        if state["suspended"]:
            _resume_process(proc.pid)
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 - wedged ffmpeg: kill it
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: "
                           + " | ".join(tail[-6:])[:400])


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
            "outline_color": "#000000", "bold": 1, "auto_emoji": False}
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
    eff["auto_emoji"] = bool(eff.get("auto_emoji"))
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
    # Auto-emoji: the render-level toggle OR the Caption Studio
    # per-params toggle - both mean "one keyword emoji per line".
    use_emojis = bool(emojis) or bool(eff and eff.get("auto_emoji"))
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
            return _esc(t)

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
                    f"{{\\1c{p_prim}&}}{_p_anim_tags()}{_p_word(w)}"
                    + (_line_emoji([w]) if use_emojis else ""))
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
                    line_txt = " ".join(parts)
                    if use_emojis:
                        _em = _line_emoji(line)
                        if _em:
                            line_txt += _em
                    body.append(
                        f"Dialogue: {1 if p_box else 0},"
                        f"{ts(w['start'])},{ts(ev_end)},Caption,,0,0,0,,"
                        + line_txt)
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
                if spk_cols is not None:
                    # speaker colors win over highlight - that IS the feature
                    col = spk_cols[w.get("speaker", 0) % 2]
                elif highlight and _is_highlight_word(w["word"]):
                    col = "&H002EFFC8"
                else:
                    col = prim_col
                parts.append(f"{{\\k{cs}\\1c{col}&}}{txt}")
            if use_emojis and parts:
                _em = _line_emoji(line)
                if _em:
                    parts[-1] += _em
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


def _guard_removed_against_words(removed, words, margin=0.03):
    """Shrink trim ranges so a cut never eats into a kept word.

    A range may fully contain the filler words it targets, but where
    it only PARTIALLY overlaps a word (filler padding, or an audio-
    detected silence whose edge lands on a word tail) the range is
    pulled back to that word's boundary, keeping a small margin of
    air. A range trapped entirely inside one word is dropped - a
    cut inside a spoken word is never allowed."""
    out = []
    for s, e in removed:
        for w in words:
            ws, we = w["start"], w["end"]
            if ws >= s and we <= e:
                continue  # word fully inside the cut: it is the target
            if ws < s < we:
                s = min(we + margin, e)
            if ws < e < we:
                e = max(ws - margin, s)
            if e - s <= 0.05:
                break
        if e - s > 0.05:
            out.append((s, e))
    return out


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
        # Clamp to the clip window: a word straddling the cut keeps
        # its full span in the transcript, but a cue must never start
        # before or end after the clip it belongs to (exports shipped
        # cue ends past the render's real duration).
        cs = max(start, line[0]["start"])
        ce = min(end, line[-1]["end"])
        if ce <= cs:
            continue
        blocks.append(f"{n}\n{ts(cs)} --> {ts(ce)}\n{text}\n")
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

# Line-level auto-emoji: at most ONE emoji per caption line, appended
# at the end of the line, chosen by the first keyword actually spoken
# in that line. Curated for police/bodycam content first (the app's
# home genre), energy/reaction words second.
LINE_EMOJI_MAP = {
    "police": "👮", "officer": "👮", "officers": "👮", "cop": "👮",
    "cops": "👮", "trooper": "👮", "sheriff": "👮", "deputy": "👮",
    "chase": "🚔", "pursuit": "🚔", "car": "🚔", "vehicle": "🚔",
    "gun": "🚨", "weapon": "🚨", "pistol": "🚨", "rifle": "🚨",
    "shots": "🚨", "shooting": "🚨", "arrest": "🚨", "arrested": "🚨",
    "siren": "🚨", "sirens": "🚨", "breaking": "🚨",
    "fire": "🔥", "flames": "🔥", "explosion": "💥", "exploded": "💥",
    "danger": "⚠️", "dog": "🐶", "k9": "🐶",
    "run": "🏃", "running": "🏃", "flee": "🏃", "fled": "🏃",
    "money": "💰", "cash": "💰",
    "scary": "😱", "terrifying": "😱", "unbelievable": "😱",
    "funny": "😂", "hilarious": "🤣", "lol": "😂", "haha": "😂",
    "wow": "😮", "insane": "🤯", "crazy": "🤪", "amazing": "😲",
    "incredible": "🤩", "caught": "📸", "exposed": "📸",
    "secret": "🤫", "love": "❤️", "win": "🏆", "fight": "🥊",
}


def _line_emoji(line_words):
    """The single emoji for a caption line (or ''): the first keyword
    in the line that has one wins, so the emoji always reflects a word
    that was actually spoken in that line."""
    for w in line_words:
        em = LINE_EMOJI_MAP.get(_clean_word(w["word"]))
        if em:
            return em
    return ""

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


# ---------------------------------------------------------------------------
# Phase 1: caption translation (Built-in AI, cue-faithful)
# ---------------------------------------------------------------------------
# Translates the SAME cues write_srt/write_vtt export for a clip -
# same grouping, same timestamps - so a translated file lines up with
# its source export cue for cue. The model only ever receives the
# cue texts and must return exactly as many lines as it was given;
# a count mismatch is retried once, then reported honestly. Without
# the Built-in AI model there is no translation - no fake output.

TRANSLATE_LANGS = {
    "bn": "Bengali", "es": "Spanish", "hi": "Hindi", "ar": "Arabic",
    "pt": "Portuguese", "fr": "French", "de": "German", "ur": "Urdu",
}

TRANSLATE_NEEDS_AI = (
    "Caption translation runs on the Built-in AI, which isn't "
    "downloaded yet on this PC. Download it (Step 2 card or the AI "
    "Manager), then translate.")


def caption_cues(words, start, end):
    """The cue list for a window, built exactly like write_srt's:
    [{start, end, text}] with the export's own timestamps. Cue
    times are clamped to the window (see write_srt): a word
    straddling the cut must not push a cue past the clip's end."""
    win = [w for w in words if w["end"] > start and w["start"] < end]
    win.sort(key=lambda w: w["start"])
    cues = []
    for line in _group_lines(win):
        cs = max(start, line[0]["start"])
        ce = min(end, line[-1]["end"])
        if ce <= cs:
            continue
        cues.append({"start": cs, "end": ce,
                     "text": " ".join(w["word"] for w in line)})
    return cues


def _cue_ts(s, sep=","):
    s = max(0, s)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    ms = int((s % 1) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}{sep}{ms:03d}"


def write_srt_cues(cues, out_path):
    blocks = []
    for n, c in enumerate(cues, 1):
        blocks.append(f"{n}\n{_cue_ts(c['start'])} --> "
                      f"{_cue_ts(c['end'])}\n{c['text']}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return out_path


def write_vtt_cues(cues, out_path):
    blocks = ["WEBVTT\n"]
    for c in cues:
        blocks.append(f"{_cue_ts(c['start'], '.')} --> "
                      f"{_cue_ts(c['end'], '.')}\n{c['text']}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return out_path


def _extract_json_array(text, n):
    """First balanced [...] in a model reply, as a list of exactly n
    strings. Raises ValueError otherwise."""
    if not text:
        raise ValueError("empty model reply")
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("[")
    if start < 0:
        raise ValueError("no JSON array in model reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    data = json.loads(t[start:i + 1])
                    if not isinstance(data, list) \
                            or len(data) != n \
                            or not all(isinstance(x, str)
                                       for x in data):
                        raise ValueError(
                            f"expected {n} translated lines, got "
                            f"{len(data) if isinstance(data, list) else 'a non-list'}")
                    return data
    raise ValueError("unbalanced JSON array in model reply")


def translate_captions(words, start, end, lang, chat_fn=None,
                       progress_cb=None):
    """Translate a clip window's caption cues into `lang` (a key of
    TRANSLATE_LANGS) with the Built-in AI.

    Returns [{start, end, text}] - cue count and timings identical
    to the source export. chat_fn is injectable for tests; the real
    path is local_ai.chat and requires the downloaded model.
    Raises RuntimeError with a plain message on any failure."""
    lang = str(lang or "").strip().lower()
    if lang not in TRANSLATE_LANGS:
        raise RuntimeError("Pick a supported language first: "
                           + ", ".join(sorted(TRANSLATE_LANGS)))
    cues = caption_cues(words, start, end)
    if not cues:
        raise RuntimeError("This clip has no captions to translate.")
    if chat_fn is None:
        import local_ai
        if not local_ai.model_ready():
            raise RuntimeError(TRANSLATE_NEEDS_AI)
        chat_fn = local_ai.chat
    name = TRANSLATE_LANGS[lang]
    out = []
    chunk = 30
    for i in range(0, len(cues), chunk):
        part = cues[i:i + chunk]
        lines = [c["text"] for c in part]
        prompt = (
            f"Translate each of these {len(lines)} subtitle lines "
            f"into {name}. Subtitles must stay short and natural; "
            "keep names, numbers and places unchanged. Reply with "
            f"ONLY a JSON array of exactly {len(lines)} strings, in "
            "the same order, with nothing else around it.\n"
            + json.dumps(lines, ensure_ascii=False))
        translated = None
        last_err = None
        for _attempt in range(2):
            reply = chat_fn([{"role": "user", "content": prompt}])
            try:
                translated = _extract_json_array(reply, len(lines))
                break
            except ValueError as e:
                last_err = e
        if translated is None:
            raise RuntimeError(
                "The Built-in AI's translation could not be used "
                f"({last_err}). Nothing was written - try again.")
        out.extend({**c, "text": t.strip()}
                   for c, t in zip(part, translated))
        if progress_cb:
            progress_cb(int(len(out) / len(cues) * 100),
                        f"Translated {len(out)}/{len(cues)} lines "
                        f"({name})")
    return out


def write_xml_timeline(clips, out_path, fps=30):
    """FCPXML 1.9 timeline of the rendered clips.

    Imports into DaVinci Resolve and Premiere Pro. clips is a list of
    {path, title, duration} with duration in seconds.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    def stamp(s):
        return f"{max(1, int(round(s * fps)))}/{fps}s"

    # The sequence format must match the clips themselves: hardcoding
    # 1080x1920 handed 16:9 / 1:1 projects a portrait timeline canvas.
    width, height = 1080, 1920
    if clips:
        try:
            _w, _h, _f = probe_video(clips[0]["path"])
            if _w and _h:
                width, height = int(_w), int(_h)
        except Exception:  # noqa: BLE001 - keep the 9:16 default
            pass
    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(resources, "format", id="f1",
                  name="FFVideoFormatRateUndefined",
                  frameDuration=f"1/{fps}s", width=str(width),
                  height=str(height))
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
                             max_clips=8, category="general"):
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
        m = {
            "start": round(s, 2), "end": round(e, 2),
            "title": title, "hook": hook, "virality": v,
            "hook_score": v, "flow_score": max(30, v - 10),
            "value_score": max(30, v - 5),
            "reason": f"Keyword match for: {q}",
            "signals": window_signals(win_words, None, e - s),
            "matched": q, "offline": True,
        }
        _attach_subscores(m, win_words, None, e - s, category=category)
        moments.append(m)
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

def _sample_face_center(video_path, start, end, samples=5):
    """Median (cx_frac, cy_frac) of the largest face across a few
    frames of [start, end], via the bundled YuNet detector - or None
    when no face is found / OpenCV can't read the video. Used to
    place smart crops for the 16:9 and 1:1 aspect exports."""
    try:
        import cv2
        from facetrack import MODEL_PATH
        if not os.path.exists(MODEL_PATH):
            return None
        det = cv2.FaceDetectorYN.create(MODEL_PATH, "", (320, 320),
                                        0.55, 0.3, 5000)
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        pts = []
        try:
            span = max(end - start, 0.1)
            for i in range(samples):
                t = start + span * (i + 0.5) / samples
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                h, w = frame.shape[:2]
                det.setInputSize((w, h))
                _, faces = det.detect(frame)
                if faces is None or len(faces) == 0:
                    continue
                f = max(faces, key=lambda f: float(f[2]) * float(f[3]))
                pts.append(((float(f[0]) + float(f[2]) / 2.0) / w,
                            (float(f[1]) + float(f[3]) / 2.0) / h))
        finally:
            cap.release()
        if not pts:
            return None
        pts.sort()
        return pts[len(pts) // 2]
    except Exception:  # noqa: BLE001 - crop placement is best-effort
        return None


def _apply_audio_polish(clip_path, denoise=False):
    """Post-pass: loudness-normalise a rendered clip to the social
    platform target (-14 LUFS integrated, -1 dBTP, LRA 11), with an
    optional gentle denoise (afftdn) first. Two-pass loudnorm: the
    clip is measured (audio only), then the correction is applied
    with the measured values so the landing is accurate; if the
    measured pass fails it retries as a plain single pass. The video
    stream is copied untouched. Raises RuntimeError on failure -
    callers treat polish as best-effort and keep the unpolished clip.
    """
    pre = ["afftdn=nf=-25"] if denoise else []
    target = "loudnorm=I=-14:TP=-1:LRA=11"
    loud = target
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", clip_path, "-vn",
             "-af", ",".join(pre + [target + ":print_format=json"]),
             "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True)
        tail = r.stderr[r.stderr.rfind("{"):]
        vals = json.loads(tail[:tail.rfind("}") + 1])
        loud = (target
                + f":measured_I={vals['input_i']}"
                + f":measured_TP={vals['input_tp']}"
                + f":measured_LRA={vals['input_lra']}"
                + f":measured_thresh={vals['input_thresh']}"
                + f":offset={vals['target_offset']}:linear=true")
    except Exception:  # noqa: BLE001 - fall back to single-pass
        loud = target
    tmp = clip_path + ".polish.mp4"
    for attempt_af in (",".join(pre + [loud]), ",".join(pre + [target])):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip_path,
               "-c:v", "copy", "-af", attempt_af,
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-movflags", "+faststart", tmp]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, clip_path)
            return clip_path
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise RuntimeError("audio polish failed: " + r.stderr[-400:])


def _render_tracked(video_path, start, end, keep_segs, removed, ass_path,
                    W, H, ratio, out_path, progress_cb=None,
                    punch_zoom=False, enc_preset="slow", enc_crf="16",
                    face_center=None):
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
    if face_center and crop_h < src_h:
        # square/landscape crop out of a taller frame: centre the
        # band on the face instead of the frame middle
        y_top = int(max(0, min(src_h - crop_h,
                               face_center[1] * src_h - crop_h / 2)))

    trajectory = None
    try:
        tracker = FaceTracker()
        if tracker.backend == "none":
            # No face detector available: the bundled YuNet model is
            # missing/corrupt AND this OpenCV build ships no Haar
            # cascade (opencv-python 5.x dropped the bundled XML).
            # Degrade to the documented center-crop fallback instead
            # of failing the render - the old hard failure blamed a
            # missing opencv-python even when cv2 was installed and
            # working fine.
            if progress_cb:
                progress_cb(3, "Face tracking unavailable - "
                               "using a center crop")
        else:
            if progress_cb:
                progress_cb(3, "Detecting faces")
            samples = sample_face_centers(
                video_path, start, end, tracker, progress_cb=None)
            samples = [(t - start, cx) for t, cx in samples]
            trajectory = smooth_trajectory(samples, src_w, crop_w)
            if progress_cb:
                n_found = sum(1 for _, cx in samples if cx is not None)
                progress_cb(12, f"Face tracking: {n_found} face samples")
    except Exception:  # noqa: BLE001 - never break a render
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
    # A "successful" encode can still be a stub: when OpenCV's bundled
    # FFmpeg cannot decode the source codec (AV1 is the common case -
    # YouTube serves it at 1080p), cap.read() fails on frame 0, the
    # loop writes nothing, and ffmpeg happily muxes an audio-only file
    # with exit code 0. Count what was actually written against the
    # kept timeline; if the pipe starved, drop the stub and let the
    # plain ffmpeg path (which decodes AV1 fine) do the render.
    expected_frames = sum(e - s for s, e in keep_segs) * fps
    if written < expected_frames * 0.9:
        try:
            os.remove(out_path)
        except OSError:
            pass
        if progress_cb:
            progress_cb(10, "Face tracking couldn't read this video's "
                            "codec - rendering without it")
        return _render_plain(video_path, start, end, keep_segs,
                             ass_path, W, H, ratio, out_path,
                             progress_cb, punch_zoom=punch_zoom,
                             enc_preset=enc_preset, enc_crf=enc_crf,
                             face_center=face_center)
    return out_path


def _render_plain(video_path, start, end, keep_segs, ass_path,
                  W, H, ratio, out_path, progress_cb=None,
                  punch_zoom=False, enhance_clarity=False,
                  enc_preset="slow", enc_crf="16", face_center=None):
    """ffmpeg-only render: crop/scale/captions, optional silence/filler
    cuts via filter_complex segment concat.

    punch_zoom: subtle zoom-punch on the hook (~1.0x -> ~1.07x over the
        first 3s) then a slow drift, capped at 1.16x (zoompan, 1:1 frames).
    enhance_clarity: light denoise + unsharp ("enhance" - NOT an AI
        upscale; honest naming is deliberate).
    face_center: optional (cx, cy) fractions of the subject's face -
        for the 16:9 / 1:1 exports the crop band/square is centred on
        the face instead of the frame middle (never a blurred pad).
    """
    crop = crop_filter(ratio)
    if face_center and ratio in ("16:9", "1:1"):
        try:
            _sw, _sh, _sf = probe_video(video_path)
        except Exception:  # noqa: BLE001 - keep the centre crop
            _sw = _sh = 0
        if _sw and _sh:
            _cx, _cy = face_center
            if ratio == "16:9":
                _ch = _even(_sw * 9 / 16)
                if _ch < _sh:
                    _y = int(max(0, min(_sh - _ch,
                                        _cy * _sh - _ch / 2)))
                    crop = f"crop={_sw}:{_ch}:0:{_y}"
            else:  # 1:1
                _side = _even(min(_sw, _sh))
                _x = int(max(0, min(_sw - _side,
                                    _cx * _sw - _side / 2)))
                _y = int(max(0, min(_sh - _side,
                                    _cy * _sh - _side / 2)))
                crop = f"crop={_side}:{_side}:{_x}:{_y}"
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


EMPHASIS_ZOOM_LEVELS = {"gentle": 0.08, "medium": 0.14,
                        "strong": 0.22}


def _strongest_beats(cap_words, total_dur, max_beats=2):
    """Strongest spoken beats on the OUTPUT timeline (cap_words are
    already rebased): 1s windows scored by energy words, ?/! marks,
    SHOUTED words and density. Returns [(center_t, score)] in time
    order - at most max_beats, >= 2.5s apart, >= 1s from the edges so
    the zoom envelope has room to ramp."""
    if not cap_words or total_dur < 4:
        return []
    wins = []
    t = 1.0
    while t + 0.5 <= total_dur - 0.5:
        ww = [w for w in cap_words
              if w["end"] > t - 0.5 and w["start"] < t + 0.5]
        score = 0.3 * len(ww)
        for w in ww:
            if _clean_word(w["word"]) in OFFLINE_ENERGY_WORDS:
                score += 2.0
            if "!" in w["word"] or "?" in w["word"]:
                score += 2.0
            if len(w["word"]) > 2 and w["word"].isupper():
                score += 1.0
        if score > 0:
            wins.append((t, score))
        t += 0.25
    if not wins:
        return []
    wins.sort(key=lambda x: x[1], reverse=True)
    top = wins[0][1]
    picked = []
    for t, s in wins:
        if picked and s < 0.7 * top:
            continue
        if all(abs(t - pt) >= 2.5 for pt, _ in picked):
            picked.append((t, s))
        if len(picked) >= max_beats:
            break
    return sorted(picked)


def _beat_center(clip_path, t):
    """(cx, cy) fractions for a zoom centre at time t: horizontally
    on the tracked face when one is visible in that frame (YuNet via
    facetrack), vertically at 40% (where a face sits in a reframed
    vertical clip); falls back to (0.5, 0.40) with no face."""
    try:
        import cv2
        cap = cv2.VideoCapture(clip_path)
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok, frame = cap.read()
        finally:
            cap.release()
        if ok and frame is not None:
            from facetrack import FaceTracker
            cx_px = FaceTracker().detect_center(frame)
            if cx_px is not None and frame.shape[1]:
                return (min(0.85, max(0.15, cx_px / frame.shape[1])),
                        0.40)
    except Exception:  # noqa: BLE001 - centre is best-effort
        pass
    return (0.5, 0.40)


def _apply_emphasis_zoom(clip_path, beats, peak, progress_cb=None):
    """Post-pass (same best-effort pattern as the logo pass): smooth
    punch-in zooms on the rendered clip at the given beats. zoompan
    evaluates the zoom factor per frame (crop cannot - its h
    expression has no frame time at configure), easing in/out
    around each beat - audio copied untouched."""
    W, H, fps = probe_video(clip_path)
    if not W or not H:
        raise RuntimeError("could not probe the rendered clip")
    env = "+".join(
        f"(clip((time-{t - 0.9:.3f})/0.35,0,1)"
        f"*clip(({t + 0.9:.3f}-time)/0.35,0,1))"
        for t, _s in beats)
    bt = max(beats, key=lambda b: b[1])[0]
    cx, cy = _beat_center(clip_path, bt)
    vf = (f"zoompan=z='1+{peak:.3f}*({env})'"
          f":x='max(0,min(iw-iw/zoom,{cx:.3f}*iw-(iw/zoom)/2))'"
          f":y='max(0,min(ih-ih/zoom,{cy:.3f}*ih-(ih/zoom)/2))'"
          f":d=1:s={W}x{H}:fps={fps or 30},setsar=1")
    tmp = clip_path + ".zoom.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip_path,
           "-vf", vf, "-c:v", "libx264", "-preset", "slow",
           "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("emphasis zoom failed: "
                           + r.stderr[-400:])
    os.replace(tmp, clip_path)
    return clip_path


# ---------------------------------------------------------------------------
# Auto B-roll: a free-licensed stock cutaway (Wikimedia Commons, no API
# key) over a long single-shot talking stretch. Audio and captions run
# untouched underneath. Needs internet - every failure degrades to
# "render without B-roll", never a failed render.
# ---------------------------------------------------------------------------
BROLL_MIN_STRETCH_S = 6.0    # talking stretch must be at least this long
BROLL_MAX_LEN_S = 6.0        # cutaway length cap
BROLL_HOOK_INSET_S = 3.0     # never cover the hook opening

_BROLL_PHRASES = (
    ("police", "police car"), ("officer", "police car"),
    ("cop", "police car"), ("sheriff", "police car"),
    ("chase", "police chase"), ("pursuit", "police chase"),
    ("dog", "police dog"), ("k9", "police dog"),
    ("fire", "fire flames"), ("gun", "police"),
    ("car", "car driving street"), ("vehicle", "car driving street"),
    ("truck", "truck highway"), ("highway", "highway traffic"),
    ("street", "city street"), ("city", "city street"),
    ("hospital", "hospital building"), ("school", "school building"),
)


def _broll_keyword(win_words):
    """Commons search phrase for a window: a curated phrase when a
    known bodycam keyword is spoken, else the window's most frequent
    content word."""
    said = {_clean_word(w["word"]) for w in win_words}
    for kw, phrase in _BROLL_PHRASES:
        if kw in said:
            return phrase
    freq = {}
    for w in win_words:
        cw = _clean_word(w["word"])
        if len(cw) > 3 and cw not in _OFFLINE_STOPWORDS:
            freq[cw] = freq.get(cw, 0) + 1
    return max(freq, key=freq.get) if freq else ""


def _find_broll_window(cap_words, total_dur):
    """(start, end, keyword) for a cutaway over the longest stretch
    of near-continuous speech (words with no gap > 1.2s) in the
    OUTPUT timeline, kept clear of the hook opening - or None."""
    if not cap_words or total_dur < BROLL_MIN_STRETCH_S + 2:
        return None
    spans = []
    a = cap_words[0]["start"]
    prev = cap_words[0]
    for w in cap_words[1:]:
        if w["start"] - prev["end"] > 1.2:
            spans.append((a, prev["end"]))
            a = w["start"]
        prev = w
    spans.append((a, prev["end"]))
    best = None
    for s, e in spans:
        if e - s >= BROLL_MIN_STRETCH_S and (best is None
                                            or e - s > best[0]):
            best = (e - s, s, e)
    if not best:
        return None
    _len, s, e = best
    w0 = max(s + 0.8, BROLL_HOOK_INSET_S)
    if e - w0 < 2.5:
        return None
    w1 = min(e, w0 + BROLL_MAX_LEN_S)
    win = [w for w in cap_words if w["end"] > w0 and w["start"] < w1]
    kw = _broll_keyword(win)
    if not kw:
        return None
    return (round(w0, 2), round(w1, 2), kw)


_BROLL_CACHE_MAX_BYTES = 500 * 1024 * 1024
_BROLL_CACHE_MAX_FILES = 60


def _broll_cache_dir():
    import local_ai
    d = os.path.join(local_ai.MODELS_DIR, "broll_cache")
    os.makedirs(d, exist_ok=True)
    # Cap the cache: it grew by one stock clip per b-roll render,
    # forever. Oldest files go first; everything in here is a
    # re-downloadable stock clip, never user work.
    try:
        files = [(os.path.getmtime(os.path.join(d, n)),
                  os.path.getsize(os.path.join(d, n)),
                  os.path.join(d, n))
                 for n in os.listdir(d)
                 if os.path.isfile(os.path.join(d, n))]
        total = sum(sz for _m, sz, _p in files)
        files.sort()
        while files and (total > _BROLL_CACHE_MAX_BYTES
                         or len(files) > _BROLL_CACHE_MAX_FILES):
            _m, sz, p = files.pop(0)
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
    except OSError:
        pass
    return d


def _strip_html(text):
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _commons_broll_clip(keyword, want_dur, progress_cb=None):
    """Find + download (cached) a free-licensed stock video from
    Wikimedia Commons for keyword. Returns (path, credit) - credit
    carries title/artist/license/page_url/source_url/duration for
    attribution. Raises RuntimeError with a plain message."""
    import urllib.parse
    import urllib.request
    api = ("https://commons.wikimedia.org/w/api.php?action=query"
           "&format=json&generator=search&gsrnamespace=6&gsrlimit=8"
           "&prop=imageinfo&iiprop=url%7Csize%7Cmime%7Cextmetadata"
           "&gsrsearch="
           + urllib.parse.quote(keyword + " filetype:video"))
    req = urllib.request.Request(
        api, headers={"User-Agent": "ItrajiCliper/1.0 (auto b-roll)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"stock search failed ({e})") from e
    pages = (data.get("query") or {}).get("pages") or {}
    cands = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        url = (ii.get("url") or "").split("?")[0]
        mime = ii.get("mime") or ""
        if not url or not (mime.startswith("video/")
                           or mime == "application/ogg"):
            continue
        size = int(ii.get("size") or 0)
        if size > 60_000_000:
            continue
        dur = float(ii.get("duration") or 0)
        meta = ii.get("extmetadata") or {}
        cands.append({
            "url": url, "size": size, "duration": dur,
            "title": str(p.get("title") or "").removeprefix("File:"),
            "page_url": ii.get("descriptionurl") or "",
            "artist": _strip_html(
                (meta.get("Artist") or {}).get("value", ""))[:80]
                or "Wikimedia Commons contributor",
            "license": ((meta.get("LicenseShortName") or {})
                        .get("value", "") or "see file page")[:40],
        })
    if not cands:
        raise RuntimeError(f"no stock video found for '{keyword}'")
    # prefer a clip at least as long as the window, then smaller files
    cands.sort(key=lambda c: (0 if c["duration"] >= want_dur else 1,
                              c["size"]))
    pick = cands[0]
    fname = (hashlib.sha1(pick["url"].encode()).hexdigest()[:16]
             + os.path.splitext(pick["url"])[1].lower())
    path = os.path.join(_broll_cache_dir(), fname)
    if not (os.path.isfile(path) and os.path.getsize(path) > 100_000):
        tmp = path + ".part"
        try:
            req = urllib.request.Request(
                pick["url"],
                headers={"User-Agent": "ItrajiCliper/1.0 (auto b-roll)"})
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError(f"stock download failed ({e})") from e
    credit = {"title": pick["title"], "artist": pick["artist"],
              "license": pick["license"], "page_url": pick["page_url"],
              "source_url": pick["url"], "duration": pick["duration"]}
    return path, credit


def _apply_broll(clip_path, broll_path, w0, w1, W, H):
    """Post-pass: full-frame cutaway to the stock clip during
    [w0, w1] of the rendered clip; the clip's own audio (and its
    burned captions) continue underneath - the overlay only swaps
    the picture for that window."""
    fc = (f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},setpts=PTS-STARTPTS+{w0:.3f}/TB[b];"
          f"[0:v][b]overlay=0:0:enable='between(t,{w0:.3f},{w1:.3f})'"
          "[v]")
    tmp = clip_path + ".broll.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", clip_path,
           "-i", broll_path, "-filter_complex", fc,
           "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "slow", "-crf", "17",
           "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("b-roll overlay failed: " + r.stderr[-400:])
    os.replace(tmp, clip_path)
    return clip_path


# ---------------------------------------------------------------------------
# Cover Maker: pick a strong frame from a rendered clip and stamp the
# hook/title on it in the app's caption style -> 1080x1920 JPG cover.
# ---------------------------------------------------------------------------
def _cover_candidates(cap_words, total_dur):
    cands = {round(total_dur * f, 2) for f in (0.2, 0.4, 0.6, 0.8)}
    for bt, _sc in _strongest_beats(cap_words, total_dur, max_beats=2):
        cands.add(round(bt, 2))
        cands.add(round(bt + 0.4, 2))
    return sorted(t for t in cands
                  if 0.25 <= t <= max(0.3, total_dur - 0.25))


def make_cover(clip_path, cap_words, out_path, text, W=1080, H=1920):
    """Auto-pick a cover frame from a rendered clip: candidates at
    the strongest spoken beats plus spread points, scored by face
    presence, sharpness (Laplacian variance) and sane brightness.
    The winner is centre-cropped to 9:16 around the face when one
    is present, and the title is stamped in the app caption style
    (Archivo Black, white with a black outline) via drawtext.
    Returns {path, t, face, sharpness, stamped}."""
    import cv2
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", clip_path], capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        raise RuntimeError("could not read the clip")
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        raise RuntimeError("could not open the clip")
    tracker = None
    try:
        import facetrack
        tracker = facetrack.FaceTracker()
    except Exception:  # noqa: BLE001 - face bonus is optional
        tracker = None
    best = None
    for t in _cover_candidates(cap_words or [], dur):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        okf, frame = cap.read()
        if not okf or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        mean = float(gray.mean())
        bright_pen = 0.0 if 35 <= mean <= 225 else 300.0
        face_cx = None
        face_bonus = 0.0
        if tracker is not None:
            try:
                faces = tracker.detect_faces(frame)
            except Exception:  # noqa: BLE001
                faces = []
            if faces:
                cx, area = max(faces, key=lambda f: f[1])
                face_cx = cx
                face_bonus = 500.0 + min(
                    500.0, area / max(1, frame.shape[0]
                                      * frame.shape[1]) * 4000.0)
        score = min(sharp, 900.0) + face_bonus - bright_pen
        if best is None or score > best["score"]:
            best = {"t": t, "frame": frame, "face_cx": face_cx,
                    "sharp": sharp, "score": score}
    cap.release()
    if best is None:
        raise RuntimeError("could not read any frame from the clip")
    frame = best["frame"]
    fh, fw = frame.shape[:2]
    target = W / H
    if fw / fh > target:            # too wide: crop the width
        cw, ch = int(fh * target), fh
        cx = best["face_cx"] if best["face_cx"] is not None else fw / 2
        x0 = int(min(max(cx - cw / 2, 0), fw - cw))
        y0 = 0
    else:                            # too tall: crop the height
        cw, ch = fw, int(fw / target)
        x0 = 0
        y0 = int(min(max(fh * 0.35 - ch / 2, 0), fh - ch))
    crop = frame[y0:y0 + ch, x0:x0 + cw]
    cover = cv2.resize(crop, (W, H), interpolation=cv2.INTER_LANCZOS4)
    stamped = False
    text = " ".join((text or "").split()).upper()
    if text:
        tmp_png = out_path + ".tmp.png"
        cv2.imwrite(tmp_png, cover)
        # wrap into balanced lines of at most ~14 chars (Archivo
        # Black caps are wide - the size estimate below assumes a
        # 0.78em average advance, so lines must stay short)
        words_t = text.split()
        n_lines = max(1, min(3, -(-len(text) // 15)))
        lines, cur = [], ""
        per = -(-len(words_t) // n_lines)
        for i, wd in enumerate(words_t):
            if i and i % per == 0:
                lines.append(cur)
                cur = ""
            cur = (cur + " " + wd).strip()
        if cur:
            lines.append(cur)
        longest = max(len(l) for l in lines)
        size = int(max(44, min(118, (W * 0.92)
                               / (0.78 * max(1, longest)))))
        tf = out_path + ".txt"
        with open(tf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        fonts_dir = FONTS_DIR or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fonts")
        font = os.path.join(fonts_dir, "ArchivoBlack-Regular.ttf")

        def _esc(p):
            return (p.replace("\\", "/").replace(":", "\\:")
                     .replace("'", "\\'"))
        vf = (f"drawtext=fontfile='{_esc(font)}'"
              f":textfile='{_esc(tf)}':fontsize={size}"
              f":fontcolor=white:borderw={max(3, size // 11)}"
              f":bordercolor=black:x=(w-text_w)/2:y=h*0.07"
              f":line_spacing={size // 5}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", tmp_png,
               "-vf", vf, "-frames:v", "1", "-q:v", "3", out_path]
        rr = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
        stamped = rr.returncode == 0 and os.path.exists(out_path)
        for junk in (tmp_png, tf):
            try:
                os.remove(junk)
            except OSError:
                pass
        if not stamped:              # plain cover beats no cover
            cv2.imwrite(out_path, cover,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
    else:
        cv2.imwrite(out_path, cover, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not os.path.exists(out_path):
        raise RuntimeError("cover export failed")
    return {"path": out_path, "t": round(best["t"], 2),
            "face": best["face_cx"] is not None,
            "sharpness": round(best["sharp"], 1), "stamped": stamped}


def render_clip(video_path, moment, words, style_name, out_path,
                progress_cb=None, face_track=False, remove_fillers=False,
                remove_silences=False, auto_censor=False, emojis=False,
                highlight=False, ratio="9:16", quality="1080p",
                custom_styles=None, edited_words=None,
                speaker_colors=None, hook_overlay=True, cta_text=None,
                safe_zone=False, punch_zoom=False, enhance_clarity=False,
                logo_path=None, quality_tier="maximum",
                caption_params=None, emphasis_zoom=False,
                emphasis_level="gentle", broll=False,
                audio_polish=False, denoise_audio=False):
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
    emphasis_zoom -> punch-in zoom centred on the subject at the
        clip's strongest spoken beat(s); emphasis_level picks the
        peak (gentle/medium/strong). Best-effort post-pass.
    broll -> free-licensed stock cutaway (Wikimedia Commons) over a
        long talking stretch; needs internet; any failure renders
        without it. When used, a <out>.broll.json sidecar records the
        source for attribution.
    audio_polish -> loudness-normalise the finished clip to -14 LUFS
        (social platform target); denoise_audio adds a gentle afftdn
        pass first. Post-pass on the rendered file, best-effort: a
        failed polish keeps the unpolished clip and says so.
    For the 16:9 / 1:1 aspect exports the crop is centred on the
    sampled face position (smart crop - never a blurred pad); 9:16
    keeps its historical centre/tracked behaviour.
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
    if style_name and style_name not in CUSTOM_STYLES \
            and style_name not in STYLES:
        # An unknown style id used to fall back to Bold Pop inside
        # _get_style with no signal anywhere, so a typo'd style (or a
        # stale project saved against a renamed style) rendered the
        # WRONG captions and nobody could tell. Fail loudly instead:
        # every legitimate caller sends an id from the corepack
        # styles or the user's custom styles (both checked here), so
        # a miss is a real error the user should hear about.
        raise ValueError(
            f"Unknown caption style '{style_name}' - it is not one "
            "of the built-in or custom styles, so this render was "
            "stopped instead of silently using Bold Pop.")

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
            removed += detect_silences(video_path, start, end,
                                       min_dur=0.7)
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

    if removed:
        # Trim safety guards: cuts may swallow filler words whole but
        # never eat into a kept word, and the total cut is capped at
        # 35% of the clip - beyond that the "trim" is really a
        # different edit, so refuse honestly instead of mangling it.
        removed = _guard_removed_against_words(removed, win_words)
        cut_total = sum(e - s for s, e in removed)
        span = end - start
        if cut_total > 0.35 * span:
            raise RuntimeError(
                "Filler/silence trim would cut "
                f"{cut_total:.1f}s of this {span:.0f}s clip "
                f"({cut_total / span * 100:.0f}%) - over the 35% "
                "safety limit, so nothing was rendered. Turn off a "
                "trim option or pick a longer moment.")

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
    # Face-aware crop placement for the landscape/square exports
    # (sampled once per render; None -> centre crop as before).
    face_center = None
    if ratio in ("16:9", "1:1"):
        face_center = _sample_face_center(video_path, start, end)
    # The encode and every post-pass below work on a .part sibling;
    # the finished file is renamed onto the real name at the end.
    # Writing straight to the final path meant a failed RE-render
    # destroyed the previously good clip at that path, and an
    # ffmpeg orphaned by an app kill kept writing the final name
    # and produced a complete clip the app had no record of.
    _final_out = out_path
    out_path = _part_sibling(_final_out)
    _render_ok = False
    try:
        if face_track:
            _render_tracked(video_path, start, end, keep_segs, removed,
                            ass_path, W, H, ratio, out_path, progress_cb,
                            punch_zoom=punch_zoom,
                            enc_preset=enc_preset, enc_crf=enc_crf,
                            face_center=face_center)
        else:
            _render_plain(video_path, start, end, keep_segs, ass_path,
                          W, H, ratio, out_path, progress_cb,
                          punch_zoom=punch_zoom,
                          enhance_clarity=enhance_clarity,
                          enc_preset=enc_preset, enc_crf=enc_crf,
                          face_center=face_center)
        _render_ok = True
    finally:
        try:
            os.remove(ass_path)
        except OSError:
            pass
        if not _render_ok:
            _remove_quietly(out_path)

    if audio_polish:
        # best-effort post-pass (like the logo/zoom passes): a failed
        # polish must never lose the finished clip
        try:
            _apply_audio_polish(out_path, denoise=denoise_audio)
            if progress_cb:
                progress_cb(96, "Audio polished to -14 LUFS"
                            + (" (denoised)" if denoise_audio else ""))
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(96, "Audio polish skipped: "
                            + str(e)[:80])

    if emphasis_zoom:
        # best-effort like the logo pass: a failed zoom must never
        # lose the finished clip
        try:
            beats = _strongest_beats(cap_words, total_dur)
            peak = EMPHASIS_ZOOM_LEVELS.get(
                str(emphasis_level or "gentle").lower(),
                EMPHASIS_ZOOM_LEVELS["gentle"])
            if beats:
                _apply_emphasis_zoom(out_path, beats, peak,
                                     progress_cb=progress_cb)
                if progress_cb:
                    progress_cb(
                        97, "Emphasis zoom on the strongest beat"
                        + ("s" if len(beats) > 1 else ""))
            elif progress_cb:
                progress_cb(97, "Emphasis zoom skipped: no strong "
                            "beat in this clip")
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(97, "Emphasis zoom skipped: "
                            + str(e)[:80])

    broll_meta = None
    if broll:
        # best-effort post-pass: audio + captions keep running under
        # the cutaway, and any failure just keeps the plain render
        try:
            plan = _find_broll_window(cap_words, total_dur)
            if not plan:
                if progress_cb:
                    progress_cb(97, "B-roll skipped: no long talking "
                                "stretch in this clip")
            else:
                w0, w1, kw = plan
                bpath, credit = _commons_broll_clip(
                    kw, w1 - w0, progress_cb=progress_cb)
                w1 = min(w1, w0 + float(credit.get("duration")
                                        or (w1 - w0)))
                if w1 - w0 >= 2.0:
                    _apply_broll(out_path, bpath, w0, w1, W, H)
                    broll_meta = {
                        "keyword": kw,
                        "window": [round(w0, 2), round(w1, 2)],
                        "title": credit["title"],
                        "artist": credit["artist"],
                        "license": credit["license"],
                        "page_url": credit["page_url"],
                        "source_url": credit["source_url"]}
                    if progress_cb:
                        progress_cb(98, f"B-roll added ({kw})")
                elif progress_cb:
                    progress_cb(98, "B-roll skipped: stock clip too "
                                "short")
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(98, "B-roll skipped: " + str(e)[:90])

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
    if broll_meta:
        # attribution sidecar: app.py folds it into the render record
        # (and from there into posting packages), then removes it.
        # It belongs to the FINAL path, not the .part working file.
        try:
            with open(_final_out + ".broll.json", "w",
                      encoding="utf-8") as f:
                json.dump(broll_meta, f)
        except OSError:
            pass
    # Publish: the file at the real name is now the complete,
    # post-passed encode, atomically renamed into place.
    os.replace(out_path, _final_out)
    if progress_cb:
        progress_cb(100, "Render complete")
    return _final_out


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
# SHA-256 of the canonical Carve/LaMa-ONNX lama_fp32.onnx blob
# (HuggingFace LFS oid, cross-checked against a known-good local
# copy). The model used to be accepted on SIZE ONLY - a same-size
# corrupted or substituted file passed forever and produced
# garbage inpainting with no self-heal. The OCR model below has
# always been pinned this way; LaMa now is too.
LAMA_MODEL_SHA256 = ("1faef5301d78db7dda502fe59966957ec4b79dd6"
                     "4e16f03ed96913c7a4eb68d6")


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
    brains. Verified by size AND sha256 (mirrors the OCR model):
    a same-size corrupted file is deleted and re-downloaded rather
    than trusted forever. Returns the model path; RuntimeError on
    failure. A failed attempt removes its .part - the old code left
    it behind on every failure path."""
    import urllib.request
    path = lama_model_path()
    if lama_ready():
        if _sha256_file(path) == LAMA_MODEL_SHA256:
            return path
        # Right size, wrong bytes: corrupt or substituted. Remove
        # it so the download below starts clean (self-heal).
        try:
            os.remove(path)
        except OSError:
            pass
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
        if got != LAMA_MODEL_SIZE \
                or _sha256_file(tmp) != LAMA_MODEL_SHA256:
            raise RuntimeError(
                f"AI model download did not verify ({got} of "
                f"{LAMA_MODEL_SIZE} bytes) - check your connection "
                "and retry.")
        os.replace(tmp, path)
    except RuntimeError:
        _remove_quietly(tmp)
        raise
    except Exception as e:
        _remove_quietly(tmp)
        raise RuntimeError(f"Could not download the AI inpainting "
                           f"model: {e}")
    return path


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _part_sibling(path):
    """The in-progress sibling of an output path: clip_01.mp4 ->
    clip_01.part.mp4. Encoders write here and the finished file is
    os.replace()d onto the real name, so a file at the real name is
    always a complete, verified encode."""
    root, ext = os.path.splitext(path)
    return root + ".part" + ext


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
        _remove_quietly(tmp)
        raise
    except Exception as e:
        _remove_quietly(tmp)
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


_OCR_SINGLETON = {"det": None, "failed_at": 0.0}
_OCR_RETRY_S = 300.0  # a failed load is retried after 5 minutes


def _get_text_detector(progress_cb=None):
    """Lazy singleton; raises RuntimeError when the OCR model cannot
    be downloaded/loaded (callers fall back honestly).

    Failures used to latch for the LIFE OF THE PROCESS: one
    transient network failure during the one-time model download
    degraded Caption Remover to its fallbacks until the app was
    restarted. The failure is now remembered for _OCR_RETRY_S only -
    long enough that an offline machine doesn't pay a download
    attempt per job, short enough that a recovered connection
    heals the feature on its own."""
    if _OCR_SINGLETON["det"] is not None:
        return _OCR_SINGLETON["det"]
    if time.time() - _OCR_SINGLETON["failed_at"] < _OCR_RETRY_S:
        raise RuntimeError("Text detection unavailable (earlier load "
                           "failed).")
    try:
        det = _TextDetector(ensure_text_detector(progress_cb))
    except Exception:
        _OCR_SINGLETON["failed_at"] = time.time()
        raise
    _OCR_SINGLETON["det"] = det
    _OCR_SINGLETON["failed_at"] = 0.0
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


def _lama_inpaint_full(sess, img_name, mask_name, frame, cy0, cy1,
                       crop_mask):
    """_lama_inpaint with the FULL frame as context, the mask given
    in working-crop coordinates. Returns the cleaned full frame.

    Why not inpaint the caption strip directly: given only the
    strip (band + a thin context margin), LaMa starves - on dark,
    textured shots it reconstructs the very letters it should
    erase from the glyph fragments at the mask boundary (sweep2
    T2, measured on the same frame/mask: strip plate kept 82% of
    the text's edge energy; full-frame plate kept 9%). Context is
    the lever; mask size is not (a column-grown strip mask still
    kept 85%)."""
    import numpy as np
    full_mask = np.zeros(frame.shape[:2], np.float32)
    full_mask[cy0:cy1] = crop_mask
    return _lama_inpaint(sess, img_name, mask_name, frame, full_mask)


def _lama_inpaint(sess, img_name, mask_name, crop_bgr, mask01):
    """Run LaMa (fixed 512x512) on a working crop with a tight 0/1
    mask; returns the inpainted crop at the crop's own resolution.

    The crop is processed at NATIVE resolution in overlapping
    512x512 tiles (reflection-padded, never squeezed, aspect kept).
    Squeezing a wide HD crop into a single 512x512 call and
    stretching it back made the cleaned area measurably softer
    than the footage around it (user-reported blur, 2026-10-24:
    band sharpness 5-9 after cleaning vs 82-356 in the untouched
    footage). Tiles whose mask is empty are skipped, and outside
    the feathered mask the original pixels are returned untouched,
    so only the letters are ever repainted."""
    import cv2
    import numpy as np
    ch, cw = crop_bgr.shape[:2]
    TILE = 512
    OVERLAP = 64

    def run_tile(img512, m512):
        rgb = cv2.cvtColor(img512, cv2.COLOR_BGR2RGB)
        inp = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        res = sess.run(None, {img_name: inp, mask_name: m512})[0]
        o = np.asarray(res)[0].transpose(1, 2, 0)
        if float(o.max()) <= 1.5:
            o = o * 255.0
        o = np.clip(o, 0, 255).astype(np.uint8)
        return cv2.cvtColor(o, cv2.COLOR_RGB2BGR)

    def starts(n):
        if n <= TILE:
            return [0]
        s = list(range(0, n - TILE + 1, TILE - OVERLAP))
        if s[-1] != n - TILE:
            s.append(n - TILE)
        return s

    bin_mask = (mask01 > 0.2).astype(np.float32)
    acc = crop_bgr.astype(np.float32) * 1e-3
    wsum = np.full((ch, cw), 1e-3, np.float32)
    for ty in starts(ch):
        for tx in starts(cw):
            th = min(TILE, ch - ty)
            tw = min(TILE, cw - tx)
            tile_m = bin_mask[ty:ty + th, tx:tx + tw]
            if float(tile_m.max()) <= 0.0:
                continue  # no text here - nothing to repaint
            tile = crop_bgr[ty:ty + th, tx:tx + tw]
            imgp = cv2.copyMakeBorder(tile, 0, TILE - th, 0, TILE - tw,
                                      cv2.BORDER_REFLECT)
            mp = cv2.copyMakeBorder(tile_m, 0, TILE - th, 0, TILE - tw,
                                    cv2.BORDER_CONSTANT, value=0)
            out_t = run_tile(imgp, mp[None, None])[:th, :tw]
            wx = np.ones(tw, np.float32)
            wy = np.ones(th, np.float32)
            if tx > 0 and tw > OVERLAP:
                wx[:OVERLAP] = np.linspace(0.1, 1.0, OVERLAP)
            if tx + tw < cw and tw > OVERLAP:
                wx[-OVERLAP:] = np.linspace(1.0, 0.1, OVERLAP)
            if ty > 0 and th > OVERLAP:
                wy[:OVERLAP] = np.linspace(0.1, 1.0, OVERLAP)
            if ty + th < ch and th > OVERLAP:
                wy[-OVERLAP:] = np.linspace(1.0, 0.1, OVERLAP)
            w2 = (wy[:, None] * wx[None, :]).astype(np.float32)
            acc[ty:ty + th, tx:tx + tw] += out_t.astype(np.float32) \
                * w2[:, :, None]
            wsum[ty:ty + th, tx:tx + tw] += w2
    stitched = (acc / wsum[:, :, None]).astype(np.uint8)
    # Outside the feathered mask, return the original pixels exactly:
    # the model only ever repaints the letters themselves.
    keep = cv2.GaussianBlur(bin_mask, (0, 0), 1.2)
    keep = np.clip(keep * 1.6, 0.0, 1.0)[:, :, None]
    out = stitched.astype(np.float32) * keep \
        + crop_bgr.astype(np.float32) * (1.0 - keep)
    return np.clip(out, 0, 255).astype(np.uint8)


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
            if len(tail) > 60:
                del tail[:30]  # only the last lines are ever read
            if line.startswith("out_time_ms=") and duration:
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = min(1.0, max(0.0, t / duration))
                if progress_cb:
                    progress_cb(lo + int((hi - lo) * frac), stage)
    finally:
        # Never wait forever: if the loop above exited early (a
        # progress callback raised) ffmpeg can be wedged on a full
        # pipe with nobody draining it, and a bare wait() would hang
        # the worker thread - and the job's render lock - for good.
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: "
                           + " | ".join(tail[-6:])[:400])


def _cv2_can_read(video_path):
    """True when OpenCV can actually decode this file. Some downloads
    arrive as AV1, which the FFmpeg bundled inside OpenCV cannot
    decode on many systems: the capture opens fine, then every read
    fails - detection sees no frames and the AI engines process zero
    frames before erroring out."""
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return False
    ok, _frame = cap.read()
    cap.release()
    return bool(ok)


def _opencv_working_copy(video_path):
    """An H.264 copy of a video OpenCV cannot decode, made by ffmpeg
    (which handles AV1/VP9 fine). Cached in the temp dir keyed by the
    source's path+size+mtime, so band detection and the removal run
    share one conversion. Returns the copy's path, or None when the
    conversion failed (callers then carry on with the original and
    surface whatever honest error follows)."""
    import hashlib
    try:
        st = os.stat(video_path)
        key = hashlib.sha1(
            f"{os.path.abspath(video_path)}|{st.st_size}|"
            f"{st.st_mtime_ns}".encode()).hexdigest()[:16]
    except OSError:
        return None
    out = os.path.join(tempfile.gettempdir(),
                       "itraji-cv2-" + key + ".mp4")
    if os.path.isfile(out) and os.path.getsize(out) > 1024:
        return out
    tmp = out + ".part.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video_path,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.isfile(tmp) \
            or os.path.getsize(tmp) <= 1024:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    os.replace(tmp, out)
    return out


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
    if mode in ("temporal", "ai") and not _cv2_can_read(video_path):
        # The AI engines read frames through OpenCV; on a codec it
        # cannot decode (AV1 downloads) they would process zero
        # frames. Convert once via ffmpeg and work on the copy -
        # same pictures, same audio, a codec OpenCV understands.
        if progress_cb:
            progress_cb(1, "Converting video (its codec needs a "
                           "working copy first)")
        copy_path = _opencv_working_copy(video_path)
        if copy_path:
            video_path = copy_path
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
                back = _lama_inpaint_full(sess, img_name, mask_name,
                                          frame, cy0, cy1, m)
                band = back[y0:y1]
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
    grid_xs, grid_ys = np.meshgrid(
        np.arange(fw, dtype=np.float32),
        np.arange(fh, dtype=np.float32))
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
    plate_src = None      # the plate as built - warps always sample
    #                       this pristine copy, never a previous warp
    plate_hist = None     # histogram of the frame the plate was
    #                       built from (scene-cut check below)
    stale_votes = 0       # consecutive frames voting "scene changed"
    Dmap = None           # accumulated scene displacement since the
    #                       plate was built (flow resolution)
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
            rebuild = plate_src is None
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
            # ---- scene-cut check against the PLATE's own frame ----
            # The triggers above compare CONSECUTIVE frames, so a
            # 2-3 frame crossfade slips between them: each step stays
            # correlated with the last, a fade is flow-consistent
            # (fb_err ~ 0), and once the new scene settles, its low
            # motion never trips the age/drift limits either - the
            # OLD scene's plate, smeared caption and all, gets
            # painted over the new scene indefinitely (sweep2 T2:
            # a title-card plate ghosting through a whole clip).
            # Compare each frame against the frame the plate was
            # built from instead: after any real scene change the
            # correlation drops and STAYS dropped. Three consecutive
            # votes ride out brief occlusions and blur frames.
            if plate_hist is not None and not rebuild \
                    and not gate_closed:
                cur_hist = cv2.calcHist([small], [0], None, [64],
                                        [0, 256]).flatten()
                denom = float(np.linalg.norm(cur_hist)
                              * np.linalg.norm(plate_hist)) or 1.0
                ref_corr = float(cur_hist @ plate_hist / denom)
                if ref_corr < 0.75:
                    stale_votes += 1
                    if stale_votes >= 3:
                        rebuild = True
                else:
                    stale_votes = 0
            if gate_closed:
                # Text-free window: no plate work and no painting -
                # the frame passes through untouched. Drop the plate
                # so text returning later is filled from a fresh
                # plate built on a text-bearing frame, never from a
                # stale plate carried across a scene change.
                plate = None
                plate_src = None
                Dmap = None
                plate_hist = None
                stale_votes = 0
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
                # Full-frame context for the inpaint (see
                # _lama_inpaint_full): a strip-only plate keeps the
                # letters on dark/textured shots.
                plate_src = _lama_inpaint_full(
                    sess, img_name, mask_name, frame, cy0, cy1,
                    build_mask)[cy0:cy1].copy()
                plates += 1
                # Verify the plate really is text-free; if letters
                # survived the inpaint, widen the mask with what is
                # still text-like in the plate and inpaint once more.
                pgray = cv2.cvtColor(plate_src, cv2.COLOR_BGR2GRAY)
                pth = _tophat_text_mask(pgray) * near
                if float(pth[by0:by1].sum()) > 0.35 * max(
                        1.0, float(build_mask[by0:by1].sum())):
                    build_mask = cv2.dilate(
                        np.maximum(build_mask, pth),
                        np.ones((9, 9), np.float32))
                    plate_src = _lama_inpaint_full(
                        sess, img_name, mask_name, frame, cy0, cy1,
                        build_mask)[cy0:cy1].copy()
                    plates += 1
                plate = plate_src
                Dmap = np.zeros((fh, fw, 2), np.float32)
                plate_age = 0
                cum_disp = 0.0
                plate_hist = cv2.calcHist([small], [0], None, [64],
                                          [0, 256]).flatten()
                stale_votes = 0
            else:
                # Track by warping the PRISTINE plate with the total
                # accumulated displacement - never by re-warping last
                # frame's warp. Chained INTER_LINEAR resamples blur
                # the plate a little more every frame, and a plate
                # can live ~300 frames, so the cleaned area slowly
                # went soft even when the plate started sharp.
                if Dmap is None:
                    Dmap = np.zeros((fh, fw, 2), np.float32)
                mx = (grid_xs - flow[:, :, 0]).astype(np.float32)
                my = (grid_ys - flow[:, :, 1]).astype(np.float32)
                Dmap = cv2.remap(Dmap, mx, my, cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE) \
                    + flow
                fup = cv2.resize(Dmap, (W, ch),
                                 interpolation=cv2.INTER_LINEAR)
                fup[:, :, 0] *= (W / float(fw))
                fup[:, :, 1] *= (ch / float(fh))
                plate = cv2.remap(
                    plate_src,
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
            if (have_text or prev_had_text) and plate is not None:
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
    if not _cv2_can_read(video_path):
        # Both detectors sample frames through OpenCV; on a codec it
        # cannot decode (AV1 downloads) they would see zero frames
        # and report a confident 'no captions' for a captioned video.
        copy_path = _opencv_working_copy(video_path)
        if copy_path:
            video_path = copy_path
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
    if not _cv2_can_read(video_path):
        # Same codec shim as detect_caption_band: without it the
        # scan sees zero frames and silently previews from t=0.
        copy_path = _opencv_working_copy(video_path)
        if copy_path:
            video_path = copy_path
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
