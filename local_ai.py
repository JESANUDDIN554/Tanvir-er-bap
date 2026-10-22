"""Built-in AI - local LLM engine for Itraji Cliper.

Runs a professional open-weights model (Qwen2.5-14B-Instruct, Q4_K_M
GGUF) entirely on this machine through llama.cpp's llama-server.
No key and no internet are needed once the one-time downloads
(engine ~24 MB, model ~9 GB) have completed.

Downloads land in the app's data area (mirrors app.py's BASE_DIR
resolution):  <data>/llama/  for the engine,  <data>/models/  for the
GGUF. Test hooks: ITRAJI_LOCAL_MODEL_URL / ITRAJI_LOCAL_MODEL_FILE /
ITRAJI_LLAMA_URL override the model/engine downloads, and
ITRAJI_LOCAL_DATA_DIR overrides the data area.
"""

import atexit
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
_MODEL_OVERRIDDEN = bool(os.environ.get("ITRAJI_LOCAL_MODEL_URL")
                         or os.environ.get("ITRAJI_LOCAL_MODEL_FILE"))


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


def download_state():
    with _state_lock:
        return dict(_download_state)


def _set_state(**kw):
    with _state_lock:
        _download_state.update(kw)


def _download_file(url, dest, stage, expected_size=None):
    """Stream url -> dest via a .part file + atomic rename.

    Updates _download_state pct/stage as bytes arrive and verifies the
    final size when it is knowable (explicit arg, pinned model size,
    or the response's own Content-Length)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            total = expected_size \
                or int(r.headers.get("Content-Length") or 0) or None
            done = 0
            _set_state(stage=stage, pct=0, error=None)
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        _set_state(pct=min(99, int(done / total * 100)))
        if total and done != total:
            raise RuntimeError(
                f"download incomplete: got {done} of {total} bytes")
        os.replace(tmp, dest)
        _set_state(pct=100)
        return dest
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
    _download_file(MODEL_URL, model_path(),
                   "Downloading Built-in AI model (~9 GB)",
                   expected_size=expected)
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
        _set_state(active=False, done=False,
                   error=str(e)[:300], stage="Download failed")


def start_download():
    """Kick off the engine+model download in a background thread.

    Idempotent: while a download is running (or everything is already
    present) this just returns the current state."""
    global _download_thread
    with _state_lock:
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
