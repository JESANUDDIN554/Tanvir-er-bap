"""Itraji Cliper - desktop entry point.

Runs a localhost HTTP server for the local UI and opens it in a
desktop-style Chrome/Edge app window. Zero native dependencies
(no pywebview / .NET needed).
All video work runs on this machine; only viral-moment detection
calls the Google Gemini API with the user's own free key.
"""

import json
import os
import sys
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS  # PyInstaller bundle
    # Bundled exe: ffmpeg.exe/ffprobe.exe ship next to the exe - put that
    # folder on PATH so pipeline.py's bare "ffmpeg"/"ffprobe" calls find them.
    _exe_dir = os.path.dirname(sys.executable)
    os.environ["PATH"] = _exe_dir + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import (  # noqa: E402
    download_video, transcribe, detect_moments, detect_moments_offline,
    find_moments_by_keywords, parse_srt_file,
    render_clip, retime_words, write_srt, write_vtt, write_xml_timeline,
    merge_clips, probe_duration, set_custom_styles,
    generate_posting_package,
    LANGUAGES, GENRES, RATIOS,
)

JOBS = {}
BATCHES = {}
SETTINGS_PATH = os.path.join(os.path.expanduser("~"),
                              ".clipmaker_pc_settings.json")

# Job pruning: long desktop sessions must not grow JOBS forever.
JOB_TTL_S = 6 * 3600   # drop non-working jobs older than 6h
JOB_HARD_CAP = 40      # ...and never keep more than 40
JOB_KEEP_MIN = 20      # hard-cap pruning stops here (most recent kept)


def _drop_job(jid):
    job = JOBS.pop(jid, None)
    if job is None:
        return
    # free the disk too - a pruned job's /files URLs 404 anyway
    import shutil
    shutil.rmtree(os.path.join(BASE_DIR, "work", jid),
                  ignore_errors=True)


def _prune_jobs():
    now = time.time()
    for jid, job in list(JOBS.items()):
        if job.get("status") == "working":
            continue  # never prune a job with a live thread
        if _is_library_job(jid):
            continue  # saved in the Library - the user deletes it, not us
        if now - job.get("created", now) > JOB_TTL_S:
            _drop_job(jid)
    if len(JOBS) > JOB_HARD_CAP:
        ordered = sorted(JOBS.items(),
                         key=lambda kv: kv[1].get("created", 0))
        for jid, job in ordered:
            if len(JOBS) <= JOB_KEEP_MIN:
                break
            if job.get("status") == "working":
                continue
            if _is_library_job(jid):
                continue
            _drop_job(jid)


# -- Library persistence -------------------------------------------------
# Jobs used to live only in memory: restart the app (or hit the 6h
# prune) and every imported video vanished. Now any job that reaches a
# stable state is written to work/<job_id>/job.json and reloaded on
# startup, so the Library keeps projects until the user deletes them.
_LIBRARY_STATUSES = ("transcribed", "moments", "done")
_LIBRARY_LOADED = False


def _is_library_job(job_id):
    return os.path.isfile(
        os.path.join(BASE_DIR, "work", job_id, "job.json"))


def _save_job(job_id):
    """Persist a job's durable state to work/<job_id>/job.json."""
    job = JOBS.get(job_id)
    if not job or job.get("status") not in _LIBRARY_STATUSES:
        return
    try:
        data = {k: job.get(k) for k in (
            "title", "source", "video_path", "duration", "words",
            "moments", "renders", "status", "stage", "language",
            "created")}
        folder = os.path.join(BASE_DIR, "work", job_id)
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(folder, "job.json"))
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def _load_library():
    """Reload saved jobs from disk into JOBS (once per process)."""
    global _LIBRARY_LOADED
    if _LIBRARY_LOADED:
        return
    _LIBRARY_LOADED = True
    work = os.path.join(BASE_DIR, "work")
    try:
        names = os.listdir(work)
    except OSError:
        return
    for name in names:
        if name in JOBS:
            continue
        meta = os.path.join(work, name, "job.json")
        if not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001 - skip corrupt entries
            continue
        video_path = data.get("video_path") or ""
        if not video_path or not os.path.isfile(video_path):
            continue
        renders = [r for r in data.get("renders", [])
                   if os.path.isfile(r.get("path", ""))]
        moments = data.get("moments", [])
        words = data.get("words", [])
        if renders:
            status = "done"
            stage = f"Done - {len(renders)} clips rendered"
        elif moments:
            status = "moments"
            stage = data.get("stage") or "Moments ready"
        elif words:
            status, stage = "transcribed", "Transcript ready"
        else:
            continue
        JOBS[name] = {
            "stage": stage, "progress": 100, "status": status, "error": "",
            "video_path": video_path,
            "title": data.get("title", ""),
            "duration": data.get("duration", 0),
            "words": words, "moments": moments, "renders": renders,
            "language": data.get("language", "auto"),
            "source": data.get("source", "url"),
            "created": data.get("created", 0) or os.path.getmtime(meta)}

DEFAULT_SETTINGS = {
    "api_key": "",
    "clip_length": "60s",          # preset key, see CLIP_LEN_PRESETS
    "clip_count": 8,
    "caption_style": "bold-pop",
    "custom_styles": [],           # [{name, primary, secondary, outline, bold}]
    "ratio": "9:16",
    "quality": "1080p",
    "language": "auto",
    "genre": "",
    "emojis": False,
    "highlight": False,
    "remove_silences": False,
    "remove_fillers": False,
    "auto_censor": False,
    "face_track": True,
    "use_cookies": False,
    # Opus-gap features (all offline-first; new toggles)
    "speaker_colors": False,       # Speaker 1/2 caption colors
    "speaker_color_1": "#ffd166",
    "speaker_color_2": "#6cc7ff",
    "hook_overlay": True,          # hook text overlay, first ~2.5s
    "cta_overlay": True,           # CTA overlay in the last 2s
    "cta_text": "Follow for more 🔔",
    "safe_zone": False,            # Reels-safe caption nudge + filename tag
    "punch_zoom": True,            # subtle zoom-punch + slow drift
    "enhance_clarity": False,      # sharpen + denoise ("enhance", not "AI upscale")
    "logo_path": "",               # brand-kit corner logo PNG
    "brand_kits": [],              # [{name, caption_style, primary, secondary,
                                   #   outline, logo_path, intro_path, outro_path}]
    "planner": [],                 # [{id, job_id, title, scheduled, done,
                                   #   export_name}] - local posting planner
}

# Clip-length presets -> (min_s, max_s) for moment detection.
CLIP_LEN_PRESETS = {
    "15s": (10, 20), "30s": (20, 40), "60s": (40, 80),
    "90s": (60, 120), "2min": (90, 150), "3min": (150, 210),
    "5min": (240, 360), "10min": (480, 720), "15min": (720, 1080),
    # legacy keys from older settings
    "30-60s": (30, 60), "1-2min": (60, 120),
}

