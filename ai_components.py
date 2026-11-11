"""AI Installation Manager backend for Itraji Cliper.

One manifest describes every AI component the app needs; one set of
functions reports each component's state, checks the machine at
startup, verifies files against pinned hashes, repairs what is
broken, and applies model updates with rollback. The download
machinery itself (resume, SHA-256 gates, mirror fallback, stall
watchdog, slowness escape) lives in local_ai.py and is NOT
reimplemented here - this module is the management, detection and
hardware layer on top of that proven core.

Components:
  brain       the Qwen2.5-14B GGUF (local_ai.MODEL_* pins)
  runtime     the llama.cpp CPU engine (local_ai._LLAMA_TAG pins)
  runtime_gpu the llama.cpp CUDA engine, Windows + NVIDIA only
  speech      the faster-whisper "base" model (pipeline.py loads it
              from the Hugging Face hub cache at first transcription)
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

import local_ai

# ---------------------------------------------------------------------------
# Manifest - the single source of truth
# ---------------------------------------------------------------------------

BRAIN_VERSION = "1.0.0"
SPEECH_MODEL_NAME = "base"
SPEECH_VERSION = "base"
# Systran/faster-whisper-base, snapshot ebe41f70: model.bin is
# exactly this many bytes; anything far smaller is a broken cache.
SPEECH_MODEL_BIN_SIZE = 145217532
SPEECH_MIN_SANE_SIZE = 100_000_000
ENGINE_VERSION = local_ai._LLAMA_TAG
GPU_ENGINE_VERSION = (local_ai._LLAMA_TAG + "+cuda"
                      + local_ai._GPU_CUDA_VARIANT)

# Pinned file hashes for the extracted engines (computed from the
# official b11803 release assets). llama-server is a thin launcher
# on Windows - the real code sits in llama-server-impl.dll - so the
# manifest pins the files that carry the code, not just the stub.
_WIN_SERVER_FILES = {
    "llama-server.exe":
        "0d97f4b3c296ed190a703b6031bdbea220380e69daff8f1e8d11e34"
        "da46b3067",
    "llama-server-impl.dll":
        "618239c083c13a41eb8124aa639c22b12e02eec529eee4dd5e61be"
        "a8ffa595a0",
}
_RUNTIME_FILES = (_WIN_SERVER_FILES if sys.platform.startswith("win")
                  else {
    "llama-server":
        "750e13aba6d7929c85ea0e7a41e68fb03187ae875884b64a145aad"
        "405fb7ca3a",
    "libllama-common.so.0.7.0":
        "bd224ad71e4ca0b128e8b210357b638e65adf27913fb5b02da0c787"
        "b92648274",
})
_GPU_FILES = dict(_WIN_SERVER_FILES)
_GPU_FILES["ggml-cuda.dll"] = (
    "a84897a4f5e713a0e2162e6f1a6ad5e9e3dc8e64f160ba7242ffe45d8c1"
    "509ac")


def _engine_archive_entry():
    name = local_ai._engine_asset_name(local_ai._LLAMA_TAG)
    return {"archive_url": f"{local_ai._LLAMA_BASE}/"
                           f"{local_ai._LLAMA_TAG}/{name}",
            "archive_size": (24069022 if sys.platform.startswith("win")
                             else 22331049),
            "archive_sha256":
                local_ai._ENGINE_ARCHIVE_SHA256.get(name)}


def _manifest():
    brain = {
        "id": "brain",
        "name": "AI Brain - Qwen2.5-14B-Instruct",
        "detail": "The language model that finds and scores clips",
        "version": BRAIN_VERSION,
        "size": local_ai.MODEL_SIZE,
        "sha256": local_ai.MODEL_SHA256,
        "urls": local_ai._model_urls(),
    }
    runtime = {
        "id": "runtime",
        "name": "AI Runtime - llama.cpp (CPU)",
        "detail": "The engine that runs the AI Brain on this PC",
        "version": ENGINE_VERSION,
        "size": (24069022 if sys.platform.startswith("win")
                 else 22331049),
        "files": _RUNTIME_FILES,
    }
    runtime.update(_engine_archive_entry())
    runtime_gpu = {
        "id": "runtime_gpu",
        "name": "AI Runtime - GPU (CUDA)",
        "detail": "The engine build that runs the AI Brain on an "
                  "NVIDIA graphics card",
        "version": GPU_ENGINE_VERSION,
        "size": local_ai._GPU_ENGINE_SIZE + local_ai._GPU_CUDART_SIZE,
        "files": _GPU_FILES,
    }
    speech = {
        "id": "speech",
        "name": "Speech Model - Whisper base",
        "detail": "Turns speech into the transcript every clip is "
                  "built from",
        "version": SPEECH_VERSION,
        "size": SPEECH_MODEL_BIN_SIZE,
        "files": {},
    }
    return [brain, runtime, runtime_gpu, speech]


MANIFEST = _manifest()


def manifest_entry(component_id, manifest=None):
    for e in (manifest or MANIFEST):
        if e["id"] == component_id:
            return e
    return None


# ---------------------------------------------------------------------------
# Install record: which versions THIS machine has, written after a
# verified install/update. Installations that predate the record
# (e.g. the Full installer) report the manifest version when their
# files check out - the record only ever overrides that after an
# in-app update lands a different version.
# ---------------------------------------------------------------------------

_RECORD_PATH = os.path.join(local_ai.DATA_DIR, "ai_install.json")


def _record():
    try:
        with open(_RECORD_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_record(rec):
    try:
        with open(_RECORD_PATH, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except OSError:
        pass


def _installed_version(component_id, manifest_version):
    rec = _record().get("versions") or {}
    return rec.get(component_id) or manifest_version


def _record_version(component_id, version):
    rec = _record()
    versions = rec.get("versions") or {}
    versions[component_id] = version
    rec["versions"] = versions
    rec["updated_at"] = time.time()
    _save_record(rec)


# ---------------------------------------------------------------------------
# Hardware detection (degrades gracefully everywhere: a missing
# nvidia-smi, an unknown OS hook, a failed probe - all become
# "not detected", never an exception)
# ---------------------------------------------------------------------------

def _ram_bytes():
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual",
                             ctypes.c_ulonglong)]

            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(ms)):
                return int(ms.ullTotalPhys)
            return 0
        return int(os.sysconf("SC_PAGE_SIZE")) * \
            int(os.sysconf("SC_PHYS_PAGES"))
    except Exception:  # noqa: BLE001 - detection must never break
        return 0


def _disk_free(path):
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def _detect_gpu():
    """The NVIDIA GPU via nvidia-smi (ships with the driver on
    Windows, standard on Linux CUDA boxes), or None."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return None
        for line in (r.stdout or "").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0]:
                vram = 0
                try:
                    vram = int(float(parts[1])) * 1024 * 1024
                except ValueError:
                    pass
                return {"vendor": "NVIDIA", "name": parts[0],
                        "vram_bytes": vram,
                        "driver": parts[2] if len(parts) > 2 else ""}
    except Exception:  # noqa: BLE001 - no nvidia-smi is an answer too
        pass
    return None


