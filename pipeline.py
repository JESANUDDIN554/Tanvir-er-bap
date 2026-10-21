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
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
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


def transcribe(video_path, model_name="base", progress_cb=None,
               language="auto"):
    """Transcribe with word-level timestamps. Returns list of word dicts.

    language: faster-whisper language code ("en", "es", ...) or "auto"."""
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
# OFFLINE heuristic moment detection (no API key)
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
    """Heuristic moment detection from the local transcript. No API key.

    Returns list of {start, end, title, hook, virality, hook_score,
    flow_score, value_score, reason, offline: True}. Titles and hooks are
    extracted verbatim from the transcript - never invented.
    """
    if not words or clip_max_s <= 0 or max_clips <= 0:
        return []
    dur = words[-1]["end"]
    win_len = min(max((clip_min_s + clip_max_s) / 2.0, 10.0), dur)
    if win_len < clip_min_s * 0.5:
        return []
    step = max(3.0, win_len / 6.0)

    candidates = []
    t = 0.0
    while t + win_len <= dur + 0.001:
        candidates.append((t, t + win_len))
        t += step
    if dur > win_len and (not candidates
                          or candidates[-1][0] < dur - win_len - 0.5):
        candidates.append((max(0.0, dur - win_len), dur))

    scored = []
    prev_content = set()
    for s, e in candidates:
        win_words = [w for w in words
                     if w["end"] > s and w["start"] < e]
        if len(win_words) < 12:
            prev_content = _content_words(win_words)
            continue
        # trim leading/trailing silence inside the window
        s = max(s, win_words[0]["start"])
        e = min(e, win_words[-1]["end"])
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
GENRE_HASHTAGS = {
    "podcast": ["#podcast", "#podcastclips", "#interview", "#podcasting"],
    "comedy": ["#comedy", "#funny", "#memes", "#laugh"],
    "gaming": ["#gaming", "#gamer", "#gameclips", "#twitch"],
    "news": ["#bodycam", "#police", "#cops", "#caughtoncamera"],
    "business": ["#business", "#entrepreneur", "#money", "#success"],
    "fitness": ["#fitness", "#gym", "#workout", "#gymlife"],
    "": ["#reels", "#viral", "#shorts", "#fyp"],
}

POSTING_PROMPT_TEMPLATE = """You are writing a Facebook Reels posting package for a short clip.
Below is the clip's transcript - verbatim words actually said in the clip.

RULES - follow strictly:
- Only use words, claims and people from the transcript. Never invent events, quotes, or people.
- Titles under 60 characters, punchy, no clickbait lies.
- Description: 2-3 sentences, Facebook-ready, ends with a question to drive comments.
- Up to 8 hashtags, lowercase, no spaces.
- Return ONLY valid JSON, no other text.

Transcript:
{transcript}

JSON format:
{{"titles": ["Title option 1", "Title option 2", "Title option 3"],
"description": "Two to three sentences with a line break, ending in a question",
"hashtags": ["#tag1", "#tag2"]}}"""


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


def generate_posting_package(words, moment, genre="", api_key=None):
    """Per-clip posting package: 3 titles, FB description, hashtags.

    With a Gemini key -> real AI copy ("ai" mode). Without -> extractive
    fallback on this PC ("extractive" mode). If the AI call fails, falls
    back to extractive and says so honestly. "mode" always reports which
    produced the copy.
    """
    moment = moment or {}
    if api_key:
        try:
            text = " ".join(w["word"] for w in words)[:6000]
            data = _gemini_call(
                POSTING_PROMPT_TEMPLATE.format(transcript=text), api_key)
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
                    "note": "Written by AI (Gemini) from the clip transcript.",
                }
        except Exception as e:  # noqa: BLE001 - honest fallback
            fb = _extractive_posting_package(words, moment, genre)
            fb["note"] = ("AI request failed (%s) - fell back to the "
                          "on-PC extractive version." % str(e)[:80])
            return fb
    return _extractive_posting_package(words, moment, genre)


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


# User-saved custom caption styles (persisted in settings, merged at render).
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


def build_ass(words, start_s, end_s, style_name, hook_text, out_path,
              res=(1080, 1920), emojis=False, highlight=False,
              speaker_colors=None, hook_overlay=True, cta_text=None,
              safe_zone=False):
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
    st = _get_style(style_name)
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

    prim_col = st["primary"]  # ASS &HAABBGGRR, e.g. &H0000FFFF
    for line in _group_lines(win):
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

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{cap_font},{st['primary']},{st['secondary']},{st['outline']},&H99000000,{st['bold']},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_w},2,40,40,{cap_margin_v},1
Style: Hook,Arial,{hook_font},&H0000FFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,3,2,0,8,40,40,{hook_margin_v},1
Style: CTA,Arial,{hook_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,3,2,0,8,40,40,{hook_margin_v},1
Style: Box,Arial,10,&HCC000000,&HCC000000,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

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
           "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
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
    event = ET.SubElement(library, "event", name="ClipMaker PC")
    project = ET.SubElement(event, "project", name="ClipMaker PC Timeline")
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
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
                    punch_zoom=False):
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
            "-c:a", "aac", "-b:a", "128k", audio_tmp]
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
    vf_parts.append(f"subtitles={_ffmpeg_ass_path(ass_path)}")
    enc = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-framerate", f"{fps:.3f}",
           "-i", "pipe:0",
           "-i", audio_tmp,
           "-vf", ",".join(vf_parts),
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
            crop = frame[y_top:y_top + crop_h, x_left:x_left + crop_w]
            small = cv2.resize(crop, (W, H),
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
    try:
        os.remove(audio_tmp)
    except OSError:
        pass
    if failed or proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg encode failed: " + err[-2000:])
    return out_path


def _render_plain(video_path, start, end, keep_segs, ass_path,
                  W, H, ratio, out_path, progress_cb=None,
                  punch_zoom=False, enhance_clarity=False):
    """ffmpeg-only render: crop/scale/captions, optional silence/filler
    cuts via filter_complex segment concat.

    punch_zoom: subtle zoom-punch on the hook (~1.0x -> ~1.07x over the
        first 3s) then a slow drift, capped at 1.16x (zoompan, 1:1 frames).
    enhance_clarity: light denoise + unsharp ("enhance" - NOT an AI
        upscale; honest naming is deliberate).
    """
    crop = crop_filter(ratio)
    vf = [crop, f"scale={W}:{H}"]
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
    vf.append(f"subtitles={_ffmpeg_ass_path(ass_path)}")
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
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k",
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
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k",
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
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
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
                logo_path=None):
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
    total_dur = kept_duration(start, end, removed)

    ass_fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix="clip_")
    os.close(ass_fd)
    build_ass(cap_words, 0, total_dur, style_name, moment.get("hook", ""),
              ass_path, res=(W, H), emojis=emojis, highlight=highlight,
              speaker_colors=speaker_colors, hook_overlay=hook_overlay,
              cta_text=cta_text, safe_zone=safe_zone)
    try:
        if face_track:
            _render_tracked(video_path, start, end, keep_segs, removed,
                            ass_path, W, H, ratio, out_path, progress_cb,
                            punch_zoom=punch_zoom)
        else:
            _render_plain(video_path, start, end, keep_segs, ass_path,
                          W, H, ratio, out_path, progress_cb,
                          punch_zoom=punch_zoom,
                          enhance_clarity=enhance_clarity)
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
