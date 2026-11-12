"""Super Admin connection for Itraji Cliper (opt-in, off by default).

Itraji Super Admin is a SEPARATE application (its own server,
database and admin login) that the owner uses to manage licenses
and see usage across installs. This module is the Cliper side:

- CliperTracker: the standalone Super Admin Python SDK, vendored
  (adapted, not redesigned). Durable SQLite outbox, background
  flusher thread, batch delivery with event_id idempotency,
  exponential backoff; emit() never raises and never does network
  I/O on the caller's thread.
- A connection manager: the user connects a server in Settings
  (server URL -> fetch + fingerprint-confirm the server's license
  public key -> PIN it -> activate this device with the license key
  -> store the device credential in settings, masked everywhere).
  NOTHING in this module runs unless the user has connected: no
  outbox file is created, no thread starts, emit() is a no-op.
- Online verification for SERVER-issued keys only: periodic verify
  against the connected server; a suspension takes effect at the
  next successful verification; when the server is unreachable the
  signed entitlement's grace window (7 days, set by the server)
  keeps the app working. Legacy keys never phone home.

Privacy contract (shown to the user before connecting): the app
reports usage and processing activity only - event types, counts,
durations, app version, error class names. Never file contents,
never transcripts, never media, never the license key itself (the
key is sent once, to the activation endpoint the user chose, over
the configured URL).
"""

import datetime
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import licensing

# ---------------------------------------------------------------------------
# Vendored SDK (from super-admin/packages/clipper-sdk-python;
# additions marked: last_delivered_at for the UI's last-sync line,
# stop_event-driven loop identical to the original's design).
# ---------------------------------------------------------------------------

EVENT_TYPES = (
    "user.registered",
    "license.activated",
    "user.login",
    "user.logout",
    "video.uploaded",
    "clipping.started",
    "clipping.completed",
    "clips.generated",
    "enhancement.started",
    "enhancement.completed",
    "video.exported",
    "video.downloaded",
    "processing.failed",
    "device.activated",
    "session.heartbeat",
    "app.version_reported",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    job_id      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_try_at);