_hw_cache = {"at": 0.0, "data": None}
_HW_TTL = 60.0


def hardware_info():
    now = time.time()
    if _hw_cache["data"] is not None and \
            now - _hw_cache["at"] < _HW_TTL:
        return _hw_cache["data"]
    data = {"gpu": _detect_gpu(),
            "ram_bytes": _ram_bytes(),
            "cpu_cores": os.cpu_count() or 0,
            "disk_free_bytes": _disk_free(local_ai.DATA_DIR),
            "platform": sys.platform}
    _hw_cache.update(at=now, data=data)
    return data


def gpu_candidate(hw=None):
    """(True, "") when the GPU engine is worth installing on this
    machine, else (False, plain reason)."""
    hw = hw or hardware_info()
    gpu = hw.get("gpu")
    if not gpu:
        return False, "no NVIDIA graphics card detected"
    if not sys.platform.startswith("win") and \
            not os.environ.get("ITRAJI_LLAMA_GPU_URL"):
        return False, "the GPU engine build is Windows-only"
    if (gpu.get("vram_bytes") or 0) < 6 * 1024 ** 3:
        return False, ("the graphics card has less than 6 GB of "
                       "memory - the CPU engine fits it better")
    return True, ""


# ---------------------------------------------------------------------------
# Component status
# ---------------------------------------------------------------------------

# Components a Verify run proved corrupt (hash mismatch). Cleared by
# a passing verify, a repair, or a successful update/install.
_marks = {}
_marks_lock = threading.Lock()


def _mark(component_id, bad=True):
    with _marks_lock:
        if bad:
            _marks[component_id] = "corrupt"
        else:
            _marks.pop(component_id, None)


def _marked(component_id):
    with _marks_lock:
        return _marks.get(component_id)


def _download_component():
    """Which component the shared download state is working on
    right now (from its stage label), and whether it is in the
    post-download verification step."""
    st = local_ai.download_state()
    if not st.get("active"):
        return None, False
    stage = st.get("stage") or ""
    verifying = stage.startswith("Checking")
    if "GPU" in stage or "CUDA" in stage:
        return "runtime_gpu", verifying
    if "engine" in stage.lower():
        return "runtime", verifying
    return "brain", verifying


_speech_warm = {"active": False, "error": None, "done": False}
_speech_lock = threading.Lock()


def _speech_cache_dir():
    try:
        import pipeline
        return pipeline._whisper_cache_dir(SPEECH_MODEL_NAME)
    except Exception:  # noqa: BLE001 - same fallback pipeline uses
        home = os.path.expanduser("~")
        return os.path.join(home, ".cache", "huggingface", "hub",
                            "models--Systran--faster-whisper-base")


def _speech_model_bin():
    d = _speech_cache_dir()
    if not d or not os.path.isdir(d):
        return None
    for root, _dirs, files in os.walk(d):
        if "model.bin" in files:
            return os.path.join(root, "model.bin")
    return None


def _base_comp(entry):
    return {"id": entry["id"], "name": entry["name"],
            "detail": entry["detail"], "version": entry["version"],
            "size": entry["size"], "applicable": True,
            "status": "missing", "healthy": False,
            "installed_version": None, "installed_size": 0,
            "note": ""}


