"""Built-in AI - local LLM engine for Itraji Cliper.

Runs a professional open-weights model (Qwen2.5-14B-Instruct, Q4_K_M
GGUF) entirely on this machine through llama.cpp's llama-server.
No key and no internet are needed once the one-time downloads
(engine ~24 MB, model ~9 GB) have completed.

Downloads land in the app's data area (mirrors app.py's BASE_DIR
resolution):  <data>/llama/  for the engine,  <data>/models/  for the
GGUF. Test hooks: ITRAJI_LOCAL_MODEL_URL / ITRAJI_LOCAL_MODEL_FILE /
ITRAJI_LOCAL_MODEL_MIRROR_URL / ITRAJI_LLAMA_URL override the
model/engine downloads, and ITRAJI_LOCAL_DATA_DIR overrides the
data area.
"""

import atexit
import errno
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from collections import deque
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

_BASE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS  # PyInstaller bundle (same rule as app.py)
DATA_DIR = os.environ.get("ITRAJI_LOCAL_DATA_DIR") or _BASE
MODELS_DIR = os.path.join(DATA_DIR, "models")
LLAMA_DIR = os.path.join(DATA_DIR, "llama")

# ---------------------------------------------------------------------------
# Pinned downloads (resolved + HTTP-200-verified 2026-10-22)
# ---------------------------------------------------------------------------

_LLAMA_TAG = "b11803"
_LLAMA_FALLBACK_TAG = "b11802"
_LLAMA_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"


def _engine_asset_name(tag):
    if sys.platform.startswith("win"):
        return f"llama-{tag}-bin-win-cpu-x64.zip"
    return f"llama-{tag}-bin-ubuntu-x64.tar.gz"


def _engine_urls():
    override = os.environ.get("ITRAJI_LLAMA_URL")
    if override:
        return [override]
    return [
        f"{_LLAMA_BASE}/{_LLAMA_TAG}/{_engine_asset_name(_LLAMA_TAG)}",
        f"{_LLAMA_BASE}/{_LLAMA_FALLBACK_TAG}/"
        f"{_engine_asset_name(_LLAMA_FALLBACK_TAG)}",
    ]


MODEL_FILE = (os.environ.get("ITRAJI_LOCAL_MODEL_FILE")
              or "Qwen2.5-14B-Instruct-Q4_K_M.gguf")
MODEL_URL = (os.environ.get("ITRAJI_LOCAL_MODEL_URL")
             or "https://huggingface.co/bartowski/"
                "Qwen2.5-14B-Instruct-GGUF/resolve/main/"
                "Qwen2.5-14B-Instruct-Q4_K_M.gguf")
# Exact byte size of the pinned model on Hugging Face (blobs API).
MODEL_SIZE = 8988110976
# SHA-256 of the pinned model file (Hugging Face blobs API, LFS hash).
MODEL_SHA256 = ("e47ad95dad6ff848b431053b375adb5d39321290ea2c638682577"
                "dafca87c008")
_MODEL_OVERRIDDEN = bool(os.environ.get("ITRAJI_LOCAL_MODEL_URL")
                         or os.environ.get("ITRAJI_LOCAL_MODEL_FILE"))
# Mirror of the same model file (same repo path on hf-mirror.com).
# Some networks cannot reliably resolve huggingface.co - the failure
# that stranded a user at 99%, every resume re-request dying in DNS -
# so the download falls back to this host on host-level failures.
# The bytes are identical, and the finished file is still verified
# against MODEL_SHA256: the hash gate decides, never the host.
_MODEL_MIRROR_URL = (os.environ.get("ITRAJI_LOCAL_MODEL_MIRROR_URL")
                     or "https://hf-mirror.com/bartowski/"
                        "Qwen2.5-14B-Instruct-GGUF/resolve/main/"
                        "Qwen2.5-14B-Instruct-Q4_K_M.gguf")


def _model_urls():
    """Download sources for the model file, best first. With a test
    override of the model URL the mirror stays out of the way unless
    ITRAJI_LOCAL_MODEL_MIRROR_URL is set explicitly."""
    urls = [MODEL_URL]
    if (not _MODEL_OVERRIDDEN
            or os.environ.get("ITRAJI_LOCAL_MODEL_MIRROR_URL")):
        urls.append(_MODEL_MIRROR_URL)
    return urls


# ---------------------------------------------------------------------------
# User-chosen storage location (Settings -> Storage)
# ---------------------------------------------------------------------------
# app.py applies the saved `data_dir` setting via set_data_dir().
# ONLY the big GGUF's home moves (plus, on the app.py side, the
# work root for new projects); the engine, the LaMa/OCR models and
# everything else keep living under DATA_DIR exactly as before.
# The env test hook ITRAJI_LOCAL_DATA_DIR always wins: when it is
# set, set_data_dir() is ignored entirely.

_custom_data_dir = None


def set_data_dir(path):
    """Point the AI model at a user-chosen folder (None/"" clears).
    Ignored when ITRAJI_LOCAL_DATA_DIR overrides the data area."""
    global _custom_data_dir
    if os.environ.get("ITRAJI_LOCAL_DATA_DIR"):
        return
    p = (path or "").strip()
    _custom_data_dir = os.path.abspath(p) if p else None


def custom_data_dir():
    return _custom_data_dir


def _gguf_dirs():
    """Folders that may hold the model GGUF, best first: the
    user-chosen location, then the legacy/app location. The legacy
    folder is ALWAYS in the list, so a model that already exists
    there (e.g. the Full install's baked-in 9 GB) is never orphaned
    by choosing a new location."""
    dirs = []
    if _custom_data_dir:
        dirs.append(os.path.join(_custom_data_dir, "models"))
    if MODELS_DIR not in dirs:
        dirs.append(MODELS_DIR)
    return dirs


def model_download_dir():
    """Where a NEW model download lands: the chosen location when
    set, else the default models folder."""
    return _gguf_dirs()[0]


def _file_complete(p):
    """True when p is a finished model file (same rule model_ready
    has always used: exact pinned size, or >1 MB for test
    overrides)."""
    if not os.path.isfile(p):
        return False
    if not _MODEL_OVERRIDDEN:
        return os.path.getsize(p) == MODEL_SIZE
    return os.path.getsize(p) > 1_000_000


def existing_model_path():
    """The complete model file to USE - chosen location first,
    legacy second - or None when no complete model exists."""
    for d in _gguf_dirs():
        p = os.path.join(d, MODEL_FILE)
        if _file_complete(p):
            return p
    return None