LONG_VIDEO_WARN_S = 2 * 3600  # Step-1 amber warning at 2h+


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data or {})
    return merged


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _hex_to_ass(hex_color):
    """#RRGGBB -> ASS &HAABBGGRR (opaque). Safe fallback to white."""
    import re
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (hex_color or "").strip())
    if not m:
        return "&H00FFFFFF"
    r, g, b = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return ("&H00" + b + g + r).upper()


def _job_progress(job_id):
    def cb(pct, stage):
        job = JOBS.get(job_id)
        if job:
            job["progress"] = pct
            job["stage"] = stage
    return cb


class Api:
    # -- options -------------------------------------------------------
    def get_options(self):
        """Static option lists for the UI (languages, genres, ratios)."""
        return {"languages": LANGUAGES,
                "genre_defaults": GENRES,
                "ratios": list(RATIOS.keys())}

    # -- settings ------------------------------------------------------
    def get_settings(self):
        return load_settings()

    def save_settings(self, settings):
        current = load_settings()
        current.update(settings or {})
        save_settings(current)
        return {"ok": True}

    # -- import + transcribe -------------------------------------------
    def _new_job(self):
        job_id = uuid.uuid4().hex[:8]
        JOBS[job_id] = {"stage": "Starting", "progress": 0,
                        "status": "working", "error": "",
                        "video_path": "", "title": "", "duration": 0,
                        "words": [], "moments": [], "renders": [],
                        "language": "auto", "source": "url",
                        "created": time.time()}
        _prune_jobs()
        return job_id

    def import_url(self, url, use_cookies=False, language="auto"):
        job_id = self._new_job()
        t = threading.Thread(target=self._run_import,
                             args=(job_id, url, bool(use_cookies),
                                   language or "auto"),
                             daemon=True)
        t.start()
        return {"job_id": job_id}

    def import_file(self, path, language="auto"):
        """Use a local video file already on the PC (no download)."""
        path = (path or "").strip().strip('"').strip("'")
        if not path or not os.path.isfile(path):
            return {"ok": False,
                    "error": "File not found: " + (path or "")[:120]}
        job_id = self._new_job()
        job = JOBS[job_id]
        job["video_path"] = os.path.abspath(path)
        job["title"] = os.path.basename(path)
        job["source"] = "file"
        try:
            job["duration"] = int(probe_duration(path))
        except Exception:  # noqa: BLE001
            job["duration"] = 0
        if job["duration"] <= 0:
            job["status"] = "error"
            job["error"] = "That file isn't a readable video."
            job["stage"] = "Failed"
            return {"ok": True, "job_id": job_id}
        t = threading.Thread(target=self._run_transcribe_only,
                             args=(job_id, language or "auto"), daemon=True)
        t.start()
        return {"ok": True, "job_id": job_id}

    def import_batch(self, urls, use_cookies=False, language="auto"):
        """Queue up to 10 URLs; they import sequentially in the background."""
        urls = [str(u or "").strip() for u in (urls or [])]
        urls = [u for u in urls if u][:10]
        if not urls:
            return {"ok": False, "error": "Paste at least one video URL."}
        batch_id = uuid.uuid4().hex[:8]
        BATCHES[batch_id] = {"urls": urls, "job_ids": [],
                             "status": "working", "done": 0,
                             "total": len(urls)}
        t = threading.Thread(target=self._run_batch,
                             args=(batch_id, urls, bool(use_cookies),
                                   language or "auto"),
                             daemon=True)
        t.start()
        return {"ok": True, "batch_id": batch_id, "count": len(urls)}

    def _run_batch(self, batch_id, urls, use_cookies, language):
        batch = BATCHES.get(batch_id)
        if not batch:
            return
        for url in urls:
            job_id = self._new_job()
            batch["job_ids"].append(job_id)
            try:
                self._run_import(job_id, url, use_cookies, language)
            except Exception:  # noqa: BLE001 - job holds the error
                pass
            batch["done"] += 1
        batch["status"] = "done"

    def batch_status(self, batch_id):
        b = BATCHES.get(batch_id)
        if not b:
            return {"status": "missing"}
        return {"status": b["status"], "done": b["done"],
                "total": b["total"], "job_ids": b["job_ids"]}

    def list_jobs(self):
        _load_library()
        return {"jobs": [
            {"job_id": jid, "title": j.get("title", ""),
             "status": j.get("status", ""), "stage": j.get("stage", ""),
             "progress": j.get("progress", 0),
             "duration": j.get("duration", 0),
             "clips": len(j.get("renders", [])),
             "created": j.get("created", 0)}
            for jid, j in JOBS.items()]}

    def library(self):
        """All saved projects - persisted on disk, survive restarts."""
        _load_library()
        projects = []
        for jid, j in JOBS.items():
            if j.get("status") not in _LIBRARY_STATUSES:
                continue
            projects.append({
                "job_id": jid, "title": j.get("title", ""),
                "status": j.get("status", ""), "stage": j.get("stage", ""),
                "duration": j.get("duration", 0),
                "created": j.get("created", 0),
                "source": j.get("source", ""),
                "clips": len(j.get("renders", [])),
                "moments": len(j.get("moments", []))})
        projects.sort(key=lambda p: p.get("created", 0), reverse=True)
        return {"projects": projects}

    def delete_job(self, job_id):
        """Delete a saved project and its files (Library action)."""
        job = JOBS.get(job_id)
        if job and job.get("status") == "working":
            return {"ok": False,
                    "error": "That video is still busy - wait for it "
                             "to finish first."}
        folder = os.path.join(BASE_DIR, "work", job_id)
        if job is None and not os.path.isdir(folder):
            return {"ok": False, "error": "Project not found."}
        _drop_job(job_id)  # pops JOBS + removes the folder when loaded
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": True}

    def _run_import(self, job_id, url, use_cookies, language):
        job = JOBS[job_id]
        try:
            workdir = os.path.join(BASE_DIR, "work", job_id)
            video_path, title, duration = download_video(
                url, workdir, _job_progress(job_id),
                use_cookies=use_cookies)
            job.update({"video_path": video_path, "title": title,
                        "duration": duration})
            self._run_transcribe_only(job_id, language)
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            job["status"] = "error"
            job["error"] = self._friendly_error(e)
            job["stage"] = "Failed"

    def _run_transcribe_only(self, job_id, language):
        job = JOBS[job_id]
        try:
            job["language"] = language
            words = transcribe(job["video_path"], "base",
                               _job_progress(job_id), language=language)
            if not words:
                raise RuntimeError("no_speech")
            job["words"] = words
            job["status"] = "transcribed"
            job["stage"] = "Transcript ready"
            job["progress"] = 100
            _save_job(job_id)
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            job["status"] = "error"
            job["error"] = self._friendly_error(e)
            job["stage"] = "Failed"

    def import_srt_transcript(self, job_id, srt_path):
        """Replace the job's transcript with words parsed from an SRT file."""
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "No job."}
        srt_path = (srt_path or "").strip().strip('"').strip("'")
        if not srt_path or not os.path.isfile(srt_path):
            return {"ok": False,
                    "error": "SRT file not found: " + (srt_path or "")[:120]}
        try:
            words = parse_srt_file(srt_path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "Couldn't parse SRT: "
                                         + str(e)[:160]}
        if not words:
            return {"ok": False, "error": "No captions found in that SRT."}
        job["words"] = words
        job["moments"] = []
        job["status"] = "transcribed"
        job["stage"] = f"Transcript ready ({len(words)} words from SRT)"
        job["progress"] = 100
        _save_job(job_id)
        return {"ok": True, "word_count": len(words)}

    def get_thumbnail(self, job_id):
        job = JOBS.get(job_id)
        if not job or not job.get("video_path"):
            return {"ok": False}
        thumb = os.path.join(BASE_DIR, "work", job_id, "thumb.jpg")
        if not os.path.exists(thumb):
            import subprocess
            os.makedirs(os.path.dirname(thumb), exist_ok=True)
            t = min(5, max(1, (job.get("duration") or 10) / 2))
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                   "-i", job["video_path"], "-frames:v", "1",
                   "-q:v", "4", thumb]
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if os.path.exists(thumb):
            return {"ok": True, "url": f"/files/{job_id}/thumb.jpg"}
        return {"ok": False}

    @staticmethod
    def _friendly_error(e):
        msg = str(e)
        if "youtube_bot_check" in msg or "not a bot" in msg.lower():
            return ("YouTube asked for bot verification. Turn on 'Use my "
                    "browser's YouTube login' in Step 1 (log into YouTube "
                    "in Chrome/Edge first), then retry.")
        if "no_speech" in msg:
            return "No speech found in this video - nothing to clip."
        if "Private video" in msg or "Login required" in msg:
            return "This video is private or login-only and can't be downloaded."
        if "Unsupported URL" in msg:
            return "That link isn't a supported video URL."
        if "timed out" in msg.lower() or "Failed to resolve" in msg:
            return "Network error while downloading - check your connection and retry."
        return "Import failed: " + msg[:220]

    # -- moment detection ----------------------------------------------
    @staticmethod
    def _clip_len_range(clip_len):
        key = (clip_len or "60s").strip()
        if key.startswith("custom:"):
            try:
                p = max(5, int(key.split(":")[1]))
                return (round(p * 0.75), round(p * 1.25))
            except (ValueError, IndexError):
                pass
        return CLIP_LEN_PRESETS.get(key, CLIP_LEN_PRESETS["60s"])

    @staticmethod
    def _clip_count(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 8
        return max(1, min(40, n))  # hard cap 40

    def detect(self, job_id, max_clips=8, clip_len="60s"):
        job = JOBS.get(job_id)
        if not job or job["status"] != "transcribed":
            return {"ok": False, "error": "Import a video first."}
        settings = load_settings()
        api_key = settings.get("api_key", "").strip()
        if not api_key:
            return {"ok": False, "error": "no_api_key",
                    "message": "Paste your free Gemini API key in Settings first "
                               "(get one at aistudio.google.com), or use "
                               "Offline mode."}
        clip_min, clip_max = self._clip_len_range(clip_len)
        job["status"] = "working"
        job.pop("detect_error", None)  # don't ship a stale failure
        t = threading.Thread(
            target=self._run_detect,
            args=(job_id, api_key, clip_min, clip_max,
                  self._clip_count(max_clips)),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_detect(self, job_id, api_key, clip_min, clip_max, max_clips):
        job = JOBS[job_id]
        try:
            moments = detect_moments(
                job["words"], api_key, clip_min, clip_max,
                max_clips=max_clips, progress_cb=_job_progress(job_id))
            if not moments:
                raise RuntimeError("no_moments")
            # keep keyword-search ("matched") moments a concurrent
            # find_moments call may have added; replace the rest
            kept = [m for m in job.get("moments", []) if m.get("matched")]
            job["moments"] = kept + moments
            job["status"] = "moments"
            job["stage"] = f"Found {len(moments)} moments"
            job["progress"] = 100
            job.pop("detect_error", None)
            _save_job(job_id)
        except Exception as e:  # noqa: BLE001
            # back to transcript state - unless keyword moments from
            # find_moments are still present, then stay on moments
            job["status"] = ("moments" if job.get("moments")
                             else "transcribed")
            msg = str(e)
            if "no_api_key" in msg or "bad_api_key" in msg:
                job["detect_error"] = ("Gemini API key was rejected. "
                                       "Check the key in Settings.")
            elif "no_moments" in msg:
                job["detect_error"] = ("The AI found no strong moments in "
                                       "this transcript.")
            else:
                job["detect_error"] = "Moment detection failed: " + msg[:220]
            job["stage"] = "Transcript ready"
            job["progress"] = 100

    # -- offline moment detection (no API key) ---------------------------
    def detect_offline(self, job_id, max_clips=8, clip_len="60s"):
        job = JOBS.get(job_id)
        if not job or job["status"] != "transcribed":
            return {"ok": False, "error": "Import a video first."}
        clip_min, clip_max = self._clip_len_range(clip_len)
        job["status"] = "working"
        job.pop("detect_error", None)  # don't ship a stale failure
        t = threading.Thread(
            target=self._run_detect_offline,
            args=(job_id, clip_min, clip_max,
                  self._clip_count(max_clips)),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_detect_offline(self, job_id, clip_min, clip_max, max_clips):
        job = JOBS[job_id]
        try:
            job["stage"] = "Scanning transcript (offline)"
            job["progress"] = 40
            moments = detect_moments_offline(
                job["words"], clip_min, clip_max, max_clips=max_clips)
            if not moments:
                raise RuntimeError("no_moments")
            # keep keyword-search ("matched") moments a concurrent
            # find_moments call may have added; replace the rest
            kept = [m for m in job.get("moments", []) if m.get("matched")]
            job["moments"] = kept + moments
            job["status"] = "moments"
            job["stage"] = f"Found {len(moments)} moments (offline)"
            job["progress"] = 100
            job.pop("detect_error", None)
            _save_job(job_id)
        except Exception as e:  # noqa: BLE001
            # back to transcript state - unless keyword moments from
            # find_moments are still present, then stay on moments
            job["status"] = ("moments" if job.get("moments")
                             else "transcribed")
            if "no_moments" in str(e):
                job["detect_error"] = ("The offline scan found no strong "
                                       "moments in this transcript.")
            else:
                job["detect_error"] = ("Offline detection failed: "
                                       + str(e)[:220])
            job["stage"] = "Transcript ready"
            job["progress"] = 100

    # -- caption editor ----------------------------------------------
    def _moment_words(self, job, index):
        """Transcript words inside a moment's window."""
        m = job["moments"][index]
        win = [w for w in job["words"]
               if w["end"] > m["start"] and w["start"] < m["end"]]
        win.sort(key=lambda w: w["start"])
        return m, win

    def get_caption_words(self, job_id, index):
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m, win = self._moment_words(job, index)
        current = m.get("edited_words") or win
        return {"ok": True, "words": " ".join(w["word"] for w in current),
                "title": m["title"], "edited": bool(m.get("edited_words"))}

    def save_caption_words(self, job_id, index, text):
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m, win = self._moment_words(job, index)
        edited = retime_words(win, text or "", m["start"], m["end"])
        if not edited:
            return {"ok": False, "error": "Caption text is empty."}
        m["edited_words"] = edited
        m["caption_edited"] = True
        _save_job(job_id)
        return {"ok": True, "word_count": len(edited)}

    def reset_caption_words(self, job_id, index):
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m = job["moments"][index]
        m.pop("edited_words", None)
        m.pop("caption_edited", None)
        _save_job(job_id)
        return {"ok": True}

    # -- AI posting package --------------------------------------------
    def posting_package(self, job_id, index):
        """Per-clip posting package: 3 titles, FB description, hashtags.

        Generated once per moment and cached on it. With a Gemini key ->
        AI copy ("ai" mode); without -> extractive on-PC fallback
        ("extractive" mode). The "mode" field always says which produced
        the copy."""
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m = job["moments"][index]
        if m.get("posting"):
            return {"ok": True, "package": m["posting"], "cached": True}
        words = m.get("edited_words") or [
            w for w in job["words"]
            if w["end"] > m["start"] and w["start"] < m["end"]]
        if not words:
            return {"ok": False, "error": "No captions in this moment."}
        settings = load_settings()
        api_key = settings.get("api_key", "").strip() or None
        pkg = generate_posting_package(
            words, m, genre=settings.get("genre", ""), api_key=api_key)
        m["posting"] = pkg
        _save_job(job_id)
        return {"ok": True, "package": pkg, "cached": False}

    # -- per-clip trim -------------------------------------------------
    def trim_moment(self, job_id, index, start, end):
        """Adjust a moment's start/end (seconds), clamped to the video."""
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        try:
            s, e = float(start), float(end)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Enter start/end in seconds."}
        dur = job.get("duration") or 0
        s = max(0.0, s)
        e = min(float(e), float(dur)) if dur else float(e)
        if s >= e:
            return {"ok": False, "error": "Start must be before end."}
        if e - s < 3:
            return {"ok": False, "error": "Clip must be at least 3 seconds."}
        m = job["moments"][index]
        m["start"], m["end"] = round(s, 2), round(e, 2)
        m["trimmed"] = True
        # drop edited captions - timings no longer line up after a trim
        m.pop("edited_words", None)
        m.pop("caption_edited", None)
        _save_job(job_id)
        return {"ok": True, "start": m["start"], "end": m["end"]}

    # -- find clip moment (keyword search) -------------------------------
    def find_moments(self, job_id, queries, clip_len="60s"):
        """Natural-language/keyword moment search over the transcript.

        Each hit is stamped with its query ("Matched: <query>"); queries
        with no hits come back in `unmatched` - reported honestly."""
        job = JOBS.get(job_id)
        if not job or not job.get("words"):
            return {"ok": False, "error": "Import a video first."}
        if isinstance(queries, str):
            queries = [queries]
        clip_min, clip_max = self._clip_len_range(clip_len)
        moments, unmatched = find_moments_by_keywords(
            job["words"], queries, clip_min, clip_max, max_clips=40)
        # dedupe: don't append the same query hit twice
        seen = {(round(m.get("start", 0), 2), round(m.get("end", 0), 2),
                 m.get("matched")) for m in job.get("moments", [])}
        fresh = [m for m in moments
                 if (round(m["start"], 2), round(m["end"], 2),
                     m.get("matched")) not in seen]
        job["moments"].extend(fresh)
        if fresh:
            if job.get("status") != "working":
                # a render/AI pass is in flight: leave its status alone,
                # the appended moments are still visible via job_status
                job["status"] = "moments"
            job["stage"] = f"Found {len(job['moments'])} moments"
            _save_job(job_id)
        return {"ok": True, "found": len(fresh), "unmatched": unmatched}

    # -- render ----------------------------------------------------------
    def render(self, job_id, indices, options=None):
        job = JOBS.get(job_id)
        if not job or not job.get("moments"):
            return {"ok": False, "error": "Nothing to render yet."}
        try:
            indices = [int(i) for i in (indices or [])]
        except (TypeError, ValueError):
            return {"ok": False, "error": "Bad clip selection."}
        if not indices or any(i < 0 or i >= len(job["moments"])
                              for i in indices):
            return {"ok": False, "error": "Clip selection out of range."}
        # one render per job at a time: two threads writing the same
        # 01_title.mp4 filenames corrupt each other's output
        lock = job.setdefault("_render_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "error": "render_in_progress",
                    "message": "A render is already running for this "
                               "video - wait for it to finish."}
        settings = load_settings()
        opts = dict(options or {})
        # defaults from settings, overridable per render call
        for key in ("caption_style", "ratio", "quality", "face_track",
                    "remove_fillers", "remove_silences", "auto_censor",
                    "emojis", "highlight", "speaker_colors", "hook_overlay",
                    "cta_overlay", "cta_text", "safe_zone", "punch_zoom",
                    "enhance_clarity", "logo_path"):
            opts.setdefault(key, settings.get(key))
        opts.setdefault("caption_style", "bold-pop")
        opts.setdefault("ratio", "9:16")
        opts.setdefault("quality", "1080p")
        opts["custom_styles"] = settings.get("custom_styles", [])
        set_custom_styles(opts["custom_styles"])
        # speaker colors: toggle + two hexes from settings -> ASS tuple
        if opts.get("speaker_colors"):
            opts["speaker_colors"] = (
                _hex_to_ass(settings.get("speaker_color_1", "#ffd166")),
                _hex_to_ass(settings.get("speaker_color_2", "#6cc7ff")))
        else:
            opts["speaker_colors"] = None
        # CTA: toggle gates the text (per-render text overrides settings)
        if opts.get("cta_overlay"):
            cta = (opts.get("cta_text")
                   or settings.get("cta_text", "")).strip()
            opts["cta_text"] = cta or None
        else:
            opts["cta_text"] = None
        opts["hook_overlay"] = bool(opts.get("hook_overlay", True))
        logo = (opts.get("logo_path")
                or settings.get("logo_path", "")).strip()
        opts["logo_path"] = logo or None
        job["last_render_opts"] = {k: opts.get(k) for k in
                                   ("caption_style", "ratio", "quality",
                                    "face_track", "remove_fillers",
                                    "remove_silences", "auto_censor",
                                    "emojis", "highlight", "speaker_colors",
                                    "hook_overlay", "cta_text", "safe_zone",
                                    "punch_zoom", "enhance_clarity",
                                    "logo_path")}
        job["status"] = "working"
        job.pop("render_error", None)  # don't ship a stale failure
        t = threading.Thread(
            target=self._run_render,
            args=(job_id, list(indices), opts),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_render(self, job_id, indices, opts):
        job = JOBS[job_id]
        lock = job.get("_render_lock")
        try:
            self._render_clips(job_id, indices, opts)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass  # already released

    def _render_clips(self, job_id, indices, opts):
        job = JOBS[job_id]
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        os.makedirs(outdir, exist_ok=True)
        renders = []
        try:
            for n, i in enumerate(indices):
                m = job["moments"][i]
                job["stage"] = f"Rendering clip {n + 1}/{len(indices)}"
                job["progress"] = int(n / max(len(indices), 1) * 100)
                safe = "".join(c if c.isalnum() or c in " -_" else ""
                               for c in m["title"])[:40].strip() or "clip"
                tag = "_reels-safe" if opts.get("safe_zone") else ""
                out = os.path.join(outdir, f"{n + 1:02d}_{safe}{tag}.mp4")
                render_clip(job["video_path"], m, job["words"],
                            opts.get("caption_style", "bold-pop"),
                            out, _job_progress(job_id),
                            face_track=bool(opts.get("face_track", True)),
                            remove_fillers=bool(opts.get("remove_fillers")),
                            remove_silences=bool(opts.get("remove_silences")),
                            auto_censor=bool(opts.get("auto_censor")),
                            emojis=bool(opts.get("emojis")),
                            highlight=bool(opts.get("highlight")),
                            ratio=opts.get("ratio", "9:16"),
                            quality=opts.get("quality", "1080p"),
                            custom_styles=opts.get("custom_styles", []),
                            edited_words=m.get("edited_words"),
                            speaker_colors=opts.get("speaker_colors"),
                            hook_overlay=bool(opts.get("hook_overlay",
                                                       True)),
                            cta_text=opts.get("cta_text"),
                            safe_zone=bool(opts.get("safe_zone")),
                            punch_zoom=bool(opts.get("punch_zoom")),
                            enhance_clarity=bool(
                                opts.get("enhance_clarity")),
                            logo_path=opts.get("logo_path"))
                renders.append({"path": out, "title": m["title"],
                                "virality": m["virality"],
                                "moment_index": i,
                                "url": f"/files/{job_id}/clips/"
                                       f"{os.path.basename(out)}"})
            job["renders"] = renders
            job["status"] = "done"
            job["stage"] = f"Done - {len(renders)} clips rendered"
            job["progress"] = 100
            job.pop("render_error", None)
        except Exception as e:  # noqa: BLE001
            job["status"] = "moments"
            job["render_error"] = "Render failed: " + str(e)[:220]
            job["stage"] = "Render failed"
            # keep any clips that finished before the failure
            job["renders"] = renders
        _save_job(job_id)

    # -- caption exports (SRT/VTT), ZIP, XML timeline, previews -----------
    def _render_words(self, job, render_idx):
        r = job["renders"][render_idx]
        if r.get("words"):
            # merged clip: captions were combined at merge time onto the
            # merged timeline - use a synthetic full-span window so the
            # SRT/VTT writers don't clip them to one source moment
            total = r.get("merged_duration") or 0
            if not total:
                total = max((w["end"] for w in r["words"]),
                            default=0)
            return r, {"start": 0, "end": total}, r["words"]
        m = job["moments"][r["moment_index"]]
        words = m.get("edited_words") or [
            w for w in job["words"]
            if w["end"] > m["start"] and w["start"] < m["end"]]
        return r, m, words

    def _download_url(self, job_id, path):
        rel = os.path.relpath(path, os.path.join(BASE_DIR, "work", job_id))
        return "/files/" + job_id + "/" + rel.replace(os.sep, "/")

    def export_srt(self, job_id, render_idx):
        job = JOBS.get(job_id)
        if not job or not (0 <= render_idx < len(job.get("renders", []))):
            return {"ok": False, "error": "Clip not found."}
        r, m, words = self._render_words(job, render_idx)
        srt_path = os.path.splitext(r["path"])[0] + ".srt"
        try:
            write_srt(words, m["start"], m["end"], srt_path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "SRT failed: " + str(e)[:160]}
        return {"ok": True, "path": srt_path,
                "url": self._download_url(job_id, srt_path)}

    def export_vtt(self, job_id, render_idx):
        job = JOBS.get(job_id)
        if not job or not (0 <= render_idx < len(job.get("renders", []))):
            return {"ok": False, "error": "Clip not found."}
        r, m, words = self._render_words(job, render_idx)
        vtt_path = os.path.splitext(r["path"])[0] + ".vtt"
        try:
            write_vtt(words, m["start"], m["end"], vtt_path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "VTT failed: " + str(e)[:160]}
        return {"ok": True, "path": vtt_path,
                "url": self._download_url(job_id, vtt_path)}

    def export_xml(self, job_id):
        """Premiere/DaVinci XML timeline of all rendered clips."""
        job = JOBS.get(job_id)
        if not job or not job.get("renders"):
            return {"ok": False, "error": "Render clips first."}
        clips = []
        for r in job["renders"]:
            try:
                dur = probe_duration(r["path"])
            except Exception:  # noqa: BLE001
                dur = 0
            clips.append({"path": r["path"], "title": r["title"],
                          "duration": dur or 1.0})
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        out = os.path.join(outdir, "timeline.fcpxml")
        try:
            write_xml_timeline(clips, out)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "XML failed: " + str(e)[:160]}
        return {"ok": True, "path": out,
                "url": self._download_url(job_id, out)}

    # -- brand kits ------------------------------------------------------
    def save_brand_kit(self, name, kit):
        """Save a named brand kit: caption style + colors + corner logo
        PNG + intro/outro paths. One-click apply happens in the UI."""
        name = (name or "").strip()[:40]
        if not name:
            return {"ok": False, "error": "Name the brand kit first."}
        kit = dict(kit or {})
        settings = load_settings()
        kits = settings.get("brand_kits") or []
        entry = {
            "name": name,
            "caption_style": str(kit.get("caption_style", "bold-pop")),
            "primary": str(kit.get("primary", "#ffffff")),
            "secondary": str(kit.get("secondary", "#2effc8")),
            "outline": str(kit.get("outline", "#000000")),
            "logo_path": str(kit.get("logo_path", ""))[:500],
            "intro_path": str(kit.get("intro_path", ""))[:500],
            "outro_path": str(kit.get("outro_path", ""))[:500],
        }
        kits = [k for k in kits if k.get("name") != name] + [entry]
        settings["brand_kits"] = kits
        save_settings(settings)
        return {"ok": True, "kits": kits}

    def delete_brand_kit(self, name):
        settings = load_settings()
        kits = [k for k in (settings.get("brand_kits") or [])
                if k.get("name") != name]
        settings["brand_kits"] = kits
        save_settings(settings)
        return {"ok": True, "kits": kits}

    # -- local posting planner (scheduler-lite) --------------------------
    # No auto-posting (no accounts connected): a persisted per-clip
    # schedule queue with a checklist view and export-day naming.
    def get_planner(self):
        return {"planner": load_settings().get("planner", [])}

    def add_planner_entry(self, job_id, render_idx, scheduled):
        """Queue a rendered clip for posting at an ISO datetime."""
        from datetime import datetime
        job = JOBS.get(job_id)
        if not job or not (0 <= render_idx < len(job.get("renders", []))):
            return {"ok": False, "error": "Clip not found."}
        try:
            dt = datetime.fromisoformat(str(scheduled))
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "Pick a valid date and time."}
        r = job["renders"][render_idx]
        safe = "".join(c if c.isalnum() or c in " -_" else ""
                       for c in r["title"])[:40].strip() or "clip"
        export_name = f"{dt.strftime('%Y-%m-%d')}_{safe}.mp4"
        entry = {
            "id": uuid.uuid4().hex[:8],
            "job_id": job_id,
            "render_idx": render_idx,
            "title": r["title"],
            "path": r["path"],
            "scheduled": dt.isoformat(timespec="minutes"),
            "done": False,
            "export_name": export_name,
        }
        settings = load_settings()
        planner = settings.get("planner", [])
        planner.append(entry)
        planner.sort(key=lambda e: e.get("scheduled", ""))
        settings["planner"] = planner
        save_settings(settings)
        return {"ok": True, "entry": entry, "planner": planner}

    def update_planner_entry(self, entry_id, fields):
        from datetime import datetime
        settings = load_settings()
        planner = settings.get("planner", [])
        for e in planner:
            if e.get("id") == entry_id:
                fields = dict(fields or {})
                if "done" in fields:
                    e["done"] = bool(fields["done"])
                if fields.get("scheduled"):
                    try:
                        dt = datetime.fromisoformat(
                            str(fields["scheduled"]))
                    except (TypeError, ValueError):
                        return {"ok": False,
                                "error": "Pick a valid date and time."}
                    e["scheduled"] = dt.isoformat(timespec="minutes")
                    safe = "".join(
                        c if c.isalnum() or c in " -_" else ""
                        for c in e["title"])[:40].strip() or "clip"
                    e["export_name"] = \
                        f"{dt.strftime('%Y-%m-%d')}_{safe}.mp4"
                break
        else:
            return {"ok": False, "error": "Entry not found."}
        planner.sort(key=lambda e: e.get("scheduled", ""))
        settings["planner"] = planner
        save_settings(settings)
        return {"ok": True, "planner": planner}

    def delete_planner_entry(self, entry_id):
        settings = load_settings()
        planner = [e for e in settings.get("planner", [])
                   if e.get("id") != entry_id]
        settings["planner"] = planner
        save_settings(settings)
        return {"ok": True, "planner": planner}

    def make_zip(self, job_id, with_captions=True):
        """Download-all ZIP: every rendered MP4 (+ SRT/VTT when present)."""
        import zipfile
        job = JOBS.get(job_id)
        if not job or not job.get("renders"):
            return {"ok": False, "error": "Render clips first."}
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        zpath = os.path.join(outdir, "clips.zip")
        try:
            with zipfile.ZipFile(zpath, "w",
                                 zipfile.ZIP_DEFLATED) as z:
                for r in job["renders"]:
                    z.write(r["path"],
                            arcname=os.path.basename(r["path"]))
                    if with_captions:
                        base = os.path.splitext(r["path"])[0]
                        for ext in (".srt", ".vtt"):
                            if os.path.exists(base + ext):
                                z.write(base + ext,
                                        arcname=os.path.basename(base + ext))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "ZIP failed: " + str(e)[:160]}
        return {"ok": True, "path": zpath,
                "url": self._download_url(job_id, zpath)}

    def preview_captions(self, job_id, index, options=None):
        """Captions-only preview: black video + burned captions for a moment.

        options (optional): {"caption_style", "emojis", "highlight"} -
        the caller's current UI selections. Falls back to saved settings
        for anything not given, so old 2-arg calls keep working."""
        import subprocess
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m = job["moments"][index]
        words = m.get("edited_words") or [
            w for w in job["words"]
            if w["end"] > m["start"] and w["start"] < m["end"]]
        if not words:
            return {"ok": False, "error": "No captions in this moment."}
        settings = load_settings()
        set_custom_styles(settings.get("custom_styles", []))
        ui_opts = options or {}
        style = ui_opts.get("caption_style") or settings.get(
            "caption_style", "bold-pop")
        emojis = ui_opts.get("emojis", settings.get("emojis", False))
        highlight = ui_opts.get("highlight",
                                settings.get("highlight", False))
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        os.makedirs(outdir, exist_ok=True)
        ass_fd, ass_path = __import__("tempfile").mkstemp(suffix=".ass",
                                                          prefix="prev_")
        os.close(ass_fd)
        out = os.path.join(outdir, f"captions_preview_{index}.mp4")
        try:
            from pipeline import build_ass, _ffmpeg_ass_path
            dur = max(1.0, m["end"] - m["start"])
            build_ass(words, m["start"], m["end"],
                      style,
                      m.get("hook", ""), ass_path, res=(540, 960),
                      emojis=bool(emojis),
                      highlight=bool(highlight))
            cmd = ["ffmpeg", "-y", "-v", "error",
                   "-f", "lavfi", "-i",
                   f"color=c=black:s=540x960:d={dur:.2f}:r=30",
                   "-vf", f"subtitles={_ffmpeg_ass_path(ass_path)}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
            if r.returncode != 0 or not os.path.exists(out):
                return {"ok": False,
                        "error": "Preview failed: " + r.stderr[-300:]}
        finally:
            try:
                os.remove(ass_path)
            except OSError:
                pass
        return {"ok": True, "url": self._download_url(job_id, out)}

    def merge_selected(self, job_id, render_indices, transition="cut",
                       intro_path="", outro_path=""):
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "No job."}
        idxs = [i for i in render_indices
                if 0 <= i < len(job.get("renders", []))]
        if len(idxs) < 2:
            return {"ok": False,
                    "error": "Tick at least 2 rendered clips to merge."}
        paths = [job["renders"][i]["path"] for i in idxs]
        intro = (intro_path or "").strip().strip('"') or None
        outro = (outro_path or "").strip().strip('"') or None
        for p in (intro, outro):
            if p and not os.path.isfile(p):
                return {"ok": False, "error": "Intro/outro file not found: "
                                              + p[:120]}
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir,
                           f"merged_{len(job['renders']) + 1:02d}.mp4")
        try:
            merge_clips(paths, out,
                        transition=transition if transition in (
                            "cut", "crossfade") else "cut",
                        intro_path=intro, outro_path=outro)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:220]}
        # combined caption words for the merged timeline (offsets by each
        # source clip's duration) so SRT/VTT exports cover the whole merge.
        # Source words are in source-video time; rebase them to each
        # clip's own 0-based timeline first (nested merges are already
        # 0-based, their synthetic window starts at 0).
        merged_words, offset = [], 0.0
        try:
            clip_durs = [probe_duration(p) for p in paths]
        except Exception:  # noqa: BLE001
            clip_durs = [0.0] * len(paths)
        for i, dur in zip(idxs, clip_durs):
            _, msrc, w = self._render_words(job, i)
            base = (msrc or {}).get("start") or 0
            for wd in w:
                merged_words.append({
                    "start": max(0.0, wd["start"] - base + offset),
                    "end": max(0.0, wd["end"] - base + offset),
                    "word": wd["word"]})
            offset += dur or 0.0
        job["renders"].append({
            "path": out, "title": "Merged clip",
            "virality": max(job["renders"][i]["virality"] for i in idxs),
            "moment_index": job["renders"][idxs[0]]["moment_index"],
            "words": merged_words, "merged": True,
            "merged_duration": round(offset, 2),
            "url": f"/files/{job_id}/clips/{os.path.basename(out)}"})
        _save_job(job_id)
        return {"ok": True, "path": out,
                "url": self._download_url(job_id, out)}

    # -- status ----------------------------------------------------------
    def job_status(self, job_id):
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"status": "missing"}
        # don't ship the full word list to the UI
        return {
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "error": job.get("error", ""),
            "detect_error": job.get("detect_error", ""),
            "render_error": job.get("render_error", ""),
            "title": job["title"],
            "duration": job["duration"],
            "long_video": bool(job["duration"]
                               and job["duration"] >= LONG_VIDEO_WARN_S),
            "word_count": len(job["words"]),
            "moments": job["moments"],
            "renders": job["renders"],
        }

    def open_folder(self, job_id):
        job = JOBS.get(job_id)
        folder = os.path.join(BASE_DIR, "work", job_id, "clips") if job \
            else os.path.join(BASE_DIR, "work")
        os.makedirs(folder, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", folder])
            else:
                import subprocess
                subprocess.run(["xdg-open", folder])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}