def _apply_record_version(c, entry):
    """Version bookkeeping is separate from file health: when the
    install record names a version for a component whose file is
    on disk, that is the installed version - even if the file
    itself is currently damaged (the status says that part)."""
    if c["installed_version"] is None and c["installed_size"]:
        rec_v = (_record().get("versions") or {}).get(entry["id"])
        if rec_v:
            c["installed_version"] = rec_v


def _apply_update_flag(comp, entry):
    """An installed component whose recorded version differs from
    the manifest has an update available."""
    if comp["status"] == "installed" and \
            comp["installed_version"] != entry["version"]:
        comp["status"] = "update_available"
        comp["note"] = ("version %s is available (installed: %s)"
                        % (entry["version"], comp["installed_version"]))


def _brain_comp(entry, dl_id, dl_verifying):
    c = _base_comp(entry)
    path = local_ai.model_path()
    c["path"] = path
    existing = local_ai.existing_model_path()
    if dl_id == "brain":
        c["status"] = "verifying" if dl_verifying else "downloading"
        return c
    if existing:
        c["installed_size"] = os.path.getsize(existing)
        if _marked("brain"):
            c["status"] = "corrupt"
            c["note"] = ("the model file failed its SHA-256 check - "
                         "Repair downloads a clean copy")
        else:
            c["status"] = "installed"
            c["healthy"] = True
            c["installed_version"] = _installed_version(
                "brain", entry["version"])
    elif os.path.isfile(path):
        c["status"] = "corrupt"
        c["installed_size"] = os.path.getsize(path)
        c["note"] = ("the model file is %d bytes but must be exactly "
                     "%d - it is damaged or incomplete"
                     % (c["installed_size"], entry["size"]))
    else:
        part = path + ".part"
        if os.path.isfile(part) and os.path.getsize(part) > 0:
            c["note"] = ("%d of %d bytes already downloaded - the "
                         "download resumes where it stopped"
                         % (os.path.getsize(part), entry["size"]))
    _apply_record_version(c, entry)
    _apply_update_flag(c, entry)
    return c


def _runtime_comp(entry, dl_id, dl_verifying):
    c = _base_comp(entry)
    p = local_ai.cpu_engine_path()
    c["path"] = p or local_ai.LLAMA_DIR
    if dl_id == "runtime":
        c["status"] = "verifying" if dl_verifying else "downloading"
        return c
    if p:
        c["installed_size"] = os.path.getsize(p)
        if _marked("runtime"):
            c["status"] = "corrupt"
            c["note"] = ("an engine file failed its checksum - "
                         "Repair downloads a clean copy")
        else:
            c["status"] = "installed"
            c["healthy"] = True
            c["installed_version"] = _installed_version(
                "runtime", entry["version"])
    _apply_record_version(c, entry)
    _apply_update_flag(c, entry)
    return c


def _gpu_comp(entry, dl_id, dl_verifying, hw):
    c = _base_comp(entry)
    ok_gpu, why = gpu_candidate(hw)
    installed = local_ai.gpu_engine_path()
    c["applicable"] = bool(ok_gpu or installed)
    c["path"] = installed or local_ai.LLAMA_GPU_DIR
    if not c["applicable"]:
        c["note"] = why
        return c
    if dl_id == "runtime_gpu":
        c["status"] = "verifying" if dl_verifying else "downloading"
        return c
    if installed:
        c["installed_size"] = os.path.getsize(installed)
        if _marked("runtime_gpu"):
            c["status"] = "corrupt"
            c["note"] = ("a GPU engine file failed its checksum - "
                         "Repair downloads a clean copy")
        else:
            c["status"] = "installed"
            c["healthy"] = True
            c["installed_version"] = _installed_version(
                "runtime_gpu", entry["version"])
    elif not ok_gpu:
        c["note"] = why
    _apply_record_version(c, entry)
    _apply_update_flag(c, entry)
    return c


def _speech_comp(entry, dl_id, dl_verifying):
    c = _base_comp(entry)
    b = _speech_model_bin()
    c["path"] = b or (_speech_cache_dir() or "")
    with _speech_lock:
        warming = _speech_warm["active"]
    if warming:
        c["status"] = "downloading"
        c["note"] = "downloading the speech model"
        return c
    if b:
        size = os.path.getsize(b)
        c["installed_size"] = size
        if size == SPEECH_MODEL_BIN_SIZE or \
                size >= SPEECH_MIN_SANE_SIZE:
            c["status"] = "installed"
            c["healthy"] = True
            c["installed_version"] = _installed_version(
                "speech", entry["version"])
            if size != SPEECH_MODEL_BIN_SIZE:
                c["note"] = ("model.bin is %d bytes (expected %d) - "
                             "close enough to be a healthy copy"
                             % (size, SPEECH_MODEL_BIN_SIZE))
        else:
            c["status"] = "corrupt"
            c["note"] = ("the speech model is only %d bytes - its "
                         "download was cut short; Repair fetches a "
                         "clean copy" % size)
    _apply_record_version(c, entry)
    _apply_update_flag(c, entry)
    return c


