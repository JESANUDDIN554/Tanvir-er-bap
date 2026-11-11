"""Owner Admin Panel backend: sessions, API-call log, usage
counters and honest local statistics for Itraji Cliper.

Architecture fact: this app is an OFFLINE DESKTOP program. There is
no server, no account database and no telemetry - so this panel
reports only what this installation itself knows:

- Admin access is a CLAIM INSIDE THE SIGNED LICENSE. The issuer
  (tools/issue_license.py --owner) can mint keys whose signed
  payload carries "role": "owner". verify_owner_key() runs the
  normal licensing verification and then reads the claim - it can
  be neither forged nor stripped without breaking the signature.
  Ordinary customer keys (and the original master key, minted
  before claims existed) activate the app exactly as before but
  can never open this panel.
- A successful unlock mints a random, server-side session token
  (30 minutes). Every /api/admin/* data route checks it in the
  WSGI layer before any data is produced - the UI hiding buttons
  is presentation, never the enforcement.
- Every number on the dashboard is computed from local records:
  work/<job>/job.json files, the in-memory session jobs app.py
  passes in, the API-call log below, and small usage counters the
  app bumps at real call sites (detect/generate/enhance/remover
  runs and file downloads). Counters only exist from the build
  that introduced them; the dashboard labels them as such.
"""

import datetime
import json
import os
import platform
import secrets
import shutil
import sys
import threading
import time

import licensing

# Data area: same resolution as local_ai.py (env override wins,
# else the app folder / PyInstaller bundle dir).
_BASE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
DATA_DIR = os.environ.get("ITRAJI_LOCAL_DATA_DIR") or _BASE

SESSION_TTL_S = 30 * 60
LOG_MAX_BYTES = 512_000      # rotate: keep the newest ~half
LOG_KEEP_BYTES = 256_000
_SIZE_WALK_CAP = 20_000      # files per directory-size walk


# ---------------------------------------------------------------------------
# Admin sessions (server-side, in-memory, single-process app)
# ---------------------------------------------------------------------------

_sessions = {}
_sessions_lock = threading.Lock()


def create_session():
    """Mint a session token; returns (token, ttl_seconds)."""
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        for t in [t for t, exp in _sessions.items() if exp <= now]:
            del _sessions[t]
        _sessions[token] = now + SESSION_TTL_S
    return token, SESSION_TTL_S


def validate_session(token):
    """Seconds left for a live session token, else None."""
    if not token:
        return None
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            return None
        left = exp - time.time()
        if left <= 0:
            del _sessions[token]
            return None
        return int(left)


def drop_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Owner-key verification (the role claim rides inside the signature)
# ---------------------------------------------------------------------------

def verify_owner_key(key_str):
    """Verify a license key AND its owner claim.

    Returns the payload on success, None for EVERY failure mode
    (damaged, forged, wrong machine, expired, or a perfectly valid
    key that simply carries no owner claim). One generic outcome:
    the unlock endpoint must never become an oracle about which
    part failed."""
    try:
        payload, _pack_key = licensing.verify_license(
            (key_str or "").strip())
    except licensing.LicenseError:
        return None
    if str(payload.get("role") or "") != "owner":
        return None
    return payload


# ---------------------------------------------------------------------------
# API-call log (JSONL, capped) - fal.ai + Built-in AI calls
# ---------------------------------------------------------------------------

def api_log_path():
    return os.path.join(DATA_DIR, "api_log.jsonl")


_log_lock = threading.Lock()


def log_api_call(provider, model, status, latency_ms, error=None,
                 kind=None, credits=None):
    """Append one call record. Best-effort by design: logging must
    never break the call it describes. `credits` is recorded only
    when the provider's own response reported a cost - otherwise
    it stays None and the UI says "not reported by provider"."""
    rec = {"ts": round(time.time(), 3), "provider": str(provider),
           "model": str(model or ""), "kind": str(kind or ""),
           "status": "ok" if status == "ok" else "error",
           "latency_ms": (None if latency_ms is None
                          else int(round(latency_ms))),
           "error": (str(error)[:300] if error else None),
           "credits": credits}
    try:
        with _log_lock:
            path = api_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            _rotate_log_locked(path)
    except Exception:  # noqa: BLE001 - never fatal
        pass
    return rec