# ---------------------------------------------------------------------------
# Localhost server + desktop-style window (no pywebview / .NET needed)
# ---------------------------------------------------------------------------
import socket
import subprocess

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


def _wsgi_response(start_response, status, content_type, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    start_response(status, [("Content-Type", content_type),
                            ("Content-Length", str(len(body)))])
    return [body]


def make_wsgi_app(api):
    ui_dir = os.path.abspath(os.path.join(BASE_DIR, "ui"))

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"

        # --- API: POST /api/<method> {"args": [...]} ---------------------
        if method == "POST" and path.startswith("/api/"):
            name = path[len("/api/"):]
            fn = getattr(api, name, None)
            if not callable(fn) or name.startswith("_"):
                return _wsgi_response(
                    start_response, "404 Not Found", "application/json",
                    json.dumps({"ok": False, "error": "unknown method"}))
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                length = 0
            raw = environ["wsgi.input"].read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return _wsgi_response(
                    start_response, "400 Bad Request", "application/json",
                    json.dumps({"ok": False, "error": "bad json"}))
            if not isinstance(payload, dict):
                return _wsgi_response(
                    start_response, "400 Bad Request", "application/json",
                    json.dumps({"ok": False, "error": "bad json"}))
            args = payload.get("args") or []
            try:
                result = fn(*args)
            except TypeError as e:
                return _wsgi_response(
                    start_response, "400 Bad Request", "application/json",
                    json.dumps({"ok": False,
                                "error": "bad args: " + str(e)[:160]}))
            except Exception as e:  # noqa: BLE001 - surfaced to UI
                return _wsgi_response(
                    start_response, "500 Server Error", "application/json",
                    json.dumps({"ok": False, "error": str(e)[:300]}))
            if not isinstance(result, dict):
                result = {"result": result}
            return _wsgi_response(start_response, "200 OK",
                                  "application/json",
                                  json.dumps(result, default=str))

        # --- file downloads: GET /files/<job_id>/<rel path> ---------------
        if method == "GET" and path.startswith("/files/"):
            rest = path[len("/files/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            job_id, rel = parts
            if (job_id not in JOBS or ".." in rel or rel.startswith("/")
                    or not rel):
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            base = os.path.abspath(os.path.join(BASE_DIR, "work", job_id))
            fpath = os.path.abspath(os.path.join(base, rel))
            if not fpath.startswith(base + os.sep) \
                    or not os.path.isfile(fpath):
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
            except OSError:
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            ext = os.path.splitext(fpath)[1].lower()
            ctype = _MIME.get(ext, "application/octet-stream")
            headers = [("Content-Type", ctype),
                       ("Content-Length", str(len(data)))]
            # downloads as attachments, media inline for preview
            if ext in (".zip", ".srt", ".vtt", ".fcpxml", ".xml", ".ass"):
                headers.append((
                    "Content-Disposition",
                    "attachment; filename=" + os.path.basename(fpath)))
            start_response("200 OK", headers)
            return [data]

        # --- static files from ui/ --------------------------------------
        rel = path.lstrip("/") or "index.html"
        fpath = os.path.normpath(os.path.join(ui_dir, rel))
        if not fpath.startswith(ui_dir + os.sep) or not os.path.isfile(fpath):
            return _wsgi_response(start_response, "404 Not Found",
                                  "text/plain", "not found")
        try:
            with open(fpath, "rb") as f:
                data = f.read()
        except OSError:
            return _wsgi_response(start_response, "404 Not Found",
                                  "text/plain", "not found")
        ctype = _MIME.get(os.path.splitext(fpath)[1].lower(),
                          "application/octet-stream")
        return _wsgi_response(start_response, "200 OK", ctype, data)

    return app


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _find_chrome_or_edge():
    """Chrome/Edge executable for the --app window (Windows)."""
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(
                r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(
                r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(
                r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(
                r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
    return None


def _focus_existing_window():
    """Bring the already-open app window to the front (Windows only).

    A duplicate launch must NEVER open a second window or a browser tab -
    the user asked for exactly one window. Best effort via the window
    title; silently does nothing when the window can't be found.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, "Itraji Cliper")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - focusing is a courtesy, never fatal
        pass


def _launch_window(url):
    """Open the UI in a desktop-style window - exactly ONE window.

    A successful Popen IS a successful launch: Chrome/Edge hand off to
    their browser process so fast that the launcher can exit within
    milliseconds while the window opens fine. (The old instant-death
    check misread that handoff as a failure and ALSO opened a normal
    browser tab - the "two windows" bug.) The plain-tab fallback now
    runs only when no app window could be started at all.
    """
    exe = _find_chrome_or_edge()
    if exe:
        profile = os.path.join(os.path.expanduser("~"),
                               ".clipmaker_pc_profile")
        os.makedirs(profile, exist_ok=True)
        try:
            return subprocess.Popen(
                [exe, "--app=" + url, "--user-data-dir=" + profile,
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)
    return None


def _auto_update_ytdlp():
    """Best-effort yt-dlp upgrade at startup (YouTube breaks old copies).

    Runs in a background thread with a short timeout; never blocks launch
    and never fails loudly."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--upgrade",
             "yt-dlp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=90)
    except Exception:  # noqa: BLE001 - silent by design
        pass


# ---------------------------------------------------------------------------
# Single-instance guard: repeated launches must not pile up servers/windows.
# A lockfile in the home dir holds the owner's pid; the port is published
# once the server binds. A second launch sees a live owner and exits
# quietly - no second server, no second window. A stale lock (dead pid)
# is removed and the launch proceeds.
# ---------------------------------------------------------------------------
LOCKFILE = os.path.join(os.path.expanduser("~"), ".clipmaker_pc.lock")


def _pid_alive(pid):
    """True if a process with this pid exists (cross-platform)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x00100000,
                                                   False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:  # noqa: BLE001 - assume dead
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, we just can't signal it
    except OSError:
        return False


def _read_lockfile():
    try:
        with open(LOCKFILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_lockfile(port, mode=None):
    """Publish this process as the owner.

    mode: the owner's window mode - "app" (desktop --app window) or
    "fallback" (plain browser tab). None until the launch decision is
    made; duplicates treat an unknown mode as "fallback"."""
    tmp = LOCKFILE + ".%d.tmp" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "mode": mode}, f)
        os.replace(tmp, LOCKFILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _update_lockfile_mode(mode):
    """Record the owner's window mode after the launch decision.

    Atomic write; only touches the lockfile when this process still
    owns it (a newer owner is never clobbered)."""
    info = _read_lockfile()
    if not info or info.get("pid") != os.getpid():
        return
    info["mode"] = mode
    tmp = LOCKFILE + ".%d.tmp" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, LOCKFILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _release_lockfile():
    info = _read_lockfile()
    if info and info.get("pid") == os.getpid():
        try:
            os.remove(LOCKFILE)
        except OSError:
            pass


def _acquire_single_instance():
    """Returns (owned, owner_mode).

    owned=True: this process owns the app and may start the server.
    owned=False: another live instance owns it - the caller must NOT
    start a second server. owner_mode is the owner's window mode
    ("app" / "fallback"), or None when unknown (e.g. an older build's
    lockfile, or the owner is still deciding) - treat None as
    "fallback".
    """
    for _ in range(3):
        try:
            # atomic create-and-fill: the lockfile never exists empty,
            # so a concurrent launcher can't misread it as corrupt
            fd = os.open(LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "port": None,
                           "mode": None}, f)
            return True, None
        except FileExistsError:
            pass
        info = _read_lockfile()
        if info is None:
            # unreadable lock: might be a concurrent owner mid-write -
            # wait a beat before judging it stale
            time.sleep(0.3)
            info = _read_lockfile() or {}
        if _pid_alive(info.get("pid")):
            # Live owner: give it a few seconds to publish its port
            # (still starting up).
            for _ in range(50):
                if (_read_lockfile() or {}).get("port"):
                    break
                time.sleep(0.1)
            return False, (_read_lockfile() or {}).get("mode")
        # stale lock (owner died without cleaning up): drop it, retry
        try:
            os.remove(LOCKFILE)
        except OSError:
            time.sleep(0.2)
    # Lock never settled (bizarre FS state): proceed as owner without a
    # lock rather than refusing to start at all. One window per process
    # still holds.
    return True, None


def _wait_port_open(port, timeout=5.0):
    """Block until the localhost server accepts connections.

    Usually instant; replaces the old fixed sleep so the window opens
    the moment the server is actually reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def main():
    from waitress import serve

    # Single instance: a repeated launch never starts a second server and
    # never opens anything - no second window, no browser tab, ever. It
    # just brings the one existing window to the front and exits. (An
    # earlier version opened the URL in a normal browser tab here, which
    # is how a plain Chrome tab appeared next to the app window.)
    owned, _owner_mode = _acquire_single_instance()
    if not owned:
        _focus_existing_window()
        return

    # Die gracefully on SIGTERM (task manager / kill): turn it into a
    # KeyboardInterrupt so the finally below still releases the lock.
    # (A lock left behind by SIGKILL is still self-healing: the next
    # launch sees a dead pid and treats it as stale.)
    def _on_term(_signum, _frame):
        raise KeyboardInterrupt()
    try:
        import signal
        signal.signal(signal.SIGTERM, _on_term)
    except (OSError, ValueError):  # noqa: BLE001 - non-fatal
        pass

    try:
        # keep yt-dlp fresh without ever blocking startup
        threading.Thread(target=_auto_update_ytdlp, daemon=True).start()

        api = Api()
        port = _free_port()
        url = "http://127.0.0.1:%d/" % port
        print("Itraji Cliper running at " + url, flush=True)

        server_thread = threading.Thread(
            target=serve,
            kwargs={"app": make_wsgi_app(api), "host": "127.0.0.1",
                    "port": port, "threads": 8},
            daemon=True)
        server_thread.start()

        _write_lockfile(port)
        # Server binds first, then the window opens the moment the port
        # accepts connections - the UI's splash screen covers the warmup.
        _wait_port_open(port)

        proc = _launch_window(url)
        _update_lockfile_mode("app" if proc is not None else "fallback")
        if proc is not None:
            # When the user closes the app window, shut the server down
            # too. But a browser launcher can hand off to its real
            # process and exit within a second while the window lives
            # on - if it exits that fast, keep serving instead of
            # killing the server under the open window.
            try:
                launched_at = time.time()
                proc.wait()
                if time.time() - launched_at > 3:
                    return
            except KeyboardInterrupt:
                return
        # Fallback lifetime (plain browser tab, or an app window whose
        # launcher handed off): keep serving until killed.
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    finally:
        _release_lockfile()


if __name__ == "__main__":
    main()