def components(hw=None):
    hw = hw or hardware_info()
    dl_id, dl_verifying = _download_component()
    out = []
    for e in MANIFEST:
        if e["id"] == "brain":
            out.append(_brain_comp(e, dl_id, dl_verifying))
        elif e["id"] == "runtime":
            out.append(_runtime_comp(e, dl_id, dl_verifying))
        elif e["id"] == "runtime_gpu":
            out.append(_gpu_comp(e, dl_id, dl_verifying, hw))
        elif e["id"] == "speech":
            out.append(_speech_comp(e, dl_id, dl_verifying))
    return out


# ---------------------------------------------------------------------------
# Startup health check (fast: file stats + system counters only -
# the 9 GB hash belongs to Verify, never to boot)
# ---------------------------------------------------------------------------

_health_cache = {"at": 0.0, "data": None}
_health_lock = threading.Lock()
_HEALTH_TTL = 30.0


def _disk_needed(comps):
    return sum(c["size"] for c in comps
               if c["applicable"] and not c["healthy"])


def ai_health(force=False):
    now = time.time()
    with _health_lock:
        if not force and _health_cache["data"] is not None and \
                now - _health_cache["at"] < _HEALTH_TTL:
            return _health_cache["data"]
    hw = hardware_info()
    comps = components(hw)
    issues = []
    for c in comps:
        if not c["applicable"] or c["healthy"]:
            continue
        if c["status"] == "downloading" or c["status"] == "verifying":
            continue
        fix = "repair" if c["status"] == "corrupt" else "install"
        problem = c["note"] or ("not installed" if
                                c["status"] == "missing" else
                                c["status"])
        issues.append({"component": c["id"], "level": "error",
                       "problem": "%s: %s" % (c["name"], problem),
                       "fix": fix})
    needed = _disk_needed(comps)
    free = hw["disk_free_bytes"]
    if needed and free < needed + 2 * 1024 ** 3:
        issues.append({
            "component": "disk", "level": "error",
            "problem": ("not enough free space: %.1f GB free, "
                        "%.1f GB needed plus 2 GB headroom"
                        % (free / 1e9, needed / 1e9)),
            "fix": "install"})
    ram = hw["ram_bytes"]
    if ram and ram < 8 * 1024 ** 3:
        issues.append({
            "component": "memory", "level": "warning",
            "problem": ("this PC has %.1f GB of memory - the AI "
                        "Brain needs about 10 GB free to load "
                        "comfortably, so it may be slow or fail to "
                        "start" % (ram / 1e9)),
            "fix": "none"})
    data = {"ok": not any(i["level"] == "error" for i in issues),
            "issues": issues,
            "disk_free_bytes": free,
            "disk_needed_bytes": needed,
            "ram_bytes": ram,
            "gpu": hw["gpu"],
            "checked_at": now}
    with _health_lock:
        _health_cache.update(at=now, data=data)
    return data


def prime_health():
    """Warm the health cache on a daemon thread at app start, so
    the first Manager open is instant and boot never blocks."""
    def _t():
        try:
            ai_health(force=True)
        except Exception:  # noqa: BLE001 - a cache warmer only
            pass
    threading.Thread(target=_t, daemon=True).start()


# ---------------------------------------------------------------------------
# Verify AI Files: full SHA-256 of the model, pinned-file hashes of
# the engines, speech model size - on a background thread, with a
# per-component verdict. A failed hash marks the component corrupt
# until a repair / passing verify clears it.
# ---------------------------------------------------------------------------

_verify = {"active": False, "done": False, "error": None, "at": 0.0,
           "current": "", "results": {}}
_verify_lock = threading.Lock()
_verify_thread = None


def verify_state():
    with _verify_lock:
        return dict(_verify)


def _set_verify(**kw):
    with _verify_lock:
        _verify.update(kw)


def _find_named(base, name):
    if not base or not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        if name in files:
            return os.path.join(root, name)
    return None


def _verify_pinned_files(base, pins):
    """(ok, detail) for a set of {filename: sha256} under base."""
    for name, want in pins.items():
        p = _find_named(base, name)
        if not p:
            return False, "%s is missing" % name
        if local_ai._sha256_file(p) != want:
            return False, "%s failed its SHA-256 check" % name
    return True, "every pinned file matches its SHA-256"


def _verify_brain():
    p = local_ai.existing_model_path() or local_ai.model_path()
    if not os.path.isfile(p):
        return False, "the model file is not installed"
    if local_ai._MODEL_OVERRIDDEN:
        if local_ai._file_complete(p):
            return True, ("custom model source: size check passed "
                          "(no pinned checksum for a custom source)")
        return False, "the model file is incomplete"
    got = local_ai._sha256_file(p)
    if got == local_ai.MODEL_SHA256:
        return True, "SHA-256 matches the pinned model checksum"
    return False, ("SHA-256 mismatch - the model file is damaged "
                   "(expected %s..., got %s...)"
                   % (local_ai.MODEL_SHA256[:12], got[:12]))


def _verify_engine_dir(base, pins, label):
    if not base or not os.path.isdir(base):
        return False, "%s is not installed" % label
    if pins:
        return _verify_pinned_files(base, pins)
    return False, "no pinned hashes for this platform"


