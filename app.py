"""Itraji Cliper - desktop entry point.

Runs a localhost HTTP server for the local UI and opens it in the
app's own desktop window (pywebview over the system WebView2
runtime), falling back to a Chrome/Edge --app window.
All video work runs on this machine, including the optional
Built-in AI (a local model) - nothing is sent to any cloud service.
"""

import json
import os
import sys
import threading
import time
import uuid

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS  # PyInstaller bundle
    # Bundled exe: ffmpeg.exe/ffprobe.exe ship next to the exe - put that
    # folder on PATH so pipeline.py's bare "ffmpeg"/"ffprobe" calls find them.
    _exe_dir = os.path.dirname(sys.executable)
    os.environ["PATH"] = _exe_dir + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import (  # noqa: E402
    download_video, transcribe, detect_moments_offline,
    detect_moments_local,
    find_moments_by_keywords, parse_srt_file,
    render_clip, retime_words, write_srt, write_vtt, write_xml_timeline,
    merge_clips, probe_duration, probe_video, set_custom_styles,
    generate_posting_package, posting_package_valid,
    remove_captions, detect_caption_band,
    preview_caption_removal,
    generate_titles, generate_script, enhance_video,
    generate_hooks, find_hook_moment, rank_hook_candidates,
    translate_captions as pipeline_translate_captions,
    TRANSLATE_LANGS, CATEGORIES, category_info,
    LANGUAGES, GENRES, RATIOS,
)

import local_ai  # noqa: E402  (built-in local LLM: engine/model/server)
import admin  # noqa: E402  (Owner Admin Panel: sessions/log/stats)
import usage  # noqa: E402  (per-key clip limits: signed max_clips)
import publishing  # noqa: E402  (Auto Publishing: accounts/queue/scheduler)
import ai_components  # noqa: E402  (AI Installation Manager backend)
import licensing  # noqa: E402  (signed license + core pack + integrity)
import updater  # noqa: E402  (signed file-replacement update packs)
from pipeline import load_corepack as _load_corepack  # noqa: E402
import pipeline as _pipeline  # noqa: E402

_pipeline.FONTS_DIR = os.path.join(BASE_DIR, "fonts")

JOBS = {}
BATCHES = {}
_REMOVER_JOBS = {}  # Caption Remover runs (session-only, not Library)
_GEN_JOBS = {}      # fal.ai generator runs (session-only)
_ENHANCE_JOBS = {}  # Video Enhancer runs (session-only)
_BENHANCE_BATCHES = {}  # Post-clipping Enhance-All queues (session-only;
                        # results persist on the job's render records)
_PROCESS_START = time.time()
SETTINGS_PATH = os.path.join(os.path.expanduser("~"),
                              ".clipmaker_pc_settings.json")

# The app's version, shown in Settings -> Updates. This is the
# version string the UI already displays ("v2.0"); bump it when a
# release changes compiled code. Update packs carry their own
# version for the apply-result line.
APP_VERSION = "2.0"

# Process start, for the Admin Panel's uptime figure.
_APP_START_TS = time.time()

# Job pruning: long desktop sessions must not grow JOBS forever.
JOB_TTL_S = 6 * 3600   # drop non-working jobs older than 6h
JOB_HARD_CAP = 40      # ...and never keep more than 40
JOB_KEEP_MIN = 20      # hard-cap pruning stops here (most recent kept)


# -- Work roots (Settings -> Storage) ------------------------------------
# New projects/jobs are written under _work_root(): <data_dir>/work
# when the user chose a storage location, else the app's own work
# folder (exactly the pre-Storage behaviour). Projects created
# BEFORE a switch stay in the folder that already holds them -
# _job_folder/_session_folder find them in either root, so the
# Library and downloads keep working across the switch.

def _work_root():
    dd = local_ai.custom_data_dir()
    if dd:
        return os.path.join(dd, "work")
    return os.path.join(BASE_DIR, "work")


def _work_roots():
    roots = [_work_root()]
    legacy = os.path.join(BASE_DIR, "work")
    if legacy not in roots:
        roots.append(legacy)
    return roots


def _job_folder(job_id):
    """The folder of an existing job, whichever root holds it; for
    a job that exists nowhere yet, its folder under the effective
    root (the creation path)."""
    for root in _work_roots():
        p = os.path.join(root, job_id)
        if os.path.isdir(p):
            return p
    return os.path.join(_work_root(), job_id)


def _session_folder(name):
    """Same lookup for session folders (remover-*, enhance-*):
    find the existing one in either root, else the effective
    root's path for creation."""
    for root in _work_roots():
        p = os.path.join(root, name)
        if os.path.isdir(p):
            return p
    return os.path.join(_work_root(), name)


_JOB_ID_RE = None  # compiled on first use (module import order)


def _valid_job_id(jid):
    """Job ids are server-minted tokens ([A-Za-z0-9_-]{1,64} - the
    same class the /files route enforces). Anything else is not a
    job id and must never reach a filesystem path."""
    global _JOB_ID_RE
    if _JOB_ID_RE is None:
        import re as _re
        _JOB_ID_RE = _re.compile(r"[A-Za-z0-9_-]{1,64}")
    return bool(_JOB_ID_RE.fullmatch(jid or ""))


def _folder_inside_work_roots(folder):
    """True when folder is a DIRECT child of a work root. The
    containment net under every rmtree of a job folder: even if a
    caller passes an unsanitised id, the resolved path can only
    ever be a folder the app itself created."""
    ap = os.path.abspath(folder)
    return any(os.path.dirname(ap) == os.path.abspath(root)
               for root in _work_roots())


def _drop_job(jid):
    job = JOBS.pop(jid, None)
    if job is None:
        return
    # free the disk too - a pruned job's /files URLs 404 anyway
    folder = _job_folder(jid)
    if not _folder_inside_work_roots(folder):
        return  # never rmtree outside the work roots
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


def _folder_referenced(folder):
    """True when any live job's video or render lives inside
    folder (e.g. a generated video imported into the Library
    keeps its file in the gen-* folder). Such folders are never
    swept, however old they look."""
    ap = os.path.abspath(folder) + os.sep
    for j in list(JOBS.values()):
        vp = j.get("video_path") or ""
        if vp and os.path.abspath(vp).startswith(ap):
            return True
        for r in j.get("renders", []):
            rp = r.get("path") or ""
            if rp and os.path.abspath(rp).startswith(ap):
                return True
    return False


def _rmtree_session_folder(name):
    folder = _session_folder(name)
    if not _folder_inside_work_roots(folder):
        return
    if _folder_referenced(folder):
        return
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


def _prune_sessions():
    """Bound the session stores + their folders. JOBS has always
    had TTL pruning; the remover/enhance/generator stores and
    their on-disk folders NEVER did - every run (and every crash
    orphan) accumulated for the life of the install (measured:
    112 work dirs / 468 MB on one machine, only 24 of them
    Library jobs). Terminal entries older than the job TTL are
    dropped with their folders; active entries are never touched."""
    now = time.time()
    for store, prefix in ((_REMOVER_JOBS, "remover-"),
                          (_ENHANCE_JOBS, "enhance-"),
                          (_GEN_JOBS, "gen-")):
        for sid, st in list(store.items()):
            if st.get("status") in ("running", "queued"):
                continue
            if now - st.get("created", now) > JOB_TTL_S:
                store.pop(sid, None)
                _rmtree_session_folder(prefix + sid)
    for bid, b in list(_BENHANCE_BATCHES.items()):
        if b.get("state") != "running" \
                and now - b.get("created", now) > JOB_TTL_S:
            _BENHANCE_BATCHES.pop(bid, None)
    for bid, b in list(BATCHES.items()):
        if b.get("status") != "working" \
                and now - b.get("created", now) > JOB_TTL_S:
            BATCHES.pop(bid, None)


def _sweep_orphan_folders():
    """Startup sweep (runs once, from _load_library): session
    folders left by a PREVIOUS process (crash/kill orphans - the
    session dicts died with that process, so nothing can ever
    serve them again) are removed, and stale render .part files
    (interrupted encodes) older than a day go with them. A folder
    holding a Library job's video/renders is never touched, and
    folders with job.json (real projects) are not session folders
    by construction."""
    import glob as _glob
    live = ({"remover-" + k for k in _REMOVER_JOBS}
            | {"enhance-" + k for k in _ENHANCE_JOBS}
            | {"gen-" + k for k in _GEN_JOBS})
    for root in _work_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(("remover-", "enhance-", "gen-")):
                continue
            if name in live:
                continue
            folder = os.path.join(root, name)
            try:
                if not os.path.isdir(folder):
                    continue
                if os.path.getmtime(folder) > _PROCESS_START - 5:
                    continue  # created by THIS process - not an orphan
            except OSError:
                continue
            if _folder_referenced(folder):
                continue
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
        # stale .part encodes inside job folders (never job.json
        # folders themselves, never fresh parts a render may own)
        for part in _glob.glob(os.path.join(root, "*", "clips",
                                            "*.part.mp4")):
            try:
                if time.time() - os.path.getmtime(part) > 86400:
                    os.remove(part)
            except OSError:
                pass


def _prune_jobs():
    _prune_sessions()
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
_CORRUPT_JOBS = set()  # folders whose job.json exists but won't parse


def _is_library_job(job_id):
    return any(os.path.isfile(os.path.join(root, job_id, "job.json"))
               for root in _work_roots())


def _save_job(job_id):
    """Persist a job's durable state to work/<job_id>/job.json."""
    job = JOBS.get(job_id)
    if not job or job.get("status") not in _LIBRARY_STATUSES:
        return
    try:
        data = {k: job.get(k) for k in (
            "title", "source", "video_path", "duration", "words",
            "moments", "renders", "status", "stage", "language",
            "category", "created")}
        folder = _job_folder(job_id)
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
    # Scan EVERY work root: projects created before a storage
    # switch live in the legacy root and must stay in the Library.
    names = []
    for work in _work_roots():
        try:
            names += [(work, n) for n in os.listdir(work)]
        except OSError:
            continue
    for work, name in names:
        if name in JOBS:
            continue
        meta = os.path.join(work, name, "job.json")
        if not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001 - corrupt entry: the
            # project must not silently VANISH from the Library
            # while its folder sits on disk. Remember it; the
            # Library listing and job_status report it as damaged.
            _CORRUPT_JOBS.add(name)
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
            "category": data.get("category", ""),
            "source": data.get("source", "url"),
            "created": data.get("created", 0) or os.path.getmtime(meta)}
    # Library is loaded: now the reference check can see every
    # saved project, so orphaned session folders from previous
    # processes can be swept safely.
    try:
        _sweep_orphan_folders()
    except Exception:  # noqa: BLE001 - housekeeping, never fatal
        pass

DEFAULT_SETTINGS = {
    "detect_mode": "local",        # "local" (Built-in AI) or "offline"
    "clip_length": "30-60",        # range string "min-max" seconds
    "clip_count": 8,
    "caption_style": "bold-pop",
    "custom_styles": [],           # [{name, primary, secondary, outline, bold}]
    "ratio": "9:16",
    "quality": "1080p",
    "language": "auto",
    "genre": "",
    "category": "general",         # Phase 1: content-category mode
                                   # (general/bodycam/podcast/interview/
                                   # gaming/education/news) - tunes
                                   # detection weights + subscores
    "audio_polish": False,         # Phase 1: loudnorm to -14 LUFS
    "denoise_audio": False,        # Phase 1: gentle afftdn with polish
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
    "emphasis_zoom": False,        # punch-in on the strongest beat(s)
    "emphasis_level": "gentle",    # gentle/medium/strong
    "broll": False,                # stock cutaway on long stretches
    "enhance_clarity": False,      # sharpen + denoise ("enhance", not "AI upscale")
    "quality_tier": "maximum",     # encode tier: maximum/balanced/fast
    "logo_path": "",               # brand-kit corner logo PNG
    "brand_kits": [],              # [{name, caption_style, primary, secondary,
                                   #   outline, logo_path, intro_path, outro_path}]
    "planner": [],                 # [{id, job_id, title, scheduled, done,
                                   #   export_name}] - local posting planner
    "fal_key": "",                 # user's own fal.ai API key (Studio
                                   # Generator) - never sent to the UI
    # -- Auto Publishing (publishing.py) ------------------------------
    # Credentials below live ONLY in this server-side settings file.
    # get_settings() masks every secret (same pattern as fal_key);
    # adapters read them through publishing.resolve_account_config().
    "pub_enabled": False,          # master switch for the worker
    "pub_approval_mode": "manual",  # manual | auto
    "pub_max_batch": 10,           # max jobs per create action
    "pub_daily_limit": 0,          # per account per day; 0 = no limit
    "pub_delay_s": 20,             # min seconds between uploads/account
    "pub_default_preset": "",      # preset id for render auto-publish
    "pub_default_visibility": "public",  # public | unlisted | private
    "pub_presets": [],             # [{id, name, platforms, approval,
                                   #   visibility, delay_minutes}]
    "pub_accounts": [],            # [{id, platform, label,
                                   #   external_id, token}] - token is
                                   #   a secret, masked in get_settings
    "pub_default_accounts": {},    # platform -> account id
    "pub_account_status": {},      # account id -> last test result
    "pub_fb_app_id": "",
    "pub_fb_app_secret": "",       # secret
    "pub_fb_base": "",             # "" = graph.facebook.com/v21.0
    "pub_yt_client_id": "",
    "pub_yt_client_secret": "",    # secret
    "pub_yt_base": "",             # "" = www.googleapis.com
    "pub_tt_client_key": "",
    "pub_tt_client_secret": "",    # secret
    "pub_tt_base": "",             # "" = open.tiktokapis.com
    "pub_tt_audited": False,       # TikTok app passed audit (else
                                   # posts are SELF_ONLY - surfaced)
    "pub_webhook_url": "",         # completion events; off when ""
    "pub_webhook_secret": "",      # secret - HMAC key for the outbox
    "data_dir": "",                # Settings -> Storage: folder for
                                   # the AI model + new projects;
                                   # "" = the app folder (default)
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


_SETTINGS_LOCK = threading.Lock()
_SETTINGS_LOAD_WARNING = {"active": False}


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        # The file EXISTS but cannot be read: the app boots on
        # defaults, which silently reverts the saved data folder
        # and looks like lost settings. Remember it so the UI can
        # say so honestly (get_settings surfaces load_warning)
        # instead of the failure being invisible.
        data = {}
        _SETTINGS_LOAD_WARNING["active"] = True
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data or {})
    # Migration: the old cloud-AI option is gone. A stored key from an
    # old settings file is stripped (and never written back), and the
    # old "ai" detect mode lands on the Built-in AI.
    merged.pop("api_key", None)
    if merged.get("detect_mode") not in ("local", "offline"):
        merged["detect_mode"] = "local"
    return merged


def save_settings(data):
    data = {k: v for k, v in (data or {}).items() if k != "api_key"}
    # tmp + os.replace: the old bare open("w") truncated the file
    # in place, so a crash or kill mid-save corrupted the settings
    # (which carry the license key and data-dir pointer). Same
    # pattern usage.py and admin.py already use for their files.
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SETTINGS_PATH)
        _SETTINGS_LOAD_WARNING["active"] = False
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Auto Publishing glue: publishing.py reads/writes app settings through
# these injected callables (it never imports app.py). The saver merges
# under the settings lock, so the module's write-backs (connection
# test results) can't race a UI settings save, and masked credential
# values never round-trip through it.
# ---------------------------------------------------------------------------

def _pub_save_settings_patch(patch):
    with _SETTINGS_LOCK:
        s = load_settings()
        s.update(patch or {})
        save_settings(s)


_PUB_BOUND = {"done": False}


def _ensure_pub():
    if not _PUB_BOUND["done"]:
        publishing.bind_settings(load_settings, _pub_save_settings_patch)
        _PUB_BOUND["done"] = True
    publishing.ensure_worker()


# ---------------------------------------------------------------------------
# Old-copy cleanup (Settings -> "Old version folders").
#
# Users accumulate loose folders of older builds (Downloads, Desktop,
# ...) plus older per-user installs, then launch an old one by mistake
# and report ghost bugs ("the fix doesn't work"). Settings can scan
# for those folders and delete them. The safety rules are strict and
# enforced server-side, at scan time AND again at delete time:
#   * a folder only qualifies if it provably IS an app copy - the
#     marker exe inside it (ClipMakerPC.exe), or an Inno uninstaller
#     (unins000.exe) in a folder whose name matches the app;
#   * the RUNNING copy's folder is never listed and never deleted,
#     and neither is any folder containing it;
#   * delete targets must be direct children of the scan roots -
#     the exact set the scan lists - so a client-supplied path can
#     never reach anywhere else.
# ---------------------------------------------------------------------------

_OLD_COPY_MARKER = "clipmakerpc.exe"
_OLD_COPY_UNINSTALLER = "unins000.exe"
_OLD_COPY_NAME_PATTERNS = ("clipmakerpc*", "itraji cliper*",
                           "clipmaker pc*")
_OLD_COPY_SIZE_WALK_CAP = 200000  # files; one copy is ~15k


def _running_app_dir():
    """Folder of the RUNNING copy - never listed, never deleted.

    Frozen: the folder holding the exe (the install or loose folder).
    BASE_DIR is NOT this when frozen (it is the _MEIPASS extraction
    dir), so sys.executable is the authority. From source: this
    file's directory."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _old_copy_scan_roots():
    """Folders whose DIRECT children are scanned for old copies."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        home, "AppData", "Local")
    roots = [home,
             os.path.join(home, "Desktop"),
             os.path.join(home, "Downloads"),
             os.path.join(home, "Documents"),
             os.path.join(local, "Programs")]
    seen, out = set(), []
    for r in roots:
        key = os.path.normcase(os.path.abspath(r))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _is_old_copy_dir(path):
    """(is_copy, has_uninstaller) for one directory.

    Primary gate: the marker exe inside. Secondary: an Inno
    uninstaller AND a folder name matching the app - an arbitrary
    program's uninstaller must never qualify a folder on its own."""
    try:
        names = {n.lower() for n in os.listdir(path)}
    except OSError:
        return (False, False)
    has_uninstaller = _OLD_COPY_UNINSTALLER in names
    if _OLD_COPY_MARKER in names:
        return (True, has_uninstaller)
    if has_uninstaller:
        import fnmatch
        base = os.path.basename(os.path.normpath(path)).lower()
        if any(fnmatch.fnmatchcase(base, pat)
               for pat in _OLD_COPY_NAME_PATTERNS):
            return (True, True)
    return (False, False)


def _dir_size_capped(path):
    """Total file bytes under path; symlinked dirs are not followed
    and the walk stops at a generous file cap, so a pathological
    tree cannot hang the scan. Unreadable entries count as 0."""
    total, seen = 0, 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > _OLD_COPY_SIZE_WALK_CAP:
                return total
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _scan_old_copies():
    """Old app-copy folders under the scan roots, largest first.
    The running copy (and any folder containing it) is excluded."""
    running = os.path.normcase(os.path.abspath(_running_app_dir()))
    roots = _old_copy_scan_roots()
    root_keys = {os.path.normcase(os.path.abspath(r)) for r in roots}
    found, seen = [], set()
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            if key == running or (running + os.sep).startswith(
                    key + os.sep):
                continue  # the running copy, or a folder holding it
            if key in root_keys:
                continue  # never list a scan root itself (~/Desktop…)
            is_copy, has_uninstaller = _is_old_copy_dir(path)
            if not is_copy:
                continue
            try:
                modified = os.path.getmtime(path)
            except OSError:
                modified = 0
            found.append({"path": path,
                          "size_bytes": _dir_size_capped(path),
                          "has_uninstaller": has_uninstaller,
                          "modified": modified})
    found.sort(key=lambda c: c["size_bytes"], reverse=True)
    return found


def _validate_old_copy_target(path):
    """Re-validate a client-supplied delete target against EVERY
    scan rule. Returns the normalized absolute path, or None to
    refuse. The client's word is never enough: the folder must
    still be a provable app copy, a direct child of a scan root,
    and unrelated to the running copy and the home dir itself."""
    if not isinstance(path, str) or not path.strip():
        return None
    p = os.path.abspath(os.path.normpath(path.strip().strip('"')))
    if os.path.islink(p) or not os.path.isdir(p):
        return None
    key = os.path.normcase(p)
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    if key == home:
        return None
    running = os.path.normcase(os.path.abspath(_running_app_dir()))
    if key == running or (running + os.sep).startswith(key + os.sep):
        return None  # the running copy, or a folder containing it
    roots = _old_copy_scan_roots()
    root_keys = {os.path.normcase(os.path.abspath(r)) for r in roots}
    if key in root_keys:
        return None  # a scan root itself (Desktop, Downloads, …)
    parent = os.path.normcase(os.path.dirname(p))
    if parent not in root_keys:
        return None  # not a direct child of any scan root
    is_copy, _un = _is_old_copy_dir(p)
    if not is_copy:
        return None
    return p


def _kill_old_copy_processes(folder):
    """Best-effort stop of ClipMakerPC.exe processes whose exe file
    lives INSIDE `folder` - a running old copy locks its files and
    would defeat the delete. Windows only; never this process.
    Failures are swallowed: the delete reports its own result."""
    if os.name != "nt":
        return
    prefix = os.path.normcase(os.path.abspath(folder)) + os.sep
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    if proc.info.get("pid") == os.getpid():
                        continue
                    exe = proc.info.get("exe") or ""
                    if os.path.normcase(exe).startswith(prefix):
                        proc.kill()
                except Exception:  # noqa: BLE001 - per-process guard
                    continue
            return
        except Exception:  # noqa: BLE001 - fall through to CIM
            pass
    try:
        import subprocess
        esc = os.path.abspath(folder).replace("'", "''")
        script = (
            "Get-CimInstance Win32_Process "
            "-Filter \"Name='ClipMakerPC.exe'\" | "
            "Where-Object { $_.ProcessId -ne " + str(os.getpid()) +
            " -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith('"
            + esc + "\\', [System.StringComparison]::OrdinalIgnoreCase)"
            " } | ForEach-Object { Stop-Process -Id $_.ProcessId "
            "-Force -ErrorAction SilentlyContinue }")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 - best effort only
        pass