"""


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.datetime.now(datetime.timezone.utc).microsecond // 1000:03d}Z"


class CliperTracker:
    """Fire-and-forget usage tracker with a durable outbox."""

    def __init__(self, server_url, device_id, device_credential,
                 storage_path, batch_size=100, flush_interval=5.0,
                 heartbeat_seconds=None, max_backoff=60.0, timeout=10.0,
                 autostart=True):
        if not server_url or not device_id or not device_credential:
            raise ValueError(
                "server_url, device_id and device_credential are required")
        self._server_url = server_url.rstrip("/")
        self._device_id = device_id
        self._credential = device_credential
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.5, float(flush_interval))
        self._heartbeat_seconds = heartbeat_seconds
        self._max_backoff = max(5.0, float(max_backoff))
        self._timeout = float(timeout)
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(storage_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._stop = threading.Event()
        self._thread = None
        self._fail_streak = 0
        self.last_delivered_at = 0.0  # epoch of last accepted batch
        if autostart:
            self.start()

    # -- public API ----------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="itraji-tracker",
                                        daemon=True)
        self._thread.start()

    def emit(self, event_type, metadata=None, job_id=None,
             occurred_at=None):
        """Queue an event. NEVER raises, never blocks on network."""
        try:
            event = {
                "event_id": uuid.uuid4().hex,
                "type": str(event_type),
                "occurred_at": occurred_at or _utcnow_iso(),
                "metadata": metadata or {},
            }
            if job_id:
                event["job_id"] = str(job_id)
            with self._db_lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO outbox"
                    " (event_id, type, occurred_at, metadata, job_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (event["event_id"], event["type"],
                     event["occurred_at"],
                     json.dumps(event["metadata"]),
                     event.get("job_id")))
                self._conn.commit()
            return event
        except Exception:  # noqa: BLE001 - tracking must never break the app
            return None

    def pending_count(self):
        try:
            with self._db_lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM outbox").fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def flush_once(self, limit=None):
        """Try to deliver one batch now. Returns events accepted."""
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT event_id, type, occurred_at, metadata, job_id"
                " FROM outbox WHERE next_try_at <= ?"
                " ORDER BY occurred_at LIMIT ?",
                (time.time(), limit or self._batch_size)).fetchall()
        if not rows:
            return 0
        events = []
        for event_id, etype, occurred_at, metadata, job_id in rows:
            ev = {"event_id": event_id, "type": etype,
                  "occurred_at": occurred_at,
                  "metadata": json.loads(metadata or "{}")}
            if job_id:
                ev["job_id"] = job_id
            events.append(ev)
        try:
            accepted = self._deliver_batch(events)
        except Exception:  # noqa: BLE001 - retry later
            self._penalise([r[0] for r in rows])
            return 0
        with self._db_lock:
            self._conn.executemany(
                "DELETE FROM outbox WHERE event_id = ?",
                [(event_id,) for event_id in accepted])
            self._conn.commit()
        if accepted:
            self._fail_streak = 0
            self.last_delivered_at = time.time()
        rejected = [r[0] for r in rows
                    if r[0] not in set(accepted)]
        if rejected:
            self._penalise(rejected)
        return len(accepted)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        try:
            with self._db_lock:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # -- internals -----------------------------------------------------

    def _penalise(self, event_ids):
        self._fail_streak += 1
        delay = min(self._max_backoff,
                    2.0 ** min(self._fail_streak, 6))
        with self._db_lock:
            self._conn.executemany(
                "UPDATE outbox SET attempts = attempts + 1,"
                " next_try_at = ? WHERE event_id = ?",
                [(time.time() + delay, eid) for eid in event_ids])
            self._conn.commit()

    def _deliver_batch(self, events):
        body = json.dumps({"events": events}).encode("utf-8")
        req = urllib.request.Request(
            self._server_url + "/api/v1/events/batch",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ItrajiCliper-Tracker/1.0",
                "x-device-id": self._device_id,
                "x-device-credential": self._credential,
            })
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        accepted = payload.get("accepted_event_ids")
        if accepted is None:
            accepted = [e["event_id"] for e in events
                        if payload.get("accepted", 0) >= len(events)]
        return list(accepted)

    def _run(self):
        next_heartbeat = time.time() + (
            self._heartbeat_seconds or 1e18)
        while not self._stop.is_set():
            try:
                if self._heartbeat_seconds \
                        and time.time() >= next_heartbeat:
                    self.emit("session.heartbeat", {})
                    next_heartbeat = time.time() + self._heartbeat_seconds
                self.flush_once()
            except Exception:  # noqa: BLE001 - the loop must survive
                pass
            self._stop.wait(self._flush_interval)


# ---------------------------------------------------------------------------
# Server client (licensing endpoints)
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = 15.0


class ServerError(Exception):
    """The Super Admin server could not be reached or answered
    unexpectedly. Carries no credential material, ever."""


def _http_json(method, url, body=None, headers=None, timeout=_HTTP_TIMEOUT):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ItrajiCliper/2.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload
    except Exception as e:  # noqa: BLE001 - DNS/timeout/refused
        raise ServerError(f"Could not reach the Super Admin server "
                          f"({type(e).__name__}). Check the server "
                          f"URL and your connection.") from e


def normalize_server_url(url):
    """Accept what a user pastes: add http:// for local spellings,
    strip trailing slash/whitespace. (Production servers use
    https://, which passes through untouched.)"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    return u


def fetch_public_key(server_url):
    """GET {server}/api/v1/licenses/public-key -> dict with the
    server's Ed25519 public key + this module's fingerprint of it.
    Raises ServerError when unreachable/malformed."""
    url = normalize_server_url(server_url)
    _status, payload = _http_json(
        "GET", url + "/api/v1/licenses/public-key")
    pub = (payload or {}).get("public_key_hex") or ""
    try:
        fingerprint = licensing.pubkey_fingerprint(pub)
    except licensing.LicenseError as e:
        raise ServerError(
            "That server did not return a valid license public "
            "key - is it an Itraji Super Admin server?") from e
    name, version = "", ""
    try:
        _s2, root = _http_json("GET", url + "/", timeout=5.0)
        if isinstance(root, dict):
            name = str(root.get("name") or root.get("app")
                       or root.get("service") or "")
            version = str(root.get("version") or "")
    except Exception:  # noqa: BLE001 - identity line is best-effort
        pass
    return {"server_url": url, "public_key_hex": pub,
            "fingerprint": fingerprint,
            "server_name": name, "server_version": version}


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()
_settings_loader = None        # app.load_settings
_settings_patch = None         # fn(dict) -> merge + persist
_app_version = ""
_data_dir_fn = None            # fn() -> writable app data dir
_tracker = None
_manager_thread = None
_manager_stop = threading.Event()
_started_once = False

# Settings keys owned by this module. sa_device_credential and
# sa_entitlement are SECRETS: app.get_settings masks them and
# app.save_settings refuses to overwrite them from the UI wholesale.
SA_SETTING_KEYS = (
    "sa_server_url", "sa_device_id", "sa_device_credential",
    "sa_pinned_pubkey", "sa_entitlement", "sa_grace_until",
    "sa_status", "sa_last_verify", "sa_last_verify_ok",
    "sa_connected_at", "sa_server_name", "sa_server_version",
    "sa_pub_seen",
)

_VERIFY_INTERVAL_S = 6 * 3600.0
_PUB_SCAN_INTERVAL_S = 60.0
_PUB_SEEN_CAP = 500


def bind_settings(loader, patch, app_version, data_dir_fn):
    """Wire the manager to the app's settings storage. Called once
    at app startup (module glue, like publishing.bind_settings)."""
    global _settings_loader, _settings_patch, _app_version, _data_dir_fn
    _settings_loader = loader
    _settings_patch = patch
    _app_version = app_version or ""
    _data_dir_fn = data_dir_fn


def _settings():
    if _settings_loader is None:
        return {}
    try:
        return _settings_loader() or {}
    except Exception:  # noqa: BLE001
        return {}


def _patch(values):
    if _settings_patch is None:
        return
    try:
        _settings_patch(values)
    except Exception:  # noqa: BLE001 - settings write must not crash flows
        pass


def is_connected(settings=None):
    s = settings if settings is not None else _settings()
    return bool(s.get("sa_server_url") and s.get("sa_device_id")
                and s.get("sa_device_credential"))


def _outbox_path():
    base = _data_dir_fn() if _data_dir_fn else os.path.expanduser("~")
    folder = os.path.join(base, "superadmin")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "outbox.sqlite3")