def _verify_speech():
    b = _speech_model_bin()
    if not b:
        return False, "the speech model is not downloaded"
    size = os.path.getsize(b)
    if size == SPEECH_MODEL_BIN_SIZE:
        return True, "model.bin is exactly the pinned size"
    if size >= SPEECH_MIN_SANE_SIZE:
        return True, ("model.bin is %d bytes - a healthy copy"
                      % size)
    return False, ("model.bin is only %d bytes - the download was "
                   "cut short" % size)


def _verify_worker():
    results = {}
    try:
        _set_verify(current="brain")
        ok, detail = _verify_brain()
        results["brain"] = {"ok": ok, "detail": detail}
        _mark("brain", bad=not ok and os.path.isfile(
            local_ai.model_path()))
        _set_verify(current="runtime")
        ok, detail = _verify_engine_dir(local_ai.LLAMA_DIR,
                                        _RUNTIME_FILES, "the CPU engine")
        results["runtime"] = {"ok": ok, "detail": detail}
        _mark("runtime", bad=not ok and
              bool(local_ai.cpu_engine_path()))
        _set_verify(current="runtime_gpu")
        if local_ai.gpu_engine_path():
            ok, detail = _verify_engine_dir(local_ai.LLAMA_GPU_DIR,
                                            _GPU_FILES, "the GPU engine")
            results["runtime_gpu"] = {"ok": ok, "detail": detail}
            _mark("runtime_gpu", bad=not ok)
        _set_verify(current="speech")
        ok, detail = _verify_speech()
        results["speech"] = {"ok": ok, "detail": detail}
        _mark("speech", bad=not ok and bool(_speech_model_bin()))
        _set_verify(active=False, done=True, error=None, current="",
                    results=results, at=time.time())
    except Exception as e:  # noqa: BLE001 - surfaced, never silent
        _set_verify(active=False, done=True, error=str(e)[:300],
                    current="")


def start_verify():
    global _verify_thread
    with _verify_lock:
        if _verify["active"]:
            return False
        if local_ai.download_state().get("active"):
            return False
        _verify.update(active=True, done=False, error=None,
                       results={}, current="")
    _verify_thread = threading.Thread(target=_verify_worker,
                                      daemon=True)
    _verify_thread.start()
    return True


# ---------------------------------------------------------------------------
# Diagnostic: the engine actually launches, the brain actually
# answers, the speech model actually loads. On demand (Manager
# button) and automatically at the end of an install.
# ---------------------------------------------------------------------------

_diag = {"active": False, "done": False, "at": 0.0, "passed": False,
         "items": []}
_diag_lock = threading.Lock()
_diag_thread = None


def diagnostic_state():
    with _diag_lock:
        return dict(_diag)


def _set_diag(**kw):
    with _diag_lock:
        _diag.update(kw)


def _diag_worker():
    items = []

    def add(cid, label, status, detail):
        items.append({"id": cid, "label": label, "status": status,
                      "detail": detail})
        _set_diag(items=list(items))

    binary = local_ai.engine_path()
    if not binary:
        add("runtime", "AI runtime launches", "fail",
            "the AI runtime is not installed")
    else:
        try:
            r = subprocess.run([binary, "--version"],
                               capture_output=True, text=True,
                               timeout=25,
                               cwd=os.path.dirname(binary))
            first = ((r.stdout or r.stderr or "").strip()
                     .splitlines() or [""])[0]
            if r.returncode == 0:
                add("runtime", "AI runtime launches", "pass",
                    first[:120] or "the engine runs")
            else:
                add("runtime", "AI runtime launches", "fail",
                    "the engine exited with code %d" % r.returncode)
        except Exception as e:  # noqa: BLE001
            add("runtime", "AI runtime launches", "fail",
                "the engine could not be run: %s" % str(e)[:160])

    if local_ai.model_ready():
        try:
            reply = local_ai.chat(
                [{"role": "user",
                  "content": "Reply with exactly one word: ready"}])
            local_ai._stop_server()
            if reply and reply.strip():
                add("brain", "AI Brain answers", "pass",
                    "the model replied: %s"
                    % reply.strip()[:60])
            else:
                add("brain", "AI Brain answers", "fail",
                    "the model returned an empty reply")
        except Exception as e:  # noqa: BLE001
            add("brain", "AI Brain answers", "fail", str(e)[:200])
    else:
        add("brain", "AI Brain answers", "skip",
            "the AI Brain is not installed yet")

    try:
        import pipeline
        model = pipeline._load_whisper_model(SPEECH_MODEL_NAME)
        del model
        add("speech", "Speech model loads", "pass",
            "Whisper %s is ready" % SPEECH_MODEL_NAME)
    except Exception as e:  # noqa: BLE001
        add("speech", "Speech model loads", "fail", str(e)[:200])

    free = _disk_free(local_ai.DATA_DIR)
    if free >= 2 * 1024 ** 3:
        add("disk", "Free disk space", "pass",
            "%.1f GB free" % (free / 1e9))
    else:
        add("disk", "Free disk space", "fail",
            "only %.1f GB free" % (free / 1e9))

    passed = all(i["status"] != "fail" for i in items) and \
        any(i["status"] == "pass" for i in items)
    _set_diag(active=False, done=True, passed=passed, items=items,
              at=time.time())


