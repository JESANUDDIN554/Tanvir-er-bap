"""ClipMaker PC - desktop entry point.

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
    LANGUAGES, GENRES, RATIOS,
)

JOBS = {}
BATCHES = {}
SETTINGS_PATH = os.path.join(os.path.expanduser("~"),
                              ".clipmaker_pc_settings.json")

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
                        "language": "auto", "source": "url"}
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
        return {"jobs": [
            {"job_id": jid, "title": j.get("title", ""),
             "status": j.get("status", ""), "stage": j.get("stage", ""),
             "progress": j.get("progress", 0),
             "duration": j.get("duration", 0)}
            for jid, j in JOBS.items()]}

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
            job["moments"] = moments
            job["status"] = "moments"
            job["stage"] = f"Found {len(moments)} moments"
            job["progress"] = 100
        except Exception as e:  # noqa: BLE001
            job["status"] = "transcribed"  # back to transcript state
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
            job["moments"] = moments
            job["status"] = "moments"
            job["stage"] = f"Found {len(moments)} moments (offline)"
            job["progress"] = 100
        except Exception as e:  # noqa: BLE001
            job["status"] = "transcribed"  # back to transcript state
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
        return {"ok": True, "word_count": len(edited)}

    def reset_caption_words(self, job_id, index):
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m = job["moments"][index]
        m.pop("edited_words", None)
        m.pop("caption_edited", None)
        return {"ok": True}

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
        job["moments"].extend(moments)
        if moments and job["status"] == "transcribed":
            job["status"] = "moments"
            job["stage"] = f"Found {len(job['moments'])} moments"
        return {"ok": True, "found": len(moments), "unmatched": unmatched}

    # -- render ----------------------------------------------------------
    def render(self, job_id, indices, options=None):
        job = JOBS.get(job_id)
        if not job or not job.get("moments"):
            return {"ok": False, "error": "Nothing to render yet."}
        settings = load_settings()
        opts = dict(options or {})
        # defaults from settings, overridable per render call
        for key in ("caption_style", "ratio", "quality", "face_track",
                    "remove_fillers", "remove_silences", "auto_censor",
                    "emojis", "highlight"):
            opts.setdefault(key, settings.get(key))
        opts.setdefault("caption_style", "bold-pop")
        opts.setdefault("ratio", "9:16")
        opts.setdefault("quality", "1080p")
        opts["custom_styles"] = settings.get("custom_styles", [])
        set_custom_styles(opts["custom_styles"])
        job["last_render_opts"] = {k: opts.get(k) for k in
                                   ("caption_style", "ratio", "quality",
                                    "face_track", "remove_fillers",
                                    "remove_silences", "auto_censor",
                                    "emojis", "highlight")}
        job["status"] = "working"
        t = threading.Thread(
            target=self._run_render,
            args=(job_id, list(indices), opts),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_render(self, job_id, indices, opts):
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
                out = os.path.join(outdir, f"{n + 1:02d}_{safe}.mp4")
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
                            edited_words=m.get("edited_words"))
                renders.append({"path": out, "title": m["title"],
                                "virality": m["virality"],
                                "moment_index": i,
                                "url": f"/files/{job_id}/clips/"
                                       f"{os.path.basename(out)}"})
            job["renders"] = renders
            job["status"] = "done"
            job["stage"] = f"Done - {len(renders)} clips rendered"
            job["progress"] = 100
        except Exception as e:  # noqa: BLE001
            job["status"] = "moments"
            job["render_error"] = "Render failed: " + str(e)[:220]
            job["stage"] = "Render failed"
            # keep any clips that finished before the failure
            job["renders"] = renders

    # -- caption exports (SRT/VTT), ZIP, XML timeline, previews -----------
    def _render_words(self, job, render_idx):
        r = job["renders"][render_idx]
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

    def preview_captions(self, job_id, index):
        """Captions-only preview: black video + burned captions for a moment."""
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
                      settings.get("caption_style", "bold-pop"),
                      m.get("hook", ""), ass_path, res=(540, 960),
                      emojis=bool(settings.get("emojis")),
                      highlight=bool(settings.get("highlight")))
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
        job["renders"].append({
            "path": out, "title": "Merged clip",
            "virality": max(job["renders"][i]["virality"] for i in idxs),
            "moment_index": job["renders"][idxs[0]]["moment_index"],
            "url": f"/files/{job_id}/clips/{os.path.basename(out)}"})
        return {"ok": True, "path": out,
                "url": self._download_url(job_id, out)}

    # -- status ----------------------------------------------------------
    def job_status(self, job_id):
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


def _launch_window(url):
    """Open the UI in a desktop-style window.

    Returns the browser Popen handle when we own the window process,
    else None (fell back to a normal browser tab).
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


def main():
    from waitress import serve

    # keep yt-dlp fresh without ever blocking startup
    threading.Thread(target=_auto_update_ytdlp, daemon=True).start()

    api = Api()
    port = _free_port()
    url = "http://127.0.0.1:%d/" % port
    print("ClipMaker PC running at " + url, flush=True)

    server_thread = threading.Thread(
        target=serve,
        kwargs={"app": make_wsgi_app(api), "host": "127.0.0.1",
                "port": port, "threads": 8},
        daemon=True)
    server_thread.start()

    time.sleep(0.6)  # let the server bind before opening the window
    proc = _launch_window(url)
    if proc is not None:
        # When the user closes the app window, shut the server down too.
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        os._exit(0)
    # Fallback (normal browser tab): keep serving until killed.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