def emit(event_type, metadata=None, job_id=None, occurred_at=None):
    """App-wide emit: a strict no-op unless connected (no outbox
    writes, no threads, no files). Never raises."""
    tracker = _tracker
    if tracker is None:
        return None
    try:
        return tracker.emit(event_type, metadata=metadata,
                            job_id=job_id, occurred_at=occurred_at)
    except Exception:  # noqa: BLE001
        return None


def start_if_connected():
    """Start the tracker + manager thread when a connection exists.
    Called at app startup; safe to call repeatedly."""
    global _tracker, _manager_thread, _started_once
    with _STATE_LOCK:
        if _tracker is not None:
            return True
        s = _settings()
        if not is_connected(s):
            return False
        try:
            _tracker = CliperTracker(
                s["sa_server_url"], s["sa_device_id"],
                s["sa_device_credential"], _outbox_path(),
                heartbeat_seconds=300)
        except Exception:  # noqa: BLE001 - a damaged outbox must not
            _tracker = None  # stop the app from starting
            return False
        _manager_stop.clear()
        _manager_thread = threading.Thread(
            target=_manager_loop, name="itraji-superadmin",
            daemon=True)
        _manager_thread.start()
        first = not _started_once
        _started_once = True
        if first:
            emit("app.version_reported", {"version": _app_version})
        return True


def stop_tracker():
    global _tracker
    with _STATE_LOCK:
        tracker, _tracker = _tracker, None
    _manager_stop.set()
    if tracker is not None:
        try:
            tracker.close()
        except Exception:  # noqa: BLE001
            pass