def start_diagnostic():
    global _diag_thread
    with _diag_lock:
        if _diag["active"]:
            return False
        _diag.update(active=True, done=False, passed=False, items=[])
    _diag_thread = threading.Thread(target=_diag_worker, daemon=True)
    _diag_thread.start()
    return True


# ---------------------------------------------------------------------------
# Updates. check_update compares the manifest against what is
# installed; the apply path NEVER overwrites in place: the new file
# is downloaded to a .new name, verified, and only then swapped in
# with the old file kept as .bak until the swap itself verifies.
# Any failure restores the old file byte-for-byte.
# ---------------------------------------------------------------------------

_update = {"active": False, "done": False, "error": None,
           "component": "", "at": 0.0}
_update_lock = threading.Lock()
_update_thread = None


def update_state():
    with _update_lock:
        return dict(_update)


def _set_update(**kw):
    with _update_lock:
        _update.update(kw)


def check_update(manifest=None):
    manifest = manifest or MANIFEST
    out = []
    for e in manifest:
        cur = None
        for c in components():
            if c["id"] == e["id"]:
                cur = c
                break
        inst = cur["installed_version"] if cur else None
        out.append({"id": e["id"], "name": e["name"],
                    "installed_version": inst,
                    "available_version": e["version"],
                    "update_available": bool(
                        inst and e["version"] != inst)})
    return {"ok": True, "components": out,
            "update_available": any(o["update_available"]
                                    for o in out)}


def perform_file_update(dest, urls, size=None, sha256=None):
    """Replace `dest` with the file at urls[0] (mirrors in urls[1:]),
    safely: download to dest + ".new" (resumable, SHA-256-verified
    by the download gate), swap with dest kept as dest + ".bak",
    verify the swapped file, and roll back on ANY failure.
    Returns (ok, error)."""
    new = dest + ".new"
    bak = dest + ".bak"
    try:
        local_ai._download_file(urls[0], new, "Downloading AI update",
                                expected_size=size,
                                expected_sha256=sha256,
                                fallback_urls=tuple(urls[1:]))
    except Exception as e:  # noqa: BLE001 - reported to the caller
        local_ai._remove_quietly(new)
        local_ai._remove_quietly(new + ".part")
        return False, str(e)[:300]
    if sha256 and local_ai._sha256_file(new) != sha256:
        local_ai._remove_quietly(new)
        return False, "the update failed its checksum verification"
    had_old = os.path.isfile(dest)
    try:
        if had_old:
            os.replace(dest, bak)
        os.replace(new, dest)
        if sha256 and local_ai._sha256_file(dest) != sha256:
            raise OSError("the swapped-in file failed verification")
    except Exception as e:  # noqa: BLE001 - roll back
        try:
            if os.path.isfile(bak):
                os.replace(bak, dest)
        except OSError:
            pass
        local_ai._remove_quietly(new)
        return False, ("the update could not be installed (%s) - "
                       "the previous version is back in place"
                       % str(e)[:160])
    local_ai._remove_quietly(bak)
    return True, None


def _update_engine_dir(component_id, entry):
    """Runtime update: fetch the (pinned) archive fresh into the
    component's folder with the old folder kept as .bak until the
    new binary is in place; roll back on failure."""
    if component_id == "runtime_gpu":
        # The GPU build is two pinned archives; the honest update
        # is a clean re-fetch of exactly the pinned files.
        bak = local_ai.LLAMA_GPU_DIR + ".bak"
        try:
            if os.path.isdir(local_ai.LLAMA_GPU_DIR):
                shutil.rmtree(bak, ignore_errors=True)
                os.replace(local_ai.LLAMA_GPU_DIR, bak)
            local_ai.ensure_gpu_engine()
            if not local_ai.gpu_engine_path():
                raise RuntimeError("no GPU engine binary after update")
            shutil.rmtree(bak, ignore_errors=True)
            return True, None
        except Exception as e:  # noqa: BLE001 - roll back
            shutil.rmtree(local_ai.LLAMA_GPU_DIR, ignore_errors=True)
            if os.path.isdir(bak):
                os.replace(bak, local_ai.LLAMA_GPU_DIR)
            return False, str(e)[:300]
    archive_url = entry.get("archive_url")
    if not archive_url:
        return False, "no download source for this component"
    tmp_archive = os.path.join(local_ai.LLAMA_DIR,
                               "_update_" +
                               archive_url.rsplit("/", 1)[-1])
    new_dir = local_ai.LLAMA_DIR + ".new"
    bak = local_ai.LLAMA_DIR + ".bak"
    try:
        os.makedirs(local_ai.LLAMA_DIR, exist_ok=True)
        local_ai._download_file(archive_url, tmp_archive,
                                "Downloading AI update",
                                expected_size=entry.get("archive_size"),
                                expected_sha256=entry.get(
                                    "archive_sha256"))
        shutil.rmtree(new_dir, ignore_errors=True)
        os.makedirs(new_dir, exist_ok=True)
        local_ai._extract_engine(tmp_archive, new_dir)
        local_ai._remove_quietly(tmp_archive)
        probe = None
        for root, _dirs, files in os.walk(new_dir):
            if "llama-server" in files or "llama-server.exe" in files:
                probe = root
                break
        if not probe:
            raise RuntimeError("no engine binary inside the update")
        if os.path.isdir(local_ai.LLAMA_DIR):
            shutil.rmtree(bak, ignore_errors=True)
            os.replace(local_ai.LLAMA_DIR, bak)
        os.replace(new_dir, local_ai.LLAMA_DIR)
        if not local_ai.cpu_engine_path():
            raise RuntimeError("the updated engine does not run")
        shutil.rmtree(bak, ignore_errors=True)
        return True, None
    except Exception as e:  # noqa: BLE001 - roll back
        local_ai._remove_quietly(tmp_archive)
        shutil.rmtree(new_dir, ignore_errors=True)
        if not os.path.isdir(local_ai.LLAMA_DIR) and \
                os.path.isdir(bak):
            os.replace(bak, local_ai.LLAMA_DIR)
        return False, str(e)[:300]