def _delete_old_copy(path):
    """Delete one validated old-copy folder. Returns (ok, error).

    Order: stop processes running from inside it, run its Inno
    uninstaller first when present (it knows about shortcuts and
    registry entries), then remove whatever is left with a plain
    recursive delete whose error handler clears read-only flags."""
    import shutil
    import subprocess
    _kill_old_copy_processes(path)
    if os.name == "nt":
        time.sleep(0.3)  # let killed processes release their files
        uninstaller = os.path.join(path, _OLD_COPY_UNINSTALLER)
        if os.path.isfile(uninstaller):
            try:
                subprocess.run(
                    [uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES",
                     "/NORESTART"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=180,
                    creationflags=getattr(subprocess,
                                          "CREATE_NO_WINDOW", 0))
            except Exception:  # noqa: BLE001 - leftovers handled below
                pass
            # Inno uninstallers often hand off to a temp copy and
            # return before the folder is actually gone.
            for _ in range(20):
                if not os.path.exists(path):
                    break
                time.sleep(0.25)
    if not os.path.exists(path):
        return (True, "")

    def _clear_readonly(func, p, _exc_info):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:  # noqa: BLE001 - the rmtree result decides
            pass

    last_error = ""
    for attempt in range(2):
        try:
            shutil.rmtree(path, onerror=_clear_readonly)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            last_error = str(e)[:200]
        if not os.path.exists(path):
            return (True, "")
        if attempt == 0:
            time.sleep(1.0)  # a handle may still be closing
    if not last_error:
        last_error = ("some files could not be removed - the copy "
                      "may still be running; close it and try again")
    return (False, last_error)


# ---------------------------------------------------------------------------
# Self-uninstall (Settings -> "Danger zone").
#
# A running exe cannot delete itself on Windows, so the endpoint
# writes a small PowerShell cleanup script into the user's TEMP dir
# (never inside the app folder - the script must survive deleting
# it), launches it fully detached, and exits. The script waits for
# this process's PID to disappear, then removes the app:
#   * installed layout (unins000.exe in the app folder): run the
#     Inno uninstaller silently; with remove_data, also sweep the
#     folder it leaves behind (runtime downloads/projects live in
#     there and Inno keeps unknown files);
#   * loose layout: deleting the app folder IS the uninstall.
# remove_data additionally deletes the per-user data OUTSIDE the app
# folder: the settings file, the Chrome-mode profile dir, the work/
# projects dir and the Built-in AI data dir (when they live outside).
#
# Safety: only the packaged app may do this - a source checkout is
# refused outright - and the app folder itself must pass the same
# sanity rules as the old-copy validator (never the home dir, never
# a drive root).
# ---------------------------------------------------------------------------

def _uninstall_data_paths():
    """Per-user data a remove_data uninstall deletes, as absolute
    paths. Deduped; the home dir itself and roots are filtered out
    as paranoia - a path bug must never widen the delete."""
    home = os.path.expanduser("~")
    candidates = [SETTINGS_PATH,
                  os.path.join(home, ".clipmaker_pc_profile"),
                  os.path.join(BASE_DIR, "work")]
    try:
        candidates.append(local_ai.DATA_DIR)
    except Exception:  # noqa: BLE001 - local_ai imports above anyway
        pass
    # A user-chosen storage location holds data too (Settings ->
    # Storage): its work root and model folder belong to a
    # remove-data uninstall just like the defaults.
    dd = local_ai.custom_data_dir()
    if dd:
        candidates.append(os.path.join(dd, "work"))
        candidates.append(os.path.join(dd, "models"))
    out, seen = [], set()
    for p in candidates:
        if not p:
            continue
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen:
            continue
        seen.add(key)
        if key == os.path.normcase(os.path.abspath(home)):
            continue
        if os.path.dirname(ap) == ap:
            continue
        out.append(ap)
    return out


def _uninstall_plan(remove_data):
    """(plan, "") or (None, reason). Only the packaged app gets a
    plan: frozen, or an exe literally named ClipMakerPC.exe."""
    exe_name = os.path.basename(sys.executable or "").lower()
    if not (getattr(sys, "frozen", False)
            or exe_name == "clipmakerpc.exe"):
        return None, ("only available in the installed app - this "
                      "copy is running from source")
    app_dir = os.path.abspath(_running_app_dir())
    home = os.path.abspath(os.path.expanduser("~"))
    if os.path.normcase(app_dir) == os.path.normcase(home):
        return None, "refused: the app folder is the home folder"
    if os.path.dirname(app_dir) == app_dir:
        return None, "refused: the app folder is a drive root"
    if not os.path.isdir(app_dir):
        return None, "refused: app folder not found"
    mode = "uninstaller" if os.path.isfile(
        os.path.join(app_dir, _OLD_COPY_UNINSTALLER)) else "folder"
    return ({"mode": mode, "app_dir": app_dir, "pid": os.getpid(),
             "remove_data": bool(remove_data),
             "data_paths": _uninstall_data_paths()}, "")


def _uninstall_script_text(plan):
    """The detached cleanup script (PowerShell) as pure text - a
    separate builder so tests can assert exactly what would run.
    The script only ever executes on a user's PC, after this
    process has exited."""
    def q(p):  # PowerShell single-quoted literal
        return "'" + str(p).replace("'", "''") + "'"

    app_dir = plan["app_dir"]
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "# Itraji Cliper self-uninstall: wait for the app to exit.",
        "for ($i = 0; $i -lt 120; $i++) {",
        "  if (-not (Get-Process -Id %d -ErrorAction SilentlyContinue))"
        " { break }" % plan["pid"],
        "  Start-Sleep -Milliseconds 500",
        "}",
        "Start-Sleep -Seconds 1",
    ]
    if plan["mode"] == "uninstaller":
        lines.append(
            "Start-Process -FilePath %s -ArgumentList "
            "'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait"
            % q(os.path.join(app_dir, _OLD_COPY_UNINSTALLER)))
    if plan["mode"] == "folder" or plan["remove_data"]:
        lines += [
            "for ($i = 0; $i -lt 20; $i++) {",
            "  if (-not (Test-Path %s)) { break }" % q(app_dir),
            "  Remove-Item %s -Recurse -Force" % q(app_dir),
            "  Start-Sleep -Milliseconds 500",
            "}",
        ]
    if plan["remove_data"]:
        for p in plan["data_paths"]:
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(
                    os.path.abspath(app_dir)):
                continue
            lines.append("Remove-Item %s -Recurse -Force" % q(p))
    lines.append("Remove-Item $PSCommandPath -Force")
    return "\r\n".join(lines) + "\r\n"