def model_path():
    """The effective model path: the existing complete model when
    one is found (chosen location first, legacy second), else the
    path a download would write to."""
    return existing_model_path() or os.path.join(
        model_download_dir(), MODEL_FILE)


def _expected_model_size():
    """Byte size the finished model file must have, when knowable."""
    if not _MODEL_OVERRIDDEN:
        return MODEL_SIZE
    try:
        r = requests.head(MODEL_URL, allow_redirects=True, timeout=30)
        n = int(r.headers.get("Content-Length") or 0)
        return n or None
    except requests.RequestException:
        return None


def cpu_engine_path():
    """Path of the extracted CPU llama-server binary, or None."""
    if not os.path.isdir(LLAMA_DIR):
        return None
    want = "llama-server.exe" if sys.platform.startswith("win") \
        else "llama-server"
    for root, _dirs, files in os.walk(LLAMA_DIR):
        if want in files:
            return os.path.join(root, want)
    return None


# ---------------------------------------------------------------------------
# GPU engine (Windows + NVIDIA): the CUDA build of the SAME pinned
# llama.cpp tag, kept in its own folder (llama-gpu/) so the CPU
# engine is always intact to fall back to. The CUDA assets verifiably
# exist in the pinned b11803 release: llama-b11803-bin-win-cuda-
# 12.4-x64.zip (272,315,003 B) and cudart-llama-bin-win-cuda-12.4-
# x64.zip (391,443,627 B). CUDA 12.4 is the variant chosen: it runs
# on the widest range of NVIDIA drivers. The GPU engine is only
# ever an accelerator - any failure at download or launch falls
# back to the CPU engine (see ensure_server), never to a dead AI.
# ---------------------------------------------------------------------------

LLAMA_GPU_DIR = os.path.join(DATA_DIR, "llama-gpu")
_GPU_CUDA_VARIANT = "12.4"
_GPU_ENGINE_SIZE = 272315003
_GPU_CUDART_SIZE = 391443627

# SHA-256 of the pinned release archives, computed from the official
# b11803 assets themselves. Downloads of these files are verified
# against the pin before extraction (the same hash-gate rule as the
# model: the hash decides, never the host).
_ENGINE_ARCHIVE_SHA256 = {
    "llama-b11803-bin-win-cpu-x64.zip":
        "68a11f1f0ab78587bfc42071d2dbd4aa1b621357ddc375c1ee25a2"
        "4e76d27954",
    "llama-b11803-bin-ubuntu-x64.tar.gz":
        "2c718373086c9eda54d1e994cd5197aa079a4247e3dc4ec9edee82"
        "54cee08746",
    "llama-b11803-bin-win-cuda-12.4-x64.zip":
        "3a9724665d7b1036d2e7f9e1cd2ef869f534bd3a1f7c11972e1eae"
        "b223d9ec07",
    "cudart-llama-bin-win-cuda-12.4-x64.zip":
        "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e"
        "32ae1d6",
}

_AI_CONFIG_PATH = os.path.join(DATA_DIR, "ai_config.json")


