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
    render_clip, retime_words, write_srt, merge_clips,
)

JOBS = {}
SETTINGS_PATH = os.path.join(os.path.expanduser("~"),
                              ".clipmaker_pc_settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"api_key": "", "clip_length": "30-60s",
                "caption_style": "bold-pop",
                "remove_fillers": False, "face_track": True}


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
    # -- settings ------------------------------------------------------
    def get_settings(self):
        return load_settings()

    def save_settings(self, settings):
        current = load_settings()
        current.update(settings or {})
        save_settings(current)
        return {"ok": True}

    # -- import + transcribe -------------------------------------------
    def import_url(self, url):
        job_id = uuid.uuid4().hex[:8]
        JOBS[job_id] = {"stage": "Starting", "progress": 0,
                        "status": "working", "error": "",
                        "video_path": "", "title": "", "duration": 0,
                        "words": [], "moments": [], "renders": []}
        t = threading.Thread(target=self._run_import, args=(job_id, url),
                             daemon=True)
        t.start()
        return {"job_id": job_id}

    def _run_import(self, job_id, url):
        job = JOBS[job_id]
        try:
            workdir = os.path.join(BASE_DIR, "work", job_id)
            video_path, title, duration = download_video(
                url, workdir, _job_progress(job_id))
            job.update({"video_path": video_path, "title": title,
                        "duration": duration})
            words = transcribe(video_path, "base", _job_progress(job_id))
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

    @staticmethod
    def _friendly_error(e):
        msg = str(e)
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
    def detect(self, job_id, max_clips=8):
        job = JOBS.get(job_id)
        if not job or job["status"] != "transcribed":
            return {"ok": False, "error": "Import a video first."}
        settings = load_settings()
        api_key = settings.get("api_key", "").strip()
        if not api_key:
            return {"ok": False, "error": "no_api_key",
                    "message": "Paste your free Gemini API key in Settings first "
                               "(get one at aistudio.google.com)."}
        clip_len = settings.get("clip_length", "30-60s")
        clip_min, clip_max = (30, 60) if clip_len == "30-60s" else (60, 120)
        job["status"] = "working"
        t = threading.Thread(
            target=self._run_detect,
            args=(job_id, api_key, clip_min, clip_max, max_clips),
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
    def detect_offline(self, job_id, max_clips=8):
        job = JOBS.get(job_id)
        if not job or job["status"] != "transcribed":
            return {"ok": False, "error": "Import a video first."}
        settings = load_settings()
        clip_len = settings.get("clip_length", "30-60s")
        clip_min, clip_max = (30, 60) if clip_len == "30-60s" else (60, 120)
        job["status"] = "working"
        t = threading.Thread(
            target=self._run_detect_offline,
            args=(job_id, clip_min, clip_max, max_clips),
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

    # -- render ----------------------------------------------------------
    def render(self, job_id, indices):
        job = JOBS.get(job_id)
        if not job or not job.get("moments"):
            return {"ok": False, "error": "Nothing to render yet."}
        settings = load_settings()
        style = settings.get("caption_style", "bold-pop")
        face_track = bool(settings.get("face_track", True))
        remove_fillers = bool(settings.get("remove_fillers", False))
        job["status"] = "working"
        t = threading.Thread(
            target=self._run_render,
            args=(job_id, list(indices), style, face_track, remove_fillers),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_render(self, job_id, indices, style, face_track,
                    remove_fillers):
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
                render_clip(job["video_path"], m, job["words"], style,
                            out, _job_progress(job_id),
                            face_track=face_track,
                            remove_fillers=remove_fillers,
                            edited_words=m.get("edited_words"))
                renders.append({"path": out, "title": m["title"],
                                "virality": m["virality"],
                                "moment_index": i})
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

    # -- SRT export + merge ----------------------------------------------
    def export_srt(self, job_id, render_idx):
        job = JOBS.get(job_id)
        if not job or not (0 <= render_idx < len(job.get("renders", []))):
            return {"ok": False, "error": "Clip not found."}
        r = job["renders"][render_idx]
        m = job["moments"][r["moment_index"]]
        words = m.get("edited_words") or [
            w for w in job["words"]
            if w["end"] > m["start"] and w["start"] < m["end"]]
        srt_path = os.path.splitext(r["path"])[0] + ".srt"
        try:
            write_srt(words, m["start"], m["end"], srt_path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "SRT failed: " + str(e)[:160]}
        return {"ok": True, "path": srt_path}

    def merge_selected(self, job_id, render_indices):
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "No job."}
        idxs = [i for i in render_indices
                if 0 <= i < len(job.get("renders", []))]
        if len(idxs) < 2:
            return {"ok": False,
                    "error": "Tick at least 2 rendered clips to merge."}
        paths = [job["renders"][i]["path"] for i in idxs]
        outdir = os.path.join(BASE_DIR, "work", job_id, "clips")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir,
                           f"merged_{len(job['renders']) + 1:02d}.mp4")
        try:
            merge_clips(paths, out)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:220]}
        job["renders"].append({"path": out, "title": "Merged clip",
                               "virality": max(job["renders"][i]["virality"]
                                               for i in idxs),
                               "moment_index": job["renders"][idxs[0]][
                                   "moment_index"]})
        return {"ok": True, "path": out}

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


def main():
    from waitress import serve

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