def _write_uninstall_script(plan):
    """Write the cleanup script into the TEMP dir (which survives
    the app folder's deletion). Returns the script path."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="itraji-uninstall-",
                                suffix=".ps1")
    # newline="" - the generator already emits CRLF; translating
    # again would produce CR-CR-LF line endings.
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(_uninstall_script_text(plan))
    return path


def _launch_uninstall_script(script_path):
    """Start the cleanup script fully detached (Windows only).
    Returns True when launched. Off Windows: False - the plan guard
    has already refused source runs, so this stays unreachable in
    development."""
    if os.name != "nt":
        return False
    import subprocess
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", script_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return True


def _schedule_self_exit(delay=0.8):
    """Exit this process shortly AFTER the HTTP response returns -
    the cleanup script waits for our PID, so the process must
    actually die for the uninstall to proceed."""
    def _die():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()


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


_JOB_LOG_CAP = 300


def _job_log(job_id, text):
    """Append a REAL milestone line to a job's event log (the Live
    Analysis console). Only called at points where the event actually
    happened - nothing here is decorative."""
    job = JOBS.get(job_id)
    if job is None:
        return
    log = job.setdefault("log", [])
    log.append({"t": time.time(), "text": str(text)})
    if len(log) > _JOB_LOG_CAP:
        del log[:len(log) - _JOB_LOG_CAP]


def _release_render_lock(job):
    """Release a job's render lock early, from inside the worker
    that holds it, BEFORE the job's status flips to its final value
    (sweep2 T4). Safe to call speculatively: a double release is
    swallowed, and the worker's finally-block release in
    _run_render stays in place as the backstop."""
    lock = job.get("_render_lock")
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            pass  # already released


def _remover_log(rid, text):
    st = _REMOVER_JOBS.get(rid)
    if st is None:
        return
    log = st.setdefault("log", [])
    log.append({"t": time.time(), "text": str(text)})
    if len(log) > _JOB_LOG_CAP:
        del log[:len(log) - _JOB_LOG_CAP]


def _copy_video_unchanged(src, out):
    """Copy a video into the output slot WITHOUT re-encoding it:
    try a stream-copy remux into .mp4 first (keeps the bytes
    bit-exact per stream), and if the source container/codecs
    refuse that, fall back to a plain file copy. Used when the
    Caption Remover finds no captions: the honest result is the
    original video, not a degraded re-encode."""
    import shutil
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src,
             "-c", "copy", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and os.path.exists(out) \
                and os.path.getsize(out) > 0:
            return
    except Exception:  # noqa: BLE001 - fall through to plain copy
        pass
    shutil.copyfile(src, out)


# -- Studio Generator: fal.ai model catalog ------------------------------
# Every entry was verified against the model's own fal.ai API docs page
# (endpoint id + input schema) on 2026-10-24. Prices are approximate
# per-generation costs from fal.ai's published pricing, shown with "~"
# in the UI - fal.ai bills the user's own account directly.
FAL_MODELS = [
    {"id": "wan25", "name": "Wan 2.5 (Alibaba)", "kind": "video",
     "endpoint": "fal-ai/wan-25-preview/text-to-video",
     "price": "~$0.05/s at 480p",
     "aspects": ["16:9", "9:16", "1:1"],
     "durations": ["5", "10"],
     "resolutions": ["480p", "720p", "1080p"],
     "def_aspect": "16:9", "def_duration": "5",
     "def_resolution": "1080p",
     "note": "Great value, native audio, up to 1080p."},
    {"id": "kling25", "name": "Kling 2.5 Turbo Pro", "kind": "video",
     "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
     "price": "~$0.07/s",
     "aspects": ["16:9", "9:16", "1:1"],
     "durations": ["5", "10"],
     "resolutions": None,
     "def_aspect": "16:9", "def_duration": "5", "def_resolution": None,
     "note": "Cinematic motion, 1080p."},
    {"id": "veo31", "name": "Veo 3.1 (Google)", "kind": "video",
     "endpoint": "fal-ai/veo3.1",
     "price": "~$0.40/s with audio",
     "aspects": ["16:9", "9:16"],
     "durations": ["4s", "6s", "8s"],
     "resolutions": ["720p", "1080p", "4k"],
     "def_aspect": "16:9", "def_duration": "8s",
     "def_resolution": "1080p",
     "note": "Top quality, generates matching audio + dialogue."},
    {"id": "veo31fast", "name": "Veo 3.1 Fast (Google)", "kind": "video",
     "endpoint": "fal-ai/veo3.1/fast",
     "price": "~$0.15/s with audio",
     "aspects": ["16:9", "9:16"],
     "durations": ["4s", "6s", "8s"],
     "resolutions": ["720p", "1080p", "4k"],
     "def_aspect": "16:9", "def_duration": "8s",
     "def_resolution": "1080p",
     "note": "Veo 3.1 quality at draft speed and price."},
    {"id": "seedance1pro", "name": "Seedance 1.0 Pro (ByteDance)",
     "kind": "video",
     "endpoint": "fal-ai/bytedance/seedance/v1/pro/text-to-video",
     "price": "~$0.12/s",
     "aspects": ["16:9", "9:16", "1:1"],
     "durations": ["2", "3", "4", "5", "6", "7", "8", "9", "10",
                   "11", "12"],
     "resolutions": ["480p", "720p", "1080p"],
     "def_aspect": "16:9", "def_duration": "5",
     "def_resolution": "1080p",
     "note": "Flexible durations, strong motion."},
    {"id": "hailuo02pro", "name": "Hailuo-02 Pro (MiniMax)",
     "kind": "video",
     "endpoint": "fal-ai/minimax/hailuo-02/pro/text-to-video",
     "price": "~$0.28 per video",
     "aspects": None, "durations": None, "resolutions": None,
     "def_aspect": None, "def_duration": None, "def_resolution": None,
     "note": "Fixed format: 1080p, ~6s, physics-strong. Prompt only."},
    {"id": "fluxdev", "name": "FLUX.1 dev (image)", "kind": "image",
     "endpoint": "fal-ai/flux/dev",
     "price": "~$0.025 per megapixel",
     "aspects": ["16:9", "9:16", "1:1"],
     "durations": None, "resolutions": None,
     "def_aspect": "16:9", "def_duration": None, "def_resolution": None,
     "note": "High-quality stills - thumbnails, posters, keyframes."},
    {"id": "fluxschnell", "name": "FLUX.1 schnell (image)",
     "kind": "image",
     "endpoint": "fal-ai/flux/schnell",
     "price": "~$0.003 per megapixel",
     "aspects": ["16:9", "9:16", "1:1"],
     "durations": None, "resolutions": None,
     "def_aspect": "16:9", "def_duration": None, "def_resolution": None,
     "note": "Fast, cheap stills for drafts."},
]

_FAL_IMAGE_SIZES = {"16:9": "landscape_16_9", "9:16": "portrait_16_9",
                    "1:1": "square_hd"}


def _fal_payload(model, prompt, aspect_ratio="", duration="",
                 resolution=""):
    """Build a fal.ai request body from the model's DOCUMENTED schema
    only - a field is sent solely when the model's catalog entry lists
    it, with the requested value when valid, else the default."""
    payload = {"prompt": prompt}
    if model.get("durations"):
        payload["duration"] = (duration if duration
                               in model["durations"]
                               else model["def_duration"])
    if model.get("resolutions"):
        payload["resolution"] = (resolution if resolution
                                 in model["resolutions"]
                                 else model["def_resolution"])
    aspect = aspect_ratio or model.get("def_aspect")
    if model["kind"] == "image":
        payload["image_size"] = _FAL_IMAGE_SIZES.get(
            aspect, "landscape_16_9")
        payload["num_images"] = 1
    elif model.get("aspects"):
        payload["aspect_ratio"] = (aspect if aspect in model["aspects"]
                                   else model["def_aspect"])
    if model["id"] in ("veo31", "veo31fast"):
        payload["generate_audio"] = True
    return payload


def _fal_error_text(resp):
    """fal.ai error bodies carry a human-readable "detail" - surface
    it plainly (bad key, no balance, validation) instead of a code."""
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or f"HTTP {resp.status_code}")[:200]
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:240]
    if isinstance(detail, list) and detail:
        parts = []
        for d in detail:
            if isinstance(d, dict):
                parts.append(str(d.get("msg") or d)[:120])
            else:
                parts.append(str(d)[:120])
        if parts:
            return "; ".join(parts)[:240]
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:240]
    return f"HTTP {resp.status_code}"


def _gen_public(st):
    """A generator job as the UI may see it (never internal URLs with
    no meaning to the page, never the key)."""
    return {"id": st["id"], "status": st["status"],
            "stage": st.get("stage", ""), "kind": st["kind"],
            "model_name": st.get("model_name", ""),
            "url": st.get("url"), "remote_url": st.get("remote_url"),
            "error": st.get("error"),
            "imported_job_id": st.get("imported_job_id")}


def _admin_log_gen(st):
    """Log a generation's terminal transition in the Admin Panel's
    API log. gen_video_status returns early for done/error jobs, so
    each flip is reached exactly once. Latency is the total
    queue-to-finish time."""
    admin.log_api_call(
        "fal.ai",
        st.get("endpoint") or st.get("model_name") or "",
        "ok" if st.get("status") == "done" else "error",
        (time.time() - (st.get("created") or time.time())) * 1000.0,
        error=st.get("error"), kind="generation")


class Api:
    # -- options -------------------------------------------------------
    def get_options(self):
        """Static option lists for the UI (languages, genres, ratios)."""
        return {"languages": LANGUAGES,
                "genre_defaults": GENRES,
                "ratios": list(RATIOS.keys()),
                "categories": [{"key": k, "label": v["label"]}
                               for k, v in CATEGORIES.items()],
                "translate_langs": [{"key": k, "label": v}
                                    for k, v in
                                    TRANSLATE_LANGS.items()]}

    def caption_presets(self):
        """Caption Studio data: preset bundles + font/size lists."""
        return {"ok": True,
                "presets": _pipeline.CAPTION_PRESETS,
                "fonts": _pipeline.CAPTION_FONTS,
                "sizes": list(_pipeline.CAPTION_SIZES.keys()),
                "size_scales": _pipeline.CAPTION_SIZES}

    # -- settings ------------------------------------------------------
    def get_settings(self):
        s = load_settings()
        # The fal.ai key is a credential: the UI only ever learns
        # WHETHER one is saved, never the key itself.
        s["fal_key_set"] = bool((s.get("fal_key") or "").strip())
        s.pop("fal_key", None)
        # Publishing credentials get the same treatment: app secrets
        # and account tokens are replaced by *_set flags, and account
        # rows go out token-free with a has_token flag.
        for key in ("pub_fb_app_secret", "pub_yt_client_secret",
                    "pub_tt_client_secret", "pub_webhook_secret"):
            s[key + "_set"] = bool(str(s.get(key) or "").strip())
            s.pop(key, None)
        s["pub_accounts"] = [
            publishing.account_view(a, s)
            for a in (s.get("pub_accounts") or [])
            if isinstance(a, dict)]
        s.pop("pub_account_status", None)
        if _SETTINGS_LOAD_WARNING["active"]:
            s["load_warning"] = (
                "Your settings file couldn't be read (it may be "
                "damaged), so the app started with default settings. "
                "Saving settings will repair the file.")
        return s

    def save_settings(self, settings):
        # The load-modify-save cycle runs under a lock: several
        # settings savers (UI saves, detect prefs, data-dir flow)
        # run on different waitress threads, and unlocked cycles
        # silently dropped each other's changes.
        with _SETTINGS_LOCK:
            current = load_settings()
            settings = dict(settings or {})
            # data_dir has its own endpoint (set_data_dir below):
            # changing it can mean moving a 9 GB model, which a generic
            # settings save must never trigger - or silently bypass.
            settings.pop("data_dir", None)
            # Publishing secrets never arrive from the UI masked (they
            # are *_set flags there), and a wholesale save must not be
            # able to wipe them; the dedicated pub_* endpoints own
            # these keys. Account rows arriving token-free keep their
            # stored token (matched by id).
            for key in ("pub_fb_app_secret", "pub_yt_client_secret",
                        "pub_tt_client_secret", "pub_webhook_secret",
                        "pub_account_status"):
                settings.pop(key, None)
                settings.pop(key + "_set", None)
            if "pub_accounts" in settings:
                stored = {a.get("id"): a for a in
                          (current.get("pub_accounts") or [])
                          if isinstance(a, dict)}
                merged_accounts = []
                for a in settings.get("pub_accounts") or []:
                    if not isinstance(a, dict):
                        continue
                    a = dict(a)
                    a.pop("has_token", None)
                    a.pop("last_test", None)
                    a.pop("is_default", None)
                    if not str(a.get("token") or "").strip():
                        old = stored.get(a.get("id")) or {}
                        a["token"] = old.get("token") or ""
                    merged_accounts.append(a)
                settings["pub_accounts"] = merged_accounts
            current.update(settings)
            save_settings(current)
        return {"ok": True}

    # -- in-app updates (signed file-replacement packs) ---------------
    def update_info(self):
        return {"ok": True, "version": APP_VERSION}

    def apply_update(self, path):
        """Apply a signed update pack (.zip) the user picked:
        replaces the app's loose files in place, with backup +
        automatic rollback - see updater.py / tools/UPDATE_PACKS.md.
        No reinstall, no delete: the Setup stays the route for big
        (compiled-code) updates, and the updater says so when a
        pack tries to carry one."""
        gate = _license_gate()
        if gate:
            return gate
        p = (path or "").strip().strip('"')
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "Choose an update pack "
                                          "(.zip) file first."}
        if not p.lower().endswith(".zip"):
            return {"ok": False, "error": "Update packs are .zip "
                                          "files."}
        try:
            res = updater.apply_update_pack(p, BASE_DIR)
        except updater.UpdateError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            return {"ok": False, "error": "Update failed: "
                                         + str(e)[:200]}
        return {"ok": True, "version": res["version"],
                "files": res["files"],
                "message": "Update applied - restart the app to "
                           "use it."}

    # -- storage location (Settings -> Storage) ------------------------
    def storage_status(self):
        """Where the big files live right now: the configured
        data_dir, the EFFECTIVE model path (a model already sitting
        in the app folder is found and used even after a new
        location is chosen), the work root new projects will use,
        and any in-progress model move."""
        s = load_settings()
        dd = (s.get("data_dir") or "").strip()
        eff = local_ai.existing_model_path()
        loc = "none"
        if eff:
            loc = "default"
            if dd:
                want = os.path.join(os.path.abspath(dd), "models",
                                    local_ai.MODEL_FILE)
                if os.path.abspath(eff) == want:
                    loc = "custom"
        return {"ok": True,
                "data_dir": dd,
                "effective_model_path": local_ai.model_path(),
                "model_location": loc,
                "model_ready": local_ai.model_ready(),
                "work_root": _work_root(),
                "legacy_work_root": os.path.join(BASE_DIR, "work"),
                "move": local_ai.model_move_state()}

    def set_data_dir(self, path):
        """Save the storage location. When a downloaded model sits
        at the old location and the new one has none, the model is
        MOVED (copy -> verified -> original deleted) in the
        background and the setting is only saved once the move
        succeeds - if the move fails, the old location stays
        active and the error is reported, never a silent switch
        that orphans 9 GB."""
        gate = _license_gate()
        if gate:
            return gate
        p = (path or "").strip().strip('"')
        s = load_settings()
        if not p:
            local_ai.set_data_dir(None)
            s["data_dir"] = ""
            with _SETTINGS_LOCK:
                save_settings(s)
            return {"ok": True, "data_dir": "",
                    "message": "Storage location cleared - the app "
                               "folder is used again."}
        ap = os.path.abspath(p)
        if ap == os.path.abspath(BASE_DIR):
            return {"ok": False,
                    "error": "That is the app's own folder - pick a "
                             "different folder for the big files."}
        try:
            os.makedirs(os.path.join(ap, "models"), exist_ok=True)
            probe = os.path.join(ap, "models", ".write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            return {"ok": False,
                    "error": "That folder cannot be used: "
                             + str(e)[:160]}
        if local_ai.model_move_state().get("active"):
            return {"ok": False,
                    "error": "A model move is already running - "
                             "wait for it to finish."}
        eff = local_ai.existing_model_path()
        want = os.path.join(ap, "models", local_ai.MODEL_FILE)
        if eff and os.path.abspath(eff) != os.path.abspath(want):
            def _done(ok, err, _ap=ap):
                if ok:
                    with _SETTINGS_LOCK:
                        cur = load_settings()
                        cur["data_dir"] = _ap
                        save_settings(cur)
            if local_ai.start_model_move(ap, on_done=_done):
                return {"ok": True, "data_dir": ap, "moving": True,
                        "message": "Moving the AI model to the new "
                                   "location - keep the app open "
                                   "until it finishes."}
            return {"ok": False,
                    "error": "The model move could not be started."}
        local_ai.set_data_dir(ap)
        s["data_dir"] = ap
        with _SETTINGS_LOCK:
            save_settings(s)
        return {"ok": True, "data_dir": ap,
                "message": "Storage location saved."}

    # -- old-copy cleanup ----------------------------------------------
    def old_copies(self):
        """Scan for folders of OTHER copies of the app (Settings ->
        "Old version folders"). The running copy is never listed."""
        try:
            copies = _scan_old_copies()
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, "copies": copies,
                "running_dir": _running_app_dir()}

    def delete_old_copies(self, paths=None):
        """Delete old-copy folders. Every path is re-validated
        server-side (_validate_old_copy_target) - the client's list
        is never trusted - and results are reported per path, so a
        partial success says exactly which folder failed and why."""
        if isinstance(paths, dict):  # tolerate a {"paths": [...]} body
            paths = paths.get("paths")
        results = []
        for raw in (paths or []):
            target = _validate_old_copy_target(raw)
            if target is None:
                results.append({
                    "path": raw if isinstance(raw, str) else str(raw),
                    "ok": False,
                    "error": "refused: not a recognised old-copy "
                             "folder (kept for safety)"})
                continue
            ok, err = _delete_old_copy(target)
            item = {"path": target, "ok": ok}
            if err:
                item["error"] = err
            results.append(item)
        return {"ok": True, "results": results}

    def uninstall_self(self, remove_data=False):
        """Uninstall Itraji Cliper from this PC (Settings -> Danger
        zone). Writes the detached cleanup script, starts it, then
        exits so the script can remove the app (see _uninstall_plan).
        The response returns BEFORE the exit, and reports which mode
        will run. Refused from a source checkout."""
        if isinstance(remove_data, dict):  # tolerate {"remove_data": x}
            remove_data = remove_data.get("remove_data")
        plan, why = _uninstall_plan(bool(remove_data))
        if plan is None:
            return {"ok": False, "error": why}
        try:
            script = _write_uninstall_script(plan)
            launched = _launch_uninstall_script(script)
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            return {"ok": False, "error": str(e)[:200]}
        if not launched:
            return {"ok": False,
                    "error": "uninstall is only available on Windows"}
        _schedule_self_exit()
        return {"ok": True, "mode": plan["mode"]}

    # -- import + transcribe -------------------------------------------
    def _new_job(self):
        job_id = uuid.uuid4().hex[:8]
        JOBS[job_id] = {"stage": "Starting", "progress": 0,
                        "status": "working", "error": "",
                        "video_path": "", "title": "", "duration": 0,
                        "words": [], "moments": [], "renders": [],
                        "language": "auto", "source": "url",
                        "created": time.time(), "log": []}
        _prune_jobs()
        _job_log(job_id, "Job queued")
        return job_id

    def import_url(self, url, use_cookies=False, language="auto"):
        job_id = self._new_job()
        t = threading.Thread(target=self._run_import,
                             args=(job_id, url, bool(use_cookies),
                                   language or "auto"),
                             daemon=True)
        t.start()
        return {"ok": True, "job_id": job_id}

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
            _job_log(job_id, "ERROR: " + job["error"])
            return {"ok": True, "job_id": job_id}
        _job_log(job_id, "Local file: " + job["title"])
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
        # Damaged projects stay VISIBLE (marked corrupt) instead of
        # vanishing from the Library while their folders sit on disk.
        for cid in sorted(_CORRUPT_JOBS):
            if cid not in JOBS:
                projects.append({
                    "job_id": cid, "title": "(damaged project)",
                    "status": "corrupt",
                    "stage": "Saved data damaged - delete and "
                             "re-import",
                    "duration": 0, "created": 0, "source": "",
                    "clips": 0, "moments": 0, "corrupt": True})
        projects.sort(key=lambda p: p.get("created", 0), reverse=True)
        return {"projects": projects}

    def delete_job(self, job_id):
        """Delete a saved project and its files (Library action)."""
        # The id goes straight into a folder path that is then
        # rmtree'd: an unsanitised "../" id deleted arbitrary
        # folders the app process could reach. Validate the id and
        # keep the delete inside the work roots (the same defences
        # the /files route has always had).
        if not _valid_job_id(job_id):
            return {"ok": False, "error": "Project not found."}
        job = JOBS.get(job_id)
        if job and job.get("status") == "working":
            return {"ok": False,
                    "error": "That video is still busy - wait for it "
                             "to finish first."}
        folder = _job_folder(job_id)
        if job is None and not os.path.isdir(folder):
            return {"ok": False, "error": "Project not found."}
        if not _folder_inside_work_roots(folder):
            return {"ok": False, "error": "Project not found."}
        _drop_job(job_id)  # pops JOBS + removes the folder when loaded
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": True}

    def _run_import(self, job_id, url, use_cookies, language):
        job = JOBS[job_id]
        try:
            workdir = _job_folder(job_id)
            _job_log(job_id, "Download started")
            video_path, title, duration = download_video(
                url, workdir, _job_progress(job_id),
                use_cookies=use_cookies)
            job.update({"video_path": video_path, "title": title,
                        "duration": duration})
            try:
                _mb = os.path.getsize(video_path) / (1024 * 1024)
                _job_log(job_id, f"Download finished ({_mb:.1f} MB)")
            except OSError:
                _job_log(job_id, "Download finished")
            self._run_transcribe_only(job_id, language)
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            job["status"] = "error"
            job["error"] = self._friendly_error(e)
            job["stage"] = "Failed"
            _job_log(job_id, "ERROR: " + job["error"])

    def _run_transcribe_only(self, job_id, language):
        job = JOBS[job_id]
        try:
            job["language"] = language
            _job_log(job_id, "Transcription started")
            words = transcribe(job["video_path"], "base",
                               _job_progress(job_id), language=language)
            if not words:
                raise RuntimeError("no_speech")
            job["words"] = words
            job["status"] = "transcribed"
            job["stage"] = "Transcript ready"
            job["progress"] = 100
            _done = f"Transcription finished - {len(words)} words"
            if language and language != "auto":
                _done += f", language: {language}"
            _job_log(job_id, _done)
            _save_job(job_id)
        except Exception as e:  # noqa: BLE001 - surfaced to UI
            job["status"] = "error"
            job["error"] = self._friendly_error(e)
            job["stage"] = "Failed"
            _job_log(job_id, "ERROR: " + job["error"])

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
        _job_log(job_id, f"Transcript loaded from SRT - {len(words)} words")
        _save_job(job_id)
        return {"ok": True, "word_count": len(words)}

    def get_thumbnail(self, job_id):
        job = JOBS.get(job_id)
        if not job or not job.get("video_path"):
            return {"ok": False}
        thumb = os.path.join(_job_folder(job_id), "thumb.jpg")
        if not os.path.exists(thumb):
            import subprocess
            os.makedirs(os.path.dirname(thumb), exist_ok=True)
            t = min(5, max(1, (job.get("duration") or 10) / 2))
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                   "-i", job["video_path"], "-frames:v", "1",
                   "-q:v", "4", thumb]
            try:
                # No timeout before: a wedged ffmpeg on a corrupt
                # source held a WSGI thread forever (8 threads
                # serve the whole UI).
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            except subprocess.TimeoutExpired:
                return {"ok": False}
        if os.path.exists(thumb):
            return {"ok": True, "url": f"/files/{job_id}/thumb.jpg"}
        return {"ok": False}

    @staticmethod
    def _friendly_error(e):
        msg = str(e)
        if "No space left on device" in msg or "[Errno 28]" in msg:
            return ("Your disk is full - free up some space, then "
                    "try again. (Videos and the Built-in AI model "
                    "need several GB.)")
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
        """Map the UI's clip-length value to (min_s, max_s).

        Current UI sends a RANGE string "30-60" (seconds), or
        "custom:<range-or-single>". Legacy single lengths ("60s",
        "custom:45") still resolve - a single custom number is now an
        exact length (min == max)."""
        import re
        key = (clip_len or "30-60").strip()

        def _range(a, b):
            return (max(5, min(a, b)), max(5, max(a, b)))

        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", key)
        if m:  # explicit range from the range pills
            return _range(int(m.group(1)), int(m.group(2)))
        if key.startswith("custom:"):
            raw = key.split(":", 1)[1].strip()
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw)
            if m:
                return _range(int(m.group(1)), int(m.group(2)))
            try:
                p = max(5, int(raw))
                return (p, p)  # single number = exact length
            except ValueError:
                pass
        return CLIP_LEN_PRESETS.get(key, (30, 60))

    @staticmethod
    def _clip_count(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 8
        return max(1, min(40, n))  # hard cap 40

    # -- content category + audio profile (Phase 1) ------------------
    def _detect_category(self, job, category=None):
        """Resolve the content category for a detect run: explicit
        per-call value, else the project's stored one, else the
        Settings default. The winner is stored on the job, so a
        project remembers its category across re-detects."""
        settings = load_settings()
        key, _info = category_info(
            category or job.get("category")
            or settings.get("category", "general"))
        job["category"] = key
        return key

    def _audio_profile(self, job):
        """Cached audio energy profile of the job's video (feeds the
        subscores' voice-arousal term). Best-effort: None when the
        audio can't be profiled - subscores then drop the term."""
        vp = job.get("video_path") or ""
        try:
            sig = (vp, os.path.getmtime(vp)) if vp else None
        except OSError:
            sig = None
        if job.get("_audio_profile_sig") == sig \
                and "_audio_profile" in job:
            return job["_audio_profile"]
        prof = None
        if vp and os.path.isfile(vp):
            try:
                prof = _pipeline.audio_energy_profile(vp)
            except Exception:  # noqa: BLE001
                prof = None
        job["_audio_profile"] = prof
        job["_audio_profile_sig"] = sig
        return prof

    # -- offline moment detection (heuristic, no model) ---------------------------
    def detect_offline(self, job_id, max_clips=8, clip_len="60s",
                       category=None):
        gate = _license_gate()
        if gate:
            return gate
        job = JOBS.get(job_id)
        # "moments" is a valid re-detect state: after a successful scan
        # (or a keyword find) the job keeps its transcript, and asking
        # for a fresh scan - another length, the other mode - must not
        # be mistaken for "no video imported".
        if not job or job["status"] not in ("transcribed", "moments", "done"):
            return {"ok": False, "error": "Import a video first."}
        clip_min, clip_max = self._clip_len_range(clip_len)
        job["status"] = "working"
        job.pop("detect_error", None)  # don't ship a stale failure
        admin.bump_counter("detect_offline")
        t = threading.Thread(
            target=self._run_detect_offline,
            args=(job_id, clip_min, clip_max,
                  self._clip_count(max_clips),
                  self._detect_category(job, category),
                  self._audio_profile(job)),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_detect_offline(self, job_id, clip_min, clip_max, max_clips,
                            category="general", audio_profile=None):
        job = JOBS[job_id]
        try:
            _job_log(job_id, "Detection started (Offline)")
            job["stage"] = "Scanning transcript (offline)"
            job["progress"] = 40
            moments = detect_moments_offline(
                job["words"], clip_min, clip_max, max_clips=max_clips,
                category=category, audio_profile=audio_profile)
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
            _job_log(job_id, f"Detection finished - {len(moments)} "
                             "moments found (Offline)")
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
            _job_log(job_id, "ERROR: " + job.get("detect_error", ""))

    # -- built-in AI moment detection (local LLM) ------------
    def detect_local(self, job_id, max_clips=8, clip_len="60s",
                     category=None):
        gate = _license_gate()
        if gate:
            return gate
        job = JOBS.get(job_id)
        # Same re-detect rule as detect_offline: "moments" still has a
        # transcript and may be scanned again.
        if not job or job["status"] not in ("transcribed", "moments", "done"):
            return {"ok": False, "error": "Import a video first."}
        if not local_ai.model_ready():
            return {"ok": False, "error": "local_ai_missing",
                    "message": "The Built-in AI model isn't downloaded "
                               "yet. Click 'Download Built-in AI' below "
                               "(one-time, ~9 GB), then try again."}
        clip_min, clip_max = self._clip_len_range(clip_len)
        job["status"] = "working"
        job.pop("detect_error", None)  # don't ship a stale failure
        admin.bump_counter("detect_local")
        t = threading.Thread(
            target=self._run_detect_local,
            args=(job_id, clip_min, clip_max,
                  self._clip_count(max_clips),
                  self._detect_category(job, category),
                  self._audio_profile(job)),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_detect_local(self, job_id, clip_min, clip_max, max_clips,
                          category="general", audio_profile=None):
        job = JOBS[job_id]
        try:
            _job_log(job_id, "Detection started (Built-in AI)")
            moments = detect_moments_local(
                job["words"], clip_min, clip_max,
                max_clips=max_clips, progress_cb=_job_progress(job_id),
                category=category, audio_profile=audio_profile)
            if not moments:
                raise RuntimeError("no_moments")
            # keep keyword-search ("matched") moments a concurrent
            # find_moments call may have added; replace the rest
            kept = [m for m in job.get("moments", []) if m.get("matched")]
            job["moments"] = kept + moments
            job["status"] = "moments"
            job["stage"] = f"Found {len(moments)} moments (built-in AI)"
            job["progress"] = 100
            job.pop("detect_error", None)
            _job_log(job_id, f"Detection finished - {len(moments)} "
                             "moments found (Built-in AI)")
            _save_job(job_id)
        except Exception as e:  # noqa: BLE001
            # back to transcript state - unless keyword moments from
            # find_moments are still present, then stay on moments
            job["status"] = ("moments" if job.get("moments")
                             else "transcribed")
            msg = str(e)
            if "no_moments" in msg:
                job["detect_error"] = ("The Built-in AI found no strong "
                                       "moments in this transcript.")
            elif "not downloaded" in msg:
                job["detect_error"] = msg[:300]
            else:
                job["detect_error"] = ("Built-in AI detection failed: "
                                       + msg[:220])
            job["stage"] = "Transcript ready"
            job["progress"] = 100
            _job_log(job_id, "ERROR: " + job.get("detect_error", ""))

    # -- built-in AI download/status (Step 2 card) ------------------------
    def local_ai_status(self):
        # This endpoint is the ONE sanctioned auto-resume trigger:
        # it exists to report on the download, so it may also heal
        # it. Every other reader uses the pure download_state().
        st = local_ai.maybe_auto_resume()
        info = local_ai.model_info()
        return {"ok": True,
                "ready": local_ai.model_ready(),
                "downloading": bool(st.get("active")),
                "pct": st.get("pct", 0),
                "size_gb": info.get("size_gb", 0),
                "error": st.get("error"),
                "stage": st.get("stage", ""),
                "paused": bool(st.get("paused")),
                "downloaded": st.get("downloaded", 0),
                "total": st.get("total", 0),
                "rate_bps": st.get("rate_bps", 0),
                "eta_seconds": st.get("eta_seconds")}

    def local_ai_download(self):
        local_ai.start_download()
        return self.local_ai_status()

    # -- AI Installation Manager -------------------------------------
    # One status payload drives the whole Manager page; the actions
    # are thin wrappers over ai_components / local_ai and return
    # that same payload so the page re-renders from one source.
    def ai_manager_status(self):
        return ai_components.manager_state()

    def ai_install(self):
        started = ai_components.start_install()
        st = ai_components.manager_state()
        st["started"] = started
        return st

    def ai_pause(self):
        local_ai.pause_download()
        return ai_components.manager_state()

    def ai_resume(self):
        local_ai.start_download()
        return ai_components.manager_state()

    def ai_verify(self):
        started = ai_components.start_verify()
        st = ai_components.manager_state()
        st["started"] = started
        return st

    def ai_repair(self):
        started = ai_components.start_repair()
        st = ai_components.manager_state()
        st["started"] = started
        return st

    def ai_reinstall(self):
        started = ai_components.start_reinstall()
        st = ai_components.manager_state()
        st["started"] = started
        return st

    def ai_update_check(self):
        return ai_components.check_update()

    def ai_update_apply(self, component_id):
        started = ai_components.start_update(component_id)
        st = ai_components.manager_state()
        st["started"] = started
        return st

    def ai_diagnostic(self):
        started = ai_components.start_diagnostic()
        st = ai_components.manager_state()
        st["started"] = started
        return st

    # -- licensing -------------------------------------------------------
    def license_status(self):
        st = _ensure_license()
        payload = st.get("payload") or {}
        return {"ok": True,
                "locked": (not st.get("unlocked"))
                          or bool(st.get("tampered")),
                "tampered": bool(st.get("tampered")),
                "machine_code": licensing.machine_code(),
                "licensee": payload.get("name", ""),
                "exp": payload.get("exp", ""),
                "reason": st.get("reason", "")}

    def usage_status(self):
        """Clip allowance of the active key, for the sidebar
        "Clips left" chip: capped keys report their numbers;
        unlimited keys say capped=False and the chip stays
        hidden. Tampered records surface as their own state."""
        u = _usage_status()
        return {"ok": True, "capped": bool(u["capped"]),
                "max_clips": u["max_clips"], "used": u["used"],
                "remaining": u["remaining"], "state": u["state"]}

    def activate_license(self, key):
        st = licensing.try_unlock((key or "").strip(), BASE_DIR,
                                  _load_corepack)
        if st.get("unlocked"):
            with _SETTINGS_LOCK:
                current = load_settings()
                current["license_key"] = (key or "").strip()
                save_settings(current)
            return {"ok": True,
                    "licensee": (st.get("payload") or {}).get("name", "")}
        return {"ok": False, "error": st.get("reason")
                or "That key was not accepted."}

    # -- Caption Remover -------------------------------------------------
    def caption_remover_probe(self, source):
        """Dimensions of a LOCAL file, so the page can size its stage.
        Links are probed after download instead (returns probed=False)."""
        source = (source or "").strip()
        if not source or source.lower().startswith(("http://", "https://")):
            return {"ok": True, "probed": False}
        if not os.path.isfile(source):
            return {"ok": False, "error": "That file was not found on "
                                          "this PC."}
        try:
            w, h, _f = probe_video(source)
            res = {"ok": True, "probed": True, "width": w, "height": h,
                   "duration": probe_duration(source) or 0}
            try:
                res["detected"] = detect_caption_band(source, samples=16)
            except Exception:
                res["detected"] = {"found": False}
            return res
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def caption_remover_start(self, source, region=None, mode="auto"):
        gate = _license_gate()
        if gate:
            return gate
        source = (source or "").strip()
        if not source:
            return {"ok": False, "error": "Paste a video link or a "
                                          "local file path first."}
        if mode not in ("auto", "crop", "fill", "ai", "temporal"):
            return {"ok": False, "error": "Unknown removal method."}
        auto = bool((region or {}).get("auto"))
        top = bot = None
        if not auto:
            try:
                top = float((region or {}).get("top_pct", 82.0))
                bot = float((region or {}).get("bottom_pct", 100.0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "The caption region needs "
                                              "numeric top/bottom values."}
            if not (0.0 <= top <= 100.0 and 0.0 <= bot <= 100.0) \
                    or bot - top < 2.0:
                return {"ok": False, "error": "The caption region is too "
                                              "thin or outside the frame - "
                                              "widen the band."}
        is_url = source.lower().startswith(("http://", "https://"))
        if not is_url and not os.path.isfile(source):
            return {"ok": False, "error": "That file was not found on "
                                          "this PC."}
        rid = uuid.uuid4().hex[:12]
        _REMOVER_JOBS[rid] = {"id": rid, "status": "running", "pct": 0,
                              "stage": "Starting", "error": None,
                              "url": None, "mode": mode, "log": [],
                              "created": time.time(),
                              "source_name": (source if is_url else
                                              os.path.basename(source))}
        admin.bump_counter("remover")
        job_region = ({"auto": True} if auto
                      else {"top_pct": top, "bottom_pct": bot})
        t = threading.Thread(target=self._run_remover,
                             args=(rid, source, is_url, job_region, mode),
                             daemon=True)
        t.start()
        return {"ok": True, "id": rid}

    def _run_remover(self, rid, source, is_url, region, mode):
        st = _REMOVER_JOBS[rid]

        def cb(pct, text):
            st["pct"] = max(0, min(100, int(pct)))
            st["stage"] = text

        mode_names = {"crop": "Crop Band", "fill": "Smart Fill",
                      "ai": "AI Inpaint (frame by frame)",
                      "temporal": "AI Temporal (clean plate, tracked)"}
        if mode == "auto":
            mode = "temporal"
            _remover_log(rid, "Auto mode: the app picked the temporal "
                              "AI engine (one clean plate per shot, "
                              "tracked by motion - no flicker)")
        _remover_log(rid, "Caption removal started ("
                     + mode_names.get(mode, mode) + ")")
        try:
            settings = load_settings()
            work = _session_folder("remover-" + rid)
            os.makedirs(work, exist_ok=True)
            if is_url:
                _remover_log(rid, "Download started")
                src, _dl_title, _dl_dur = download_video(
                    source, work,
                    lambda p, t: cb(int(p * 0.25), t),
                    use_cookies=bool(settings.get("use_cookies")))
                try:
                    _mb = os.path.getsize(src) / (1024 * 1024)
                    _remover_log(rid, f"Download finished ({_mb:.1f} MB)")
                except OSError:
                    _remover_log(rid, "Download finished")
            else:
                src = source
            no_band = False
            if region and region.get("auto"):
                _remover_log(rid, "Auto-detect: scanning the video to "
                                  "find the captions")
                cb(26, "Auto-detecting the caption band")
                det = detect_caption_band(src)
                if det.get("found"):
                    region = {"top_pct": det["top_pct"],
                              "bottom_pct": det["bottom_pct"]}
                    _remover_log(
                        rid, "Auto-detected caption band: "
                        f"{det['top_pct']:.0f}% - {det['bottom_pct']:.0f}% "
                        "of the frame height")
                else:
                    # The probe says this video has no caption band
                    # we trust. The old behaviour guessed the bottom
                    # band (82%-100%) and inpainted it anyway, which
                    # measurably DAMAGES a caption-free video (sweep2
                    # T1: 26.6 dB PSNR vs the 44.5 dB of a plain
                    # re-encode). Never paint a guessed band over a
                    # clean video: return it unchanged and say so.
                    no_band = True
                    _remover_log(rid, "No caption band detected - "
                                      "the video looks caption-free, "
                                      "so it will be returned "
                                      "unchanged. If it does have "
                                      "captions, run again with a "
                                      "Custom region.")
            out = os.path.join(work, "output.mp4")
            tier = settings.get("quality_tier", "maximum")
            if no_band:
                cb(60, "No captions found - returning the video "
                       "unchanged")
                _copy_video_unchanged(src, out)
                st["status"] = "done"
                st["pct"] = 100
                st["stage"] = ("Done - no captions found, "
                               "video unchanged")
                st["url"] = "/remover-files/" + rid + "/output.mp4"
                _remover_log(rid, "Finished - output.mp4 is the "
                                  "original video, unchanged")
                return
            try:
                remove_captions(
                    src, region, mode, out,
                    progress_cb=lambda p, t: cb(25 + int(p * 0.75), t),
                    quality_tier=tier)
            except RuntimeError as e:
                if mode in ("temporal", "ai") \
                        and "model" in str(e).lower():
                    _remover_log(rid, "The AI engine could not start ("
                                      + str(e)[:140] + ") - finishing "
                                      "with Smart Fill instead, so you "
                                      "still get a clean video.")
                    remove_captions(
                        src, region, "fill", out,
                        progress_cb=lambda p, t: cb(25 + int(p * 0.75),
                                                    t),
                        quality_tier=tier)
                else:
                    raise
            st["status"] = "done"
            st["pct"] = 100
            st["stage"] = "Done"
            st["url"] = "/remover-files/" + rid + "/output.mp4"
            _remover_log(rid, "Caption removal finished - output.mp4")
        except Exception as e:
            st["status"] = "error"
            st["error"] = str(e)[:300]
            _remover_log(rid, "ERROR: " + st["error"])

    def caption_remover_status(self, rid):
        st = _REMOVER_JOBS.get(rid or "")
        if not st:
            return {"ok": False, "error": "That removal job is gone "
                                          "(the app was restarted)."}
        return {"ok": True, **st}

    def caption_remover_preview(self, source, region=None, mode="auto"):
        """Start a ~5s preview job: the worst section (a caption
        change on a busy background), so quality can be judged
        before committing to a full run."""
        gate = _license_gate()
        if gate:
            return gate
        source = (source or "").strip()
        if not source:
            return {"ok": False, "error": "Paste a video link or a "
                                          "local file path first."}
        if mode not in ("auto", "crop", "fill", "ai", "temporal"):
            return {"ok": False, "error": "Unknown removal method."}
        auto = bool((region or {}).get("auto"))
        top = bot = None
        if not auto:
            try:
                top = float((region or {}).get("top_pct", 82.0))
                bot = float((region or {}).get("bottom_pct", 100.0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "The caption region needs "
                                              "numeric top/bottom values."}
            if not (0.0 <= top <= 100.0 and 0.0 <= bot <= 100.0) \
                    or bot - top < 2.0:
                return {"ok": False, "error": "The caption region is too "
                                              "thin or outside the frame - "
                                              "widen the band."}
        is_url = source.lower().startswith(("http://", "https://"))
        if not is_url and not os.path.isfile(source):
            return {"ok": False, "error": "That file was not found on "
                                          "this PC."}
        rid = uuid.uuid4().hex[:12]
        _REMOVER_JOBS[rid] = {"id": rid, "status": "running", "pct": 0,
                              "stage": "Starting", "error": None,
                              "url": None, "mode": mode, "log": [],
                              "kind": "preview", "section": None}
        job_region = ({"auto": True} if auto
                      else {"top_pct": top, "bottom_pct": bot})
        t = threading.Thread(target=self._run_remover_preview,
                             args=(rid, source, is_url, job_region, mode),
                             daemon=True)
        t.start()
        return {"ok": True, "id": rid}

    def _run_remover_preview(self, rid, source, is_url, region, mode):
        st = _REMOVER_JOBS[rid]

        def cb(pct, text):
            st["pct"] = max(0, min(100, int(pct)))
            st["stage"] = text

        _remover_log(rid, "Preview started (worst 5 seconds: a caption "
                          "change on a busy background)")
        try:
            settings = load_settings()
            work = _session_folder("remover-" + rid)
            os.makedirs(work, exist_ok=True)
            if is_url:
                _remover_log(rid, "Download started")
                src, _dl_title, _dl_dur = download_video(
                    source, work,
                    lambda p, t: cb(int(p * 0.25), t),
                    use_cookies=bool(settings.get("use_cookies")))
                _remover_log(rid, "Download finished")
            else:
                src = source
            if region and region.get("auto"):
                cb(26, "Auto-detecting the caption band")
                det = detect_caption_band(src)
                if det.get("found"):
                    region = {"top_pct": det["top_pct"],
                              "bottom_pct": det["bottom_pct"]}
                    _remover_log(
                        rid, "Auto-detected caption band: "
                        f"{det['top_pct']:.0f}% - {det['bottom_pct']:.0f}% "
                        "of the frame height")
                else:
                    # Same rule as the full run: never paint a guessed
                    # band over a video the probe calls clean. A
                    # preview has nothing to show in that case, so
                    # stop honestly instead of previewing damage.
                    raise RuntimeError(
                        "No caption band detected - nothing to "
                        "preview. If the video does have captions, "
                        "choose a Custom region.")
            out = os.path.join(work, "output.mp4")
            tier = settings.get("quality_tier", "maximum")
            engine = "temporal" if mode == "auto" else mode
            try:
                _o, start, end = preview_caption_removal(
                    src, region, engine, out,
                    progress_cb=lambda p, t: cb(25 + int(p * 0.75), t),
                    quality_tier=tier)
            except RuntimeError as e:
                if engine in ("temporal", "ai") \
                        and "model" in str(e).lower():
                    _remover_log(rid, "The AI engine could not start - "
                                      "previewing with Smart Fill "
                                      "instead.")
                    _o, start, end = preview_caption_removal(
                        src, region, "fill", out,
                        progress_cb=lambda p, t: cb(25 + int(p * 0.75),
                                                    t),
                        quality_tier=tier)
                else:
                    raise
            st["section"] = f"{start:.0f}s - {end:.0f}s"
            st["status"] = "done"
            st["pct"] = 100
            st["stage"] = "Done"
            st["url"] = "/remover-files/" + rid + "/output.mp4"
            _remover_log(rid, "Preview finished - output.mp4 "
                              f"(section {st['section']})")
        except Exception as e:
            st["status"] = "error"
            st["error"] = str(e)[:300]
            _remover_log(rid, "ERROR: " + st["error"])

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
        gate = _license_gate()
        if gate:
            return gate
        """Per-clip posting package: 3 titles, FB description, hashtags.

        Generated once per moment and cached on it. Written by the
        Built-in AI when its model is downloaded ("ai" mode); otherwise
        an extractive on-PC fallback ("extractive" mode). The "mode"
        field always says which produced the copy."""
        job = JOBS.get(job_id)
        if not job or not (0 <= index < len(job.get("moments", []))):
            return {"ok": False, "error": "Moment not found."}
        m = job["moments"][index]

        def _with_broll_credits(pkg):
            # attribution for any render of this moment that used an
            # auto B-roll cutaway - appended at return time (never
            # baked into the cached package), so renders finished
            # after the package was generated still get credited
            lines = []
            for r in job.get("renders", []):
                b = r.get("broll")
                if r.get("moment_index") != index or not b:
                    continue
                line = (f"B-roll: \"{b.get('title', '')}\" by "
                        f"{b.get('artist', '')} "
                        f"({b.get('license', '')}), via Wikimedia "
                        f"Commons {b.get('page_url', '')}").strip()
                if line not in lines:
                    lines.append(line)
            if lines and "B-roll:" not in pkg.get("description", ""):
                pkg = dict(pkg)
                pkg["description"] = (
                    pkg.get("description", "")
                    + "\n\n" + "\n".join(lines)).strip()
            return pkg

        words = m.get("edited_words") or [
            w for w in job["words"]
            if w["end"] > m["start"] and w["start"] < m["end"]]
        if m.get("posting"):
            # A cached "ai" package written by a weaker model build can
            # be instruction echo, not copy - re-validate before
            # serving; an invalid cache falls through and regenerates.
            if posting_package_valid(m["posting"], words):
                return {"ok": True,
                        "package": _with_broll_credits(m["posting"]),
                        "cached": True}
            m.pop("posting", None)
        if not words:
            return {"ok": False, "error": "No captions in this moment."}
        settings = load_settings()
        pkg = generate_posting_package(
            words, m, genre=settings.get("genre", ""))
        m["posting"] = pkg
        _save_job(job_id)
        return {"ok": True, "package": _with_broll_credits(pkg),
                "cached": False}

    # -- Auto Publishing ------------------------------------------------
    def _pub_render(self, job_id, render_index):
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return None, None, "Project not found."
        renders = job.get("renders") or []
        try:
            idx = int(render_index)
        except (TypeError, ValueError):
            return None, None, "Bad clip index."
        if not (0 <= idx < len(renders)):
            return None, None, "Clip not found in this project."
        return job, renders[idx], None

    def _pub_package_for(self, job_id, render):
        """The clip's posting package via the existing generator
        (posting_package(): local-AI when ready, offline extractive
        otherwise). Falls back to a title-only package when the moment
        has no captions, so metadata generation never hard-fails."""
        title = render.get("title") or "New clip"
        mi = render.get("moment_index")
        if mi is not None:
            try:
                res = self.posting_package(job_id, mi)
            except Exception:  # noqa: BLE001
                res = None
            if res and res.get("ok") and res.get("package"):
                return res["package"]
        genre = load_settings().get("genre") or ""
        tags = []
        try:
            cp = _load_corepack() or {}
            tags = (cp.get("genre_hashtags") or {}).get(genre) or []
        except Exception:  # noqa: BLE001
            tags = []
        return {"titles": [title], "description": "",
                "hashtags": list(tags), "mode": "fallback"}

    def _pub_platform_tag(self, job, render):
        mi = render.get("moment_index")
        moments = (job or {}).get("moments") or []
        name = ""
        if mi is not None and 0 <= mi < len(moments):
            plat = moments[mi].get("platform") or {}
            name = plat.get("name") if isinstance(plat, dict) \
                else str(plat)
        name = str(name or "").lower()
        for key in publishing.PLATFORMS:
            if key in name:
                return key
        if "short" in name:
            return "youtube"
        if "reel" in name:
            return "facebook"
        return ""

    def _pub_build_item(self, job_id, render_index, platform,
                        account_id, meta_override, scheduled_at,
                        override, source, approval):
        job, render, err = self._pub_render(job_id, render_index)
        if err:
            return None, err
        path = render.get("path") or ""
        settings = load_settings()
        if not account_id:
            account_id = publishing.default_account_id(platform,
                                                       settings) or ""
        if meta_override:
            meta = {k: meta_override.get(k) for k in
                    ("title", "description", "caption", "hashtags",
                     "visibility") if k in meta_override}
            meta.setdefault("visibility",
                            settings.get("pub_default_visibility")
                            or "public")
        else:
            pkg = self._pub_package_for(job_id, render)
            vertical = True
            try:
                w, h, _f = probe_video(path)
                vertical = bool(h > w > 0)
            except Exception:  # noqa: BLE001
                pass
            meta = publishing.platform_metadata(
                pkg, platform,
                clip_title=render.get("title") or "",
                vertical=vertical,
                visibility=settings.get("pub_default_visibility")
                or "public")
        item = {"file_path": path,
                "title": render.get("title")
                or os.path.basename(path),
                "clip_job_id": job_id, "render_index": render_index,
                "thumb_url": self._enh_thumb(job_id, path),
                "platform": platform, "account_id": account_id,
                "metadata": meta, "scheduled_at": scheduled_at,
                "override": bool(override), "source": source,
                "approval": approval or ""}
        return item, None

    def _pub_auto_queue(self, job_id, indices):
        """Auto-publish on render completion: when the master switch
        is on and a default preset is chosen, queue each freshly
        rendered clip for the preset's platforms. Best-effort by
        design - a publishing hiccup must never fail a render batch.
        Publishing never consumes clip credits (renders were counted
        when they completed, above)."""
        try:
            _ensure_pub()
            settings = load_settings()
            if not settings.get("pub_enabled"):
                return
            preset = None
            for p in settings.get("pub_presets") or []:
                if p.get("id") == settings.get("pub_default_preset"):
                    preset = p
                    break
            if not preset or not preset.get("platforms"):
                return
            delay = int(preset.get("delay_minutes") or 0)
            scheduled = time.time() + delay * 60 if delay else None
            for idx in indices:
                for platform in preset["platforms"]:
                    item, err = self._pub_build_item(
                        job_id, idx, platform, "", None, scheduled,
                        False, "auto", preset.get("approval") or "")
                    if item:
                        publishing.create_job(item)
        except Exception:  # noqa: BLE001
            pass

    def pub_state(self):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        settings = load_settings()
        return {"ok": True,
                "platforms": [publishing.connection_state(p)
                              for p in publishing.PLATFORMS],
                "config": {
                    "enabled": bool(settings.get("pub_enabled")),
                    "approval_mode": settings.get("pub_approval_mode")
                    or "manual",
                    "max_batch": settings.get("pub_max_batch") or 10,
                    "daily_limit": settings.get("pub_daily_limit") or 0,
                    "delay_s": settings.get("pub_delay_s") or 0,
                    "default_preset": settings.get(
                        "pub_default_preset") or "",
                    "default_visibility": settings.get(
                        "pub_default_visibility") or "public",
                    "presets": settings.get("pub_presets") or [],
                    "webhook_url": settings.get("pub_webhook_url")
                    or "",
                    "webhook_secret_set": bool(str(
                        settings.get("pub_webhook_secret") or "")
                        .strip()),
                    "tt_audited": bool(settings.get("pub_tt_audited")),
                    "pause_all": publishing.pause_all()},
                "stats": publishing.admin_stats()}

    def pub_save_app_creds(self, platform, fields):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        if platform not in publishing.PLATFORMS:
            return {"ok": False, "error": "Unknown platform."}
        fields = fields or {}
        patch = {}
        for key, _label, secret in \
                publishing.APP_CRED_FIELDS[platform]:
            if key not in fields:
                continue
            val = str(fields.get(key) or "")
            if secret and not val.strip():
                continue  # blank secret field = keep the saved one
            patch[key] = val
        base_key = publishing.BASE_KEYS[platform]
        if base_key in fields:
            patch[base_key] = str(fields.get(base_key) or "").strip()
        if patch:
            _pub_save_settings_patch(patch)
        return self.pub_state()

    def pub_save_account(self, platform, account):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        if platform not in publishing.PLATFORMS:
            return {"ok": False, "error": "Unknown platform."}
        account = account or {}
        settings = load_settings()
        accounts = [dict(a) for a in
                    (settings.get("pub_accounts") or [])
                    if isinstance(a, dict)]
        aid = account.get("id") or ""
        target = None
        for a in accounts:
            if a.get("id") == aid and a.get("platform") == platform:
                target = a
                break
        if target is None:
            target = {"id": uuid.uuid4().hex[:12],
                      "platform": platform, "token": ""}
            accounts.append(target)
        target["label"] = str(account.get("label") or "")[:80]
        target["external_id"] = str(
            account.get("external_id") or "")[:120]
        token = str(account.get("token") or "")
        if token.strip():
            target["token"] = token.strip()
        _pub_save_settings_patch({"pub_accounts": accounts})
        if account.get("make_default"):
            self.pub_set_default_account(platform, target["id"])
        return self.pub_state()

    def pub_disconnect(self, platform, account_id):
        """Disconnect = wipe the stored token (the account row stays
        so it can be reconnected without re-typing the Page/ID)."""
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        settings = load_settings()
        accounts = [dict(a) for a in
                    (settings.get("pub_accounts") or [])
                    if isinstance(a, dict)]
        for a in accounts:
            if a.get("id") == account_id:
                a["token"] = ""
        status = dict(settings.get("pub_account_status") or {})
        status.pop(account_id, None)
        _pub_save_settings_patch({"pub_accounts": accounts,
                                  "pub_account_status": status})
        return self.pub_state()

    def pub_delete_account(self, platform, account_id):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        settings = load_settings()
        accounts = [a for a in (settings.get("pub_accounts") or [])
                    if isinstance(a, dict)
                    and a.get("id") != account_id]
        defaults = dict(settings.get("pub_default_accounts") or {})
        if defaults.get(platform) == account_id:
            defaults.pop(platform, None)
        status = dict(settings.get("pub_account_status") or {})
        status.pop(account_id, None)
        _pub_save_settings_patch(
            {"pub_accounts": accounts,
             "pub_default_accounts": defaults,
             "pub_account_status": status})
        return self.pub_state()

    def pub_set_default_account(self, platform, account_id):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        settings = load_settings()
        defaults = dict(settings.get("pub_default_accounts") or {})
        if account_id:
            defaults[platform] = account_id
        else:
            defaults.pop(platform, None)
        _pub_save_settings_patch({"pub_default_accounts": defaults})
        return self.pub_state()

    def pub_test_connection(self, platform, account_id=""):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        if platform not in publishing.PLATFORMS:
            return {"ok": False, "error": "Unknown platform."}
        if not account_id:
            account_id = publishing.default_account_id(platform) or ""
        result = publishing.test_connection(platform, account_id)
        result["ok"] = bool(result.get("ok"))
        return result

    def pub_assistant(self, job_id, render_index):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        job, render, err = self._pub_render(job_id, render_index)
        if err:
            return {"ok": False, "error": err}
        pkg = self._pub_package_for(job_id, render)
        settings = load_settings()
        vertical = True
        try:
            w, h, _f = probe_video(render.get("path") or "")
            vertical = bool(h > w > 0)
        except Exception:  # noqa: BLE001
            pass
        tag = self._pub_platform_tag(job, render)
        variants = {p: publishing.platform_metadata(
            pkg, p, clip_title=render.get("title") or "",
            vertical=vertical,
            visibility=settings.get("pub_default_visibility")
            or "public") for p in publishing.PLATFORMS}
        return {"ok": True, "variants": variants,
                "assistant": publishing.assistant_options(
                    pkg, platform_tag=tag, vertical=vertical),
                "package_mode": pkg.get("mode") or "",
                "per_platform_gates": {
                    p: publishing.quality_gate(
                        render.get("path") or "", p)[1]
                    for p in publishing.PLATFORMS}}

    def pub_create_jobs(self, items):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        items = items or []
        settings = load_settings()
        max_batch = int(settings.get("pub_max_batch") or 10)
        if len(items) > max_batch:
            return {"ok": False,
                    "error": "That is %d jobs — the max uploads per "
                             "batch is %d (Publishing settings)."
                             % (len(items), max_batch)}
        created, refused = [], []
        for it in items:
            item, err = self._pub_build_item(
                it.get("job_id") or "", it.get("render_index"),
                it.get("platform") or "", it.get("account_id") or "",
                it.get("metadata"), it.get("scheduled_at"),
                it.get("override"), it.get("source") or "manual",
                it.get("approval") or "")
            if err:
                refused.append({"platform": it.get("platform"),
                                "reason": err})
                continue
            job, reason = publishing.create_job(item)
            if job:
                created.append(job)
            else:
                refused.append({"platform": it.get("platform"),
                                "title": item.get("title"),
                                "reason": reason})
        return {"ok": True, "created": created, "refused": refused}

    def pub_projects(self):
        """Projects with renders, for the Publishing view's picker."""
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        _load_library()
        out = []
        for jid, j in JOBS.items():
            renders = j.get("renders") or []
            if not renders:
                continue
            rows = []
            for i, r in enumerate(renders):
                p = r.get("path") or ""
                if not p or not os.path.isfile(p):
                    continue
                rows.append({"idx": i,
                             "title": r.get("title")
                             or os.path.basename(p),
                             "thumb_url": self._enh_thumb(jid, p),
                             "platform": self._pub_platform_tag(j, r)})
            if rows:
                out.append({"job_id": jid,
                            "title": j.get("title") or jid,
                            "renders": rows})
        out.sort(key=lambda x: x["title"].lower())
        return {"ok": True, "projects": out}

    def pub_jobs(self, platform="", status=""):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        return {"ok": True,
                "jobs": publishing.list_jobs(platform or "",
                                             status or ""),
                "pause_all": publishing.pause_all()}

    def pub_set_pause_all(self, paused):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        publishing.set_pause_all(bool(paused))
        return {"ok": True, "pause_all": publishing.pause_all()}

    def pub_job_action(self, pub_job_id, action):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        out = publishing.job_action(pub_job_id, action)
        if isinstance(out, str):
            return {"ok": False, "error": out}
        return {"ok": True, "job": out}

    def pub_save_config(self, config):
        gate = _license_gate()
        if gate:
            return gate
        _ensure_pub()
        config = config or {}
        patch = {}
        if "enabled" in config:
            patch["pub_enabled"] = bool(config["enabled"])
        if config.get("approval_mode") in ("manual", "auto"):
            patch["pub_approval_mode"] = config["approval_mode"]
        if "max_batch" in config:
            patch["pub_max_batch"] = max(
                1, min(100, int(config["max_batch"] or 10)))
        if "daily_limit" in config:
            patch["pub_daily_limit"] = max(
                0, min(10000, int(config["daily_limit"] or 0)))
        if "delay_s" in config:
            patch["pub_delay_s"] = max(
                0, min(3600, int(config["delay_s"] or 0)))
        if "default_preset" in config:
            patch["pub_default_preset"] = str(
                config["default_preset"] or "")
        if config.get("default_visibility") in ("public", "unlisted",
                                                "private"):
            patch["pub_default_visibility"] = \
                config["default_visibility"]
        if "tt_audited" in config:
            patch["pub_tt_audited"] = bool(config["tt_audited"])
        if "webhook_url" in config:
            url = str(config["webhook_url"] or "").strip()
            patch["pub_webhook_url"] = url if url.startswith(
                ("http://", "https://")) else ""
        if "webhook_secret" in config:
            patch["pub_webhook_secret"] = str(
                config["webhook_secret"] or "")
        if "presets" in config:
            presets = []
            for p in config.get("presets") or []:
                if not isinstance(p, dict):
                    continue
                presets.append({
                    "id": str(p.get("id")
                              or uuid.uuid4().hex[:12]),
                    "name": str(p.get("name") or "Preset")[:60],
                    "platforms": [x for x in
                                  (p.get("platforms") or [])
                                  if x in publishing.PLATFORMS],
                    "approval": p.get("approval")
                    if p.get("approval") in ("manual", "auto")
                    else "manual",
                    "visibility": p.get("visibility")
                    if p.get("visibility") in
                    ("public", "unlisted", "private") else "public",
                    "delay_minutes": max(
                        0, min(10080,
                               int(p.get("delay_minutes") or 0)))})
            patch["pub_presets"] = presets
        if patch:
            _pub_save_settings_patch(patch)
        return self.pub_state()

    # -- Cover Maker ---------------------------------------------------
    def make_cover(self, job_id, render_index, text=""):
        gate = _license_gate()
        if gate:
            return gate
        """Auto cover for a render: strongest frame (face + sharpness
        + spoken beat) stamped with the title -> 1080x1920 JPG saved
        next to the render and recorded on it."""
        job = JOBS.get(job_id)
        renders = (job or {}).get("renders") or []
        if not job or not (0 <= render_index < len(renders)):
            return {"ok": False, "error": "Render not found."}
        r = renders[render_index]
        clip = r.get("path")
        if not clip or not os.path.isfile(clip):
            return {"ok": False, "error": "Render file is missing."}
        moments = job.get("moments") or []
        mi = r.get("moment_index", 0)
        m = moments[mi] if 0 <= mi < len(moments) else {}
        if m:
            words = m.get("edited_words") or [
                w for w in job.get("words", [])
                if w["end"] > m.get("start", 0)
                and w["start"] < m.get("end", 0)]
            base = float(m.get("start", 0))
        else:
            # Hook/teaser/merged renders carry moment_index -1 (no
            # source moment): the old code fell through with an
            # empty moment, so their covers were generated with NO
            # spoken-beat input at all. Their words live on the
            # render record, already on the clip's own timeline.
            words = r.get("words") or []
            base = 0.0
        cap = [dict(w, start=max(0.0, w["start"] - base),
                    end=w["end"] - base) for w in words]
        out = os.path.splitext(clip)[0] + "_cover.jpg"
        try:
            info = _pipeline.make_cover(
                clip, cap, out,
                (text or "").strip() or r.get("title")
                or m.get("title") or "")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:160]}
        r["cover"] = {"file": os.path.basename(out),
                      "url": self._download_url(job_id, out),
                      "t": info["t"], "face": info["face"]}
        _save_job(job_id)
        return {"ok": True, "cover": r["cover"]}

    # -- Studio: Title & Hook Generator ---------------------------------
    def ai_titles(self, topic="", job_id=""):
        """10 titles + 5 hooks with scores. Built-in AI when ready,
        honest extractive "basic" mode from a project transcript
        otherwise (see pipeline.generate_titles)."""
        words = None
        if job_id:
            _load_library()
            job = JOBS.get(job_id)
            if not job:
                return {"ok": False, "error": "Project not found."}
            words = job.get("words") or None
            if not words and not (topic or "").strip():
                return {"ok": False, "error": "That project has no "
                                              "transcript yet."}
        settings = load_settings()
        res = generate_titles(words=words, topic=topic or "",
                              genre=settings.get("genre", ""))
        return {"ok": True, **res}

    # -- Studio: AI Scriptwriter -----------------------------------------
    def ai_script(self, topic="", tone="Energetic", seconds=60):
        """Hook / Body / CTA script from the Built-in AI. No fake
        fallback: without the model this returns needs_model."""
        return generate_script(topic or "", tone or "Energetic",
                               seconds)

    # -- Studio: Hook Maker ----------------------------------------------
    # For videos with no hook of their own: analyze finds the most
    # gripping moment + hook lines from the transcript, then renders a
    # 3-8s hook clip (chosen hook text burned in as the opening
    # overlay), alone or stitched in front of one of the video's clips.

    def _hook_opts(self):
        """Render opts for hook clips: the standard Settings-based
        assembly, with two deliberate overrides - filler/silence
        cutting stays OFF (a hook is 3-8 raw seconds, and skipping
        the cuts keeps the caption timing an exact rebase of the
        source window) and the ratio is always 9:16 (a hook clip is
        a ready-to-post vertical, whatever ratio the wizard last
        used for other work)."""
        opts = self._render_opts(None)
        opts["remove_fillers"] = False
        opts["remove_silences"] = False
        opts["emphasis_zoom"] = False
        opts["broll"] = False
        opts["ratio"] = "9:16"
        return opts

    def _hook_window(self, job, start, end):
        """Validate a hook window against the video: returns
        (start, end) floats, or None when unusable."""
        try:
            s, e = float(start), float(end)
        except (TypeError, ValueError):
            return None
        dur = float(job.get("duration") or 0)
        if dur <= 0:
            return None
        s = max(0.0, min(s, dur))
        e = max(0.0, min(e, dur))
        if e - s < 1.0 or e - s > 15.0:
            return None
        return round(s, 2), round(e, 2)

    def hook_analyze(self, job_id):
        """Hook lines + the strongest hook moment for one project."""
        gate = _license_gate()
        if gate:
            return gate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        if job.get("status") == "working":
            return {"ok": False, "state": "working",
                    "error": "That video is still importing / "
                             "transcribing - wait for it to finish, "
                             "then analyze."}
        words = job.get("words") or []
        if not words:
            return {"ok": False, "state": "no_transcript",
                    "error": "That project has no transcript yet - a "
                             "hook is built from what is said in the "
                             "video."}
        # session cache: re-analyzing an unchanged project (notably
        # with the Built-in AI, whose chat call is slow) returns
        # instantly; a fresh detection run changes the moment count
        # and so invalidates it
        cache = job.get("_hook_cache") or {}
        if cache.get("n_words") == len(words) \
                and cache.get("n_moments") == len(job.get("moments")
                                                  or []) \
                and cache.get("result"):
            return dict(cache["result"])
        settings = load_settings()
        res = generate_hooks(words, genre=settings.get("genre", ""))
        moment = find_hook_moment(words, job.get("duration", 0),
                                  job.get("moments") or None)
        clips = [{"index": i, "start": m["start"], "end": m["end"],
                  "title": m.get("title", ""),
                  "virality": m.get("virality", 0)}
                 for i, m in enumerate(job.get("moments") or [])]
        if not clips and moment and moment["source"] == "detected":
            clips = [{"index": -1,
                      "start": moment["segment_start"],
                      "end": moment["segment_end"],
                      "title": "Strongest detected moment",
                      "virality": moment["score"]}]
        out = {"ok": True, "hooks": res["hooks"], "mode": res["mode"],
               "note": res["note"], "moment": moment, "clips": clips,
               "candidates": rank_hook_candidates(
                   words, job.get("moments") or None,
                   job.get("duration", 0)),
               "duration": job.get("duration", 0),
               "title": job.get("title", "")}
        job["_hook_cache"] = {"n_words": len(words),
                              "n_moments": len(job.get("moments")
                                               or []),
                              "result": out}
        return out

    def _hook_begin(self, job_id, stage):
        """Shared start for hook renders: render lock + status flip.
        Returns (job, lock, error_dict)."""
        job = JOBS.get(job_id)
        if not job:
            return None, None, {"ok": False,
                                "error": "Project not found."}
        lock = job.setdefault("_render_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return None, None, {
                "ok": False, "error": "render_in_progress",
                "message": "A render is already running for this "
                           "video - wait for it to finish."}
        job["_hook_prev_status"] = job.get("status", "transcribed")
        job["status"] = "working"
        job["stage"] = stage
        job["progress"] = 0
        job.pop("render_error", None)
        return job, lock, None

    def _hook_end(self, job, ok, stage):
        # Release before the status flips (same sweep2 T4 race as
        # _render_clips): an observer must never see a finished
        # status while the render lock is still held.
        lock = job.get("_render_lock")
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass  # already released
        if ok:
            job["status"] = "done"
            job["stage"] = stage
            job["progress"] = 100
        else:
            job["status"] = job.pop("_hook_prev_status", "transcribed")
            job["stage"] = stage
        job.pop("_hook_prev_status", None)

    def _hook_out_path(self, job, outdir, prefix):
        """Next free hook-render filename in the job's clips folder.
        The sequence counter is session-only, so names already
        referenced by saved renders (e.g. after an app restart) are
        skipped - a new render never overwrites a registered one."""
        used = {os.path.basename(r.get("path", ""))
                for r in job.get("renders", [])}
        seq = int(job.get("_hook_seq", 0))
        while True:
            seq += 1
            name = f"{prefix}_{seq:02d}.mp4"
            if name not in used:
                break
        job["_hook_seq"] = seq
        return os.path.join(outdir, name)

    def _render_hook_file(self, job_id, job, hook_text, start, end,
                          score):
        """Render the hook clip itself and register it in the job's
        renders (its words are stored rebased onto the clip timeline,
        so SRT/VTT exports and merges treat it like a merged entry).
        Returns the render entry's index in job["renders"]."""
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        out = self._hook_out_path(job, outdir, "hook")
        moment = {"start": start, "end": end, "title": "Hook",
                  "hook": hook_text, "virality": score}
        self._call_render_clip(job_id, job, moment, out,
                               self._hook_opts())
        rebased = [{"start": round(max(0.0, w["start"] - start), 3),
                    "end": round(max(0.0, w["end"] - start), 3),
                    "word": w["word"]}
                   for w in job["words"]
                   if w["end"] > start and w["start"] < end]
        job.setdefault("renders", []).append({
            "path": out, "title": "Hook clip",
            "virality": score, "moment_index": -1,
            "words": rebased, "merged_duration": round(end - start, 2),
            "hook_clip": True, "hook_text": hook_text,
            "url": self._download_url(job_id, out)})
        _job_log(job_id, "Render finished - " + os.path.basename(out))
        return len(job["renders"]) - 1

    def hook_render(self, job_id, hook_text, start, end):
        """Render just the hook clip (3-8s, chosen hook text as the
        opening overlay + standard captions)."""
        gate = _license_gate()
        if gate:
            return gate
        ugate = _usage_gate()
        if ugate:
            return ugate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        hook_text = (hook_text or "").strip()
        if not hook_text:
            return {"ok": False,
                    "error": "Pick a hook line first."}
        win = self._hook_window(job, start, end)
        if not win:
            return {"ok": False,
                    "error": "That hook window doesn't fit the video "
                             "(needs 1-15s inside it)."}
        job, lock, err = self._hook_begin(job_id, "Rendering hook clip")
        if err:
            return err
        t = threading.Thread(
            target=self._run_hook_render,
            args=(job_id, hook_text[:90], win[0], win[1]),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_hook_render(self, job_id, hook_text, start, end):
        with _capped_render_slot():
            self._run_hook_render_impl(job_id, hook_text, start, end)

    def _run_hook_render_impl(self, job_id, hook_text, start, end):
        job = JOBS[job_id]
        try:
            self._render_hook_file(job_id, job, hook_text, start, end,
                                   90)
            _usage_count(1)  # one completed hook clip
            self._hook_end(job, True, "Hook clip ready")
        except Exception as e:  # noqa: BLE001
            job["render_error"] = "Hook render failed: " + str(e)[:220]
            _job_log(job_id, "ERROR: " + job["render_error"])
            self._hook_end(job, False, "Hook render failed")
        _save_job(job_id)

    def hook_combo(self, job_id, hook_text, start, end,
                   clip_index=-1, clip_start=None, clip_end=None,
                   clip_title=""):
        """Render hook + one clip with the hook stitched in front.

        clip_index >= 0 picks one of the job's detected moments (an
        existing render of it is reused when present); otherwise
        clip_start/clip_end give the segment directly."""
        gate = _license_gate()
        if gate:
            return gate
        ugate = _usage_gate()
        if ugate:
            return ugate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        hook_text = (hook_text or "").strip()
        if not hook_text:
            return {"ok": False, "error": "Pick a hook line first."}
        win = self._hook_window(job, start, end)
        if not win:
            return {"ok": False,
                    "error": "That hook window doesn't fit the video "
                             "(needs 1-15s inside it)."}
        try:
            clip_index = int(clip_index)
        except (TypeError, ValueError):
            clip_index = -1
        moment = None
        if 0 <= clip_index < len(job.get("moments") or []):
            moment = job["moments"][clip_index]
        else:
            # a raw segment (e.g. the detected candidate when the job
            # has no moments of its own): validate against the plain
            # video bounds - a clip may be longer than a hook window
            if clip_start is not None:
                try:
                    cs, ce = float(clip_start), float(clip_end)
                except (TypeError, ValueError):
                    cs, ce = -1.0, -1.0
                dur = float(job.get("duration") or 0)
                if 0 <= cs < ce <= dur and ce - cs >= 1.0:
                    moment = {"start": round(cs, 2),
                              "end": round(ce, 2),
                              "title": clip_title or "Clip",
                              "hook": "", "virality": 0}
            if moment is None:
                return {"ok": False,
                        "error": "Pick a clip to put the hook in "
                                 "front of."}
        job, lock, err = self._hook_begin(
            job_id, "Rendering hook + clip")
        if err:
            return err
        t = threading.Thread(
            target=self._run_hook_combo,
            args=(job_id, hook_text[:90], win[0], win[1], moment,
                  clip_index),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_hook_combo(self, job_id, hook_text, start, end, moment,
                        clip_index):
        with _capped_render_slot():
            self._run_hook_combo_impl(job_id, hook_text, start, end,
                                      moment, clip_index)

    def _run_hook_combo_impl(self, job_id, hook_text, start, end,
                             moment, clip_index):
        job = JOBS[job_id]
        try:
            ih = self._render_hook_file(
                job_id, job, hook_text, start, end,
                int(moment.get("virality", 0) or 0) or 90)
            job["stage"] = "Rendering hook + clip"
            # the clip itself: reuse an existing straight render of
            # the moment when there is one, else render it now (with
            # no hook overlay of its own - the hook segment carries
            # the hook)
            ic = None
            if clip_index >= 0:
                for i, r in enumerate(job.get("renders", [])):
                    if r.get("moment_index") == clip_index \
                            and not r.get("hook_clip") \
                            and not r.get("merged") \
                            and os.path.isfile(r.get("path", "")):
                        ic = i
                        break
            if ic is None:
                outdir = os.path.join(_job_folder(job_id), "clips")
                os.makedirs(outdir, exist_ok=True)
                out = self._hook_out_path(job, outdir, "hookclip")
                cm = dict(moment)
                cm["hook"] = ""
                self._call_render_clip(job_id, job, cm, out,
                                       self._hook_opts())
                job["renders"].append({
                    "path": out, "title": moment.get("title", "Clip"),
                    "virality": int(moment.get("virality", 0) or 0),
                    "moment_index": clip_index,
                    "url": self._download_url(job_id, out)})
                _job_log(job_id, "Render finished - "
                         + os.path.basename(out))
                ic = len(job["renders"]) - 1
            merged = self._merge_selected_internal(job_id, [ih, ic],
                                         transition="cut")
            if not merged.get("ok"):
                raise RuntimeError(merged.get("error")
                                   or "merge failed")
            job["renders"][-1]["title"] = "Hook + clip"
            _usage_count(1)  # the combo counts once, as one clip
            self._hook_end(job, True, "Hook + clip ready")
        except Exception as e:  # noqa: BLE001
            job["render_error"] = ("Hook + clip failed: "
                                   + str(e)[:220])
            _job_log(job_id, "ERROR: " + job["render_error"])
            self._hook_end(job, False, "Hook + clip failed")
        _save_job(job_id)

    def _teaser_clip_render(self, job_id, job, moment, clip_index):
        """Render index of the teaser's main clip: an existing
        straight render of the moment when there is one, else a
        fresh render with no hook overlay of its own (the cold open
        carries the hook). Mirrors the hook+clip reuse rule."""
        if clip_index >= 0:
            for i, r in enumerate(job.get("renders", [])):
                if r.get("moment_index") == clip_index \
                        and not r.get("hook_clip") \
                        and not r.get("merged") \
                        and not r.get("teaser") \
                        and os.path.isfile(r.get("path", "")):
                    return i
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        out = self._hook_out_path(job, outdir, "teaserclip")
        cm = dict(moment)
        cm["hook"] = ""
        self._call_render_clip(job_id, job, cm, out, self._hook_opts())
        job["renders"].append({
            "path": out, "title": moment.get("title", "Clip"),
            "virality": int(moment.get("virality", 0) or 0),
            "moment_index": clip_index,
            "url": self._download_url(job_id, out)})
        _job_log(job_id, "Render finished - " + os.path.basename(out))
        return len(job["renders"]) - 1

    def hook_teaser(self, job_id, hook_text, hook_start, hook_end,
                    clip_index=-1, clip_start=None, clip_end=None,
                    clip_title=""):
        """Teaser render (Hook v2): the chosen hook moment plays
        FIRST as a cold open, then the picked clip plays in full in
        its original order (the proven hook+clip stitch machinery -
        caption offsets across the seam are the merge's own).

        Honest dedupe: when the hook window sits at the picked
        clip's own opening, the clip is rendered once as-is (with
        the hook text as its overlay) instead of duplicating its
        first seconds, and the render record says so."""
        gate = _license_gate()
        if gate:
            return gate
        ugate = _usage_gate()
        if ugate:
            return ugate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        hook_text = (hook_text or "").strip()
        if not hook_text:
            return {"ok": False,
                    "error": "Pick a hook moment first."}
        win = self._hook_window(job, hook_start, hook_end)
        if not win:
            return {"ok": False,
                    "error": "That hook window doesn't fit the video "
                             "(needs 1-15s inside it)."}
        try:
            clip_index = int(clip_index)
        except (TypeError, ValueError):
            clip_index = -1
        moment = None
        if 0 <= clip_index < len(job.get("moments") or []):
            moment = job["moments"][clip_index]
        elif clip_start is not None:
            try:
                cs, ce = float(clip_start), float(clip_end)
            except (TypeError, ValueError):
                cs, ce = -1.0, -1.0
            dur = float(job.get("duration") or 0)
            if 0 <= cs < ce <= dur and ce - cs >= 1.0:
                moment = {"start": round(cs, 2),
                          "end": round(ce, 2),
                          "title": clip_title or "Clip",
                          "hook": "", "virality": 0}
        if moment is None:
            return {"ok": False,
                    "error": "Pick the clip the teaser should open "
                             "into."}
        job, lock, err = self._hook_begin(job_id, "Rendering teaser")
        if err:
            return err
        t = threading.Thread(
            target=self._run_hook_teaser,
            args=(job_id, hook_text[:90], win, moment, clip_index),
            daemon=True)
        t.start()
        return {"ok": True}

    def _run_hook_teaser(self, job_id, hook_text, win, moment,
                         clip_index):
        with _capped_render_slot():
            self._run_hook_teaser_impl(job_id, hook_text, win,
                                       moment, clip_index)

    def _run_hook_teaser_impl(self, job_id, hook_text, win, moment,
                              clip_index):
        job = JOBS[job_id]
        hs, he = win
        ms, me = float(moment["start"]), float(moment["end"])
        hook_len = max(he - hs, 0.1)
        inter = max(0.0, min(he, me) - max(hs, ms))
        at_opening = (hs >= ms - 0.5
                      and hs <= ms + 0.25 * max(me - ms, 0.1))
        try:
            if at_opening and inter >= 0.5 * hook_len:
                # The hook IS this clip's opening: one clean render
                # of the clip with the hook text overlaid - no
                # duplicated seconds.
                outdir = os.path.join(_job_folder(job_id), "clips")
                os.makedirs(outdir, exist_ok=True)
                out = self._hook_out_path(job, outdir, "teaser")
                cm = dict(moment)
                cm["hook"] = hook_text
                self._call_render_clip(job_id, job, cm, out,
                                       self._hook_opts())
                rebased = [
                    {"start": round(max(0.0, w["start"] - ms), 3),
                     "end": round(max(0.0, w["end"] - ms), 3),
                     "word": w["word"]}
                    for w in job["words"]
                    if w["end"] > ms and w["start"] < me]
                job.setdefault("renders", []).append({
                    "path": out, "title": "Teaser",
                    "virality": int(moment.get("virality", 0) or 0),
                    "moment_index": clip_index,
                    "words": rebased,
                    "merged_duration": round(me - ms, 2),
                    "teaser": True,
                    "teaser_note": "The hook is this clip's own "
                                   "opening, so the clip was "
                                   "rendered as-is with the hook "
                                   "text overlaid.",
                    "url": self._download_url(job_id, out)})
                _job_log(job_id, "Render finished - "
                         + os.path.basename(out))
                _usage_count(1)  # the teaser counts once
                self._hook_end(job, True, "Teaser ready")
                _save_job(job_id)
                return
            ih = self._render_hook_file(
                job_id, job, hook_text, hs, he,
                int(moment.get("virality", 0) or 0) or 90)
            job["stage"] = "Rendering teaser"
            ic = self._teaser_clip_render(job_id, job, moment,
                                          clip_index)
            merged = self._merge_selected_internal(job_id, [ih, ic],
                                         transition="cut")
            if not merged.get("ok"):
                raise RuntimeError(merged.get("error")
                                   or "merge failed")
            job["renders"][-1]["title"] = "Teaser"
            _usage_count(1)  # the teaser counts once, as one clip
            job["renders"][-1]["teaser"] = True
            job["renders"][-1]["teaser_note"] = (
                f"Cold open {hs:.1f}-{he:.1f}s, then the full clip "
                "in its original order.")
            self._hook_end(job, True, "Teaser ready")
        except Exception as e:  # noqa: BLE001
            job["render_error"] = "Teaser failed: " + str(e)[:220]
            _job_log(job_id, "ERROR: " + job["render_error"])
            self._hook_end(job, False, "Teaser failed")
        _save_job(job_id)

    # -- Studio: Transcript tool ------------------------------------------
    def get_transcript(self, job_id):
        """A project's full transcript: plain text + timed segments."""
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        words = sorted(job.get("words") or [],
                       key=lambda w: w["start"])
        if not words:
            return {"ok": False, "error": "That project has no "
                                          "transcript yet."}
        segments = []
        cur, cur_start = [], None
        for w in words:
            if cur_start is None:
                cur_start = w["start"]
            cur.append(w["word"])
            if w["word"].rstrip().endswith((".", "!", "?")) \
                    or len(cur) >= 14:
                segments.append({"start": round(cur_start, 2),
                                 "text": " ".join(cur)})
                cur, cur_start = [], None
        if cur:
            segments.append({"start": round(cur_start, 2),
                             "text": " ".join(cur)})
        return {"ok": True, "title": job.get("title", ""),
                "text": " ".join(w["word"] for w in words),
                "segments": segments,
                "word_count": len(words),
                "duration": job.get("duration", 0)}

    def export_transcript(self, job_id, fmt="txt"):
        """Write the full transcript as .txt/.srt/.vtt next to the
        project and return its download URL."""
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        words = job.get("words") or []
        if not words:
            return {"ok": False, "error": "That project has no "
                                          "transcript yet."}
        fmt = (fmt or "txt").lower()
        if fmt not in ("txt", "srt", "vtt"):
            return {"ok": False, "error": "Unknown format."}
        folder = _job_folder(job_id)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "transcript." + fmt)
        try:
            if fmt == "srt":
                write_srt(words, 0, float(job.get("duration") or 0)
                          or words[-1]["end"], path)
            elif fmt == "vtt":
                write_vtt(words, 0, float(job.get("duration") or 0)
                          or words[-1]["end"], path)
            else:
                res = self.get_transcript(job_id)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(s["text"]
                                      for s in res["segments"]))
        except Exception as e:  # noqa: BLE001
            return {"ok": False,
                    "error": "Export failed: " + str(e)[:160]}
        return {"ok": True, "path": path,
                "url": self._download_url(job_id, path)}

    # -- Studio: AI Generator (fal.ai, user's own key) -------------------
    def gen_models(self):
        """The fal.ai model catalog + whether a key is saved. The key
        itself never leaves the server."""
        settings = load_settings()
        return {"ok": True,
                "has_key": bool((settings.get("fal_key") or "").strip()),
                "models": FAL_MODELS}

    def gen_video_start(self, model_id="", prompt="",
                        aspect_ratio="", duration="", resolution=""):
        """Submit a generation to fal.ai's queue (server-side, so the
        user's key is never exposed to the page)."""
        gate = _license_gate()
        if gate:
            return gate
        settings = load_settings()
        key = (settings.get("fal_key") or "").strip()
        if not key:
            return {"ok": False, "error": "no_key",
                    "message": "No fal.ai API key saved yet. Add yours "
                               "in Settings - one key unlocks every "
                               "model here, billed by fal.ai directly."}
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "Describe the video or "
                                          "image first."}
        model = next((m for m in FAL_MODELS if m["id"] == model_id),
                     None)
        if model is None:
            return {"ok": False, "error": "Unknown model."}
        payload = _fal_payload(model, prompt, aspect_ratio, duration,
                               resolution)
        t0 = time.time()
        try:
            r = requests.post(
                "https://queue.fal.run/" + model["endpoint"],
                headers={"Authorization": "Key " + key,
                         "Content-Type": "application/json"},
                json=payload, timeout=60)
        except requests.RequestException as e:
            err = "Couldn't reach fal.ai: " + str(e)[:160]
            admin.log_api_call("fal.ai", model["endpoint"], "error",
                               (time.time() - t0) * 1000.0,
                               error=err, kind="submit")
            return {"ok": False, "error": err}
        if r.status_code >= 400:
            err = "fal.ai said: " + _fal_error_text(r)
            admin.log_api_call("fal.ai", model["endpoint"], "error",
                               (time.time() - t0) * 1000.0,
                               error=err, kind="submit")
            return {"ok": False, "error": "fal_error",
                    "message": err}
        try:
            data = r.json()
            request_id = data["request_id"]
            status_url = data["status_url"]
            response_url = data["response_url"]
        except (ValueError, KeyError):
            err = "fal.ai returned an unexpected reply."
            admin.log_api_call("fal.ai", model["endpoint"], "error",
                               (time.time() - t0) * 1000.0,
                               error=err, kind="submit")
            return {"ok": False, "error": err}
        admin.log_api_call("fal.ai", model["endpoint"], "ok",
                           (time.time() - t0) * 1000.0, kind="submit")
        admin.bump_counter("generator")
        gid = uuid.uuid4().hex[:12]
        _GEN_JOBS[gid] = {
            "id": gid, "status": "queued", "stage": "In queue on fal.ai",
            "model_id": model["id"], "model_name": model["name"],
            "endpoint": model["endpoint"],
            "kind": model["kind"], "prompt": prompt[:200],
            "status_url": status_url, "response_url": response_url,
            "created": time.time(), "url": None, "remote_url": None,
            "error": None, "imported_job_id": None}
        return {"ok": True, "gen_id": gid, "status": "queued"}

    def gen_video_status(self, gen_id):
        """Poll fal.ai for one generation; on completion, download
        the result next to the app so it can be previewed and sent
        to the Library like any other video."""
        st = _GEN_JOBS.get(gen_id or "")
        if not st:
            return {"ok": False, "error": "That generation is gone "
                                          "(the app was restarted)."}
        if st["status"] in ("done", "error"):
            return {"ok": True, **_gen_public(st)}
        if st.get("_dl_claimed"):
            # The result download is already running on its worker
            # thread - don't fetch fal status again, and never
            # start a second download of the same file.
            return {"ok": True, **_gen_public(st)}
        if time.time() - st["created"] > 1800:
            st["status"] = "error"
            st["error"] = "Timed out waiting for fal.ai (30 min)."
            _admin_log_gen(st)
            return {"ok": True, **_gen_public(st)}
        settings = load_settings()
        key = (settings.get("fal_key") or "").strip()
        headers = {"Authorization": "Key " + key}
        try:
            r = requests.get(st["status_url"], headers=headers,
                             timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            st["stage"] = "Connection hiccup - still waiting…"
            return {"ok": True, **_gen_public(st)}
        status = (data.get("status") or "").upper()
        if status == "IN_QUEUE":
            st["status"] = "queued"
            pos = data.get("queue_position")
            st["stage"] = ("In queue on fal.ai"
                           + (f" (#{pos})" if pos else "") + "…")
            return {"ok": True, **_gen_public(st)}
        if status == "IN_PROGRESS":
            st["status"] = "running"
            st["stage"] = "Generating on fal.ai…"
            return {"ok": True, **_gen_public(st)}
        if status != "COMPLETED":
            st["status"] = "error"
            st["error"] = ("fal.ai reported status: "
                           + (status or "unknown"))
            _admin_log_gen(st)
            return {"ok": True, **_gen_public(st)}
        # Completed: fetch the result payload.
        try:
            r = requests.get(st["response_url"], headers=headers,
                             timeout=60)
            if r.status_code >= 400:
                st["status"] = "error"
                st["error"] = "fal.ai said: " + _fal_error_text(r)
                _admin_log_gen(st)
                return {"ok": True, **_gen_public(st)}
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            st["status"] = "error"
            st["error"] = "Couldn't fetch the result: " + str(e)[:160]
            _admin_log_gen(st)
            return {"ok": True, **_gen_public(st)}
        remote = None
        if isinstance(payload.get("video"), dict):
            remote = payload["video"].get("url")
        if not remote and payload.get("images"):
            first = payload["images"][0]
            if isinstance(first, dict):
                remote = first.get("url")
        if not remote:
            st["status"] = "error"
            st["error"] = "fal.ai finished but returned no file."
            _admin_log_gen(st)
            return {"ok": True, **_gen_public(st)}
        st["remote_url"] = remote
        # Claim the result download exactly once and run it on a
        # worker thread. This poll used to download the whole file
        # INLINE - up to 300 s blocking one of the 8 WSGI threads -
        # and two concurrent polls both downloaded to the same
        # path (interleaved writes) and both wrote the admin "ok"
        # log line, double-counting the generation.
        st["_dl_claimed"] = True
        st["status"] = "running"
        st["stage"] = "Downloading result…"
        threading.Thread(target=self._gen_download,
                         args=(st, remote), daemon=True).start()
        return {"ok": True, **_gen_public(st)}

    def _gen_download(self, st, remote):
        """Worker: fetch a finished generation's file next to the
        app so it survives + can enter the Library. Runs exactly
        once per generation (claimed by gen_video_status)."""
        folder_name = "gen-" + st["id"]
        folder = os.path.join(_work_root(), folder_name)
        os.makedirs(folder, exist_ok=True)
        ext = ".png" if st["kind"] == "image" else ".mp4"
        if ".jpg" in remote or ".jpeg" in remote:
            ext = ".jpg"
        fname = "generated" + ext
        fpath = os.path.join(folder, fname)
        try:
            with requests.get(remote, stream=True, timeout=300) as dr:
                dr.raise_for_status()
                with open(fpath, "wb") as f:
                    for chunk in dr.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
            st["local_path"] = fpath
            st["url"] = "/files/" + folder_name + "/" + fname
        except requests.RequestException:
            st["url"] = remote  # preview straight from fal.ai instead
        st["status"] = "done"
        st["stage"] = "Done"
        _admin_log_gen(st)

    def gen_video_import(self, gen_id):
        """Send a finished generated VIDEO into the Library through
        the normal import path, so it can be clipped like any video."""
        st = _GEN_JOBS.get(gen_id or "")
        if not st or st["status"] != "done":
            return {"ok": False, "error": "That generation isn't "
                                          "finished yet."}
        if st["kind"] != "video":
            return {"ok": False, "error": "Only videos can be sent "
                                          "to the Library."}
        if st.get("imported_job_id"):
            return {"ok": True, "job_id": st["imported_job_id"],
                    "cached": True}
        path = st.get("local_path")
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "The generated file isn't "
                                          "on this PC (download "
                                          "failed) - use the fal.ai "
                                          "link instead."}
        res = self.import_file(path)
        if res.get("ok"):
            st["imported_job_id"] = res["job_id"]
        return res

    # -- Studio: Video Enhancer -------------------------------------------
    def enhance_start(self, source="", job_id=""):
        """Queue an enhance run (upscale to 1080 + sharpen) for a
        local file path or a Library project."""
        gate = _license_gate()
        if gate:
            return gate
        path = (source or "").strip().strip('"').strip("'")
        if job_id:
            _load_library()
            job = JOBS.get(job_id)
            if not job:
                return {"ok": False, "error": "Project not found."}
            path = job.get("video_path") or ""
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "Pick a Library project or "
                                          "paste a local video file "
                                          "path first."}
        # Double-submit: a second enhance of the SAME input while
        # one is running is pure duplicate compute - return the
        # live one instead of queueing a twin.
        apath = os.path.abspath(path)
        for ex in list(_ENHANCE_JOBS.values()):
            if ex.get("status") == "running" \
                    and ex.get("source_path") == apath:
                return {"ok": True, "id": ex["id"],
                        "note": "already enhancing this file"}
        eid = uuid.uuid4().hex[:12]
        _ENHANCE_JOBS[eid] = {"id": eid, "status": "running", "pct": 0,
                              "stage": "Starting", "error": None,
                              "url": None, "created": time.time(),
                              "source_path": apath,
                              "source_name": os.path.basename(path)}
        admin.bump_counter("enhancer")
        t = threading.Thread(target=self._run_enhance,
                             args=(eid, path), daemon=True)
        t.start()
        return {"ok": True, "id": eid}

    def _run_enhance(self, eid, src):
        st = _ENHANCE_JOBS[eid]

        def cb(pct, text):
            st["pct"] = max(0, min(100, int(pct)))
            st["stage"] = text

        try:
            settings = load_settings()
            folder = _session_folder("enhance-" + eid)
            os.makedirs(folder, exist_ok=True)
            out = os.path.join(folder, "enhanced.mp4")
            enhance_video(src, out, progress_cb=cb,
                          quality_tier=settings.get(
                              "quality_tier", "maximum"))
            st["status"] = "done"
            st["pct"] = 100
            st["stage"] = "Done"
            st["url"] = "/files/enhance-" + eid + "/enhanced.mp4"
            st["folder"] = folder
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            st["status"] = "error"
            st["error"] = str(e)[:300]

    def enhance_status(self, eid):
        st = _ENHANCE_JOBS.get(eid or "")
        if not st:
            return {"ok": False, "error": "That enhance job is gone "
                                          "(the app was restarted)."}
        return {"ok": True, **st}

    def enhance_open_folder(self, eid):
        st = _ENHANCE_JOBS.get(eid or "")
        folder = (st or {}).get("folder") or _session_folder(
            "enhance-" + (eid or ""))
        os.makedirs(folder, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", folder])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    # -- Studio: post-clipping batch enhancement ------------------------
    # "Enhance All Clips": a queue over the renders of one project,
    # processed by the SAME enhancer toolbox as the Video Enhancer
    # page (pipeline.enhance_video's filters; plans come from
    # pipeline.smart_enhance_plan / manual_enhance_plan). Originals
    # are never touched - output lands beside them as
    # <name>_enhanced.mp4 and is recorded on the render record.

    def enhance_caps(self):
        """Optional-filter availability of the runtime ffmpeg."""
        return {"ok": True, **_pipeline.enhance_capabilities()}

    def _enh_thumb(self, job_id, src):
        """Cached per-clip thumbnail (frame at ~1s) for the panel."""
        thumb = os.path.splitext(src)[0] + ".thumb.jpg"
        try:
            if not os.path.isfile(thumb) \
                    or os.path.getmtime(thumb) < os.path.getmtime(src):
                dur = probe_duration(src) or 0
                t = 1.0 if dur > 2 else max(0.0, dur / 2.0)
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                     "-i", src, "-frames:v", "1",
                     "-vf", "scale=270:-2", thumb],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30)
            if os.path.isfile(thumb):
                return self._download_url(job_id, thumb)
        except Exception:  # noqa: BLE001 - thumbs are decorative
            pass
        return None

    def enhance_panel(self, job_id):
        """Per-render info for the 'Your Clips Are Ready!' panel."""
        gate = _license_gate()
        if gate:
            return gate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        out = []
        for i, r in enumerate(job.get("renders", [])):
            p = r.get("path") or ""
            if not p or not os.path.isfile(p):
                continue
            try:
                W, H, _f = probe_video(p)
                dur = probe_duration(p) or 0
                size = os.path.getsize(p)
            except Exception:  # noqa: BLE001
                continue
            enh = r.get("enhanced") or None
            if enh and not os.path.isfile(enh.get("path") or ""):
                enh = None
            out.append({
                "idx": i, "title": r.get("title") or
                os.path.basename(p), "name": os.path.basename(p),
                "w": W, "h": H, "duration": round(dur, 1),
                "size": size, "url": self._download_url(job_id, p),
                "thumb_url": self._enh_thumb(job_id, p),
                "enhanced": enh})
        return {"ok": True, "renders": out}

    def enhance_batch_start(self, job_id, indices=None, options=None):
        """Queue an Enhance-All/selected batch over a project's
        renders. options: {mode: smart|custom, target, sharpen,
        denoise, colour, stabilize} (custom fields ignored in smart
        mode, where each clip's plan comes from its own analysis)."""
        gate = _license_gate()
        if gate:
            return gate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        # One active batch per project: two batches over one job
        # share every item's .part and output paths (out paths are
        # derived from the render paths), so a double-submit made
        # two workers interleave encodes on the same files.
        for ex in list(_BENHANCE_BATCHES.values()):
            # "Active" means a live worker or items still queued -
            # not merely a stale "running" label a dead worker may
            # have left behind, which must not brick re-enhancing.
            active = ex.get("state") == "running" and (
                ex.get("worker_alive")
                or any(it.get("state") in ("queued", "running")
                       for it in ex.get("items", [])))
            if ex.get("job_id") == job_id and active:
                return {"ok": False,
                        "error": "enhance_in_progress",
                        "message": "An enhance batch is already "
                                   "running for this project - wait "
                                   "for it to finish."}
        options = options or {}
        mode = options.get("mode") or "smart"
        renders = job.get("renders", [])
        if indices is None:
            indices = list(range(len(renders)))
        items = []
        for i in indices:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(renders)):
                continue
            r = renders[i]
            src = r.get("path") or ""
            if not src or not os.path.isfile(src):
                continue
            base, _ext = os.path.splitext(src)
            items.append({
                "idx": i, "title": r.get("title") or
                os.path.basename(src), "src": src,
                "out": base + "_enhanced.mp4",
                "state": "queued", "pct": 0, "stage": "Queued",
                "error": None, "plan": None, "why": None,
                "seconds": None, "attempts": 0,
                "orig": None, "new": None, "url": None,
                "control": {"cancel": False, "pause": False}})
        if not items:
            return {"ok": False, "error": "No rendered clips on disk "
                                          "to enhance."}
        if mode != "smart":
            probe = _pipeline.analyze_clip(items[0]["src"])
            plan0 = _pipeline.manual_enhance_plan(options, probe)
            if _pipeline.plan_is_empty(plan0) \
                    and not plan0.get("target"):
                return {"ok": False, "error": "Pick at least one "
                        "enhancement option, or use Smart mode."}
        bid = uuid.uuid4().hex[:12]
        batch = {"id": bid, "job_id": job_id, "mode": mode,
                 "options": options, "items": items, "paused": False,
                 "state": "running", "worker_alive": False,
                 "created": time.time()}
        _BENHANCE_BATCHES[bid] = batch
        self._benhance_kick(batch)
        return {"ok": True, "batch_id": bid, "count": len(items)}

    def _benhance_kick(self, batch):
        if batch.get("worker_alive"):
            return
        batch["worker_alive"] = True
        t = threading.Thread(target=self._run_benhance,
                             args=(batch,), daemon=True)
        t.start()

    def _run_benhance(self, batch):
        try:
            while True:
                item = next((it for it in batch["items"]
                             if it["state"] == "queued"), None)
                if item is None:
                    return
                while batch["paused"]:
                    time.sleep(0.3)
                    if not any(it["state"] == "queued"
                               for it in batch["items"]):
                        return
                self._run_benhance_item(batch, item)
        finally:
            batch["worker_alive"] = False
            if not any(it["state"] in ("queued", "running")
                       for it in batch["items"]):
                batch["state"] = "done"

    def _run_benhance_item(self, batch, item):
        item["state"] = "running"
        item["pct"] = 0
        item["stage"] = "Analysing"
        item["error"] = None
        ctl = item["control"]
        ctl["cancel"] = False
        ctl["pause"] = bool(batch["paused"])
        src = item["src"]
        tmp = os.path.splitext(item["out"])[0] + ".part.mp4"
        trf = os.path.splitext(item["out"])[0] + ".trf"
        t0 = time.time()
        try:
            W, H, _f = probe_video(src)
            dur = probe_duration(src) or 0
            item["orig"] = {"w": W, "h": H,
                            "size": os.path.getsize(src),
                            "duration": round(dur, 1)}
            a = _pipeline.analyze_clip(src)
            if batch["mode"] == "smart":
                plan = _pipeline.smart_enhance_plan(a)
            else:
                plan = _pipeline.manual_enhance_plan(
                    batch["options"], a)
            item["plan"] = {k: plan.get(k) for k in
                            ("target", "sharpen", "denoise", "colour",
                             "stabilize")}
            item["why"] = plan.get("why")
            if plan.get("skip"):
                item["state"] = "skipped"
                item["pct"] = 100
                item["stage"] = "Skipped"
                item["seconds"] = round(time.time() - t0, 1)
                return
            settings = load_settings()
            passes = _pipeline.build_enhance_passes(
                src, tmp, plan, a,
                quality_tier=settings.get("quality_tier", "maximum"),
                transforms_path=trf)

            def cb(pct, text, _item=item):
                _item["pct"] = max(0, min(100, int(pct)))
                _item["stage"] = text

            for ps in passes:
                _pipeline.run_ffmpeg_controlled(
                    ps["cmd"], dur, cb, ps["lo"], ps["hi"],
                    ps["stage"], ctl)
            if not os.path.isfile(tmp) \
                    or os.path.getsize(tmp) < 1024:
                raise RuntimeError("Enhance produced no output file.")
            os.replace(tmp, item["out"])
            W2, H2, _f2 = probe_video(item["out"])
            item["new"] = {"w": W2, "h": H2,
                           "size": os.path.getsize(item["out"])}
            item["url"] = self._download_url(batch["job_id"],
                                             item["out"])
            item["seconds"] = round(time.time() - t0, 1)
            item["pct"] = 100
            item["stage"] = "Done"
            item["state"] = "done"
            # record on the render record + persist
            _load_library()
            job = JOBS.get(batch["job_id"])
            if job and 0 <= item["idx"] < len(job.get("renders", [])):
                job["renders"][item["idx"]]["enhanced"] = {
                    "path": item["out"], "url": item["url"],
                    "w": W2, "h": H2,
                    "size": item["new"]["size"],
                    "seconds": item["seconds"],
                    "why": item["why"]}
                _save_job(batch["job_id"])
            _job_log(batch["job_id"],
                     "Enhanced - " + os.path.basename(item["out"]))
        except _pipeline.FfmpegCancelled:
            item["state"] = "cancelled"
            item["stage"] = "Cancelled"
            for p in (tmp, trf):
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001 - surfaced per clip
            for p in (tmp, trf):
                try:
                    os.remove(p)
                except OSError:
                    pass
            item["attempts"] += 1
            if item["attempts"] < 2:
                item["state"] = "queued"
                item["stage"] = "Retrying"
                item["error"] = None
            else:
                item["state"] = "failed"
                item["stage"] = "Failed"
                item["error"] = str(e)[:300]
        finally:
            try:
                os.remove(trf)
            except OSError:
                pass

    def _benhance_public(self, batch):
        items = []
        for it in batch["items"]:
            items.append({k: it.get(k) for k in (
                "idx", "title", "state", "pct", "stage", "error",
                "plan", "why", "seconds", "attempts", "orig", "new",
                "url")})
        total = len(items) or 1
        eff = sum(100 if it["state"] in ("done", "skipped", "failed",
                                         "cancelled")
                  else (it["pct"] if it["state"] == "running" else 0)
                  for it in batch["items"])
        counts = {}
        for it in batch["items"]:
            counts[it["state"]] = counts.get(it["state"], 0) + 1
        return {"ok": True, "id": batch["id"], "job_id": batch["job_id"],
                "mode": batch["mode"], "paused": batch["paused"],
                "state": batch["state"],
                "overall": int(eff / total), "counts": counts,
                "items": items}

    def enhance_batch_status(self, batch_id):
        batch = _BENHANCE_BATCHES.get(batch_id or "")
        if not batch:
            return {"ok": False, "error": "That enhance queue is gone "
                                          "(the app was restarted). "
                                          "Finished clips stay on "
                                          "their render cards."}
        return self._benhance_public(batch)

    def enhance_batch_pause(self, batch_id):
        batch = _BENHANCE_BATCHES.get(batch_id or "")
        if not batch:
            return {"ok": False, "error": "Queue not found."}
        batch["paused"] = True
        for it in batch["items"]:
            if it["state"] == "running":
                it["control"]["pause"] = True
        return self._benhance_public(batch)

    def enhance_batch_resume(self, batch_id):
        batch = _BENHANCE_BATCHES.get(batch_id or "")
        if not batch:
            return {"ok": False, "error": "Queue not found."}
        batch["paused"] = False
        for it in batch["items"]:
            it["control"]["pause"] = False
        self._benhance_kick(batch)
        return self._benhance_public(batch)

    def enhance_batch_cancel(self, batch_id, idx):
        batch = _BENHANCE_BATCHES.get(batch_id or "")
        if not batch:
            return {"ok": False, "error": "Queue not found."}
        for it in batch["items"]:
            if it["idx"] == idx:
                if it["state"] == "running":
                    it["control"]["cancel"] = True
                elif it["state"] == "queued":
                    it["state"] = "cancelled"
                    it["stage"] = "Cancelled"
                break
        return self._benhance_public(batch)

    def enhance_batch_retry(self, batch_id, idx):
        batch = _BENHANCE_BATCHES.get(batch_id or "")
        if not batch:
            return {"ok": False, "error": "Queue not found."}
        for it in batch["items"]:
            if it["idx"] == idx and it["state"] in ("failed",
                                                    "cancelled"):
                it["state"] = "queued"
                it["stage"] = "Queued"
                it["error"] = None
                it["attempts"] = 0
                it["pct"] = 0
                batch["state"] = "running"
                self._benhance_kick(batch)
                break
        return self._benhance_public(batch)

    def enhance_zip(self, job_id):
        """Download-all-enhanced ZIP: every enhanced file recorded
        on the project's renders (plus any *_enhanced.mp4 beside
        them), built on demand like make_zip."""
        import zipfile
        gate = _license_gate()
        if gate:
            return gate
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Project not found."}
        paths = []
        seen = set()
        for r in job.get("renders", []):
            enh = r.get("enhanced") or {}
            p = enh.get("path") or ""
            if p and os.path.isfile(p) and p not in seen:
                seen.add(p)
                paths.append(p)
            src = r.get("path") or ""
            if src:
                cand = os.path.splitext(src)[0] + "_enhanced.mp4"
                if os.path.isfile(cand) and cand not in seen:
                    seen.add(cand)
                    paths.append(cand)
        if not paths:
            return {"ok": False, "error": "No enhanced clips yet - "
                                          "run Enhance All first."}
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        build = uuid.uuid4().hex[:8]
        zpath = os.path.join(outdir, f"enhanced-{build}.zip")
        tmp = zpath + ".part"
        try:
            with zipfile.ZipFile(tmp, "w",
                                 zipfile.ZIP_DEFLATED) as z:
                for p in paths:
                    z.write(p, arcname=os.path.basename(p))
            os.replace(tmp, zpath)
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "error": "ZIP failed: " + str(e)[:160]}
        for name in os.listdir(outdir):
            if name.startswith("enhanced-") and name.endswith(".zip") \
                    and os.path.join(outdir, name) != zpath:
                try:
                    os.remove(os.path.join(outdir, name))
                except OSError:
                    pass
        _job_log(job_id, f"Enhanced ZIP built - {len(paths)} clips")
        return {"ok": True, "path": zpath,
                "url": self._download_url(job_id, zpath),
                "filename": "enhanced.zip", "clips": len(paths)}

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
        gate = _license_gate()
        if gate:
            return gate
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
            job["words"], queries, clip_min, clip_max, max_clips=40,
            category=self._detect_category(job))
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
    @staticmethod
    def _reframe_note(ratio, src_w, src_h, face_track):
        """How a render's frame was produced from the source - the
        honest label stored on the render record."""
        if not src_w or not src_h:
            return ""
        targets = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0,
                   "4:5": 4 / 5}
        t = targets.get(ratio)
        if t and abs(src_w / src_h - t) < 0.03:
            return f"Full frame (source already {ratio})"
        if face_track:
            return "Face-tracked crop"
        if ratio in ("16:9", "1:1"):
            return "Smart crop (face-aware)"
        return "Centre crop"

    def _render_opts(self, options=None):
        """Assemble render options: per-call values layered over the
        Settings defaults (shared by Clip Maker and Hook Maker)."""
        settings = load_settings()
        opts = dict(options or {})
        # defaults from settings, overridable per render call
        for key in ("caption_style", "ratio", "quality", "face_track",
                    "remove_fillers", "remove_silences", "auto_censor",
                    "emojis", "highlight", "speaker_colors", "hook_overlay",
                    "cta_overlay", "cta_text", "safe_zone", "punch_zoom",
                    "emphasis_zoom", "emphasis_level", "broll",
                    "enhance_clarity", "logo_path", "quality_tier",
                    "audio_polish", "denoise_audio"):
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
        return opts

    def render(self, job_id, indices, options=None):
        gate = _license_gate()
        if gate:
            return gate
        ugate = _usage_gate()
        if ugate:
            return ugate
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
        opts = self._render_opts(options)
        job["last_render_opts"] = {k: opts.get(k) for k in
                                   ("caption_style", "ratio", "quality",
                                    "face_track", "remove_fillers",
                                    "remove_silences", "auto_censor",
                                    "emojis", "highlight", "speaker_colors",
                                    "hook_overlay", "cta_text", "safe_zone",
                                    "punch_zoom", "enhance_clarity",
                                    "emphasis_zoom", "emphasis_level",
                                    "broll", "audio_polish",
                                    "denoise_audio",
                                    "logo_path", "quality_tier")}
        job["status"] = "working"
        job.pop("render_error", None)  # don't ship a stale failure
        job.pop("limit_note", None)  # ...or a stale cap notice
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
            with _capped_render_slot():
                self._render_clips(job_id, indices, opts)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass  # already released

    def _call_render_clip(self, job_id, job, moment, out, opts):
        """One render_clip call with the standard opts mapping -
        shared by batch renders (below) and Hook Maker."""
        render_clip(job["video_path"], moment, job["words"],
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
                    edited_words=moment.get("edited_words"),
                    speaker_colors=opts.get("speaker_colors"),
                    hook_overlay=bool(opts.get("hook_overlay",
                                               True)),
                    cta_text=opts.get("cta_text"),
                    safe_zone=bool(opts.get("safe_zone")),
                    punch_zoom=bool(opts.get("punch_zoom")),
                    emphasis_zoom=bool(opts.get("emphasis_zoom")),
                    emphasis_level=opts.get("emphasis_level",
                                            "gentle"),
                    broll=bool(opts.get("broll")),
                    audio_polish=bool(opts.get("audio_polish")),
                    denoise_audio=bool(opts.get("denoise_audio")),
                    enhance_clarity=bool(
                        opts.get("enhance_clarity")),
                    logo_path=opts.get("logo_path"),
                    quality_tier=opts.get("quality_tier",
                                          "maximum"),
                    caption_params=opts.get("caption_params"))

    def _render_clips(self, job_id, indices, opts):
        job = JOBS[job_id]
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        # Keep clips from earlier render batches: a second batch must
        # extend the project, not replace it (Download-all relies on
        # the full list). Clips whose moment is being re-rendered now
        # are dropped here and re-added below with their fresh file.
        redo = set(indices)
        renders = [r for r in job.get("renders", [])
                   if r.get("moment_index") not in redo
                   and os.path.isfile(r.get("path", ""))]
        # Caption Studio: explicit per-render caption params
        # (normalized once here for storage; render_clip normalizes
        # again for the actual ASS build - cheap and consistent)
        eff_params = None
        if opts.get("caption_params"):
            eff_params = _pipeline.normalize_caption_params(
                opts["caption_params"],
                opts.get("caption_style", "bold-pop"))
        # Usage limit: a capped key renders only while allowance
        # remains. The remaining count is re-read before EVERY
        # clip (another job may be counting against the same
        # key), and each finished clip is counted the moment its
        # file exists. Hitting the cap mid-batch keeps the clips
        # already made and stops cleanly - see the tail below.
        made = 0
        limit_hit = False
        tamper_hit = False
        fresh_pub = []  # render indices this batch made (auto-publish)
        try:
            for n, i in enumerate(indices):
                u0 = _usage_status()
                if u0["state"] == "tampered":
                    tamper_hit = True
                    break
                if u0["capped"] and (u0["remaining"] or 0) <= 0:
                    limit_hit = True
                    break
                m = job["moments"][i]
                _job_log(job_id, f"Render started - clip {n + 1} of "
                                 f"{len(indices)}: {m['title']}")
                job["stage"] = f"Rendering clip {n + 1}/{len(indices)}"
                job["progress"] = int(n / max(len(indices), 1) * 100)
                safe = "".join(c if c.isalnum() or c in " -_" else ""
                               for c in m["title"])[:40].strip() or "clip"
                tag = "_reels-safe" if opts.get("safe_zone") else ""
                # named by MOMENT index (stable across batches), so
                # two batches can never overwrite each other's files
                out = os.path.join(outdir, f"{i + 1:02d}_{safe}{tag}.mp4")
                self._call_render_clip(job_id, job, m, out, opts)
                _rd = {"path": out, "title": m["title"],
                       "virality": m["virality"],
                       "moment_index": i,
                       "url": self._download_url(job_id, out)}
                # Phase 1: aspect + how the frame was reframed +
                # whether the audio was polished - labelled on the
                # record so the UI (and exports) can say it plainly.
                _ratio = opts.get("ratio", "9:16")
                _rd["aspect"] = _ratio
                _rd["audio_polish"] = bool(opts.get("audio_polish"))
                try:
                    _sw, _sh, _sf = probe_video(job["video_path"])
                except Exception:  # noqa: BLE001
                    _sw = _sh = 0
                _rd["reframe"] = self._reframe_note(
                    _ratio, _sw, _sh, bool(opts.get("face_track", True)))
                if m.get("platform"):
                    _rd["platform"] = m["platform"]["name"]
                if eff_params:
                    _rd["caption_params"] = eff_params
                    m["caption_params"] = eff_params
                # auto B-roll attribution sidecar (written by
                # render_clip when a cutaway was actually used)
                _bf = out + ".broll.json"
                if os.path.isfile(_bf):
                    try:
                        with open(_bf, encoding="utf-8") as fh:
                            _rd["broll"] = json.load(fh)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        os.remove(_bf)
                    except OSError:
                        pass
                renders.append(_rd)
                # publish each clip the moment it exists so the Live
                # Analysis rail fills one by one during polling
                job["renders"] = renders
                _job_log(job_id, "Render finished - "
                         + os.path.basename(out))
                # count the finished clip against the key's
                # allowance (no-op for unlimited keys)
                made += 1
                _usage_count(1)
            job["renders"] = renders
            # Release BEFORE the status flips to done: the caller's
            # finally used to be the only release, so a re-render
            # started in that gap saw status=done yet was refused
            # with render_in_progress (sweep2 T4).
            _release_render_lock(job)
            if tamper_hit:
                job["status"] = "moments"
                job["stage"] = "Usage data problem"
                job["limit_note"] = (
                    "Usage data problem — contact your provider. "
                    f"{made} of {len(indices)} clips were rendered "
                    "before the render stopped.")
                job["progress"] = 100
                job.pop("render_error", None)
                _job_log(job_id, "Usage record failed its integrity "
                                 "check - batch stopped")
            elif limit_hit:
                # Honest partial: the clips that exist are listed,
                # the stage + limit_note say exactly how many were
                # made vs stopped - never a plain "Done".
                u = _usage_status()
                job["status"] = "moments"
                job["stage"] = (
                    f"Clip limit reached - {made} of "
                    f"{len(indices)} clips rendered")
                job["limit_note"] = (
                    "Clip limit reached — this key includes "
                    f"{u['max_clips']} clips and all are used. "
                    f"{made} of {len(indices)} clips were rendered; "
                    "the rest were stopped. Please contact your "
                    "provider to top up.")
                job["progress"] = 100
                job.pop("render_error", None)
                _job_log(job_id, "Clip limit reached - batch "
                                 f"stopped after {made} clips")
            else:
                job["status"] = "done"
                job["stage"] = f"Done - {len(renders)} clips rendered"
                job["progress"] = 100
                job.pop("render_error", None)
                # Auto Publishing hook point: only a clean full-batch
                # completion queues the clips this batch just made
                # (the last `made` render records). Runs after
                # _save_job below; publishing never touches the usage
                # counter - these renders were already counted above.
                if made:
                    fresh_pub = list(range(len(renders) - made,
                                           len(renders)))
                # A batch that lands exactly on the cap is a full
                # success - but say the key is now used up, so the
                # next refusal is never a surprise.
                u = _usage_status()
                if u["capped"] and u["state"] == "limit":
                    job["limit_note"] = (
                        "Clip limit reached — this key includes "
                        f"{u['max_clips']} clips and all are now "
                        "used. Please contact your provider to "
                        "top up.")
        except Exception as e:  # noqa: BLE001
            _release_render_lock(job)
            job["status"] = "moments"
            job["render_error"] = "Render failed: " + str(e)[:220]
            job["stage"] = "Render failed"
            _job_log(job_id, "ERROR: " + job["render_error"])
            # keep any clips that finished before the failure
            job["renders"] = renders
        _save_job(job_id)
        if fresh_pub:
            self._pub_auto_queue(job_id, fresh_pub)

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
        from urllib.parse import quote
        rel = os.path.relpath(path, _job_folder(job_id))
        parts = [quote(p) for p in rel.replace(os.sep, "/").split("/")]
        return "/files/" + job_id + "/" + "/".join(parts)

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

    # -- caption translation (Phase 1) --------------------------------
    def translate_captions(self, job_id, render_idx, lang):
        """Translate one rendered clip's captions with the Built-in
        AI into <clip>_<lang>.srt + .vtt, cue count and timings
        preserved exactly. Honest needs-AI state when no model is
        downloaded - never a fabricated translation."""
        gate = _license_gate()
        if gate:
            return gate
        job = JOBS.get(job_id)
        try:
            render_idx = int(render_idx)
        except (TypeError, ValueError):
            render_idx = -1
        if not job or not (0 <= render_idx
                            < len(job.get("renders", []))):
            return {"ok": False, "error": "Clip not found."}
        lang = str(lang or "").strip().lower()
        if lang not in TRANSLATE_LANGS:
            return {"ok": False,
                    "error": "Pick a supported language."}
        if not local_ai.model_ready():
            return {"ok": False, "error": "needs_ai",
                    "message": _pipeline.TRANSLATE_NEEDS_AI}
        if (job.get("translate") or {}).get("busy"):
            return {"ok": False,
                    "error": "A translation is already running "
                             "for this project."}
        job["translate"] = {"busy": True, "lang": lang,
                            "render_idx": render_idx, "error": ""}
        t = threading.Thread(target=self._run_translate,
                             args=(job_id, render_idx, lang),
                             daemon=True)
        t.start()
        return {"ok": True}

    def _run_translate(self, job_id, render_idx, lang):
        job = JOBS[job_id]
        try:
            r, m, words = self._render_words(job, render_idx)

            def _cb(pct, stage):
                job["translate"] = {"busy": True, "lang": lang,
                                    "render_idx": render_idx,
                                    "error": "",
                                    "progress": pct,
                                    "stage": stage}

            cues = pipeline_translate_captions(
                words, m["start"], m["end"], lang, progress_cb=_cb)
            base = os.path.splitext(r["path"])[0] + "_" + lang
            srt_path = base + ".srt"
            vtt_path = base + ".vtt"
            _pipeline.write_srt_cues(cues, srt_path)
            _pipeline.write_vtt_cues(cues, vtt_path)
            r.setdefault("translations", {})[lang] = {
                "srt": self._download_url(job_id, srt_path),
                "vtt": self._download_url(job_id, vtt_path),
                "lines": len(cues)}
            job["translate"] = {"busy": False, "lang": lang,
                                "render_idx": render_idx,
                                "error": "", "done": True}
            _job_log(job_id, "Captions translated ("
                     + TRANSLATE_LANGS[lang] + ") - "
                     + os.path.basename(srt_path))
        except Exception as e:  # noqa: BLE001
            job["translate"] = {"busy": False, "lang": lang,
                                "render_idx": render_idx,
                                "error": str(e)[:220]}
            _job_log(job_id, "ERROR: caption translation failed: "
                     + str(e)[:200])
        _save_job(job_id)

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
        outdir = os.path.join(_job_folder(job_id), "clips")
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

    def _job_renders_on_disk(self, job_id):
        """The project's renders as they actually exist on disk: the
        in-memory list UNION the persisted job.json list, filtered to
        files that are really there. Returns (renders, missing_count).
        Downloads must work from the persisted folder alone - a job
        that only exists in memory is one restart away from 404s."""
        seen = {}
        missing = 0
        sources = []
        job = JOBS.get(job_id)
        if job:
            sources.append(job.get("renders", []))
        meta = os.path.join(_job_folder(job_id), "job.json")
        try:
            with open(meta, encoding="utf-8") as f:
                sources.append(json.load(f).get("renders", []))
        except (OSError, ValueError):
            pass
        for lst in sources:
            for r in lst or []:
                p = r.get("path", "")
                if not p or p in seen:
                    continue
                if os.path.isfile(p):
                    seen[p] = r
                else:
                    missing += 1
        return list(seen.values()), missing

    def make_zip(self, job_id, with_captions=True):
        """Download-all ZIP: every rendered MP4 (+ SRT/VTT when
        present), built ON DEMAND at click time from the persisted
        clip files - never a stale pre-built archive, and never a
        bare 404: if the clips are genuinely gone it says so here."""
        import zipfile
        renders, missing = self._job_renders_on_disk(job_id)
        if not renders:
            return {"ok": False,
                    "error": "No rendered clip files were found for "
                             "this project on this PC. Render the "
                             "clips again, then download."}
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        # unique name per build: a rebuild can never truncate the
        # file a download is streaming, and vice versa
        build = uuid.uuid4().hex[:8]
        zpath = os.path.join(outdir, f"clips-{build}.zip")
        tmp = zpath + ".part"
        try:
            with zipfile.ZipFile(tmp, "w",
                                 zipfile.ZIP_DEFLATED) as z:
                for r in renders:
                    z.write(r["path"],
                            arcname=os.path.basename(r["path"]))
                    if with_captions:
                        base = os.path.splitext(r["path"])[0]
                        for ext in (".srt", ".vtt"):
                            if os.path.exists(base + ext):
                                z.write(base + ext,
                                        arcname=os.path.basename(
                                            base + ext))
            os.replace(tmp, zpath)
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "error": "ZIP failed: " + str(e)[:160]}
        # prune earlier builds of this project's ZIP
        for name in os.listdir(outdir):
            if name.startswith("clips-") and name.endswith(".zip") \
                    and os.path.join(outdir, name) != zpath:
                try:
                    os.remove(os.path.join(outdir, name))
                except OSError:
                    pass
        _job_log(job_id, f"ZIP built - {len(renders)} clips "
                         "(clips.zip)")
        resp = {"ok": True, "path": zpath,
                "url": self._download_url(job_id, zpath),
                "filename": "clips.zip", "clips": len(renders)}
        if missing:
            resp["message"] = (f"{missing} clip(s) from this project "
                               "are missing on disk and were left out.")
        return resp

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
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        ass_fd, ass_path = __import__("tempfile").mkstemp(suffix=".ass",
                                                          prefix="prev_")
        os.close(ass_fd)
        out = os.path.join(outdir, f"captions_preview_{index}.mp4")
        try:
            from pipeline import (build_ass, _ffmpeg_ass_path,
                                  _fontsdir_opt)
            dur = max(1.0, m["end"] - m["start"])
            build_ass(words, m["start"], m["end"],
                      style,
                      m.get("hook", ""), ass_path, res=(540, 960),
                      emojis=bool(emojis),
                      highlight=bool(highlight))
            # fontsdir included so the preview uses the same bundled
            # caption fonts as a real render (it silently fell back to
            # system fonts before, so previews could lie about fonts).
            cmd = ["ffmpeg", "-y", "-v", "error",
                   "-f", "lavfi", "-i",
                   f"color=c=black:s=540x960:d={dur:.2f}:r=30",
                   "-vf", f"subtitles={_ffmpeg_ass_path(ass_path)}"
                          f"{_fontsdir_opt()}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
            try:
                # Bounded like get_thumbnail: previews can span the
                # 15-minute moment preset, and an unbounded run on a
                # WSGI thread starves the whole 8-thread server.
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True,
                                   timeout=180)
            except subprocess.TimeoutExpired:
                return {"ok": False,
                        "error": "Preview timed out - try a shorter "
                                 "moment."}
            if r.returncode != 0 or not os.path.exists(out):
                return {"ok": False,
                        "error": "Preview failed: " + r.stderr[-300:]}
        finally:
            try:
                os.remove(ass_path)
            except OSError:
                pass
        return {"ok": True, "url": self._download_url(job_id, out)}

    def _merge_prepare(self, job_id, render_indices, intro_path,
                       outro_path):
        """Shared validation for merges: returns an error dict, or
        (job, idxs, paths, intro, outro)."""
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
        return job, idxs, paths, intro, outro

    def merge_selected(self, job_id, render_indices, transition="cut",
                       intro_path="", outro_path=""):
        gate = _license_gate()
        if gate:
            return gate
        prep = self._merge_prepare(job_id, render_indices,
                                   intro_path, outro_path)
        if isinstance(prep, dict):
            return prep
        job, idxs, paths, intro, outro = prep
        # Take the job's render lock: merging ran with NO lock,
        # synchronously on a WSGI thread, and its output name was
        # derived from the renders count - a render finishing
        # mid-merge (or a double-submitted merge) could land two
        # writers on the same merged_NN.mp4. The lock refuses a
        # concurrent merge/render honestly; the name scan makes
        # collisions impossible even across restarts.
        lock = job.setdefault("_render_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "error": "render_in_progress",
                    "message": "A render is already running for this "
                               "video - wait for it to finish."}
        try:
            return self._merge_selected_locked(job_id, job, idxs,
                                               paths, transition,
                                               intro, outro)
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass  # already released

    def _merge_selected_internal(self, job_id, render_indices,
                                 transition="cut", intro_path="",
                                 outro_path=""):
        """Merge for callers that ALREADY hold the job's render
        lock (the Hook Maker workers, via _hook_begin): the public
        merge_selected's non-blocking acquire can never succeed
        there - it is the same lock - so combo/teaser merges went
        through this path from the start... except the sweep's
        first cut of the lock fix pointed them at the public
        method and they failed with render_in_progress (caught by
        the sweep's own regression battery). Same validation, same
        body - just no second acquire."""
        prep = self._merge_prepare(job_id, render_indices,
                                   intro_path, outro_path)
        if isinstance(prep, dict):
            return prep
        job, idxs, paths, intro, outro = prep
        return self._merge_selected_locked(job_id, job, idxs, paths,
                                           transition, intro, outro)

    def _merge_selected_locked(self, job_id, job, idxs, paths,
                               transition, intro, outro):
        outdir = os.path.join(_job_folder(job_id), "clips")
        os.makedirs(outdir, exist_ok=True)
        used = {os.path.basename(r.get("path", ""))
                for r in job.get("renders", [])}
        seq = len(job["renders"]) + 1
        while f"merged_{seq:02d}.mp4" in used:
            seq += 1
        out = os.path.join(outdir, f"merged_{seq:02d}.mp4")
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
            "url": self._download_url(job_id, out)})
        _job_log(job_id, "Render finished - " + os.path.basename(out))
        _save_job(job_id)
        return {"ok": True, "path": out,
                "url": self._download_url(job_id, out)}

    # -- status ----------------------------------------------------------
    def job_status(self, job_id):
        _load_library()
        job = JOBS.get(job_id)
        if not job:
            if job_id in _CORRUPT_JOBS:
                return {"status": "error",
                        "error": "This project's saved data is "
                                 "damaged (its job.json can't be "
                                 "read). The folder is still on disk; "
                                 "delete the project and import the "
                                 "video again."}
            return {"status": "missing"}
        # don't ship the full word list to the UI
        return {
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "error": job.get("error", ""),
            "detect_error": job.get("detect_error", ""),
            "render_error": job.get("render_error", ""),
            "limit_note": job.get("limit_note", ""),
            "title": job["title"],
            "duration": job["duration"],
            "long_video": bool(job["duration"]
                               and job["duration"] >= LONG_VIDEO_WARN_S),
            "word_count": len(job["words"]),
            "moments": job["moments"],
            "renders": job["renders"],
            "category": job.get("category", ""),
            "translate": job.get("translate") or {},
            "log": job.get("log", []),
        }

    def open_folder(self, job_id):
        job = JOBS.get(job_id)
        folder = os.path.join(_job_folder(job_id), "clips") if job \
            else _work_root()
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
# Localhost server + the app's own window (pywebview / WebView2),
# with a Chrome/Edge --app window as fallback
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