def _update_worker(component_id, entry):
    try:
        if component_id in ("runtime", "runtime_gpu"):
            # The engine files may be in use by a running server.
            local_ai._stop_server()
        if component_id == "brain":
            dest = local_ai.existing_model_path() or \
                local_ai.model_path()
            ok, err = perform_file_update(
                dest, entry.get("urls") or local_ai._model_urls(),
                entry.get("size"), entry.get("sha256"))
            if ok:
                local_ai._stop_server()
        elif component_id in ("runtime", "runtime_gpu"):
            ok, err = _update_engine_dir(component_id, entry)
        elif component_id == "speech":
            d = _speech_cache_dir()
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            try:
                _warm_speech_now()
                ok, err = True, None
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e)[:300]
        else:
            ok, err = False, "unknown component"
        if ok:
            _record_version(component_id, entry["version"])
            _mark(component_id, bad=False)
            _set_update(active=False, done=True, error=None,
                        component=component_id, at=time.time())
        else:
            _set_update(active=False, done=False, error=err,
                        component=component_id, at=time.time())
    except Exception as e:  # noqa: BLE001 - surfaced, never silent
        _set_update(active=False, done=False, error=str(e)[:300],
                    component=component_id, at=time.time())


def start_update(component_id, manifest=None):
    global _update_thread
    entry = manifest_entry(component_id, manifest)
    if entry is None:
        return False
    with _update_lock:
        if _update["active"]:
            return False
        if local_ai.download_state().get("active"):
            return False
        _update.update(active=True, done=False, error=None,
                       component=component_id)
    _update_thread = threading.Thread(target=_update_worker,
                                      args=(component_id, entry),
                                      daemon=True)
    _update_thread.start()
    return True


# ---------------------------------------------------------------------------
# Install / Repair / Reinstall: one orchestrator over the proven
# download core. Install = GPU engine when the hardware earns it
# (CPU fallback with an honest note), then engine + brain via
# local_ai.start_download(), then the speech model warm-up, then
# the diagnostic. Repair touches only what is broken or corrupt;
# Reinstall wipes the AI components first (with a UI confirm).
# ---------------------------------------------------------------------------

_op = {"active": False, "kind": "", "stage": "", "error": None,
       "done": False, "at": 0.0, "fallback": None, "warnings": []}
_op_lock = threading.Lock()
_op_thread = None


def install_state():
    with _op_lock:
        return dict(_op)


def _set_op(**kw):
    with _op_lock:
        _op.update(kw)


def _warm_speech_now():
    with _speech_lock:
        _speech_warm.update(active=True, error=None)
    try:
        import pipeline
        pipeline._load_whisper_model(SPEECH_MODEL_NAME)
        with _speech_lock:
            _speech_warm.update(active=False, done=True)
    except Exception as e:  # noqa: BLE001
        with _speech_lock:
            _speech_warm.update(active=False, error=str(e)[:200])
        raise


def _wipe_speech():
    d = _speech_cache_dir()
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def _wipe_corrupt():
    brain_path = local_ai.model_path()
    if _marked("brain") or (os.path.isfile(brain_path) and
                            not local_ai.existing_model_path()):
        local_ai._remove_quietly(brain_path)
        _mark("brain", bad=False)
    if _marked("runtime"):
        shutil.rmtree(local_ai.LLAMA_DIR, ignore_errors=True)
        _mark("runtime", bad=False)
    if _marked("runtime_gpu"):
        shutil.rmtree(local_ai.LLAMA_GPU_DIR, ignore_errors=True)
        _mark("runtime_gpu", bad=False)
    if _marked("speech"):
        _wipe_speech()
        _mark("speech", bad=False)


def _wipe_all():
    local_ai._remove_quietly(local_ai.model_path())
    local_ai._remove_quietly(local_ai.model_path() + ".part")
    shutil.rmtree(local_ai.LLAMA_DIR, ignore_errors=True)
    shutil.rmtree(local_ai.LLAMA_GPU_DIR, ignore_errors=True)
    _wipe_speech()
    try:
        os.remove(_RECORD_PATH)
    except OSError:
        pass
    with _marks_lock:
        _marks.clear()