def _ai_config():
    try:
        with open(_AI_CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ai_config(cfg):
    try:
        with open(_AI_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass  # a preference that cannot persist is not fatal


def engine_preference():
    """The recorded engine preference: "auto" (default - GPU when
    its engine is installed), "cpu" or "gpu"."""
    return _ai_config().get("engine") or "auto"


def set_engine_preference(pref):
    cfg = _ai_config()
    cfg["engine"] = pref if pref in ("cpu", "gpu") else "auto"
    _save_ai_config(cfg)


# Session-scoped GPU launch failure: once the GPU engine has failed
# to start in this process, the session runs on CPU and the AI
# Manager can say so honestly (gpu_fallback_note) instead of
# retrying a broken engine on every detection.
_gpu_fallback = {"failed": False, "reason": ""}


def gpu_fallback_note():
    return dict(_gpu_fallback) if _gpu_fallback["failed"] else None


def _mark_gpu_fallback(reason):
    _gpu_fallback.update(failed=True, reason=str(reason)[:200])


def gpu_engine_path():
    """Path of the extracted GPU (CUDA) llama-server binary, or
    None. Accepts either binary name on any platform so test rigs
    can stage a Windows layout on a Linux box."""
    if not os.path.isdir(LLAMA_GPU_DIR):
        return None
    for root, _dirs, files in os.walk(LLAMA_GPU_DIR):
        for want in ("llama-server.exe", "llama-server"):
            if want in files:
                return os.path.join(root, want)
    return None


def engine_flavor():
    """"gpu" when the GPU engine is installed and usable as far as
    this process knows, else "cpu"."""
    if _gpu_fallback["failed"]:
        return "cpu"
    if engine_preference() == "cpu":
        return "cpu"
    return "gpu" if gpu_engine_path() else "cpu"


def engine_path():
    """Path of the llama-server binary the app should run: the GPU
    build when it is the active flavour, else the CPU build."""
    if engine_flavor() == "gpu":
        got = gpu_engine_path()
        if got:
            return got
    return cpu_engine_path()


def _gpu_engine_asset():
    return f"llama-{_LLAMA_TAG}-bin-win-cuda-{_GPU_CUDA_VARIANT}-x64.zip"


def _gpu_cudart_asset():
    return f"cudart-llama-bin-win-cuda-{_GPU_CUDA_VARIANT}-x64.zip"


def _place_cudart_beside_server():
    """The CUDA runtime DLLs must sit in the same folder as
    llama-server.exe (that is how Windows resolves them, and how
    llama.cpp's own install notes lay the cudart zip out). If the
    extraction left them in a subfolder, copy them beside the exe."""
    exe = gpu_engine_path()
    if not exe:
        return
    exe_dir = os.path.dirname(exe)
    wanted = ("cudart64_", "cublas64_", "cublasLt64_")
    for root, _dirs, files in os.walk(LLAMA_GPU_DIR):
        if os.path.abspath(root) == os.path.abspath(exe_dir):
            continue
        for name in files:
            if name.lower().endswith(".dll") and \
                    name.lower().startswith(wanted):
                dst = os.path.join(exe_dir, name)
                if not os.path.exists(dst):
                    import shutil
                    shutil.copyfile(os.path.join(root, name), dst)


def ensure_gpu_engine():
    """Download + extract the CUDA engine build. Returns the binary
    path. Raises RuntimeError with a plain reason on any failure -
    callers fall back to the CPU engine, never to a dead AI. The
    archives are size- AND SHA-256-verified by _download_file
    before anything is extracted (never execute a download blind)."""
    if not sys.platform.startswith("win") and \
            not os.environ.get("ITRAJI_LLAMA_GPU_URL"):
        raise RuntimeError("the GPU (CUDA) engine build is "
                           "Windows-only - this system uses the "
                           "CPU engine")
    got = gpu_engine_path()
    if got:
        return got
    os.makedirs(LLAMA_GPU_DIR, exist_ok=True)
    base = f"{_LLAMA_BASE}/{_LLAMA_TAG}"
    override = os.environ.get("ITRAJI_LLAMA_GPU_URL")
    if override:
        pairs = [(override, None, None)]
    else:
        eng, cud = _gpu_engine_asset(), _gpu_cudart_asset()
        pairs = [(f"{base}/{eng}", _GPU_ENGINE_SIZE,
                  _ENGINE_ARCHIVE_SHA256.get(eng)),
                 (f"{base}/{cud}", _GPU_CUDART_SIZE,
                  _ENGINE_ARCHIVE_SHA256.get(cud))]
    for url, size, sha in pairs:
        archive = os.path.join(LLAMA_GPU_DIR,
                               "_gpu_" + url.rsplit("/", 1)[-1])
        _download_file(url, archive, "Downloading GPU AI engine (CUDA)"
                       if url == pairs[0][0] else
                       "Downloading CUDA runtime",
                       expected_size=size, expected_sha256=sha)
        with zipfile.ZipFile(archive) as z:
            z.extractall(LLAMA_GPU_DIR)
        _remove_quietly(archive)
        if override:
            break
    _place_cudart_beside_server()
    got = gpu_engine_path()
    if not got:
        raise RuntimeError("GPU engine download finished but no "
                           "llama-server binary was found inside it")
    return got


def model_ready():
    if not engine_path():
        return False
    return existing_model_path() is not None


def model_info():
    p = model_path()
    size = None
    if os.path.isfile(p):
        size = os.path.getsize(p)
    elif not _MODEL_OVERRIDDEN:
        size = MODEL_SIZE
    return {
        "path": p,
        "file": MODEL_FILE,
        "size_gb": round((size or 0) / 1e9, 2),
        "downloaded": os.path.isfile(p),
    }


# ---------------------------------------------------------------------------
# Moving the model to a new storage location
# ---------------------------------------------------------------------------

_move_state = {"active": False, "pct": 0, "error": None,
               "done": False, "target": ""}
_move_lock = threading.Lock()
_move_thread = None


def model_move_state():
    with _move_lock:
        return dict(_move_state)


def _set_move_state(**kw):
    with _move_lock:
        _move_state.update(kw)


def start_model_move(new_data_dir, on_done=None):
    """Move the existing model into <new_data_dir>/models on a
    background thread: copy -> verify the copy (size always, and
    SHA-256 against the pinned hash for the production model) ->
    delete the original -> switch the effective location. On ANY
    failure the partial copy is removed, the original stays put
    and active, and the error lands in model_move_state() and
    on_done(False, err). Returns False (no-op) when a move is
    already running, there is no complete model to move, or the
    model already lives at the target."""
    global _move_thread
    with _move_lock:
        if _move_state["active"]:
            return False
    src = existing_model_path()
    if not src:
        return False
    dst_dir = os.path.join(os.path.abspath(new_data_dir), "models")
    dst = os.path.join(dst_dir, MODEL_FILE)
    if os.path.abspath(src) == os.path.abspath(dst):
        return False
    _set_move_state(active=True, pct=0, error=None, done=False,
                    target=dst)

    def _work():
        tmp = dst + ".moving"
        try:
            os.makedirs(dst_dir, exist_ok=True)
            total = os.path.getsize(src)
            done_b = 0
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
                    done_b += len(chunk)
                    _set_move_state(
                        pct=int(done_b / total * 100) if total
                        else 100)
            if os.path.getsize(tmp) != total:
                raise OSError("the copy came out the wrong size - "
                              "the original was kept")
            if not _MODEL_OVERRIDDEN:
                h = hashlib.sha256()
                with open(tmp, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                if h.hexdigest() != MODEL_SHA256:
                    raise OSError("the copy failed its integrity "
                                  "check - the original was kept")
            os.replace(tmp, dst)
            os.remove(src)
            set_data_dir(new_data_dir)
            _set_move_state(active=False, pct=100, done=True)
            if on_done:
                on_done(True, None)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            err = str(e)[:200]
            _set_move_state(active=False, error=err, done=False)
            if on_done:
                on_done(False, err)

    _move_thread = threading.Thread(target=_work, daemon=True)
    _move_thread.start()
    return True


# ---------------------------------------------------------------------------
# Download state (shared with the UI via app.py endpoints)
# ---------------------------------------------------------------------------

_download_state = {"active": False, "pct": 0, "error": None,
                   "done": False, "stage": "",
                   "paused": False, "downloaded": 0, "total": 0,
                   "rate_bps": 0, "eta_seconds": None}
_state_lock = threading.Lock()
_download_thread = None


class _PausedError(RuntimeError):
    """The user paused the download: the worker stops now, the
    .part keeps every downloaded byte, and the stop is neither a
    failure nor a retryable error - it must not burn attempts,
    switch servers, or surface as an error message."""


_pause_requested = False


def pause_download():
    """Ask the download worker to stop at the next chunk boundary
    (the in-flight response is also force-closed, the same way the
    stall watchdog interrupts a blocked read, so a trickling
    connection cannot keep the worker parked). Idempotent: pausing
    an idle download just returns the current state. The pause is
    per-session: the state it leaves behind (inactive, no error)
    means a later relaunch treats the leftover .part exactly like
    any other interrupted download - the boot auto-resume."""
    global _pause_requested
    with _state_lock:
        active = _download_state["active"]
        if active:
            _pause_requested = True
            _download_state.update(stage="Pausing...")
    if active:
        with _progress_lock:
            resp = _progress["response"]
        if resp is not None:
            _force_close(resp)
    return download_state()


# Rolling throughput for the UI's speed + ETA readouts. Samples are
# (monotonic_ts, file_absolute_bytes) for the CURRENT .part; the
# tracker resets when a different file starts, so the engine's rate
# never bleeds into the model's.
_rate = {"tmp": None, "samples": deque()}
_rate_lock = threading.Lock()


def _note_progress(tmp, done, total):
    """Record one written chunk: refresh downloaded/total plus a
    trailing-window bytes/sec and the ETA it implies."""
    now = time.monotonic()
    with _rate_lock:
        if _rate["tmp"] != tmp:
            _rate["tmp"] = tmp
            _rate["samples"].clear()
        samples = _rate["samples"]
        samples.append((now, done))
        while len(samples) > 1 and now - samples[0][0] > 12:
            samples.popleft()
        rate = 0.0
        if len(samples) >= 2 and now - samples[0][0] >= 1.0:
            rate = (done - samples[0][1]) / (now - samples[0][0])
    eta = None
    if total and rate > 0 and done < total:
        eta = int((total - done) / rate)
    _set_state(downloaded=done, total=total or 0,
               rate_bps=int(rate), eta_seconds=eta)


def _heal_state_locked():
    """Caller holds _state_lock. If the state claims a download is
    running but the worker thread is gone (the app was cut mid-
    download, or the thread died without reporting), the claim is a
    wedge: the UI hides the Download button for as long as the state
    says "downloading". Reset to idle - the .part on disk is
    untouched, so the next click resumes where it stopped."""
    if _download_state["active"] and (
            _download_thread is None
            or not _download_thread.is_alive()):
        _download_state.update(
            active=False, done=False,
            stage="Download stopped - click Download to resume")


# ---------------------------------------------------------------------------
# Auto-resume on boot.
#
# A model .part on disk only exists because a download was started
# before and interrupted (app closed, PC restarted, process cut).
# But the download state is per-process: after a relaunch nothing
# restarted the download on its own - the state sat idle at 0% and
# the sidebar card showed its placeholder, which reads as "it won't
# resume" until the user thinks to click Download again. So the
# first state poll of a fresh process resumes automatically:
# exactly once, and only when there is real partial progress to
# resume. A fresh install (no .part) still waits for the user's
# click, and a download that already ran - and possibly failed with
# a user-visible error - this session is never second-guessed. The
# attempt-start re-verify in _download_once makes resuming safe for
# a complete .part: it is verified (and its rename finished) or
# discarded per the corrupt-restart rule, never trusted blindly.
# ---------------------------------------------------------------------------

_auto_resume_lock = threading.Lock()
_auto_resume_fired = False


def _maybe_auto_resume(state):
    """Resume an interrupted model download, once per process.

    `state` is the snapshot download_state() just took. Returns True
    when start_download() was fired, so the caller can re-read the
    (now active) state instead of returning the stale snapshot."""
    global _auto_resume_fired
    if state.get("active") or state.get("done") or state.get("error"):
        return False
    with _auto_resume_lock:
        if _auto_resume_fired:
            return False
        # Claim the single shot before checking anything else, so
        # concurrent polls can never double-fire it. If the checks
        # below decline, no later poll could legitimately fire
        # either: only a download creates a .part, and a download
        # starting is exactly what the _download_thread guard sees.
        _auto_resume_fired = True
    try:
        if _download_thread is not None:
            # A download already ran this session; whatever stopped
            # it was surfaced to the user (error state / heal). The
            # next move is the user's click, not an automatic retry.
            return False
        part = model_path() + ".part"
        if not os.path.isfile(part) or os.path.getsize(part) <= 0:
            return False  # fresh install: wait for the user's click
        if model_ready():
            return False
        start_download()
        return True
    except Exception:  # noqa: BLE001 - a courtesy, never fatal
        return False


def download_state():
    with _state_lock:
        _heal_state_locked()
        state = dict(_download_state)
    if _maybe_auto_resume(state):
        with _state_lock:
            state = dict(_download_state)
    return state


def _set_state(**kw):
    with _state_lock:
        _download_state.update(kw)


# ---------------------------------------------------------------------------
# Downloads with HTTP Range resume.
#
# A download streams into <dest>.part and is atomically renamed to <dest>
# only after the finished .part passes verification (exact byte count
# against the known total, plus SHA-256 where one is pinned). The .part
# lifecycle is deliberate:
#   * kept across transient failures and app restarts, so the next
#     attempt (or the next Download click) RESUMES from it via a Range
#     request instead of restarting a multi-GB file from zero;
#   * deleted only when it is provably wrong: larger than the known
#     total, or failing the final size/checksum verification.
# ---------------------------------------------------------------------------

_DOWNLOAD_ATTEMPTS = 5  # per file, resuming from the .part between tries


class _PermanentDownloadError(RuntimeError):
    """A download failure retrying cannot fix (HTTP 4xx, disk full,
    corrupt .part). Surfaces immediately instead of burning attempts."""


class _CorruptDownloadError(_PermanentDownloadError):
    """The finished .part is provably wrong (overshot the known total
    or failed its SHA-256 check) and has already been discarded.

    A subclass of _PermanentDownloadError so existing handlers treat
    it as terminal, but _download_file catches it FIRST: one bad copy
    can be a single damaged transfer, so the driver restarts clean
    exactly once before giving up. Without that distinction a user
    whose 9 GB .part is complete-but-corrupt hits an instant failure
    on every click (attempt-start re-verify), and a user whose fresh
    download fails its check has to notice and re-click by hand -
    while a hypothetical verify/retry cycle at the capped 99% would
    look exactly like the "stuck at 99%" hang this replaces."""


class _TooSlowError(RuntimeError):
    """The connection is alive but pathologically slow: bytes keep
    arriving often enough to defeat both the per-read timeout and
    the stall watchdog, yet the sustained rate makes finishing
    unrealistic. Raised out of the streaming loop so the driver can
    switch to the fallback server (resume keeps the .part, so no
    downloaded bytes are lost); with no fallback left it degrades
    to an ordinary transient failure."""


# ---------------------------------------------------------------------------
# Stall watchdog.
#
# A connection can keep the socket alive while effectively delivering
# nothing: a hung CDN edge, or a mirror dripping a few bytes every
# couple of seconds - just often enough to reset the 120 s read
# timeout, while the buffered reader waits for a full 1 MB chunk that
# never completes. The worker then blocks inside iter_content
# forever, the state claims "downloading" forever, and the UI hides
# the Download button behind a frozen progress bar (the "dead
# download" users hit after cutting the app mid-download: the wedged
# process outlives its window and every relaunch talks to it).
#
# The worker stamps every chunk it writes; if no byte has landed for
# _STALL_SECONDS the watchdog shuts the in-flight response's socket
# down - shutdown() is what actually wakes a blocked recv; a plain
# close() can leave it hanging - turning the hang into an ordinary
# transient failure that the retry loop resumes from the .part.
# ---------------------------------------------------------------------------

_STALL_SECONDS = float(os.environ.get("ITRAJI_DL_STALL_SECONDS", "120"))
_progress = {"at": 0.0, "response": None}
_progress_lock = threading.Lock()


def _mark_progress(response="keep"):
    """Stamp download progress. response: "keep" leaves the tracked
    in-flight response alone; a response object tracks it; None
    clears it (no response in flight right now)."""
    with _progress_lock:
        _progress["at"] = time.time()
        if response != "keep":
            _progress["response"] = response


def _response_socket(response):
    """Best-effort hunt for the live socket under a streaming
    requests/urllib3 response, or None.

    urllib3 2.x DETACHES the socket from the connection object once
    the response starts streaming (conn.sock reads back None), which
    silently disarmed the first version of this hunt: the watchdog
    "fired", found no socket, and fell back to response.close() -
    which can itself block behind the reading thread, wedging the
    watchdog and leaving the worker parked in recv until the 120 s
    read timeout (the exact forever-hang this exists to prevent).
    While in flight the socket lives under the response's buffered
    reader chain: raw._fp (BufferedReader) -> .raw (SocketIO) ->
    ._sock. Check every known location."""
    raw = getattr(response, "raw", None)
    if raw is None:
        return None
    for conn in (getattr(raw, "_connection", None),
                 getattr(raw, "connection", None)):
        sock = getattr(conn, "sock", None)
        if sock is not None:
            return sock
    fp = getattr(raw, "_fp", None)
    for node in (fp, getattr(fp, "raw", None), getattr(fp, "fp", None)):
        if node is None:
            continue
        for attr in ("_sock", "sock"):
            sock = getattr(node, attr, None)
            if sock is not None:
                return sock
        inner = getattr(node, "raw", None)
        sock = getattr(inner, "_sock", None)
        if sock is not None:
            return sock
    return None


def _force_close(response):
    """Interrupt a response whose read is blocked in the worker.

    The socket shutdown comes FIRST: shutdown() is what wakes a recv
    blocked in another thread. response.close() is only a cleanup
    afterwards - on its own it can block behind the reading thread,
    so it must never be the mechanism."""
    sock = _response_socket(response)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    try:
        response.close()
    except Exception:  # noqa: BLE001
        pass


def _stall_watchdog():
    """Daemon companion to the download worker: while a download is
    active, force-close a response that has delivered nothing for
    _STALL_SECONDS. Exits when the download state goes inactive."""
    while True:
        with _state_lock:
            if not _download_state["active"]:
                return
        with _progress_lock:
            resp = _progress["response"]
            idle = time.time() - _progress["at"]
        if resp is not None and idle > _STALL_SECONDS:
            _force_close(resp)
            # re-arm: the retry registers its own response and gets a
            # fresh window; without this the watchdog would spin on
            # the corpse while the worker backs off.
            _mark_progress(None)
        time.sleep(2)


# ---------------------------------------------------------------------------
# Slowness escape.
#
# The watchdog above only sees total silence. A route can instead be
# alive but useless - a throttled or far-away CDN edge dripping bytes
# every few seconds: often enough that no single read stalls for
# _STALL_SECONDS and the read timeout never trips, yet so slow that
# the last 1% of a 9 GB model (~90 MB) takes half an hour or more at
# the capped "99%" display, with no error and no mirror switch (the
# mirror only engages on host-level failure / repeated 5xx). That is
# the second way a download wedges at 99% forever.
#
# So the streaming loop also tracks sustained throughput over a
# trailing window. The floor is 25 KB/s: at that rate the 90 MB tail
# alone would take ~1 hour and the whole model ~100 hours - no route
# worth staying on is that slow for a full window, and switching
# costs nothing because resume keeps the .part. The window is 180 s
# and is only judged once the stream itself has run for a full
# window, so a slow start (connect, TLS, first byte, ramp-up) can
# never false-trigger it.
# ---------------------------------------------------------------------------

_SLOW_WINDOW = float(os.environ.get("ITRAJI_DL_SLOW_WINDOW", "180"))
_SLOW_MIN_RATE = float(os.environ.get("ITRAJI_DL_SLOW_MIN_RATE", "25000"))
_SLOW_MIN_REMAINING = 2 << 20  # almost done: let it finish, don't switch


def _raise_if_too_slow(samples, stream_t0, total, done):
    """Raise _TooSlowError when the trailing-window throughput is
    pathologically low. samples: deque of (monotonic_ts, done_bytes)
    for this response, oldest first, starting with the stream's
    first sample. The rate is measured from an ANCHOR sample - the
    newest sample at or before the window's start - because with
    coarse chunks the samples inside the window alone can span far
    less than the window and understate how long the recent bytes
    actually took."""
    now = time.monotonic()
    if now - stream_t0 < _SLOW_WINDOW:
        return  # never judge an attempt younger than one full window
    if total and total - done <= _SLOW_MIN_REMAINING:
        return  # the tail is tiny: switching would cost more than waiting
    # Trim to the anchor: drop samples while the NEXT one is still
    # at or before the window's start, leaving samples[0] as the
    # last sample taken outside the window.
    horizon = now - _SLOW_WINDOW
    while len(samples) > 1 and samples[1][0] <= horizon:
        samples.popleft()
    if len(samples) < 2:
        return
    span = now - samples[0][0]
    if span < _SLOW_WINDOW * 0.9:
        return
    rate = (done - samples[0][1]) / span
    if rate < _SLOW_MIN_RATE:
        raise _TooSlowError(
            f"download is pathologically slow "
            f"(~{int(rate)} bytes/s over {int(span)}s)")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _content_range_total(value):
    """'bytes 100-199/1234' (or 'bytes */1234') -> 1234, else None."""
    if not value:
        return None
    m = re.search(r"/(\d+)\s*$", value)
    return int(m.group(1)) if m else None


def _header_total(headers, expected_size, status, have):
    """Best known total byte count for a response, or None."""
    if expected_size:
        return expected_size
    if status == 206:
        total = _content_range_total(headers.get("Content-Range"))
        if total:
            return total
    linked = headers.get("X-Linked-Size")
    if linked:
        try:
            return int(linked)
        except ValueError:
            pass
    length = headers.get("Content-Length")
    if length:
        try:
            n = int(length)
        except ValueError:
            return None
        # On a 206 the Content-Length is only the remaining range.
        return have + n if status == 206 else n
    return None


def _checking_stage(stage):
    """The stage label shown while a complete file is being verified:
    'Downloading X' becomes 'Checking X'. Hashing ~9 GB takes real
    time, and leaving 'Downloading ... 99%' on screen for it made a
    working verification indistinguishable from a hang."""
    if stage and stage.startswith("Downloading"):
        return "Checking" + stage[len("Downloading"):]
    return "Checking the downloaded file..."


def _verify_part(tmp, total, expected_sha256, stage=None):
    """Final gate before the atomic rename. Raises
    _CorruptDownloadError (and removes the .part) when the finished
    file is provably wrong; a short file is a transient failure."""
    size = os.path.getsize(tmp)
    if total:
        if size > total:
            _remove_quietly(tmp)
            raise _CorruptDownloadError(
                f"download overshot: got {size} of {total} bytes - "
                "discarded")
        if size < total:
            raise RuntimeError(
                f"download incomplete: got {size} of {total} bytes")
    if expected_sha256:
        # The bytes are all here; what follows is verification, and
        # the state must say so (pct stays at the 99 cap: the file
        # is not usable until the check passes).
        _set_state(stage=_checking_stage(stage), pct=99)
        if _sha256_file(tmp) != expected_sha256:
            _remove_quietly(tmp)
            raise _CorruptDownloadError(
                "download failed its checksum verification - "
                "discarded")


def _download_once(url, tmp, stage, expected_size, expected_sha256):
    """One download attempt into tmp, resuming from its current size.

    Returns when tmp holds the complete, verified file. Raises
    _PermanentDownloadError for failures retrying cannot fix and any
    other exception (requests errors, short reads) for transient ones;
    in the transient case tmp keeps every byte written so far."""
    have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    _mark_progress(None)  # new attempt: no response in flight yet
    if expected_size and have > expected_size:
        _remove_quietly(tmp)  # foreign/oversized .part: start clean
        have = 0
    if expected_size and have == expected_size and have > 0:
        # A previous run finished the bytes but died before the rename.
        _verify_part(tmp, expected_size, expected_sha256, stage=stage)
        return
    _set_state(stage=stage, error=None)
    restarts = 0
    while True:
        if _pause_requested:
            raise _PausedError("download paused")
        headers = {"Range": f"bytes={have}-"} if have > 0 else {}
        with requests.get(url, headers=headers, stream=True,
                          timeout=(15, 120)) as r:
            if r.status_code == 416 and have > 0:
                # The .part already reaches (or passes) the end. A 416
                # carries the real total in Content-Range ("bytes */N");
                # its Content-Length is just the error page, ignore it.
                total = _content_range_total(
                    r.headers.get("Content-Range")) or expected_size
                if total and have == total:
                    _verify_part(tmp, total, expected_sha256, stage=stage)
                    return
                _remove_quietly(tmp)
                have = 0
                restarts += 1
                if restarts > 1:
                    raise RuntimeError(
                        "server refused the resume range twice")
                continue
            if r.status_code >= 400:
                if r.status_code in (408, 429) or r.status_code >= 500:
                    raise RuntimeError(
                        f"server error (HTTP {r.status_code})")
                raise _PermanentDownloadError(
                    f"server refused the download (HTTP {r.status_code})")
            if r.status_code == 206:
                m = re.match(r"bytes\s+(\d+)-",
                             r.headers.get("Content-Range") or "")
                if not m or int(m.group(1)) != have:
                    # Server disagrees about where the resume starts:
                    # truncate and restart this file once, cleanly.
                    _remove_quietly(tmp)
                    have = 0
                    restarts += 1
                    if restarts > 1:
                        raise RuntimeError(
                            "server sent an unusable resume range twice")
                    continue
            elif have > 0:
                have = 0  # Range ignored (200): restart from zero
            total = _header_total(r.headers, expected_size,
                                  r.status_code, have)
            if total and have > total:
                _remove_quietly(tmp)
                raise _CorruptDownloadError(
                    "partial file is larger than the download itself - "
                    "discarded")
            if total and have == total:
                _verify_part(tmp, total, expected_sha256, stage=stage)
                return
            if total:
                _set_state(pct=min(99, int(have / total * 100)))
            _mark_progress(r)  # the watchdog watches this response
            done = have
            # Sustained-throughput tracking for this response (see
            # the slowness-escape note above): samples span the
            # trailing window and are judged per chunk.
            stream_t0 = time.monotonic()
            samples = deque([(stream_t0, done)])
            stream_error = None
            try:
                with open(tmp, "ab" if have > 0 else "wb") as f:
                    # Read the raw stream in 64 KB reads, deliberately
                    # not iter_content: iter_content only yields FULL
                    # chunks, so when a connection breaks (or a server
                    # that lied about Content-Length closes) the final
                    # partial chunk is discarded inside urllib3 - up
                    # to 64 KB of received bytes thrown away per
                    # attempt, which against a length-lying,
                    # range-refusing server made every attempt end
                    # one chunk short and the download never converge
                    # (sweep2 N5). raw.read() hands over every byte
                    # actually received before it raises. 64 KB reads
                    # (not 1 MB) for the same reason as before: at
                    # 1 MB a ~5 KB/s route yields (and stamps
                    # progress) less often than the 120 s watchdog
                    # window, and throughput samples arrive too
                    # coarsely to judge; at 64 KB even that route
                    # yields every ~13 s.
                    raw = getattr(r, "raw", None)
                    while True:
                        try:
                            if raw is not None:
                                chunk = raw.read(1 << 16)
                            else:  # transport without a raw stream
                                chunk = next(r.iter_content(1 << 16),
                                             b"")
                        except Exception as e:  # noqa: BLE001
                            # Judged after the loop: a premature
                            # close raises here only AFTER every
                            # received byte has been delivered.
                            stream_error = e
                            break
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        _mark_progress()
                        if total:
                            _set_state(
                                pct=min(99, int(done / total * 100)))
                        _note_progress(tmp, done, total)
                        samples.append((time.monotonic(), done))
                        _raise_if_too_slow(samples, stream_t0,
                                           total, done)
                        if _pause_requested:
                            raise _PausedError("download paused")
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise _PermanentDownloadError(
                        f"not enough free disk space ({e})")
                raise
            if stream_error is not None:
                # A pause force-closes the response, which surfaces
                # here as a stream error - it is not one.
                if _pause_requested:
                    raise _PausedError("download paused")
                # The stream broke. If the .part nevertheless holds
                # exactly the expected byte count (the classic case:
                # Content-Length lied long, the true body arrived in
                # full, then the close raised), the download is not
                # a failure - fall through to _verify_part and let
                # the SHA-256 gate decide. Anything shorter keeps
                # the original error and the normal resume path.
                if not (expected_size and done == expected_size):
                    raise stream_error
            break
    if _pause_requested:
        raise _PausedError("download paused")
    _verify_part(tmp, total, expected_sha256, stage=stage)


def _is_host_failure(exc):
    """True when a download failure happened at the host level - the
    name would not resolve, or no connection could be established -
    as opposed to a mid-stream drop. Only host-level failures are
    worth switching download servers over: a drop resumes from the
    .part on any host, but a host that cannot even be reached will
    fail every retry the same way."""
    cur, seen = exc, set()
    while isinstance(cur, BaseException) and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        if type(cur).__name__ in ("NameResolutionError",
                                  "NewConnectionError",
                                  "ConnectTimeoutError",
                                  "ReadTimeoutError"):
            return True
        nxt = getattr(cur, "reason", None)  # urllib3 MaxRetryError
        if not isinstance(nxt, BaseException):
            nxt = cur.__cause__ or cur.__context__
        cur = nxt
    text = str(exc)
    return ("Failed to resolve" in text
            or "NameResolutionError" in text
            or "Failed to establish a new connection" in text)


def _download_file(url, dest, stage, expected_size=None,
                   expected_sha256=None, fallback_urls=()):
    """Stream url -> dest via a .part file + atomic rename, resuming
    across dropped connections and app restarts (see the section
    comment above for the .part lifecycle).

    Transient failures are retried up to _DOWNLOAD_ATTEMPTS times with
    a short backoff, each time resuming from the bytes already on disk;
    progress in _download_state counts those resumed bytes. The file is
    renamed to dest only after verification (byte count against the
    known total, SHA-256 when pinned).

    fallback_urls are mirror locations of the SAME file. When the
    current host fails at the host level (DNS / connect - see
    _is_host_failure), keeps answering 5xx, or is pathologically
    slow (see _TooSlowError), the next attempt switches to the
    mirror and resumes mid-file from the same .part: Range offsets
    are host-independent because the bytes are identical, and the
    final size + SHA-256 gate verifies the assembled file, never
    the host it came from.

    A finished file that fails verification (_CorruptDownloadError:
    the bytes are provably wrong and already discarded) restarts
    clean exactly once WITHOUT consuming the retry budget - one
    damaged transfer should not be terminal, but a second failure
    on a fresh copy means the arriving bytes keep getting damaged,
    so it stops with an honest error instead of looping at 99%."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    urls = [url] + [u for u in fallback_urls if u != url]
    which = 0
    server_errors = 0
    corrupt_restarts = 0
    attempt = 0
    while attempt < _DOWNLOAD_ATTEMPTS:
        attempt += 1
        active_stage = (stage if which == 0
                        else stage + " - backup server")
        try:
            _download_once(urls[which], tmp, active_stage,
                           expected_size, expected_sha256)
            break
        except _CorruptDownloadError as e:
            # Must precede the _PermanentDownloadError clause (it is
            # a subclass). The .part is already discarded; restart
            # from zero on the same host (the host delivered bytes
            # fine - they were damaged, not unreachable).
            corrupt_restarts += 1
            if corrupt_restarts > 1:
                raise _PermanentDownloadError(
                    "the downloaded file failed its integrity check "
                    "twice, even after one clean re-download - the "
                    "copy arriving over this connection keeps getting "
                    "damaged. Please try Download again; if it keeps "
                    "happening, try another network.") from e
            attempt -= 1  # a clean restart is not a retry
            _set_state(stage=stage + " - restarting clean download",
                       error=None, pct=0)
            time.sleep(2)
        except _PermanentDownloadError:
            raise
        except Exception as e:  # noqa: BLE001 - classified below
            if _pause_requested:
                # A pause is not a failure: no retries, no server
                # switch, no error state - the worker reports the
                # paused state itself.
                raise _PausedError("download paused") from e
            if attempt >= _DOWNLOAD_ATTEMPTS:
                if len(urls) > 1 and which > 0:
                    names = " or ".join(
                        urlparse(u).hostname or u for u in urls)
                    raise RuntimeError(
                        f"could not download from either server "
                        f"({names}): {e}") from e
                raise
            if isinstance(e, _TooSlowError):
                # Alive but useless route: switch at once when a
                # fallback remains (resume keeps the .part). With
                # no fallback it degrades to the retries below.
                if which + 1 < len(urls):
                    which += 1
                    server_errors = 0
            else:
                if str(e).startswith("server error (HTTP 5"):
                    server_errors += 1
                if which + 1 < len(urls) and (
                        _is_host_failure(e) or server_errors >= 2):
                    which += 1
                    server_errors = 0
            time.sleep(min(2 ** attempt, 16))
    os.replace(tmp, dest)
    _set_state(pct=100, rate_bps=0, eta_seconds=None)
    return dest


def _extract_engine(archive, dest_dir):
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest_dir)
    elif archive.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(dest_dir, filter="data")
    else:  # bare binary served directly (test overrides)
        import shutil
        target = os.path.join(
            dest_dir, "llama-server.exe"
            if sys.platform.startswith("win") else "llama-server")
        shutil.copyfile(archive, target)
    got = engine_path()
    if got and not sys.platform.startswith("win"):
        try:
            os.chmod(got, 0o755)
        except OSError:
            pass
    return got


def ensure_engine():
    """Download + extract llama.cpp if needed. Returns binary path."""
    got = engine_path()
    if got:
        return got
    os.makedirs(LLAMA_DIR, exist_ok=True)
    errors = []
    for url in _engine_urls():
        archive = os.path.join(LLAMA_DIR,
                               "_engine_" + url.rsplit("/", 1)[-1])
        try:
            _download_file(url, archive, "Downloading Built-in AI engine",
                           expected_sha256=_ENGINE_ARCHIVE_SHA256.get(
                               url.rsplit("/", 1)[-1]))
            got = _extract_engine(archive, LLAMA_DIR)
            try:
                os.remove(archive)
            except OSError:
                pass
            if got:
                return got
            errors.append(url + " (no llama-server binary inside)")
        except Exception as e:  # noqa: BLE001 - try the next mirror
            errors.append(f"{url} ({e})")
    raise RuntimeError("Could not download the Built-in AI engine. "
                       + "; ".join(errors)[:300])


def ensure_model():
    """Download the GGUF model if needed. Returns the model path."""
    if model_ready():
        return model_path()
    expected = None if _MODEL_OVERRIDDEN else MODEL_SIZE
    urls = _model_urls()
    _download_file(urls[0], model_path(),
                   "Downloading Built-in AI model (~9 GB)",
                   expected_size=expected,
                   expected_sha256=None if _MODEL_OVERRIDDEN
                   else MODEL_SHA256,
                   fallback_urls=urls[1:])
    if not model_ready():
        raise RuntimeError("Built-in AI model download finished but the "
                           "file failed its size check - please retry.")
    return model_path()


def _download_worker():
    try:
        ensure_engine()
        ensure_model()
        _set_state(active=False, done=True, pct=100, error=None,
                   paused=False, stage="Built-in AI ready")
    except _PausedError:
        # The .part keeps every downloaded byte; Resume continues
        # from it. Not an error, and the stage must not claim the
        # download is still running.
        with _state_lock:
            pct = _download_state["pct"]
        _set_state(active=False, done=False, paused=True, error=None,
                   rate_bps=0, eta_seconds=None,
                   stage=("Paused at %d%% - click Resume to continue"
                          % pct if pct else
                          "Paused - click Resume to continue"))
    except Exception as e:  # noqa: BLE001 - surfaced to the UI
        with _state_lock:
            pct = _download_state["pct"]
        # The stage wording must say the download STOPPED (the .part
        # is kept and the next click resumes) - a stage still reading
        # "Downloading..." after a failure is a stale label that made
        # a dead download look alive at 99%.
        stage = ("Download stopped at %d%% - click Download to resume"
                 % pct if pct else
                 "Download stopped - click Download to resume")
        _set_state(active=False, done=False, paused=False,
                   error=str(e)[:300], stage=stage)


def start_download():
    """Kick off the engine+model download in a background thread.

    Idempotent: while a download is running (or everything is already
    present) this just returns the current state."""
    global _download_thread, _pause_requested
    with _state_lock:
        _heal_state_locked()
        if _download_state["active"]:
            return dict(_download_state)
        if model_ready():
            _download_state.update(done=True, pct=100, error=None)
            return dict(_download_state)
        _pause_requested = False
        _download_state.update(active=True, done=False, error=None, pct=0,
                               paused=False, downloaded=0, total=0,
                               rate_bps=0, eta_seconds=None,
                               stage="Starting download")
        _download_thread = threading.Thread(target=_download_worker,
                                            daemon=True)
        _download_thread.start()
        _mark_progress(None)
        threading.Thread(target=_stall_watchdog, daemon=True).start()
        return dict(_download_state)


# ---------------------------------------------------------------------------
# llama-server lifecycle
# ---------------------------------------------------------------------------

_server = {"proc": None, "port": None, "log": None}
_server_lock = threading.Lock()


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _server_alive():
    proc = _server["proc"]
    return proc is not None and proc.poll() is None


def _stop_server():
    proc = _server["proc"]
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    _server["proc"] = None
    _server["port"] = None
    if _server["log"] is not None:
        try:
            _server["log"].close()
        except Exception:  # noqa: BLE001
            pass
        _server["log"] = None


atexit.register(_stop_server)


def _server_log_tail(n=1500):
    try:
        with open(os.path.join(LLAMA_DIR, "server.log"),
                  "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""


def _launch_server(binary, progress_cb=None):
    """Launch llama-server on `binary` and wait until it answers
    health checks. Returns the base URL; raises RuntimeError with
    the server's log tail when it cannot start. The GPU build gets
    all layers offloaded (-ngl 99); the CPU build's command line is
    exactly what it has always been."""
    port = _free_port()
    log = open(os.path.join(LLAMA_DIR, "server.log"), "ab")
    cmd = [binary, "-m", model_path(),
           "--host", "127.0.0.1", "--port", str(port),
           "-c", "32768", "-t", str(os.cpu_count() or 4)]
    gpu = gpu_engine_path()
    if gpu and os.path.abspath(binary) == os.path.abspath(gpu):
        cmd += ["-ngl", "99"]
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    _server.update(proc=proc, port=port, log=log)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 300
    tick = 0
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _server_log_tail()
            _stop_server()
            raise RuntimeError(
                "Built-in AI engine exited while loading the model. "
                + tail[-400:])
        try:
            r = requests.get(base + "/health", timeout=2)
            if r.status_code == 200:
                if progress_cb:
                    progress_cb(8, "Built-in AI ready")
                return base
        except requests.RequestException:
            pass
        tick += 1
        if progress_cb and tick % 5 == 0:
            progress_cb(5, "Starting Built-in AI "
                           "(loading the model into memory)")
        time.sleep(1)
    _stop_server()
    raise RuntimeError("Built-in AI engine did not become ready in "
                       "5 minutes. " + _server_log_tail()[-400:])


def ensure_server(progress_cb=None):
    """Start llama-server (once) and wait until it answers health checks.

    Returns the base URL. Raises RuntimeError with a plain message when
    the model is missing or the server cannot start. When the GPU
    engine is the active flavour but fails to launch, the session
    falls back to the CPU engine (recorded in gpu_fallback_note())
    instead of leaving the user with a dead AI."""
    if not model_ready():
        raise RuntimeError(
            "Built-in AI model is not downloaded yet. In Step 2 pick "
            "'Built-in AI (no key)' and click 'Download Built-in AI' "
            "(one-time download, ~9 GB), then try again.")
    with _server_lock:
        if _server_alive():
            return f"http://127.0.0.1:{_server['port']}"
        binary = ensure_engine()
        try:
            return _launch_server(binary, progress_cb)
        except RuntimeError:
            gpu = gpu_engine_path()
            if not gpu or os.path.abspath(binary) != \
                    os.path.abspath(gpu):
                raise
            _mark_gpu_fallback(
                "the GPU engine failed to start - the CPU engine "
                "is being used instead")
            return _launch_server(ensure_engine(), progress_cb)


# ---------------------------------------------------------------------------
# Chat + JSON extraction
# ---------------------------------------------------------------------------

def chat(messages, progress_cb=None):
    """One chat-completion round-trip against the local server.

    Returns the assistant's reply text. Raises RuntimeError on any
    failure - callers must surface it, never invent a fallback."""
    base = ensure_server(progress_cb)
    try:
        r = requests.post(
            base + "/v1/chat/completions",
            json={"model": MODEL_FILE, "messages": messages,
                  "temperature": 0.3, "max_tokens": 4096},
            timeout=(10, 1800))
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except requests.HTTPError as e:
        raise RuntimeError(f"Built-in AI server error: {e}") from e
    except (requests.RequestException, KeyError, IndexError,
            TypeError, ValueError) as e:
        raise RuntimeError(f"Built-in AI request failed: {e}") from e


def extract_json(text):
    """Extract the first balanced JSON object from a model reply.

    Tolerates surrounding prose and markdown fences. Raises ValueError
    when no complete JSON object is present."""
    if not text:
        raise ValueError("empty model reply")
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in model reply")
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(t[start:i + 1])
    raise ValueError("unbalanced JSON in model reply")