def _current_license_key():
    env_key = os.environ.get("ITRAJI_LICENSE_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return (load_settings().get("license_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _ensure_license():
    """Verify the stored/env license and load the core pack (the
    verification is memoized per key inside licensing)."""
    return licensing.try_unlock(_current_license_key(), BASE_DIR,
                                _load_corepack)


def _license_locked():
    st = _ensure_license()
    return (not st.get("unlocked")) or bool(st.get("tampered"))


def _license_gate():
    """Per-operation re-check for the major features. Returns None
    when unlocked, else an error dict for the API caller."""
    _ensure_license()
    reason = licensing.require_unlocked()
    if reason is None and not _COREPACK_READY():
        reason = ("This copy is locked - activate it with your "
                  "license key first.")
    if reason is None:
        return None
    return {"ok": False, "error": "locked", "message": reason}


# -- Usage limits (signed "max_clips" allowance per key) -------------
# Only NEW clip renders are gated/counted (see usage.py for the
# record + HMAC design). Detection, transcription, the Library and
# enhancements never consult this: at the cap the app stays fully
# usable, only rendering pauses.

def _usage_status():
    """Usage state of the ACTIVE license key (see usage.status)."""
    st = _ensure_license()
    return usage.status(_current_license_key(),
                        st.get("payload") or {})


def _usage_gate():
    """None when a new clip render may start, else an error dict.
    Two distinct refusals: the allowance is used up (clip_limit),
    or the local usage record failed its integrity check
    (usage_data_problem) - never conflated."""
    u = _usage_status()
    if u["state"] == "tampered":
        return {"ok": False, "error": "usage_data_problem",
                "message": "Usage data problem — contact your "
                           "provider."}
    if u["state"] == "limit":
        return {"ok": False, "error": "clip_limit",
                "message": "Clip limit reached — this key includes "
                           f"{u['max_clips']} clips. Please contact "
                           "your provider to top up."}
    return None


def _usage_count(n=1):
    """Count n completed clip renders against the active key.
    No-op for unlimited keys (no record is ever written for
    them). Returns the fresh usage status, or None on trouble -
    counting must never break a finished render."""
    try:
        st = _ensure_license()
        return usage.count_clip(_current_license_key(),
                                st.get("payload") or {}, n)
    except Exception:  # noqa: BLE001
        return None


_CAPPED_RENDER_LOCK = threading.Lock()


class _capped_render_slot:
    """Serialize renders APP-WIDE while the active key is capped.

    The cap check and the count increment bracket a whole render,
    so two jobs rendering simultaneously each saw the same
    remaining allowance and both spent it: measured live, a cap
    of 3 with two concurrent 4-clip batches completed 4 clips
    total (overshoot exactly +1 per extra simultaneous job; the
    counter itself was exact). Holding this lock for the whole
    render - only for capped keys; unlimited keys never wait -
    makes check-then-count atomic per render. Per-job locks still
    apply on top, exactly as before."""

    def __enter__(self):
        self.held = False
        try:
            if _usage_status().get("state") != "unlimited":
                _CAPPED_RENDER_LOCK.acquire()
                self.held = True
        except Exception:  # noqa: BLE001 - never break a render
            self.held = False
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                _CAPPED_RENDER_LOCK.release()
            except RuntimeError:
                pass
        return False


def _COREPACK_READY():
    import pipeline as _p
    return _p.corepack_loaded()


def _work_in_progress():
    """Short labels for work that closing the app would cut off: the
    Built-in AI download, and any job in its "working" state (import,
    transcription, detection, render, caption removal, enhance...).
    Drives the confirm-on-close guard in both window modes; the
    download check self-heals first (see local_ai), so a wedged
    download whose worker is long gone does not claim to be work."""
    what = []
    try:
        if local_ai.download_state().get("active"):
            what.append("the Built-in AI download")
    except Exception:  # noqa: BLE001 - the guard must never break
        pass
    try:
        if any(j.get("status") == "working" for j in list(JOBS.values())):
            what.append("video processing")
    except Exception:  # noqa: BLE001
        pass
    # The session tools were invisible to the guard: closing the
    # app mid Caption-Remover / Enhancer / Generator / Enhance-All
    # run cut the work off with no warning at all, despite this
    # function's contract ("work that closing the app would cut").
    for store, label in ((_REMOVER_JOBS, "caption removal"),
                         (_ENHANCE_JOBS, "video enhancement"),
                         (_GEN_JOBS, "AI video generation")):
        try:
            if any(s.get("status") in ("running", "queued")
                   for s in list(store.values())):
                what.append(label)
        except Exception:  # noqa: BLE001
            pass
    try:
        if any(b.get("state") == "running"
               for b in list(_BENHANCE_BATCHES.values())):
            what.append("clip enhancement")
    except Exception:  # noqa: BLE001
        pass
    try:
        if any(b.get("status") == "working"
               for b in list(BATCHES.values())):
            what.append("batch import")
    except Exception:  # noqa: BLE001
        pass
    return what


_CLOSE_GUARD_SNIPPET = b"""
<script>
/* Close guard (injected by app.py, not part of ui/index.html): while
   work is running - the Built-in AI download or any processing job -
   arm beforeunload so closing the window/tab asks first, instead of
   silently cutting a 9 GB download or a render. In the app's own
   (WebView2) window the native closing handler asks instead; this
   guard is what covers the Chrome/Edge --app window and plain tabs. */
(function () {
  var active = false;
  function check() {
    fetch("/api/work-status").then(function (r) { return r.json(); })
      .then(function (d) { active = !!(d && d.active); })
      .catch(function () { });
  }
  check();
  setInterval(check, 3000);
  window.addEventListener("beforeunload", function (e) {
    if (active) { e.preventDefault(); e.returnValue = ""; }
  });
})();
</script>
"""


# ---------------------------------------------------------------------------
# Owner Admin Panel: session extraction + dashboard payload
# ---------------------------------------------------------------------------

_ADMIN_COOKIE = "itraji_admin"


def _admin_token_from(environ):
    """The admin session token from the HttpOnly cookie, or the
    X-Admin-Token header (non-browser clients)."""
    hdr = environ.get("HTTP_X_ADMIN_TOKEN")
    if hdr:
        return hdr.strip()
    cookie = environ.get("HTTP_COOKIE") or ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(_ADMIN_COOKIE + "="):
            return part[len(_ADMIN_COOKIE) + 1:].strip()
    return ""


def _admin_stats_payload():
    """Assemble the dashboard payload from this installation's own
    records only (see admin.py's docstring for the honesty rules).

    Source of every number:
    - videos/clips/series/activity for Clip Maker: job.json files
      across both work roots (incl. a custom data_dir root).
    - generator/enhancer/remover activity + failures, and clip-job
      failures: the in-memory session job dicts (errors are
      session-only by design and are labeled as such in the UI).
    - downloads + per-tool run counts: admin counters, bumped at
      the real call sites, existing only since this build.
    - storage: capped byte walks of the work roots, the models
      data area and the app folder.
    - license/device: the live license state + this PC's machine
      code; server/uptime: process start time."""
    _load_library()
    disk_jobs = admin.collect_disk_jobs(_work_roots())
    disk_ids = {j.get("_job_id") for j in disk_jobs}
    session_rows, failed = [], []

    def fail_row(ts, tool, title, error):
        failed.append({"ts": ts or 0, "tool": tool,
                       "title": title or tool,
                       "error": str(error or "")[:300]})

    for jid, j in list(JOBS.items()):
        err = j.get("error") or j.get("detect_error") or ""
        if jid not in disk_ids:
            session_rows.append({
                "ts": j.get("created") or 0, "tool": "Clip Maker",
                "title": j.get("title") or jid,
                "status": j.get("status") or "", "error": err})
        if err or j.get("status") == "error":
            fail_row(j.get("created"), "Clip Maker",
                     j.get("title") or jid, err or "Job failed")
    for st in list(_GEN_JOBS.values()):
        session_rows.append({
            "ts": st.get("created") or 0, "tool": "AI Generator",
            "title": st.get("model_name") or "Generation",
            "status": st.get("status") or "",
            "error": st.get("error") or ""})
        if st.get("status") == "error":
            fail_row(st.get("created"), "AI Generator",
                     st.get("model_name"), st.get("error"))
    for st in list(_REMOVER_JOBS.values()):
        session_rows.append({
            "ts": st.get("created") or 0, "tool": "Caption Remover",
            "title": st.get("source_name") or "Removal",
            "status": st.get("status") or "",
            "error": st.get("error") or ""})
        if st.get("status") == "error":
            fail_row(st.get("created"), "Caption Remover",
                     st.get("source_name"), st.get("error"))
    for st in list(_ENHANCE_JOBS.values()):
        session_rows.append({
            "ts": st.get("created") or 0, "tool": "Video Enhancer",
            "title": st.get("source_name") or "Enhance",
            "status": st.get("status") or "",
            "error": st.get("error") or ""})
        if st.get("status") == "error":
            fail_row(st.get("created"), "Video Enhancer",
                     st.get("source_name"), st.get("error"))
    failed.sort(key=lambda r: r.get("ts") or 0, reverse=True)

    counters = admin.read_counters()
    stats = admin.aggregate_stats(disk_jobs, session_rows, failed[:30],
                                  counters)

    roots = _work_roots()
    storage = {"work_bytes": sum(admin.dir_size_capped(r)
                                 for r in roots),
               "work_roots": roots,
               "models_bytes": admin.dir_size_capped(local_ai.DATA_DIR),
               "models_dir": local_ai.DATA_DIR,
               "app_bytes": admin.dir_size_capped(BASE_DIR),
               "app_dir": BASE_DIR}

    state = _ensure_license()
    payload = state.get("payload") or {}
    settings = load_settings()
    try:
        dl = local_ai.download_state()
    except Exception:  # noqa: BLE001
        dl = {}
    # License & Limits: the signed clip allowance (if any) + this
    # installation's own usage record - the same numbers app.py's
    # render gates enforce (usage.py). Unlimited keys say so.
    lic_usage = usage.status(_current_license_key(), payload)
    license_panel = {
        "unlocked": bool(state.get("unlocked")),
        "holder": payload.get("name") or "",
        "role": payload.get("role") or "customer",
        "machine_claim": payload.get("machine") or "",
        "key_type": ("Universal - works on any PC"
                     if payload.get("machine") == "ANY"
                     else "This PC only"),
        "expires": payload.get("exp") or "",
        "machine_code": licensing.machine_code(),
        "app_version": APP_VERSION,
        "model_ready": bool(local_ai.model_ready()),
        "model_file": local_ai.MODEL_FILE,
        "model_downloading": bool(dl.get("downloading")),
        "fal_key_set": bool((settings.get("fal_key") or "").strip()),
        "clip_allowance": lic_usage["max_clips"],
        "clips_used": lic_usage["used"],
        "clips_remaining": lic_usage["remaining"],
        "usage_state": lic_usage["state"],
    }
    entries = admin.read_api_log(500)
    return {"ok": True,
            "generated_at": time.time(),
            "uptime_s": int(time.time() - _APP_START_TS),
            "stats": stats,
            "storage": storage,
            "license": license_panel,
            "system": admin.system_info(disk_path=_work_root()),
            "providers": admin.provider_summaries(entries),
            "publishing": publishing.admin_stats()}


_MAX_API_BODY = 16 * 1024 * 1024  # no endpoint takes a bigger body
_UNLOCK_GUARD = {"fails": 0, "until": 0.0}
_UNLOCK_GUARD_LOCK = threading.Lock()


def _post_origin_ok(environ):
    """Drive-by defence for the local API's POST surface.

    The server binds 127.0.0.1 on an ephemeral port, but every
    mutating action is a POST (delete a project, apply an update,
    uninstall) and any web page open in the user's browser could
    try to POST to a guessed port. Browsers always send Origin on
    POST, and a cross-origin page's Origin can never equal our
    Host - so those are refused, as is any request addressed to a
    non-loopback Host (DNS rebinding). Same-origin UI fetches
    pass; requests with no Origin at all (the app's own tooling,
    curl) pass. A hostile local PROCESS could still call the API,
    but it already runs with the user's full file access - that
    layer is not defensible from here and never was."""
    host = (environ.get("HTTP_HOST") or "").strip().lower()
    if host and not host.startswith(("127.0.0.1", "localhost",
                                      "[::1]")):
        return False
    origin = (environ.get("HTTP_ORIGIN") or "").strip()
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc.lower() != host:
            return False
    return True


def make_wsgi_app(api):
    _ensure_pub()  # bind publishing settings + start its worker
    ui_dir = os.path.abspath(os.path.join(BASE_DIR, "ui"))

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"

        # --- POST origin guard (see _post_origin_ok) --------------
        if method == "POST" and not _post_origin_ok(environ):
            return _wsgi_response(
                start_response, "403 Forbidden", "application/json",
                json.dumps({"ok": False, "error": "forbidden origin"}))

        # --- license gate: while locked, only the activation page
        # and the two license endpoints exist -------------------------
        if _license_locked():
            if method == "GET" and path in ("/", "/index.html"):
                apath = os.path.join(ui_dir, "activate.html")
                try:
                    with open(apath, "rb") as f:
                        data = f.read()
                except OSError:
                    data = b"<h1>This copy is locked</h1>"
                start_response("200 OK",
                               [("Content-Type", "text/html; "
                                 "charset=utf-8"),
                                ("Content-Length", str(len(data)))])
                return [data]
            if method == "GET" and path in ("/icon-192.png",
                                            "/icon-512.png"):
                # The activation page's brand logo + favicon. Without
                # this pass-through the locked screen showed a broken
                # image where the logo belongs (user screenshot,
                # 2026-10-25) - the gate 404'd every static file.
                ipath = os.path.join(ui_dir, path.lstrip("/"))
                try:
                    with open(ipath, "rb") as f:
                        data = f.read()
                except OSError:
                    return _wsgi_response(start_response,
                                          "404 Not Found",
                                          "text/plain", "not found")
                start_response("200 OK",
                               [("Content-Type", "image/png"),
                                ("Content-Length", str(len(data)))])
                return [data]
            if method == "POST" and path.startswith("/api/") \
                    and path[len("/api/"):] in ("license_status",
                                                "activate_license"):
                pass  # handled by the normal dispatch below
            elif path.startswith("/api/"):
                return _wsgi_response(
                    start_response, "200 OK", "application/json",
                    json.dumps({"ok": False, "error": "locked",
                                "message": "This copy is locked - "
                                           "activate it with your "
                                           "license key first."}))
            else:
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")

        # --- built-in AI: GET /api/local-ai/status ---------------------
        if method == "GET" and path == "/api/local-ai/status":
            return _wsgi_response(start_response, "200 OK",
                                  "application/json",
                                  json.dumps(api.local_ai_status(),
                                             default=str))

        # --- built-in AI: POST /api/local-ai/download ------------------
        if method == "POST" and path == "/api/local-ai/download":
            return _wsgi_response(start_response, "200 OK",
                                  "application/json",
                                  json.dumps(api.local_ai_download(),
                                             default=str))

        # --- old-copy cleanup: GET /api/old_copies ----------------------
        # A GET (not the POST method dispatch) because a scan is a
        # pure read; deletion stays a POST Api method. The scan walks
        # candidate sizes, so it runs only when Settings asks for it.
        if method == "GET" and path == "/api/old_copies":
            return _wsgi_response(start_response, "200 OK",
                                  "application/json",
                                  json.dumps(api.old_copies(),
                                             default=str))

        # --- work in progress: GET /api/work-status -------------------
        # Polled by the close guard injected into the main page: while
        # anything is active, the page arms beforeunload so closing
        # the window/tab asks first (Chrome mode). The own (WebView2)
        # window asks natively instead - see _run_own_window.
        if method == "GET" and path == "/api/work-status":
            what = _work_in_progress()
            return _wsgi_response(
                start_response, "200 OK", "application/json",
                json.dumps({"ok": True, "active": bool(what),
                            "what": what}))

        # --- Owner Admin Panel: dedicated routes -------------------
        # These are NOT Api methods: unlock needs Set-Cookie control
        # and every data route is gated on the server-side session
        # store before a single byte of data is produced.
        if path.startswith("/api/admin/"):
            def _json(status, obj, extra=None):
                body = json.dumps(obj).encode("utf-8")
                hdrs = [("Content-Type", "application/json"),
                        ("Content-Length", str(len(body)))]
                hdrs += [("Set-Cookie", c) for c in (extra or [])]
                start_response(status, hdrs)
                return [body]

            def _read_key():
                try:
                    length = int(environ.get("CONTENT_LENGTH") or 0)
                except (TypeError, ValueError):
                    length = 0
                length = min(length, 65536)  # a key is never bigger
                raw = (environ["wsgi.input"].read(length)
                       if length > 0 else b"{}")
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return ""
                if isinstance(body, dict):
                    if body.get("key"):
                        return str(body["key"])
                    args = body.get("args") or []
                    if args:
                        return str(args[0])
                return ""

            if method == "POST" and path == "/api/admin/unlock":
                # Brute-force backoff: key verification itself is
                # cheap (a local Ed25519 check), and there was NO
                # limit on attempts. After 10 consecutive failures
                # the endpoint pauses for a minute. The legitimate
                # owner succeeds on the first try, so the suites and
                # the real unlock flow never notice.
                with _UNLOCK_GUARD_LOCK:
                    if time.time() < _UNLOCK_GUARD["until"]:
                        return _json(
                            "429 Too Many Requests",
                            {"ok": False,
                             "error": "Too many unlock attempts - "
                                      "wait a minute, then try "
                                      "again."})
                payload = admin.verify_owner_key(_read_key())
                if payload is None:
                    with _UNLOCK_GUARD_LOCK:
                        _UNLOCK_GUARD["fails"] += 1
                        if _UNLOCK_GUARD["fails"] >= 10:
                            _UNLOCK_GUARD["fails"] = 0
                            _UNLOCK_GUARD["until"] = time.time() + 60
                    # One generic refusal for every failure mode:
                    # damaged, forged, expired, wrong machine, or a
                    # valid key without the owner claim.
                    return _json("403 Forbidden",
                                 {"ok": False,
                                  "error": "That key doesn't unlock "
                                           "the Admin Panel."})
                with _UNLOCK_GUARD_LOCK:
                    _UNLOCK_GUARD["fails"] = 0
                    _UNLOCK_GUARD["until"] = 0.0
                token, ttl = admin.create_session()
                cookie = (f"{_ADMIN_COOKIE}={token}; HttpOnly; "
                          f"Path=/; SameSite=Strict; Max-Age={ttl}")
                return _json("200 OK",
                             {"ok": True, "expires_in": ttl,
                              "holder": payload.get("name") or ""},
                             extra=[cookie])
            if method == "POST" and path == "/api/admin/logout":
                admin.drop_session(_admin_token_from(environ))
                cookie = (f"{_ADMIN_COOKIE}=; HttpOnly; Path=/; "
                          f"SameSite=Strict; Max-Age=0")
                return _json("200 OK", {"ok": True}, extra=[cookie])
            left = admin.validate_session(_admin_token_from(environ))
            if method == "GET" and path == "/api/admin/session":
                if left is None:
                    return _json("403 Forbidden",
                                 {"ok": False, "active": False})
                return _json("200 OK", {"ok": True, "active": True,
                                        "seconds_left": left})
            if left is None:
                return _json("403 Forbidden",
                             {"ok": False, "error": "admin_locked"})
            if method == "GET" and path == "/api/admin/stats":
                return _json("200 OK", _admin_stats_payload())
            if method == "GET" and path == "/api/admin/api-log":
                entries = admin.read_api_log(200)
                return _json("200 OK",
                             {"ok": True, "entries": entries,
                              "providers":
                                  admin.provider_summaries(entries),
                              "failures": [e for e in entries
                                           if e.get("status")
                                           != "ok"][:50]})
            if method == "POST" and path == "/api/admin/pub-pause":
                # Owner-session-gated like every /api/admin/* route:
                # the Publishing pause-all kill switch.
                try:
                    length = int(environ.get("CONTENT_LENGTH") or 0)
                except (TypeError, ValueError):
                    length = 0
                raw = (environ["wsgi.input"].read(
                    min(length, 65536)) if length > 0 else b"{}")
                try:
                    data = json.loads(raw or b"{}")
                except Exception:  # noqa: BLE001
                    data = {}
                publishing.set_pause_all(bool(data.get("paused")))
                return _json("200 OK",
                             {"ok": True,
                              "publishing": publishing.admin_stats()})
            return _json("404 Not Found",
                         {"ok": False, "error": "unknown admin route"})

        # --- API: POST /api/<method> {"args": [...]} ---------------------
        if method == "POST" and path.startswith("/api/"):
            # Persisted Library jobs must be visible to EVERY method:
            # per-method _load_library() calls were easy to miss (a
            # fresh server answered posting_package with "Moment not
            # found" for a job sitting on disk until some other method
            # happened to load the Library first). It is idempotent.
            _load_library()
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
            if length > _MAX_API_BODY:
                # Bodies were read at whatever Content-Length a
                # caller claimed, unbounded: a single request could
                # make the server buffer arbitrary memory. Imports
                # pass file PATHS, not bytes - nothing legitimate
                # is anywhere near this cap.
                return _wsgi_response(
                    start_response, "413 Request Entity Too Large",
                    "application/json",
                    json.dumps({"ok": False,
                                "error": "request too large"}))
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

        # --- Caption Remover output: GET /remover-files/<id>/output.mp4 -
        if method == "GET" and path.startswith("/remover-files/"):
            rid = path[len("/remover-files/"):].split("/", 1)[0]
            fpath = os.path.join(_session_folder("remover-" + rid),
                                 "output.mp4")
            if rid not in _REMOVER_JOBS or not os.path.isfile(fpath):
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            start_response("200 OK",
                           [("Content-Type", "video/mp4"),
                            ("Content-Length",
                             str(os.path.getsize(fpath)))])

            def _stream_remover():
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        yield chunk
            return _stream_remover()

        # --- file downloads: GET /files/<job_id>/<rel path> ---------------
        if method == "GET" and path.startswith("/files/"):
            rest = path[len("/files/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            job_id, rel = parts
            # The persisted folder is the authority, not the in-memory
            # JOBS dict: after a restart (or if the job was pruned from
            # memory) its files on disk must still download. The job
            # id is restricted to a safe token and the resolved path
            # must stay inside work/<job_id>/.
            import re as _re
            if (not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or "")
                    or ".." in rel or rel.startswith("/") or not rel):
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            base = os.path.abspath(_job_folder(job_id))
            fpath = os.path.abspath(os.path.join(base, rel))
            if not fpath.startswith(base + os.sep) \
                    or not os.path.isfile(fpath):
                return _wsgi_response(start_response, "404 Not Found",
                                      "text/plain", "not found")
            ext = os.path.splitext(fpath)[1].lower()
            ctype = _MIME.get(ext, "application/octet-stream")
            size = os.path.getsize(fpath)
            headers = [("Content-Type", ctype),
                       ("Content-Length", str(size))]
            # downloads as attachments, media inline for preview; the
            # ZIP's on-disk name is unique per build - present the
            # stable friendly name to the browser
            if ext in (".zip", ".srt", ".vtt", ".fcpxml", ".xml", ".ass"):
                fname = os.path.basename(fpath)
                if ext == ".zip" and fname.startswith("clips-"):
                    fname = "clips.zip"
                if ext == ".zip" and fname.startswith("enhanced-"):
                    fname = "enhanced.zip"
                headers.append((
                    "Content-Disposition",
                    "attachment; filename=" + fname))
                # Admin Panel counter: attachment responses are the
                # app's real "user saved a file" events (inline
                # media previews are not downloads).
                admin.bump_counter("downloads")
            start_response("200 OK", headers)

            def _stream():
                # chunked streaming: never hold a whole (possibly
                # multi-hundred-MB) archive in memory
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        yield chunk
            return _stream()

        # --- bundled fonts (Caption Studio preview @font-face) --------
        if method == "GET" and path.startswith("/fonts/"):
            fname = os.path.basename(path)
            fpath = os.path.join(BASE_DIR, "fonts", fname)
            if fname.lower().endswith(".ttf") \
                    and os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                start_response("200 OK",
                               [("Content-Type", "font/ttf"),
                                ("Content-Length", str(len(data)))])
                return [data]
            return _wsgi_response(start_response, "404 Not Found",
                                  "text/plain", "not found")

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
        if rel == "index.html":
            # The main page gets the close guard (see
            # _CLOSE_GUARD_SNIPPET). Injected at serve time so the
            # on-disk file - and its signed integrity hash - is
            # untouched.
            if b"</body>" in data:
                data = data.replace(b"</body>",
                                    _CLOSE_GUARD_SNIPPET + b"</body>", 1)
            else:
                data = data + _CLOSE_GUARD_SNIPPET
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
    title; returns True only when a window was actually found (the
    caller uses that to tell a live instance from a windowless ghost).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        for title in ("Itraji Cliper", "Itraji Cliper — Activation"):
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True
    except Exception:  # noqa: BLE001 - focusing is a courtesy
        pass
    return False


def _clamp_window_geometry(x0, y0, work_w, work_h):
    """Pure clamp: window size/position for a work area with origin
    (x0, y0). Returns (w, h, x, y) - at most 96% of the work area,
    capped at 1600x1050, centered - or None if the work area is
    unusably small. Unit-testable without an OS."""
    if work_w < 400 or work_h < 400:
        return None
    w = min(1600, int(work_w * 0.96))
    h = min(1050, int(work_h * 0.96))
    x = x0 + (work_w - w) // 2
    y = y0 + (work_h - h) // 2
    return (w, h, x, y)


_DPI_AWARE_ATTEMPTED = False


def _ensure_system_dpi_aware():
    """Make this process system-DPI-aware BEFORE any window geometry
    is measured, once, best-effort.

    pywebview only calls SetProcessDPIAware() inside its own
    create_window (winforms backend), which runs during
    webview.start() - AFTER _window_geometry() / _dpi_scale() below
    have already measured. If the process were still DPI-unaware at
    measurement time (nothing else in the app sets awareness, and
    the exe manifest is not ours to control), SPI_GETWORKAREA and
    GetDpiForSystem would answer VIRTUALIZED values: the work area
    in logical pixels and a DPI of 96 - so _dpi_scale() would read
    1.0, skip the logical conversion entirely, and pywebview would
    then multiply already-logical geometry by the real scale,
    oversizing the window again. Setting awareness up front - the
    exact call pywebview itself makes later, so its own call simply
    becomes a no-op - makes every measurement below genuinely
    physical and the conversions deterministic. No-op off Windows
    or on any failure (geometry stays a nicety, never fatal)."""
    global _DPI_AWARE_ATTEMPTED
    if _DPI_AWARE_ATTEMPTED or os.name != "nt":
        return
    _DPI_AWARE_ATTEMPTED = True
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - a nicety, never fatal
        pass


def _dpi_scale():
    """The primary display's DPI scale (1.0 = 100%) in the SAME
    coordinate space _window_geometry() measures in.

    SPI_GETWORKAREA answers follow this process's DPI awareness, and
    GetDpiForSystem reports under that same awareness, so physical
    geometry divided by this scale gives true logical pixels - which
    is the unit pywebview's create_window expects (it multiplies its
    inputs back by GetDpiForWindow/96). 1.0 off Windows or on any
    failure, where the conversion is a no-op anyway."""
    if os.name != "nt":
        return 1.0
    _ensure_system_dpi_aware()
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:  # noqa: BLE001 - geometry is a nicety, never fatal
        pass
    return 1.0


def _window_geometry():
    """(w, h, x, y) for the app window, clamped to the primary
    monitor's work area (Windows only) - or None. Shared by the own
    (WebView2) window and the Chrome --app window (as flags)."""
    if os.name != "nt":
        return None
    _ensure_system_dpi_aware()
    try:
        import ctypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long)]

        rect = _RECT()
        SPI_GETWORKAREA = 48
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok:
            x0, y0 = rect.left, rect.top
            work_w = rect.right - rect.left
            work_h = rect.bottom - rect.top
        else:  # fall back to the full primary-monitor size
            x0 = y0 = 0
            work_w = ctypes.windll.user32.GetSystemMetrics(0)
            work_h = ctypes.windll.user32.GetSystemMetrics(1)
        return _clamp_window_geometry(x0, y0, work_w, work_h)
    except Exception:  # noqa: BLE001 - geometry is a nicety, never fatal
        return None