def _wait_for_download():
    """Block until the shared download worker settles; returns an
    error string or None."""
    while True:
        st = local_ai.download_state()
        if st.get("done"):
            return None
        if st.get("error"):
            return st["error"]
        if st.get("paused"):
            return "the download was paused"
        if not st.get("active"):
            return "the download stopped unexpectedly"
        time.sleep(1)


def _install_worker(kind):
    warnings = []
    try:
        if kind == "reinstall":
            _set_op(stage="Removing the old AI components")
            _wipe_all()
        elif kind == "repair":
            _set_op(stage="Removing the damaged components")
            _wipe_corrupt()

        hw = hardware_info()
        ok_gpu, _why = gpu_candidate(hw)
        gpu_broken = bool(_marked("runtime_gpu"))
        want_gpu = ok_gpu and (kind in ("install", "reinstall")
                               or gpu_broken
                               or bool(local_ai.gpu_engine_path()))
        if want_gpu:
            _set_op(stage="Setting up the GPU AI engine")
            try:
                local_ai.ensure_gpu_engine()
                local_ai.set_engine_preference("gpu")
            except Exception as e:  # noqa: BLE001 - CPU fallback
                note = ("the GPU engine could not be set up (%s) - "
                        "the CPU engine is used instead"
                        % str(e)[:160])
                _set_op(fallback=note)
                rec = _record()
                rec["gpu_note"] = note
                _save_record(rec)

        _set_op(stage="Downloading the AI components")
        local_ai.start_download()
        err = _wait_for_download()
        if err:
            raise RuntimeError(err)
        # The shared worker only completes once the model passed
        # its SHA-256 gate and the engine is extracted: any stale
        # corrupt marks for those pieces are now untrue.
        for _cid in ("brain", "runtime", "runtime_gpu"):
            _mark(_cid, bad=False)

        _set_op(stage="Setting up the speech model")
        try:
            _warm_speech_now()
            _mark("speech", bad=False)
        except Exception as e:  # noqa: BLE001 - brain is the core
            warnings.append("the speech model could not be set up "
                            "(%s) - transcription will retry it on "
                            "first use, or use Repair here"
                            % str(e)[:140])

        rec = _record()
        rec["installed_at"] = time.time()
        versions = rec.get("versions") or {}
        for _c in components():
            if _c["healthy"] and _c["installed_version"]:
                versions[_c["id"]] = _c["installed_version"]
        rec["versions"] = versions
        _save_record(rec)
        _set_op(stage="Running the AI diagnostic",
                warnings=warnings)
        start_diagnostic()
        _set_op(active=False, done=True, error=None,
                stage="AI installation complete", at=time.time(),
                warnings=warnings)
    except Exception as e:  # noqa: BLE001 - surfaced, never silent
        _set_op(active=False, done=False, error=str(e)[:300],
                stage="AI installation stopped", at=time.time(),
                warnings=warnings)


def _start_op(kind):
    global _op_thread
    with _op_lock:
        if _op["active"]:
            return False
        if kind != "install" and \
                local_ai.download_state().get("active"):
            return False
        _op.update(active=True, kind=kind, stage="Starting",
                   error=None, done=False, fallback=None,
                   warnings=[])
    _op_thread = threading.Thread(target=_install_worker,
                                  args=(kind,), daemon=True)
    _op_thread.start()
    return True


def start_install():
    return _start_op("install")


def start_repair():
    return _start_op("repair")


def start_reinstall():
    return _start_op("reinstall")


# ---------------------------------------------------------------------------
# The Manager's single status payload
# ---------------------------------------------------------------------------

def manager_state():
    hw = hardware_info()
    comps = components(hw)
    dl = local_ai.download_state()
    total = sum(c["size"] for c in comps if c["applicable"])
    done_b = sum(c["size"] for c in comps
                 if c["applicable"] and c["healthy"])
    dl_id, _v = _download_component()
    if dl.get("active") and dl_id:
        for c in comps:
            if c["id"] == dl_id and not c["healthy"]:
                done_b += dl.get("downloaded") or 0
                break
    overall_pct = int(done_b / total * 100) if total else 100
    all_ok = all(c["healthy"] for c in comps if c["applicable"])
    diag = diagnostic_state()
    op = install_state()
    # A completed diagnostic is the strongest evidence there is:
    # its verdict wins over a merely-finished install op (an
    # install whose diagnostic FAILED must never show success).
    if diag.get("done"):
        proven = bool(diag.get("passed"))
    else:
        proven = bool(op.get("done"))
    show_success = bool(all_ok and proven)
    rec = _record()
    return {
        "ok": True,
        "components": comps,
        "health": ai_health(),
        "hardware": hw,
        "download": dl,
        "install": op,
        "verify": verify_state(),
        "diagnostic": diag,
        "update": update_state(),
        "engine": {
            "flavor": local_ai.engine_flavor(),
            "preference": local_ai.engine_preference(),
            "fallback": local_ai.gpu_fallback_note()
                        or rec.get("gpu_note"),
        },
        "overall": {"pct": min(100, overall_pct), "downloaded": done_b,
                    "total": total},
        "success": show_success,
    }
