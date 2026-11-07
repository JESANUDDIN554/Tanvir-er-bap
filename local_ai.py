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


def model_path():
    return os.path.join(MODELS_DIR, MODEL_FILE)


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


def engine_path():
    """Path of the extracted llama-server binary, or None."""
    if not os.path.isdir(LLAMA_DIR):
        return None
    want = "llama-server.exe" if sys.platform.startswith("win") \
        else "llama-server"
    for root, _dirs, files in os.walk(LLAMA_DIR):
        if want in files:
            return os.path.join(root, want)
    return None


def model_ready():
    if not engine_path():
        return False
    p = model_path()
    if not os.path.isfile(p):
        return False
    if not _MODEL_OVERRIDDEN:
        return os.path.getsize(p) == MODEL_SIZE
    return os.path.getsize(p) > 1_000_000


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
# Download state (shared with the UI via app.py endpoints)
# ---------------------------------------------------------------------------

_download_state = {"active": False, "pct": 0, "error": None,
                   "done": False, "stage": ""}
_state_lock = threading.Lock()
_download_thread = None


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


def download_state():
    with _state_lock:
        _heal_state_locked()
        return dict(_download_state)


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


def _verify_part(tmp, total, expected_sha256):
    """Final gate before the atomic rename. Raises
    _PermanentDownloadError (and removes the .part) when the finished
    file is provably wrong; a short file is a transient failure."""
    size = os.path.getsize(tmp)
    if total:
        if size > total:
            _remove_quietly(tmp)
            raise _PermanentDownloadError(
                f"download overshot: got {size} of {total} bytes - "
                "discarded, please retry")
        if size < total:
            raise RuntimeError(
                f"download incomplete: got {size} of {total} bytes")
    if expected_sha256 and _sha256_file(tmp) != expected_sha256:
        _remove_quietly(tmp)
        raise _PermanentDownloadError(
            "download failed its checksum verification - "
            "discarded, please retry")


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
        _verify_part(tmp, expected_size, expected_sha256)
        return
    _set_state(stage=stage, error=None)
    restarts = 0
    while True:
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
                    _verify_part(tmp, total, expected_sha256)
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
                raise _PermanentDownloadError(
                    "partial file is larger than the download itself - "
                    "discarded, please retry")
            if total and have == total:
                _verify_part(tmp, total, expected_sha256)
                return
            if total:
                _set_state(pct=min(99, int(have / total * 100)))
            _mark_progress(r)  # the watchdog watches this response
            done = have
            try:
                with open(tmp, "ab" if have > 0 else "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        _mark_progress()
                        if total:
                            _set_state(
                                pct=min(99, int(done / total * 100)))
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise _PermanentDownloadError(
                        f"not enough free disk space ({e})")
                raise
            break
    _verify_part(tmp, total, expected_sha256)


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
    _is_host_failure) or keeps answering 5xx, the next attempt
    switches to the mirror and resumes mid-file from the same .part:
    Range offsets are host-independent because the bytes are
    identical, and the final size + SHA-256 gate verifies the
    assembled file, never the host it came from."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    urls = [url] + [u for u in fallback_urls if u != url]
    which = 0
    server_errors = 0
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        active_stage = (stage if which == 0
                        else stage + " - backup server")
        try:
            _download_once(urls[which], tmp, active_stage,
                           expected_size, expected_sha256)
            break
        except _PermanentDownloadError:
            raise
        except Exception as e:  # noqa: BLE001 - classified below
            if attempt >= _DOWNLOAD_ATTEMPTS:
                if len(urls) > 1 and which > 0:
                    names = " or ".join(
                        urlparse(u).hostname or u for u in urls)
                    raise RuntimeError(
                        f"could not download from either server "
                        f"({names}): {e}") from e
                raise
            if str(e).startswith("server error (HTTP 5"):
                server_errors += 1
            if which + 1 < len(urls) and (
                    _is_host_failure(e) or server_errors >= 2):
                which += 1
                server_errors = 0
            time.sleep(min(2 ** attempt, 16))
    os.replace(tmp, dest)
    _set_state(pct=100)
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
            _download_file(url, archive, "Downloading Built-in AI engine")
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
                   stage="Built-in AI ready")
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
        _set_state(active=False, done=False,
                   error=str(e)[:300], stage=stage)


def start_download():
    """Kick off the engine+model download in a background thread.

    Idempotent: while a download is running (or everything is already
    present) this just returns the current state."""
    global _download_thread
    with _state_lock:
        _heal_state_locked()
        if _download_state["active"]:
            return dict(_download_state)
        if model_ready():
            _download_state.update(done=True, pct=100, error=None)
            return dict(_download_state)
        _download_state.update(active=True, done=False, error=None, pct=0,
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


def ensure_server(progress_cb=None):
    """Start llama-server (once) and wait until it answers health checks.

    Returns the base URL. Raises RuntimeError with a plain message when
    the model is missing or the server cannot start."""
    if not model_ready():
        raise RuntimeError(
            "Built-in AI model is not downloaded yet. In Step 2 pick "
            "'Built-in AI (no key)' and click 'Download Built-in AI' "
            "(one-time download, ~9 GB), then try again.")
    with _server_lock:
        if _server_alive():
            return f"http://127.0.0.1:{_server['port']}"
        binary = ensure_engine()
        port = _free_port()
        log = open(os.path.join(LLAMA_DIR, "server.log"), "ab")
        cmd = [binary, "-m", model_path(),
               "--host", "127.0.0.1", "--port", str(port),
               "-c", "32768", "-t", str(os.cpu_count() or 4)]
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