def connect(server_url, pinned_pub_hex, license_key, machine_code,
            device_name=""):
    """Confirm step of the Connect flow: the caller has already
    fetched the server's public key and the USER has confirmed its
    fingerprint. Activates this device against the server with the
    given license key, pins the key, persists the connection and
    starts tracking. Returns (ok, message_or_state)."""
    url = normalize_server_url(server_url)
    try:
        licensing.pubkey_fingerprint(pinned_pub_hex)  # format gate
    except licensing.LicenseError as e:
        return False, str(e)
    try:
        _status, payload = _http_json(
            "POST", url + "/api/v1/licenses/activate",
            body={"license_key": license_key,
                  "machine_code": machine_code,
                  "device_name": device_name or None,
                  "platform": sys.platform,
                  "app_version": _app_version})
    except ServerError as e:
        return False, str(e)
    if not payload.get("valid"):
        reason = payload.get("reason") or payload.get("message") \
            or "unknown_license"
        return False, _activation_refusal(reason)
    credential = payload.get("device_credential") or ""
    device_id = payload.get("device_id") or ""
    if not credential or not device_id:
        return False, ("The server accepted the activation but did "
                       "not return a device credential.")
    now = time.time()
    _patch({
        "sa_server_url": url,
        "sa_device_id": device_id,
        "sa_device_credential": credential,
        "sa_pinned_pubkey": (pinned_pub_hex or "").strip().lower(),
        "sa_entitlement": payload.get("entitlement_token") or "",
        "sa_grace_until": payload.get("grace_until") or "",
        "sa_status": "active",
        "sa_last_verify": now,
        "sa_last_verify_ok": now,
        "sa_connected_at": now,
    })
    stop_tracker()
    start_if_connected()
    emit("device.activated", {"via": "super_admin_connect"})
    return True, status()


def _activation_refusal(reason):
    table = {
        "unknown_license": "The server does not know that license "
                           "key. Server connections need a key the "
                           "Super Admin server issued.",
        "machine_mismatch": "That license key belongs to a different "
                            "computer on the server's records.",
        "device_limit_reached": "This license already has the "
                                "maximum number of devices on the "
                                "server. Ask your provider to free a "
                                "device slot.",
    }
    if reason in table:
        return table[reason]
    if reason.startswith("license_"):
        return ("The server reports this license is "
                + reason[len("license_"):] + ".")
    return "The server refused the activation (" + str(reason) + ")."


def disconnect():
    """Kill switch: stop the SDK and wipe every stored trace of the
    connection (credential, entitlement, pin, device id)."""
    stop_tracker()
    _patch({
        "sa_server_url": "", "sa_device_id": "",
        "sa_device_credential": "", "sa_pinned_pubkey": "",
        "sa_entitlement": "", "sa_grace_until": "",
        "sa_status": "", "sa_last_verify": 0, "sa_last_verify_ok": 0,
        "sa_connected_at": 0, "sa_server_name": "",
        "sa_server_version": "", "sa_pub_seen": [],
    })
    return status()


def verify_now():
    """Verify the active server license with the server NOW.
    Returns (ok, detail_dict). Updates the stored status +
    entitlement: a suspension lands here; an unreachable server
    leaves the previous state untouched (grace governs)."""
    s = _settings()
    if not is_connected(s):
        return False, {"error": "Not connected to a Super Admin "
                                "server."}
    now = time.time()
    try:
        _status, payload = _http_json(
            "POST", s["sa_server_url"] + "/api/v1/licenses/verify",
            body={},
            headers={"x-device-id": s["sa_device_id"],
                     "x-device-credential": s["sa_device_credential"]})
    except ServerError as e:
        _patch({"sa_last_verify": now})
        return False, {"unreachable": True, "error": str(e)}
    if payload.get("valid"):
        token = payload.get("entitlement_token") or ""
        if token:
            try:
                licensing.verify_entitlement(
                    token, s.get("sa_pinned_pubkey") or "")
            except licensing.LicenseError:
                _patch({"sa_last_verify": now})
                return False, {"error": "The server's signed "
                                        "entitlement did not verify "
                                        "against the pinned key."}
        _patch({"sa_status": "active",
                "sa_entitlement": token or s.get("sa_entitlement", ""),
                "sa_grace_until": payload.get("grace_until")
                or s.get("sa_grace_until", ""),
                "sa_last_verify": now, "sa_last_verify_ok": now})
        return True, status()
    reason = payload.get("reason") or ""
    new_status = ""
    if reason.startswith("license_"):
        new_status = reason[len("license_"):]
    elif payload.get("status"):
        new_status = str(payload["status"])
    if new_status:
        _patch({"sa_status": new_status, "sa_last_verify": now})
        return False, {"status": new_status,
                       "error": "The server reports this license "
                                "is " + new_status + "."}
    _patch({"sa_last_verify": now})
    return False, {"error": "The server could not verify this "
                            "device (" + (reason or "unknown") + ")."}


