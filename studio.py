"""Itraji Cliper -> Facebook Automation OS ("one app", Step 3).

Sends a finished Cliper clip to the platform's Clipper webhook
(POST /api/v1/webhooks/clipper) and reads its status back.

CONTRACT (platform apps/api/src/webhooks.module.ts, packages/shared):
  POST {platform_url}/api/v1/webhooks/clipper
  body: ClipperCompletedEvent
    {event: "video.processing.completed", event_id, video_id,
     organization_id, asset_uri, title, duration, aspect_ratio,
     thumbnail_uri?}
  header: x-fbos-signature: "t=<unix>,v1=<hex HMAC-SHA256 of
          '<t>.<raw body>' with the shared secret>"
  response: {received, duplicate, videoId, approval?, jobsCreated}
  asset_uri schemes file://, local://, s3:// — both apps run on one
  PC, so file://<absolute clip path> is read directly by the worker.

This is a DIFFERENT signature scheme from Cliper's existing outbox
(publishing.py _emit_event: X-Itraji-Signature over
json.dumps(sort_keys=True)) — do not confuse the two. The shared
secret is the same one the platform uses for both, and the same one
ai_endpoint.py already reads (pub_webhook_secret); fbos_webhook_secret
overrides it when set.

Idempotency: event_id is a deterministic hash of the clip identity
(job_id + render_index + file sha256), so a retry of the same clip
reuses the same event_id and the platform dedupes it. video_id is
"clipper:<job_id>:<render_index>" — stable, so the platform upserts
one Video row per clip.

Status: one HMAC-signed call, GET
{platform_url}/api/v1/webhooks/clipper/status/{videoId} (platform
apps/api/src/webhooks.module.ts, WebhooksController.clipperStatus).
The header is x-fbos-signature over the canonical request line
"<ts>.GET /webhooks/clipper/status/{videoId}" with the SAME shared
secret as the arrival webhook — no platform JWT, no bearer token.
The guessed JWT routes (GET /videos/:id, GET /publishing/jobs)
were removed: they are JWT-only and Cliper holds no platform user
credential, so they could never work. fbos_api_token is therefore
unused by Step 3 (the setting key is kept, harmless, for a future
user-scoped feature). get_clip_status() returns
platform_status_unavailable only when the org id / shared secret
are not configured at all.

State lives in studio.json next to publishing.json (same DATA_DIR),
written atomically, keyed by "<job_id>:<render_index>".
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PureWindowsPath

import publishing

_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE = _DIR
DATA_DIR = os.environ.get("ITRAJI_LOCAL_DATA_DIR") or _BASE
STORE_PATH = os.path.join(DATA_DIR, "studio.json")

_LOCK = threading.Lock()
_SETTINGS_LOCK = threading.Lock()
_settings_loader = None
_settings_saver = None

DEFAULT_PLATFORM_URL = "http://127.0.0.1:4000"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3000"
WEBHOOK_PATH = "/api/v1/webhooks/clipper"
SIGNATURE_HEADER = "x-fbos-signature"
TOLERANCE_S = 300  # platform webhook tolerance; signing uses "now"


def bind_settings(loader, saver=None):
    """app.py binds its load_settings/save machinery once at startup
    (same pattern as publishing.bind_settings)."""
    global _settings_loader, _settings_saver
    with _SETTINGS_LOCK:
        _settings_loader = loader
        _settings_saver = saver


def _settings():
    with _SETTINGS_LOCK:
        loader = _settings_loader
    if loader is None:
        return {}
    try:
        return loader() or {}
    except Exception:  # noqa: BLE001 - studio must never crash the app
        return {}


def _save_settings_patch(patch):
    with _SETTINGS_LOCK:
        saver = _settings_saver
    if saver is None:
        return
    try:
        saver(patch)
    except Exception:  # noqa: BLE001
        pass


def platform_url(settings=None):
    st = settings if settings is not None else _settings()
    return (str(st.get("fbos_platform_url") or "").strip()
            or DEFAULT_PLATFORM_URL).rstrip("/")


def dashboard_url(settings=None):
    st = settings if settings is not None else _settings()
    return (str(st.get("fbos_dashboard_url") or "").strip()
            or DEFAULT_DASHBOARD_URL).rstrip("/")


def organization_id(settings=None):
    st = settings if settings is not None else _settings()
    return str(st.get("fbos_org_id") or "").strip()


def webhook_secret(settings=None):
    """Shared HMAC secret. fbos_webhook_secret wins; falls back to the
    publishing outbox secret (pub_webhook_secret) — the platform uses
    one CLIPPER_WEBHOOK_SECRET for both channels, so the two keys
    must hold the same value."""
    st = settings if settings is not None else _settings()
    return (str(st.get("fbos_webhook_secret") or "").strip()
            or str(st.get("pub_webhook_secret") or "").strip())


def api_token(settings=None):
    """Platform JWT for live status reads — NOT used by Step 3.
    Status now reads back over the HMAC-signed endpoint (see
    studio.get_clip_status), so no token is needed. The setting key
    is kept, harmless, for a future user-scoped feature."""
    st = settings if settings is not None else _settings()
    return str(st.get("fbos_api_token") or "").strip()


def config_view(settings=None):
    """Masked config for the UI/API — secret and token never leave."""
    st = settings if settings is not None else _settings()
    return {"platform_url": platform_url(st),
            "dashboard_url": dashboard_url(st),
            "organization_id": organization_id(st),
            "webhook_secret_set": bool(webhook_secret(st)),
            "api_token_set": bool(api_token(st)),
            "configured": bool(organization_id(st)
                               and webhook_secret(st))}


# ------------------------------------------------------------ signing

def sign_webhook(secret, raw_body, timestamp=None):
    """Mirror of @fbos/shared signWebhook: hex HMAC-SHA256 over
    '<timestamp>.<raw body>', header 't=<ts>,v1=<hex>'."""
    ts = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode("utf-8"),
                   ("%d." % ts).encode("utf-8") + raw_body,
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, mac), ts


# ------------------------------------------------------------ identity

def clip_key(job_id, render_index):
    return "%s:%s" % (job_id, int(render_index))


def clip_video_id(job_id, render_index):
    """Stable external video id for the platform (upsert key)."""
    return "clipper:%s:%s" % (job_id, int(render_index))


def clip_event_id(job_id, render_index, file_sha256):
    """Deterministic per clip: a retry reuses it, the platform
    dedupes on event_id."""
    raw = "fbos-clip|%s|%s|%s" % (job_id, int(render_index),
                                  file_sha256 or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _asset_uri(path):
    """file:// URI for an absolute clip path. PureWindowsPath handles
    a Windows path even when this code runs on Linux (QA sandbox)."""
    p = os.path.abspath(path)
    if os.name == "nt":
        return PureWindowsPath(p).as_uri()
    return "file://" + urllib.parse.quote(p)


def _aspect_ratio(path):
    try:
        info = publishing._ffprobe(path)
        w, h = int(info.get("w") or 0), int(info.get("h") or 0)
        if w > 0 and h > 0:
            return "%d:%d" % (w, h) if w == h else (
                "9:16" if h > w else "16:9")
    except Exception:  # noqa: BLE001 - aspect is best-effort
        pass
    return "9:16"


def _duration(path, fallback=0):
    try:
        info = publishing._ffprobe(path)
        if info.get("duration"):
            return round(float(info["duration"]), 2)
    except Exception:  # noqa: BLE001
        pass
    return round(float(fallback or 0), 2)


def build_event(job_id, render_index, path, title="",
                duration=0, aspect_ratio="", thumbnail_uri=""):
    """ClipperCompletedEvent for one finished render. Raises
    ValueError when the clip file is missing."""
    if not path or not os.path.isfile(path):
        raise ValueError("The clip file no longer exists on disk.")
    fhash = publishing.file_sha256(path)
    event = {"event": "video.processing.completed",
             "event_id": clip_event_id(job_id, render_index, fhash),
             "video_id": clip_video_id(job_id, render_index),
             "organization_id": organization_id(),
             "asset_uri": _asset_uri(path),
             "title": title or os.path.basename(path),
             "duration": _duration(path, duration),
             "aspect_ratio": aspect_ratio or _aspect_ratio(path)}
    if thumbnail_uri:
        event["thumbnail_uri"] = thumbnail_uri
    return event


# -------------------------------------------------------------- store

def _empty_doc():
    return {"v": 1, "clips": {}}


def _read_doc():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("clips"), dict):
            return doc
    except Exception:  # noqa: BLE001 - missing/corrupt store starts clean
        pass
    return _empty_doc()


def _write_doc(doc):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, STORE_PATH)


def _mutate(fn):
    with _LOCK:
        doc = _read_doc()
        out = fn(doc)
        _write_doc(doc)
        return out


def get_record(job_id, render_index):
    with _LOCK:
        doc = _read_doc()
    return doc["clips"].get(clip_key(job_id, render_index))


def _record(key, patch):
    def apply(doc):
        rec = doc["clips"].setdefault(key, {"key": key})
        rec.update(patch)
        rec["updated_at"] = time.time()
        return rec
    return _mutate(apply)


def _public_record(rec):
    if not rec:
        return None
    return {k: rec.get(k) for k in
            ("key", "job_id", "render_index", "video_id", "event_id",
             "platform_video_id", "title", "file_path", "status",
             "approval", "jobs_created", "duplicate", "post_urls",
             "last_error", "sent_at", "updated_at")}


# --------------------------------------------------------------- http

class StudioError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.message = str(message)[:400]
        self.status = status


def _http_json(method, url, json_body=None, headers=None, timeout=30):
    hdrs = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            parsed = {}
        msg = ""
        if isinstance(parsed, dict):
            msg = (parsed.get("message") or parsed.get("error") or "")
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
        raise StudioError(msg or "Platform returned HTTP %d." % e.code,
                          status=e.code)
    except Exception as e:  # noqa: BLE001 - DNS/connect/timeout
        raise StudioError("Could not reach the Automation OS at %s "
                          "(%s). Is it running?"
                          % (urllib.parse.urlparse(url).netloc, e))
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return {}


# -------------------------------------------------------------- send

def send_clip_to_platform(clip):
    """Send one finished clip to the platform webhook.

    clip: {job_id, render_index, path, title, duration,
           aspect_ratio, thumbnail_uri?}
    Returns the stored record (public view). Raises StudioError on
    missing config / unreachable platform / platform rejection.
    A retry sends the SAME event_id, so the platform dedupes it."""
    job_id = clip.get("job_id")
    render_index = int(clip.get("render_index") or 0)
    key = clip_key(job_id, render_index)
    org = organization_id()
    secret = webhook_secret()
    if not org:
        raise StudioError("No Automation OS organization id set — "
                          "save it in Settings → Automation OS first.")
    if not secret:
        raise StudioError("No Automation OS shared secret set — save "
                          "it in Settings → Automation OS first.")
    event = build_event(job_id, render_index, clip.get("path") or "",
                        title=clip.get("title") or "",
                        duration=clip.get("duration") or 0,
                        aspect_ratio=clip.get("aspect_ratio") or "",
                        thumbnail_uri=clip.get("thumbnail_uri") or "")
    event["organization_id"] = org
    raw = json.dumps(event).encode("utf-8")
    header, _ts = sign_webhook(secret, raw)
    url = platform_url() + WEBHOOK_PATH
    # Post the EXACT signed bytes: re-encoding the JSON would change
    # the raw body the signature covers.
    req = urllib.request.Request(
        url, data=raw, method="POST",
        headers={SIGNATURE_HEADER: header,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            parsed = {}
        msg = ""
        if isinstance(parsed, dict):
            msg = parsed.get("message") or parsed.get("error") or ""
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
        raise StudioError(msg or "Platform rejected the clip "
                          "(HTTP %d)." % e.code, status=e.code)
    except Exception as e:  # noqa: BLE001
        raise StudioError("Could not reach the Automation OS at %s "
                          "(%s). Is it running?"
                          % (urllib.parse.urlparse(url).netloc, e))
    approval = resp.get("approval") or ""
    status = ("awaiting_approval" if approval == "pending"
              else "queued")
    prev = get_record(job_id, render_index) or {}
    patch = {
        "job_id": job_id, "render_index": render_index,
        "video_id": event["video_id"], "event_id": event["event_id"],
        "platform_video_id": resp.get("videoId") or "",
        "title": event["title"], "file_path": clip.get("path") or "",
        "status": status, "approval": approval,
        "duplicate": bool(resp.get("duplicate")),
        "post_urls": [], "last_error": "",
        "sent_at": time.time()}
    # A duplicate reply carries no jobsCreated — keep the first
    # send's count instead of overwriting it with null.
    if resp.get("jobsCreated") is not None:
        patch["jobs_created"] = resp.get("jobsCreated")
    elif prev.get("jobs_created") is not None:
        patch["jobs_created"] = prev.get("jobs_created")
    rec = _record(key, patch)
    return _public_record(rec)


# ------------------------------------------------------------- status

def _chip(video_status, jobs):
    """UI status chip from the platform video + its jobs."""
    vs = str(video_status or "").lower()
    if vs == "awaiting_approval":
        return "awaiting_approval"
    if vs == "rejected":
        return "rejected"
    published = [j for j in jobs
                 if str(j.get("status") or "").lower() == "published"]
    failed = [j for j in jobs
              if str(j.get("status") or "").lower()
              in ("failed", "dead_letter")]
    if jobs and published and len(published) == len(jobs):
        return "published"
    if failed and not published:
        return "failed"
    if vs in ("registered", "validating", "ready", "queued",
              "scheduled", "importing", "captioning", "publishing"):
        return "queued"
    return vs or "unknown"


def _job_view(j):
    page = j.get("page") or {}
    if isinstance(page, dict):
        page_name = page.get("name") or ""
    else:
        page_name = ""
    page_name = page_name or j.get("pageName") or ""
    url = j.get("publishedUrl") or ""
    result = j.get("result") or {}
    if isinstance(result, dict):
        url = url or result.get("postUrl") or result.get("url") or ""
    return {"id": j.get("id"), "status": j.get("status"),
            "page": page_name,
            "channel": j.get("publishingChannel") or j.get("channel"),
            "post_url": url,
            "published_post_id": j.get("publishedPostId") or "",
            "last_error": j.get("lastError") or "",
            "scheduled_at": j.get("scheduledAt") or ""}


def sign_status(secret, video_id, timestamp=None):
    """Mirror of the platform's signWebhook scheme for the status
    read-back: hex HMAC-SHA256 over '<ts>.GET
    /webhooks/clipper/status/<videoId>' (the /api/v1 prefix is part
    of the URL but NOT of the signed string). A signature for one
    videoId does not authorize reading another."""
    ts = int(timestamp if timestamp is not None else time.time())
    canonical = "GET /webhooks/clipper/status/%s" % video_id
    mac = hmac.new(secret.encode("utf-8"),
                   ("%d.%s" % (ts, canonical)).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, mac)


def get_clip_status(job_id, render_index):
    """Platform status for a sent clip.

    ONE HMAC-signed call (platform
    WebhooksController.clipperStatus):
    GET {platform_url}/api/v1/webhooks/clipper/status/{videoId}
    with x-fbos-signature over the canonical request line, keyed by
    the shared secret. videoId is the stored platform id when we
    have it, else Cliper's own external id
    (clipper:<job_id>:<render_index>) with
    ?organization_id=<org id>. No bearer token is used or needed.
    Returns {"ok": False, "error": "platform_status_unavailable",
    ...} only when the org id / shared secret are not configured;
    the local record is returned alongside so the UI can still show
    the last known state."""
    key = clip_key(job_id, render_index)
    rec = _public_record(get_record(job_id, render_index))
    if not rec:
        return {"ok": False, "error": "not_on_platform",
                "detail": "This clip has not been sent to the "
                          "Automation OS yet (or it was deleted "
                          "there).", "record": None}
    org = organization_id()
    secret = webhook_secret()
    if not org or not secret:
        return {"ok": False,
                "error": "platform_status_unavailable",
                "detail": "No Automation OS organization id or "
                          "shared secret saved, so live status "
                          "cannot be read. Save them in Settings → "
                          "Automation OS (the Studio launcher fills "
                          "them in automatically). Showing the last "
                          "known state instead.",
                "record": rec}
    base = platform_url()
    platform_vid = (rec or {}).get("platform_video_id") or ""
    if platform_vid:
        video_id = platform_vid
        query = ""
    else:
        video_id = clip_video_id(job_id, render_index)
        query = "?organization_id=%s" % urllib.parse.quote(org, safe="")
    header = sign_status(secret, video_id)
    try:
        body = _http_json(
            "GET", "%s/api/v1/webhooks/clipper/status/%s%s"
            % (base, urllib.parse.quote(video_id, safe=""), query),
            headers={SIGNATURE_HEADER: header}, timeout=20)
    except StudioError as e:
        if e.status == 404:
            return {"ok": False, "error": "not_on_platform",
                    "detail": "The Automation OS has no record of "
                              "this clip (it may have been deleted "
                              "there).", "record": rec}
        if e.status == 401:
            return {"ok": False, "error": "platform_unauthorized",
                    "detail": "The Automation OS rejected the "
                              "status signature (HTTP 401). The "
                              "shared secret saved here does not "
                              "match the platform's — re-run "
                              "START-ALL so the launcher re-syncs "
                              "it.", "record": rec}
        return {"ok": False, "error": "platform_unreachable",
                "detail": e.message, "record": rec}
    if not isinstance(body, dict) or not body.get("videoId"):
        return {"ok": False, "error": "platform_unreachable",
                "detail": "The Automation OS returned an "
                          "unexpected status reply.", "record": rec}
    jobs = body.get("jobs") or []
    if not isinstance(jobs, list):
        jobs = []
    job_views = [_job_view(j) for j in jobs if isinstance(j, dict)]
    post_urls = [j["post_url"] for j in job_views if j["post_url"]]
    status = _chip(body.get("status"), jobs)
    _record(key, {"status": status,
                  "platform_video_id": body.get("videoId") or "",
                  "post_urls": post_urls})
    return {"ok": True, "video_id": clip_video_id(job_id, render_index),
            "platform_video_id": body.get("videoId") or "",
            "status": status,
            "video_status": body.get("status") or "",
            "approval": body.get("approval") or "",
            "title": body.get("title") or (rec or {}).get("title") or "",
            "jobs": job_views,
            "post_urls": post_urls,
            "record": _public_record(get_record(job_id, render_index))}