def _rotate_log_locked(path):
    """Trim the log to its newest ~LOG_KEEP_BYTES when it grows
    past LOG_MAX_BYTES. Drops the partial first line after the cut
    so every remaining line is valid JSON."""
    try:
        if os.path.getsize(path) <= LOG_MAX_BYTES:
            return
        with open(path, "rb") as f:
            data = f.read()
        tail = data[-LOG_KEEP_BYTES:]
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, path)
    except OSError:
        pass


def read_api_log(limit=200):
    """Newest-first parsed log entries (malformed lines skipped)."""
    out = []
    try:
        with open(api_log_path(), "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def provider_summaries(entries):
    """Per-provider rollups over log entries (any order in):
    calls, ok, errors, success rate, mean latency, last call and
    last error. Providers appear in first-seen order."""
    agg = {}
    for e in entries:
        name = e.get("provider") or "?"
        a = agg.setdefault(name, {"provider": name, "calls": 0,
                                  "ok": 0, "errors": 0,
                                  "_lat": [], "last_ts": 0,
                                  "last_status": None,
                                  "last_error": None,
                                  "credits_reported": False})
        a["calls"] += 1
        if e.get("status") == "ok":
            a["ok"] += 1
        else:
            a["errors"] += 1
        if isinstance(e.get("latency_ms"), (int, float)):
            a["_lat"].append(e["latency_ms"])
        ts = e.get("ts") or 0
        if ts >= a["last_ts"]:
            a["last_ts"] = ts
            a["last_status"] = e.get("status")
            if e.get("status") != "ok":
                a["last_error"] = e.get("error")
        if e.get("credits") is not None:
            a["credits_reported"] = True
    out = []
    for a in agg.values():
        lat = a.pop("_lat")
        a["avg_latency_ms"] = (int(round(sum(lat) / len(lat)))
                               if lat else None)
        a["success_rate"] = (round(100.0 * a["ok"] / a["calls"], 1)
                             if a["calls"] else None)
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# Usage counters (persisted; exist only from this build onward)
# ---------------------------------------------------------------------------

def counters_path():
    return os.path.join(DATA_DIR, "admin_counters.json")


_counters_lock = threading.Lock()


def read_counters():
    try:
        with open(counters_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def bump_counter(name, n=1):
    """Increment a persisted counter. Best-effort, never raises."""
    try:
        with _counters_lock:
            data = read_counters()
            data[name] = int(data.get(name) or 0) + n
            data.setdefault("since", round(time.time(), 3))
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = counters_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, counters_path())
    except Exception:  # noqa: BLE001 - never fatal
        pass


# ---------------------------------------------------------------------------
# Local statistics (pure functions over job records where possible)
# ---------------------------------------------------------------------------

def collect_disk_jobs(work_roots):
    """Every persisted job.json under the given work roots, as
    dicts with an added "_job_id". Corrupt entries are skipped,
    exactly like the Library loader treats them."""
    jobs = []
    seen = set()
    for root in work_roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if name in seen:
                continue
            meta = os.path.join(root, name, "job.json")
            if not os.path.isfile(meta):
                continue
            try:
                with open(meta, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001 - skip corrupt entries
                continue
            if not isinstance(data, dict):
                continue
            seen.add(name)
            data = dict(data)
            data["_job_id"] = name
            if not data.get("created"):
                try:
                    data["created"] = os.path.getmtime(meta)
                except OSError:
                    data["created"] = 0
            jobs.append(data)
    return jobs


def _day_key(ts):
    return datetime.date.fromtimestamp(ts).isoformat()


def _series(jobs, now):
    """Daily (30d) / weekly (12w) / monthly (12m) buckets of
    videos + clips, oldest first. A job's clips are attributed to
    the job's own creation day - renders carry no timestamps."""
    today = datetime.date.fromtimestamp(now)
    daily = []
    for i in range(29, -1, -1):
        d = today - datetime.timedelta(days=i)
        daily.append({"label": d.isoformat(), "videos": 0, "clips": 0})
    dmap = {p["label"]: p for p in daily}
    weekly = []
    monday = today - datetime.timedelta(days=today.weekday())
    for i in range(11, -1, -1):
        w = monday - datetime.timedelta(weeks=i)
        weekly.append({"label": w.isoformat(), "videos": 0,
                       "clips": 0, "_monday": w})
    monthly = []
    y, m = today.year, today.month
    for _i in range(12):
        monthly.append({"label": "%04d-%02d" % (y, m), "videos": 0,
                        "clips": 0})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    monthly.reverse()
    mmap = {p["label"]: p for p in monthly}
    for job in jobs:
        ts = job.get("created") or 0
        if not ts:
            continue
        d = datetime.date.fromtimestamp(ts)
        clips = len(job.get("renders") or [])
        p = dmap.get(d.isoformat())
        if p:
            p["videos"] += 1
            p["clips"] += clips
        wmonday = d - datetime.timedelta(days=d.weekday())
        for p in weekly:
            if p["_monday"] == wmonday:
                p["videos"] += 1
                p["clips"] += clips
                break
        p = mmap.get("%04d-%02d" % (d.year, d.month))
        if p:
            p["videos"] += 1
            p["clips"] += clips
    for p in weekly:
        p.pop("_monday", None)
    return {"daily": daily, "weekly": weekly, "monthly": monthly}


def aggregate_stats(disk_jobs, session_activity, failed, counters,
                    now=None):
    """The dashboard's job-derived numbers.

    disk_jobs: collect_disk_jobs() output (persisted history).
    session_activity: normalized rows for session-only tools
      ({ts, tool, title, status, error}) built by app.py.
    failed: normalized failure rows (same shape), already deduped
      by the caller against the activity rows.
    counters: read_counters() output (runs/downloads tracked
      since the Admin Panel shipped)."""
    now = now or time.time()
    videos = len(disk_jobs)
    clips = sum(len(j.get("renders") or []) for j in disk_jobs)
    activity = []
    for j in disk_jobs:
        activity.append({
            "ts": j.get("created") or 0, "tool": "Clip Maker",
            "title": j.get("title") or j.get("_job_id") or "Video",
            "status": j.get("status") or "",
            "error": ""})
    activity += list(session_activity or [])
    activity.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {
        "videos_processed": videos,
        "clips_generated": clips,
        "downloads": int(counters.get("downloads") or 0),
        "counters_since": counters.get("since"),
        "ai_jobs": {
            "detect_local": int(counters.get("detect_local") or 0),
            "detect_offline": int(counters.get("detect_offline") or 0),
            "generator": int(counters.get("generator") or 0),
            "enhancer": int(counters.get("enhancer") or 0),
            "remover": int(counters.get("remover") or 0),
        },
        "failed_jobs": list(failed or []),
        "failed_count": len(failed or []),
        "activity": activity[:30],
        "series": _series(disk_jobs, now),
    }


def dir_size_capped(path, cap_files=_SIZE_WALK_CAP):
    """Total bytes under path; stops after cap_files entries so a
    huge tree cannot hang the dashboard (same rule as app.py's
    old-copy scanner). Missing paths count as 0."""
    total, seen = 0, 0
    if not path or not os.path.isdir(path):
        return 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > cap_files:
                return total
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def system_info(disk_path=None):
    """Best-effort host facts from the stdlib only. Anything the
    platform cannot answer comes back None and the UI shows '—' -
    values are never guessed."""
    info = {"os": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "ram_total": None, "ram_free": None,
            "disk_total": None, "disk_free": None}
    try:
        if os.name == "nt":
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
                info["ram_total"] = int(ms.ullTotalPhys)
                info["ram_free"] = int(ms.ullAvailPhys)
        elif os.path.exists("/proc/meminfo"):
            vals = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        vals[parts[0].strip()] = parts[1].strip()
            if "MemTotal" in vals:
                info["ram_total"] = int(vals["MemTotal"].split()[0]) * 1024
            if "MemAvailable" in vals:
                info["ram_free"] = int(
                    vals["MemAvailable"].split()[0]) * 1024
    except Exception:  # noqa: BLE001 - degrade, never fail
        pass
    try:
        du = shutil.disk_usage(disk_path or DATA_DIR)
        info["disk_total"] = du.total
        info["disk_free"] = du.free
    except Exception:  # noqa: BLE001
        pass
    return info