def gate_block(issuer):
    """The license-gate hook for SERVER-issued keys while connected.
    Returns a user-facing block message or None. Legacy keys and
    unconnected installs are NEVER blocked by this layer."""
    if issuer != "server":
        return None
    s = _settings()
    if not is_connected(s):
        return None
    st = (s.get("sa_status") or "").strip()
    if st in ("suspended", "revoked", "deactivated", "expired"):
        return ("Your license has been " + st + " by your provider "
                "(checked with your Super Admin server). Contact "
                "your provider, or press 'Check license status now' "
                "in Settings -> Super Admin if this has been fixed.")
    grace = _parse_iso_epoch(s.get("sa_grace_until") or "")
    if grace and time.time() > grace:
        return ("Cliper could not verify your license with your "
                "Super Admin server within the signed entitlement's "
                "7-day grace window. Connect to the internet and "
                "press 'Check license status now' in Settings -> "
                "Super Admin.")
    return None


def _parse_iso_epoch(text):
    try:
        dt = datetime.datetime.fromisoformat(
            str(text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def status():
    """Real local connection state for Settings + the Admin Panel.
    Contains NO credential or entitlement material."""
    s = _settings()
    connected = is_connected(s)
    queued = 0
    last_sync = 0.0
    if _tracker is not None:
        queued = _tracker.pending_count()
        last_sync = _tracker.last_delivered_at
    fingerprint = ""
    try:
        if s.get("sa_pinned_pubkey"):
            fingerprint = licensing.pubkey_fingerprint(
                s["sa_pinned_pubkey"])
    except licensing.LicenseError:
        fingerprint = ""
    return {
        "connected": connected,
        "server_url": s.get("sa_server_url") or "",
        "server_name": s.get("sa_server_name") or "",
        "server_version": s.get("sa_server_version") or "",
        "device_id": s.get("sa_device_id") or "",
        "status": s.get("sa_status") or "",
        "fingerprint": fingerprint,
        "grace_until": s.get("sa_grace_until") or "",
        "last_verify": s.get("sa_last_verify") or 0,
        "last_verify_ok": s.get("sa_last_verify_ok") or 0,
        "last_sync": last_sync,
        "queued": queued,
        "connected_at": s.get("sa_connected_at") or 0,
    }


# ---------------------------------------------------------------------------
# Manager loop: periodic online verify + publishing outcome scan
# ---------------------------------------------------------------------------

def _manager_loop():
    last_verify = 0.0
    last_scan = 0.0
    while not _manager_stop.wait(5.0):
        now = time.time()
        try:
            if now - last_verify >= _VERIFY_INTERVAL_S:
                last_verify = now
                if licensing.key_issuer() == "server":
                    verify_now()
            if now - last_scan >= _PUB_SCAN_INTERVAL_S:
                last_scan = now
                _scan_publishing()
        except Exception:  # noqa: BLE001 - manager must never crash the app
            pass


def _iso_or_none(value):
    """Publishing timestamps are epoch seconds (floats); the event
    API carries ISO strings. Convert numbers, pass strings through,
    anything else -> None (the tracker stamps the current time)."""
    if isinstance(value, (int, float)) and value > 0:
        dt = datetime.datetime.fromtimestamp(
            float(value), datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{dt.microsecond // 1000:03d}Z"
    if isinstance(value, str) and value:
        return value
    return None


def _scan_publishing():
    """Report terminal publishing outcomes (read-only over the
    Step 1b publishing store; publishing.py is not modified)."""
    try:
        import publishing
    except Exception:  # noqa: BLE001
        return
    s = _settings()
    seen = list(s.get("sa_pub_seen") or [])
    seen_set = set(seen)
    try:
        jobs = publishing.list_jobs(limit=200)
    except Exception:  # noqa: BLE001
        return
    changed = False
    for job in jobs:
        jid = job.get("id")
        st = job.get("status")
        if not jid or jid in seen_set:
            continue
        if st == "published":
            emit("publishing.completed",
                 {"platform": job.get("platform") or "",
                  "account_id": job.get("account_id") or ""},
                 job_id=jid,
                 occurred_at=_iso_or_none(job.get("published_at")
                                          or job.get("updated_at")))
            seen.append(jid)
            changed = True
        elif st in ("failed", "needs_attention"):
            emit("publishing.failed",
                 {"platform": job.get("platform") or "",
                  "account_id": job.get("account_id") or ""},
                 job_id=jid,
                 occurred_at=_iso_or_none(job.get("updated_at")))
            seen.append(jid)
            changed = True
    if changed:
        _patch({"sa_pub_seen": seen[-_PUB_SEEN_CAP:]})