def _pull_window_on_screen():
    """Verify the own window actually landed inside its monitor's
    work area, and pull it back if any visible edge sticks out.

    The geometry handed to pywebview crosses two coordinate spaces
    (physical here, logical there, multiplied back by the window's
    DPI), so despite the clamp the placed rectangle is only certain
    once the window exists - on the user's PC it hung off the left
    edge, clipping the sidebar and even the title text. This runs
    after the window is shown, in the now DPI-aware GUI process:
    GetWindowRect / DwmGetWindowAttribute / GetMonitorInfo all speak
    physical pixels here, so the check and the SetWindowPos
    correction share one consistent space. Best-effort throughout:
    any failure leaves the window exactly as it is."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = 0
        for title in ("Itraji Cliper", "Itraji Cliper — Activation"):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
        if not hwnd:
            return

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long)]

        full = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(full)):
            return
        # Visible bounds (excludes the invisible resize-frame margin
        # Windows adds to GetWindowRect): a healthy window's VISIBLE
        # rect sits flush inside the work area even though its full
        # rect overhangs by a few px - only correct real overhangs.
        vis = _RECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(vis), ctypes.sizeof(vis))
        if hr != 0:
            vis = full
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not mon:
            return

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", _RECT),
                        ("rcWork", _RECT),
                        ("dwFlags", wintypes.DWORD)]

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return
        wk = mi.rcWork
        if (vis.left >= wk.left - 1 and vis.top >= wk.top - 1
                and vis.right <= wk.right + 1
                and vis.bottom <= wk.bottom + 1):
            return  # fully on-screen (1px rounding tolerance)
        fit = _clamp_window_geometry(wk.left, wk.top,
                                     wk.right - wk.left,
                                     wk.bottom - wk.top)
        if fit is None:
            return
        nw, nh, nx, ny = fit
        # SetWindowPos places the FULL rect (incl. the invisible
        # frame); translate the visible-bounds fit by the frame
        # delta so the visible window lands exactly on it.
        fx = vis.left - full.left
        fy = vis.top - full.top
        fw = (full.right - full.left) - (vis.right - vis.left)
        fh = (full.bottom - full.top) - (vis.bottom - vis.top)
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, 0, nx - fx, ny - fy, nw + fw, nh + fh,
                            SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001 - a nicety, never fatal
        pass


def _window_geometry_args():
    """Chrome --window-size / --window-position flags from
    _window_geometry().

    Without explicit geometry Chrome restores whatever size/position a
    previous session left behind - which can be wider than the screen,
    pushing the top-right buttons off-screen. Fully guarded: any
    failure returns [] and Chrome keeps its default behavior.

    _window_geometry() returns PHYSICAL pixels, but Chromium window
    bounds are in logical (DIP) pixels - the same mismatch the
    own-window branch had with pywebview. Feeding physical values
    straight in made the Chrome window oversized and displaced on
    125%/150% displays (the clipped sidebar the user kept seeing in
    the fallback mode), so convert with _dpi_scale() first - a no-op
    at 100% scaling. Any failure anywhere returns []: Chrome's own
    placement beats a wrong rectangle."""
    try:
        geo = _window_geometry()
        if geo is None:
            return []
        w, h, x, y = geo
        scale = _dpi_scale()
        if scale and scale != 1.0:
            w = int(round(w / scale))
            h = int(round(h / scale))
            x = int(round(x / scale))
            y = int(round(y / scale))
        return [f"--window-size={w},{h}",
                f"--window-position={x},{y}"]
    except Exception:  # noqa: BLE001 - geometry is a nicety, never fatal
        return []


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
                 "--no-first-run", "--no-default-browser-check"]
                + _window_geometry_args(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)
    return None


def _window_mode():
    """Which window hosts the UI: "own" (the app's own WebView2
    window - the taskbar then shows Itraji Cliper with its own name
    and logo, and pinning pins the app, not Chrome) or "chrome" (the
    Chrome/Edge --app fallback). The ITRAJI_WINDOW env var wins, then
    the Settings choice (window_mode), default "own"."""
    env = os.environ.get("ITRAJI_WINDOW", "").strip().lower()
    if env in ("own", "webview"):
        return "own"
    if env == "chrome":
        return "chrome"
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("window_mode") == "chrome":
            return "chrome"
    except (OSError, ValueError):
        pass
    return "own"


def _run_own_window(url):
    """Open the UI in the app's OWN window (pywebview over the system
    WebView2 runtime). The window belongs to this process - whose exe
    carries the Itraji Cliper name and logo - so the taskbar button,
    the pin and Alt-Tab all show the app itself, never Chrome.

    webview.start() blocks until the user closes the window, which is
    exactly the app's lifetime, so a normal return means "ran and
    closed" -> True. Any failure BEFORE the window is shown (pywebview
    missing, WebView2 runtime missing, no GUI) -> False, and the
    caller falls back to the Chrome --app window. A failure after the
    window was shown still counts as ran: re-opening Chrome on top of
    a window the user just closed would be worse.
    """
    try:
        import webview
    except Exception:  # noqa: BLE001 - not installed: caller's fallback
        return False
    shown = {"yes": False}
    try:
        kwargs = {"title": "Itraji Cliper", "url": url,
                  "width": 1280, "height": 860, "min_size": (900, 600)}
        geo = _window_geometry()
        if geo is not None:
            w, h, x, y = geo
            # pywebview's create_window speaks LOGICAL pixels: its
            # Windows backend multiplies width/height/x/y by the
            # window's DPI scale (GetDpiForWindow/96) to get physical
            # pixels. _window_geometry() returns PHYSICAL pixels, so
            # convert first - feeding physical values straight in
            # made the window oversized and displaced on 125%/150%
            # displays (it landed hanging off the left edge, sidebar
            # and title text clipped). At 100% scaling this is a
            # no-op. min_size stays as-is: it is already in logical
            # (CSS) pixels, which is the layout's own unit.
            scale = _dpi_scale()
            if scale and scale != 1.0:
                w = int(round(w / scale))
                h = int(round(h / scale))
                x = int(round(x / scale))
                y = int(round(y / scale))
            kwargs.update({"width": w, "height": h, "x": x, "y": y})
        win = webview.create_window(**kwargs)

        def _on_closing():
            """Confirm-on-close: while work is running (the Built-in
            AI download, a render...), ask first - the user's words:
            a "you sure to off" message. Cancelling keeps everything
            running; confirming closes cleanly. Any failure in the
            guard itself allows the close: the app must never become
            unclosable."""
            try:
                what = _work_in_progress()
                if not what:
                    return True
                return bool(win.create_confirmation_dialog(
                    "Itraji Cliper",
                    "You sure to off? " + " and ".join(what) +
                    " still in progress. Closing stops it - a stopped"
                    " download can be resumed later."))
            except Exception:  # noqa: BLE001 - never trap the user
                return True

        try:
            win.events.closing += _on_closing
        except Exception:  # noqa: BLE001 - event hook is best-effort
            pass

        def _mark_shown():
            shown["yes"] = True
            # Whatever the geometry conversions above produced on
            # this particular display, guarantee the placed window
            # is fully on-screen before the user starts working.
            _pull_window_on_screen()
            # Second pass ~1.2s later: pywebview applies the size
            # and position in the form constructor (before Shown),
            # but the form also carries WinForms AutoScaleMode.Dpi
            # with a 96-DPI baseline, and the runtime / Windows can
            # still adjust the placed rectangle as the window
            # settles - after the shown-time check has already run.
            # The corrector is idempotent and a no-op when the
            # window is fully on-screen, so a late drift gets the
            # same correction instead of surviving it.
            def _second_pass():
                try:
                    time.sleep(1.2)
                    _pull_window_on_screen()
                except Exception:  # noqa: BLE001 - best-effort
                    pass

            try:
                threading.Thread(target=_second_pass,
                                 daemon=True).start()
            except Exception:  # noqa: BLE001 - best-effort
                pass

        try:
            win.events.shown += _mark_shown
        except Exception:  # noqa: BLE001 - event hook is best-effort
            pass
        webview.start()
        return True
    except Exception:  # noqa: BLE001 - see docstring
        return shown["yes"]


def _auto_update_ytdlp():
    """Best-effort yt-dlp upgrade at startup (YouTube breaks old copies).

    Runs in a background thread with a short timeout; never blocks launch
    and never fails loudly."""
    if getattr(sys, "frozen", False):
        # In the shipped build sys.executable IS ClipMakerPC.exe:
        # "ClipMakerPC.exe -m pip install" just launched a second
        # copy of the app (bounced by the single-instance guard)
        # and upgraded nothing. Source runs keep the real upgrade.
        return
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


def _owner_serves_app(port):
    """True when 127.0.0.1:<port> answers like this app - the
    local-ai status endpoint, in its normal or its locked form.
    Distinguishes the real owner from a recycled pid, or from a
    process that holds the lock but never got its server up."""
    for _ in range(3):
        try:
            r = requests.get(
                "http://127.0.0.1:%d/api/local-ai/status" % port,
                timeout=1.5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and (
                        "downloading" in data
                        or data.get("error") == "locked"):
                    return True
        except Exception:  # noqa: BLE001 - any failure: not (yet) ours
            pass
        time.sleep(0.5)
    return False


def _replace_ghost_owner(owner_mode):
    """Cleanly replace a windowless ghost owner (Windows only).

    The ghost is real: the app process can outlive its window (the
    Chrome process kept alive by background mode after the --app
    window closed, or a launcher that handed off), still holding the
    lock and the port, still telling the UI a cut download is
    "downloading" - while owning no window the user can see or close.
    Every later launch just defers to it, so the app looks dead.

    Replace ONLY when every check holds: a duplicate launch already
    failed to find/focus the owner's window (the caller did); the
    owner's window mode is a dedicated-window mode ("app"/"webview")
    - a plain-tab "fallback" owner is a live browser session by
    design and is never replaced; the lock is not fresh (an instance
    still starting up keeps its grace period); and the recorded port
    verifiably serves this app. Then terminate the owner and wait
    for it to die so the caller can re-acquire. Any doubt -> False:
    defer, exactly as before.
    """
    if os.name != "nt":
        return False
    if owner_mode not in ("app", "webview"):
        return False
    info = _read_lockfile()
    if not info:
        return False
    try:
        if time.time() - os.path.getmtime(LOCKFILE) < 30:
            return False  # owner is still starting up - hands off
    except OSError:
        return False
    port, pid = info.get("port"), info.get("pid")
    if not port or not pid or not _owner_serves_app(port):
        return False
    if not _pid_alive(pid):
        return True  # died on its own - the re-acquire will see that
    import signal
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.kill(pid, sig)
        except OSError:
            return False
        for _ in range(50):
            if not _pid_alive(pid):
                return True
            time.sleep(0.1)
    return not _pid_alive(pid)


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
            info = _read_lockfile() or {}
            port = info.get("port")
            if not port:
                return False, info.get("mode")
            if _owner_serves_app(port):
                return False, info.get("mode")
            # The pid is alive and a port is recorded, but nothing on
            # that port answers like this app: the pid was recycled
            # by an unrelated process (or the owner died between the
            # checks). The lock is bogus - drop it and take over,
            # WITHOUT touching the unrelated process.
            try:
                os.remove(LOCKFILE)
            except OSError:
                pass
            continue
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

    # Protection startup checks (packaged app): signed integrity over
    # the loose files + anti-debug. A failure locks the app to the
    # "modified copy" screen. ITRAJI_DEV=1 skips both for source runs.
    _integ_ok, _integ_bad = licensing.verify_integrity(BASE_DIR)
    if not _integ_ok:
        licensing.mark_tampered()
    if licensing.debugger_present():
        licensing.mark_tampered()
    _ensure_license()

    # Apply the saved storage location (Settings -> Storage) before
    # anything resolves model or work paths this session.
    try:
        local_ai.set_data_dir(load_settings().get("data_dir") or "")
    except Exception:  # noqa: BLE001 - a bad setting must not brick
        pass              # the boot; defaults apply instead

    # AI health check: cheap file/system probes on a daemon thread,
    # warming the cache the AI Installation Manager reads. Boot
    # never blocks on it.
    ai_components.prime_health()

    # Single instance: a repeated launch never starts a second server and
    # never opens anything - no second window, no browser tab, ever. It
    # just brings the one existing window to the front and exits. (An
    # earlier version opened the URL in a normal browser tab here, which
    # is how a plain Chrome tab appeared next to the app window.)
    owned, _owner_mode = _acquire_single_instance()
    if not owned:
        if _focus_existing_window():
            return
        # No window could be found for the recorded owner: it may be
        # a windowless ghost still serving a dead session (see
        # _replace_ghost_owner - the checks are strict). Replace it
        # and take over; otherwise exit quietly, as before.
        if _replace_ghost_owner(_owner_mode):
            owned, _owner_mode = _acquire_single_instance()
        if not owned:
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

        # First choice: the app's OWN window (WebView2 via pywebview),
        # so the taskbar shows Itraji Cliper itself. It blocks until
        # the window closes, then the app exits. Any failure to open
        # it falls through to the Chrome --app window below.
        if _window_mode() == "own":
            _update_lockfile_mode("webview")
            if _run_own_window(url):
                return

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
