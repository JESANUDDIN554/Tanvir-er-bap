"""Auto Publishing: Social Accounts, per-platform metadata, quality
gates, and a persistent publish queue with a scheduler.

HONESTY CONTRACT (the product brief's own rule): no fake publishing.
A job only reaches "published" when its platform adapter completes the
platform's official API flow against the platform's real base URL (or a
base URL the user configured). There is no simulation path in this
module: without credentials an adapter reports not_configured and a job
waits in awaiting_connection; it never pretends to have posted.

Credentials live ONLY in the app's server-side settings file. They are
read here through an injected settings loader (app.py binds its own),
never returned by any function in this module (account views mask the
token), and never written to logs or job history.

Persistence mirrors usage.py / the sweep's settings pattern: one JSON
document in the app data dir, written atomically (tmp + os.replace)
under a module lock. Every status transition appends to the job's
history, so the queue dashboard and the admin panel read real records.

The worker is a single background thread. Time comes from _now() so a
test rig can drive the scheduler through the exact production code path
with an accelerated clock. Speed knobs for tests come from env vars:
ITRAJI_PUB_TICK (seconds between worker passes, default 2),
ITRAJI_PUB_BACKOFF (base backoff seconds, default 30),
ITRAJI_PUB_POLL (seconds between processing polls, default 5).
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE = _DIR
DATA_DIR = os.environ.get("ITRAJI_LOCAL_DATA_DIR") or _BASE
STORE_PATH = os.path.join(DATA_DIR, "publishing.json")

_LOCK = threading.Lock()
_SETTINGS_LOCK = threading.Lock()
_settings_loader = None
_settings_saver = None

PLATFORMS = ("facebook", "instagram", "youtube", "tiktok")
PLATFORM_LABELS = {"facebook": "Facebook", "instagram": "Instagram",
                   "youtube": "YouTube", "tiktok": "TikTok"}

# Per-platform requirements for the quality gate. Vertical-first
# formats (Reels / Shorts / TikTok) require a portrait frame; the
# duration caps follow each platform's current published limits for
# those formats (FB Reels 90s, IG Reels 3min, YT Shorts 3min,
# TikTok in-app uploads 10min). Audio is required everywhere: a silent
# "clip" is a broken render, not a post.
REQUIREMENTS = {
    "facebook": {"vertical": True, "max_duration": 90,
                 "label": "Facebook Reels"},
    "instagram": {"vertical": True, "max_duration": 180,
                  "label": "Instagram Reels"},
    "youtube": {"vertical": True, "max_duration": 180,
                "label": "YouTube Shorts"},
    "tiktok": {"vertical": True, "max_duration": 600,
               "label": "TikTok"},
}

ACTIVE_STATUSES = ("awaiting_approval", "waiting", "ready", "uploading",
                   "publishing", "scheduled", "paused",
                   "awaiting_connection")
MAX_ATTEMPTS = 5
MAX_HISTORY = 80
MAX_OUTBOX = 200

_SETUP_STEPS = {
    "facebook": [
        "Create an app at developers.facebook.com (type: Business).",
        "Add the Facebook Login product and request pages_show_list, "
        "pages_read_engagement and pages_manage_posts.",
        "Generate a User access token in the Graph API Explorer, "
        "exchange it for a long-lived token, then get the Page access "
        "token for your Page.",
        "Paste the App ID, App Secret, your Page ID and the Page "
        "access token here."],
    "instagram": [
        "Use the same Meta app as Facebook (developers.facebook.com).",
        "Link your Instagram professional account to your Facebook "
        "Page in Meta Accounts Center.",
        "Request instagram_basic and instagram_content_publish.",
        "Paste your Instagram user ID and a User access token with "
        "those permissions here."],
    "youtube": [
        "Create a project at console.cloud.google.com and enable the "
        "YouTube Data API v3.",
        "Create an OAuth client (Desktop app) and note the Client ID "
        "and Client Secret.",
        "Run the OAuth consent flow for your channel and copy the "
        "refresh token it returns.",
        "Paste the Client ID, Client Secret and refresh token here."],
    "tiktok": [
        "Create an app at developers.tiktok.com and add the Content "
        "Posting API product.",
        "Request the video.publish (and video.upload) scopes; until "
        "TikTok audits your app, posts are private (SELF_ONLY).",
        "Complete the OAuth flow for your TikTok account and copy "
        "the access token.",
        "Paste the Client Key, Client Secret and access token here."],
}


def setup_steps(platform):
    return list(_SETUP_STEPS.get(platform, []))


def _now():
    return time.time()


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


def bind_settings(loader, saver=None):
    """app.py binds its load_settings/save machinery once at startup.
    saver(patch_dict) merges server-side so status write-backs never
    race the UI's settings saves and never see masked values."""
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
    except Exception:  # noqa: BLE001 - publishing must never crash the app
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


# ---------------------------------------------------------------- store

def _empty_doc():
    return {"v": 1, "jobs": [], "outbox": [], "pause_all": False,
            "last_upload": {}}


def _read_doc():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("jobs"), list):
            doc.setdefault("outbox", [])
            doc.setdefault("pause_all", False)
            doc.setdefault("last_upload", {})
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


def _log(job, event, detail=""):
    job.setdefault("history", []).append(
        {"ts": _now(), "event": event, "detail": str(detail)[:300]})
    job["history"] = job["history"][-MAX_HISTORY:]
    job["updated_at"] = _now()


def _set_status(job, status, detail=""):
    job["status"] = status
    _log(job, status, detail)


# --------------------------------------------------------- file probing

def _ffprobe(path):
    """Return {w, h, duration, has_audio} or raise ValueError."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration",
             "-of", "json", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=30)
        data = json.loads(r.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        raise ValueError("could not probe the file (%s)" % e)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"),
                 None)
    if not video:
        raise ValueError("no video stream found")
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    return {"w": int(video.get("width") or 0),
            "h": int(video.get("height") or 0),
            "duration": dur,
            "has_audio": any(s.get("codec_type") == "audio"
                             for s in streams)}


def quality_gate(path, platform):
    """Check one clip file against one platform's table.
    Returns (ok, [reasons], info). Reasons are plain language, meant to
    be shown to the user verbatim."""
    req = REQUIREMENTS[platform]
    if not path or not os.path.isfile(path):
        return False, ["The clip file no longer exists on disk."], {}
    try:
        info = _ffprobe(path)
    except ValueError as e:
        return False, [str(e)], {}
    reasons = []
    if req["vertical"] and not (info["h"] > info["w"] > 0):
        reasons.append(
            "%s needs a vertical video (this clip is %dx%d, landscape "
            "or square)." % (req["label"], info["w"], info["h"]))
    if info["duration"] and info["duration"] > req["max_duration"]:
        reasons.append(
            "%s allows at most %ds for this format (this clip is %ds)."
            % (req["label"], req["max_duration"],
               int(round(info["duration"]))))
    if not info["has_audio"]:
        reasons.append("The clip has no audio track — %s posts need "
                       "sound." % req["label"])
    return (not reasons), reasons, info


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def idempotency_key(file_hash, platform, account_id):
    raw = "%s|%s|%s" % (file_hash, platform, account_id or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- accounts

APP_CRED_FIELDS = {
    # platform -> [(settings key, label, secret)]
    "facebook": [("pub_fb_app_id", "App ID", False),
                 ("pub_fb_app_secret", "App Secret", True)],
    "instagram": [("pub_fb_app_id", "Meta App ID", False),
                  ("pub_fb_app_secret", "Meta App Secret", True)],
    "youtube": [("pub_yt_client_id", "Client ID", False),
                ("pub_yt_client_secret", "Client Secret", True)],
    "tiktok": [("pub_tt_client_key", "Client Key", False),
               ("pub_tt_client_secret", "Client Secret", True)],
}

BASE_KEYS = {"facebook": "pub_fb_base", "instagram": "pub_fb_base",
             "youtube": "pub_yt_base", "tiktok": "pub_tt_base"}
DEFAULT_BASES = {"facebook": "https://graph.facebook.com/v21.0",
                 "instagram": "https://graph.facebook.com/v21.0",
                 "youtube": "https://www.googleapis.com",
                 "tiktok": "https://open.tiktokapis.com"}


def platform_base(platform, settings=None):
    st = settings if settings is not None else _settings()
    base = (st.get(BASE_KEYS[platform]) or "").strip()
    return (base or DEFAULT_BASES[platform]).rstrip("/")


def accounts(settings=None):
    st = settings if settings is not None else _settings()
    out = []
    for a in st.get("pub_accounts") or []:
        if isinstance(a, dict) and a.get("platform") in PLATFORMS:
            out.append(a)
    return out


def get_account(account_id, settings=None):
    for a in accounts(settings):
        if a.get("id") == account_id:
            return a
    return None


def default_account_id(platform, settings=None):
    st = settings if settings is not None else _settings()
    defaults = st.get("pub_default_accounts") or {}
    if defaults.get(platform):
        return defaults[platform]
    for a in accounts(st):
        if a.get("platform") == platform:
            return a.get("id")
    return None


def account_view(account, settings=None):
    """Token-free view of an account for API responses."""
    st = settings if settings is not None else _settings()
    status_map = st.get("pub_account_status") or {}
    last = status_map.get(account.get("id")) or {}
    return {"id": account.get("id"), "platform": account.get("platform"),
            "label": account.get("label") or account.get("external_id")
            or "Account",
            "external_id": account.get("external_id") or "",
            "has_token": bool((account.get("token") or "").strip()),
            "is_default": default_account_id(
                account.get("platform"), st) == account.get("id"),
            "last_test": last}


def platform_config(platform, settings=None):
    """What an adapter needs + what is missing. Never includes token
    values in the 'missing' report; the adapter gets them via
    resolve_account_config()."""
    st = settings if settings is not None else _settings()
    missing = []
    for key, label, _secret in APP_CRED_FIELDS[platform]:
        if not str(st.get(key) or "").strip():
            missing.append(label)
    accts = [a for a in accounts(st) if a.get("platform") == platform]
    if not accts:
        missing.append("an account (add one below)")
    return {"platform": platform, "missing": missing,
            "configured": not missing,
            "accounts": [account_view(a, st) for a in accts],
            "base": platform_base(platform, st)}


def resolve_account_config(platform, account_id, settings=None):
    """Full server-side config for one adapter run (includes token).
    Internal only — never serialise this to the client."""
    st = settings if settings is not None else _settings()
    account = get_account(account_id, st) if account_id else None
    if account is None:
        aid = default_account_id(platform, st)
        account = get_account(aid, st) if aid else None
    cfg = {"platform": platform, "base": platform_base(platform, st),
           "account": account,
           "account_id": (account or {}).get("id"),
           "external_id": (account or {}).get("external_id") or "",
           "token": ((account or {}).get("token") or "").strip(),
           "settings": st}
    for key, _label, _secret in APP_CRED_FIELDS[platform]:
        cfg[key] = str(st.get(key) or "").strip()
    return cfg


# -------------------------------------------------------------- errors

class PubError(Exception):
    """An adapter failure. retryable=True for network/5xx/rate-limit;
    kind='auth' for expired/invalid tokens (job waits for reconnection
    instead of burning attempts)."""

    def __init__(self, message, retryable=False, kind="", retry_after=0,
                 status=0):
        super().__init__(message)
        self.message = str(message)[:400]
        self.retryable = bool(retryable)
        self.kind = kind
        self.retry_after = float(retry_after or 0)
        self.status = status


# ----------------------------------------------------------------- http

def _http(method, url, data=None, json_body=None, headers=None,
          timeout=120):
    """One HTTP call through urllib. Returns (status, headers, body
    bytes). HTTP error statuses are returned, not raised, so adapters
    can classify them; transport errors raise PubError(retryable)."""
    hdrs = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type",
                        "application/x-www-form-urlencoded")
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:  # noqa: BLE001
            payload = b""
        return e.code, dict(e.headers or {}), payload
    except Exception as e:  # noqa: BLE001 - DNS/connect/timeout
        raise PubError("Network error reaching %s: %s"
                       % (urllib.parse.urlparse(url).netloc, e),
                       retryable=True)


def _http_json(method, url, **kw):
    status, headers, body = _http(method, url, **kw)
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        parsed = {}
    return status, headers, parsed


def _classify(status, payload, platform_label):
    """Turn an error HTTP response into a PubError."""
    err = {}
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(payload.get("data"), dict):
            pass
    msg = ""
    if isinstance(err, dict):
        msg = err.get("message") or ""
        code = err.get("code")
        if code in (190, 102, 401) or status == 401:
            return PubError(msg or "The saved token was rejected.",
                            kind="auth", status=status)
    if not msg and isinstance(payload, dict):
        msg = payload.get("error_description") or ""
    if not msg:
        msg = "%s returned HTTP %d." % (platform_label, status)
    if status == 429:
        return PubError(msg, retryable=True, status=status)
    if status >= 500:
        return PubError(msg, retryable=True, status=status)
    return PubError(msg, retryable=False, status=status)


def _retry_after(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0
    return 0


def _read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


# ------------------------------------------------------------- adapters
# Common interface: test_connection(cfg), upload(cfg, job) ->
# {"remote_id": str}, check_status(cfg, job) ->
# {"state": "processing"|"done"|"failed", "url": str, "detail": str}.
# upload() performs the transfer phase; check_status() drives the
# processing/publish phase until done. Both raise PubError on failure.

class MetaFacebookAdapter:
    platform = "facebook"
    label = "Facebook"

    def test_connection(self, cfg):
        page = cfg["external_id"]
        status, _h, payload = _http_json(
            "GET", "%s/%s?fields=id,name&access_token=%s"
            % (cfg["base"], page,
               urllib.parse.quote(cfg["token"], safe="")))
        if status != 200:
            raise _classify(status, payload, self.label)
        return {"account_name": payload.get("name") or page,
                "detail": "Page reachable with the saved token."}

    def upload(self, cfg, job):
        page = cfg["external_id"]
        token = cfg["token"]
        status, _h, payload = _http_json(
            "POST", "%s/%s/video_reels" % (cfg["base"], page),
            data={"upload_phase": "start", "access_token": token})
        if status != 200 or not payload.get("video_id"):
            raise _classify(status, payload, self.label)
        video_id = str(payload["video_id"])
        upload_url = payload.get("upload_url") or ""
        body = _read_file(job["file_path"])
        status, headers, _b = _http(
            "POST", upload_url, data=body,
            headers={"Authorization": "OAuth %s" % token,
                     "file_size": str(len(body)), "offset": "0",
                     "Content-Type": "application/octet-stream"})
        if status not in (200, 201):
            err = _classify(status, {}, self.label)
            if status == 429:
                err.retry_after = _retry_after(headers)
            raise err
        caption = (job.get("metadata") or {}).get("caption") or \
            (job.get("metadata") or {}).get("title") or ""
        status, _h, payload = _http_json(
            "POST", "%s/%s/video_reels" % (cfg["base"], page),
            data={"upload_phase": "finish", "video_id": video_id,
                  "video_state": "PUBLISHED", "description": caption,
                  "access_token": token})
        if status != 200:
            raise _classify(status, payload, self.label)
        return {"remote_id": video_id}

    def check_status(self, cfg, job):
        vid = job.get("remote_id")
        status, _h, payload = _http_json(
            "GET", "%s/%s?fields=status,permalink_url&access_token=%s"
            % (cfg["base"], vid,
               urllib.parse.quote(cfg["token"], safe="")))
        if status != 200:
            raise _classify(status, payload, self.label)
        st = payload.get("status") or {}
        vstat = (st.get("video_status") if isinstance(st, dict)
                 else "") or payload.get("status") or ""
        vstat = str(vstat).lower()
        if vstat in ("ready", "published", "complete", "uploaded"):
            url = payload.get("permalink_url") or \
                "https://www.facebook.com/reel/%s" % vid
            return {"state": "done", "url": url}
        if vstat in ("error", "failed"):
            return {"state": "failed",
                    "detail": "Facebook reported the video failed "
                              "processing."}
        return {"state": "processing"}


class MetaInstagramAdapter:
    platform = "instagram"
    label = "Instagram"

    def test_connection(self, cfg):
        status, _h, payload = _http_json(
            "GET", "%s/%s?fields=id,username&access_token=%s"
            % (cfg["base"], cfg["external_id"],
               urllib.parse.quote(cfg["token"], safe="")))
        if status != 200:
            raise _classify(status, payload, self.label)
        return {"account_name": payload.get("username")
                or cfg["external_id"],
                "detail": "Instagram account reachable with the "
                          "saved token."}

    def upload(self, cfg, job):
        ig = cfg["external_id"]
        meta = job.get("metadata") or {}
        caption = meta.get("caption") or meta.get("title") or ""
        status, _h, payload = _http_json(
            "POST", "%s/%s/media" % (cfg["base"], ig),
            data={"media_type": "REELS", "upload_type": "resumable",
                  "caption": caption, "access_token": cfg["token"]})
        if status != 200 or not payload.get("id"):
            raise _classify(status, payload, self.label)
        container = str(payload["id"])
        uri = payload.get("uri") or ""
        body = _read_file(job["file_path"])
        status, headers, _b = _http(
            "POST", uri, data=body,
            headers={"Authorization": "OAuth %s" % cfg["token"],
                     "file_size": str(len(body)), "offset": "0",
                     "Content-Type": "application/octet-stream"})
        if status not in (200, 201):
            err = _classify(status, {}, self.label)
            if status == 429:
                err.retry_after = _retry_after(headers)
            raise err
        return {"remote_id": container}

    def check_status(self, cfg, job):
        cid = job.get("remote_id")
        token = cfg["token"]
        status, _h, payload = _http_json(
            "GET", "%s/%s?fields=status_code&access_token=%s"
            % (cfg["base"], cid, urllib.parse.quote(token, safe="")))
        if status != 200:
            raise _classify(status, payload, self.label)
        code = str(payload.get("status_code") or "").upper()
        if code in ("ERROR", "EXPIRED"):
            return {"state": "failed",
                    "detail": "Instagram container status: %s" % code}
        if code != "FINISHED":
            return {"state": "processing"}
        if not (job.get("meta") or {}).get("ig_publish_called"):
            status, _h, payload = _http_json(
                "POST", "%s/%s/media_publish" % (cfg["base"],
                                                 cfg["external_id"]),
                data={"creation_id": cid, "access_token": token})
            if status != 200 or not payload.get("id"):
                raise _classify(status, payload, self.label)
            job.setdefault("meta", {})["ig_publish_called"] = True
            job["meta"]["media_id"] = str(payload["id"])
            job["remote_id"] = str(payload["id"])
            return {"state": "processing",
                    "detail": "Publish requested; confirming."}
        media_id = (job.get("meta") or {}).get("media_id") or cid
        status, _h, payload = _http_json(
            "GET", "%s/%s?fields=permalink&access_token=%s"
            % (cfg["base"], media_id,
               urllib.parse.quote(token, safe="")))
        url = ""
        if status == 200:
            url = payload.get("permalink") or ""
        return {"state": "done",
                "url": url or "https://www.instagram.com/reel/%s/"
                % media_id}


class YouTubeAdapter:
    platform = "youtube"
    label = "YouTube"

    def _access_token(self, cfg):
        base = cfg["base"]
        if base.rstrip("/") == DEFAULT_BASES["youtube"]:
            token_url = "https://oauth2.googleapis.com/token"
        else:
            token_url = base + "/oauth2/token"
        status, _h, payload = _http_json(
            "POST", token_url,
            data={"client_id": cfg.get("pub_yt_client_id") or "",
                  "client_secret": cfg.get("pub_yt_client_secret")
                  or "",
                  "refresh_token": cfg["token"],
                  "grant_type": "refresh_token"})
        if status != 200 or not payload.get("access_token"):
            err = _classify(status, payload, self.label)
            if status in (400, 401):
                err.kind = "auth"
                err.message = ("The YouTube refresh token was "
                               "rejected — reconnect the account.")
            raise err
        return payload["access_token"]

    def test_connection(self, cfg):
        access = self._access_token(cfg)
        status, _h, payload = _http_json(
            "GET", "%s/youtube/v3/channels?part=snippet&mine=true"
            % cfg["base"],
            headers={"Authorization": "Bearer %s" % access})
        if status != 200:
            raise _classify(status, payload, self.label)
        items = payload.get("items") or []
        name = ""
        if items:
            name = ((items[0].get("snippet") or {}).get("title")
                    or "")
        return {"account_name": name or "YouTube channel",
                "detail": "Refresh token exchanges and the channel "
                          "is readable."}

    def upload(self, cfg, job):
        access = self._access_token(cfg)
        meta = job.get("metadata") or {}
        snippet = {"title": (meta.get("title") or "")[:100],
                   "description": meta.get("description") or "",
                   "tags": [t.lstrip("#") for t in
                            (meta.get("hashtags") or [])][:15],
                   "categoryId": "24"}
        visibility = (meta.get("visibility") or "public").lower()
        privacy = {"public": "public", "private": "private",
                   "unlisted": "unlisted"}.get(visibility, "public")
        init = {"snippet": snippet,
                "status": {"privacyStatus": privacy,
                           "selfDeclaredMadeForKids": False}}
        size = os.path.getsize(job["file_path"])
        status, headers, _b = _http(
            "POST", "%s/upload/youtube/v3/videos?uploadType="
            "resumable&part=snippet,status" % cfg["base"],
            json_body=init,
            headers={"Authorization": "Bearer %s" % access,
                     "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)})
        if status != 200:
            raise _classify(status, {}, self.label)
        location = ""
        for k, v in headers.items():
            if k.lower() == "location":
                location = v
        if not location:
            raise PubError("YouTube did not return an upload URL.",
                           retryable=True)
        body = _read_file(job["file_path"])
        status, headers, payload_bytes = _http(
            "PUT", location, data=body,
            headers={"Content-Type": "video/mp4",
                     "Content-Length": str(len(body))})
        if status == 429:
            err = PubError("YouTube rate limit.", retryable=True,
                           status=429)
            err.retry_after = _retry_after(headers)
            raise err
        if status not in (200, 201):
            raise _classify(status, {}, self.label)
        try:
            payload = json.loads(payload_bytes.decode("utf-8")
                                 or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        vid = str(payload.get("id") or "")
        if not vid:
            raise PubError("YouTube upload finished without a "
                           "video id.", retryable=True)
        return {"remote_id": vid}

    def check_status(self, cfg, job):
        access = self._access_token(cfg)
        status, _h, payload = _http_json(
            "GET", "%s/youtube/v3/videos?part=status&id=%s"
            % (cfg["base"], job.get("remote_id")),
            headers={"Authorization": "Bearer %s" % access})
        if status != 200:
            raise _classify(status, payload, self.label)
        items = payload.get("items") or []
        if not items:
            return {"state": "processing"}
        ustat = ((items[0].get("status") or {}).get("uploadStatus")
                 or "")
        if ustat == "processed":
            vid = job.get("remote_id")
            if (job.get("info") or {}).get("vertical"):
                url = "https://youtube.com/shorts/%s" % vid
            else:
                url = "https://www.youtube.com/watch?v=%s" % vid
            return {"state": "done", "url": url}
        if ustat in ("failed", "rejected"):
            detail = ((items[0].get("status") or {})
                      .get("failureReason") or ustat)
            return {"state": "failed",
                    "detail": "YouTube upload status: %s" % detail}
        return {"state": "processing"}


class TikTokAdapter:
    platform = "tiktok"
    label = "TikTok"

    def _audited(self, cfg):
        return bool((cfg.get("settings") or {}).get("pub_tt_audited"))

    def connection_note(self, cfg):
        if not self._audited(cfg):
            return ("This TikTok app is not yet audited by TikTok — "
                    "the Content Posting API limits unaudited clients "
                    "to private (SELF_ONLY) posts. The post still "
                    "uploads for real; only its audience is limited "
                    "until TikTok approves the app.")
        return ""

    def _tt_error(self, status, payload):
        """TikTok answers many errors as HTTP 200 with error.code set.
        Returns a PubError or None."""
        err = payload.get("error") or {}
        code = err.get("code")
        if status != 200:
            return _classify(status, payload, self.label)
        if code not in (None, "", "ok", 0):
            return PubError("TikTok: %s"
                            % (err.get("message") or code),
                            kind="auth" if "token" in
                            str(code).lower() or "auth" in
                            str(code).lower() else "",
                            retryable=False)
        return None

    def test_connection(self, cfg):
        status, _h, payload = _http_json(
            "GET", "%s/v2/user/info/?fields=open_id,display_name"
            % cfg["base"],
            headers={"Authorization": "Bearer %s" % cfg["token"]})
        err = self._tt_error(status, payload)
        if err:
            raise err
        data = (payload.get("data") or {}).get("user") or {}
        return {"account_name": data.get("display_name")
                or cfg["external_id"] or "TikTok account",
                "detail": "Account reachable with the saved token. "
                          + self.connection_note(cfg)}

    def upload(self, cfg, job):
        meta = job.get("metadata") or {}
        size = os.path.getsize(job["file_path"])
        chunk = size if size <= 64 * 1024 * 1024 else 32 * 1024 * 1024
        total_chunks = max(1, -(-size // chunk))
        audited = self._audited(cfg)
        visibility = (meta.get("visibility") or "public").lower()
        privacy = "PUBLIC_TO_EVERYONE" if (audited and
                                           visibility == "public") \
            else "SELF_ONLY"
        title = (meta.get("title") or meta.get("caption") or "")[:2200]
        status, _h, payload = _http_json(
            "POST", "%s/v2/post/publish/video/init/" % cfg["base"],
            json_body={
                "post_info": {"title": title,
                              "privacy_level": privacy,
                              "disable_comment": False},
                "source_info": {"source": "FILE_UPLOAD",
                                "video_size": size,
                                "chunk_size": chunk,
                                "total_chunk_count": total_chunks}},
            headers={"Authorization": "Bearer %s" % cfg["token"]})
        data = payload.get("data") or {}
        err = payload.get("error") or {}
        if status != 200 or not data.get("publish_id"):
            if isinstance(err, dict) and err.get("message"):
                raise PubError("TikTok: %s" % err["message"],
                               retryable=status >= 500 or status == 429,
                               kind="auth" if status == 401 else "",
                               status=status)
            raise _classify(status, payload, self.label)
        publish_id = str(data["publish_id"])
        upload_url = data.get("upload_url") or ""
        body = _read_file(job["file_path"])
        offset = 0
        part = 0
        while offset < size:
            piece = body[offset:offset + chunk]
            status, headers, _b = _http(
                "PUT", upload_url, data=piece,
                headers={"Content-Type": "video/mp4",
                         "Content-Length": str(len(piece)),
                         "Content-Range": "bytes %d-%d/%d"
                         % (offset, offset + len(piece) - 1, size)})
            if status == 429:
                e = PubError("TikTok rate limit.", retryable=True,
                             status=429)
                e.retry_after = _retry_after(headers)
                raise e
            if status not in (200, 201):
                raise _classify(status, {}, self.label)
            offset += len(piece)
            part += 1
        return {"remote_id": publish_id}

    def check_status(self, cfg, job):
        status, _h, payload = _http_json(
            "POST", "%s/v2/post/publish/status/fetch/" % cfg["base"],
            json_body={"publish_id": job.get("remote_id")},
            headers={"Authorization": "Bearer %s" % cfg["token"]})
        err = self._tt_error(status, payload)
        if err:
            raise err
        data = payload.get("data") or {}
        st = str(data.get("status") or "").upper()
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            url = ""
            if data.get("post_id"):
                url = "https://www.tiktok.com/video/%s" % data["post_id"]
            detail = ""
            if st == "SEND_TO_USER_INBOX" or not url:
                detail = ("Posted to the account (TikTok returned no "
                          "public URL — expected for unaudited apps "
                          "posting privately).")
            return {"state": "done", "url": url, "detail": detail}
        if st == "FAILED":
            return {"state": "failed",
                    "detail": "TikTok: %s"
                    % (data.get("fail_reason") or "publish failed")}
        return {"state": "processing"}


ADAPTERS = {"facebook": MetaFacebookAdapter(),
            "instagram": MetaInstagramAdapter(),
            "youtube": YouTubeAdapter(),
            "tiktok": TikTokAdapter()}


def test_connection(platform, account_id):
    """A REAL read-only call through the adapter. Returns {ok, ...}
    and records the outcome on the account (for status display)."""
    cfg = resolve_account_config(platform, account_id)
    acct = cfg.get("account")
    missing = platform_config(platform)["missing"]
    if missing:
        return {"ok": False, "state": "not_configured",
                "error": "Missing: %s." % ", ".join(missing),
                "setup_steps": setup_steps(platform)}
    if acct is None:
        return {"ok": False, "state": "not_configured",
                "error": "No account saved for %s."
                % PLATFORM_LABELS[platform],
                "setup_steps": setup_steps(platform)}
    if not cfg["token"]:
        return {"ok": False, "state": "not_connected",
                "error": "This account has no token saved yet."}
    adapter = ADAPTERS[platform]
    try:
        info = adapter.test_connection(cfg)
        state = "connected"
        detail = info.get("detail") or "Connected."
        ok = True
    except PubError as e:
        ok = False
        state = "token_expired" if e.kind == "auth" else "error"
        detail = e.message
        info = {}
    record = {"ts": _now(), "ok": ok, "state": state, "detail": detail,
              "account_name": info.get("account_name") or ""}
    st = _settings()
    status_map = dict(st.get("pub_account_status") or {})
    status_map[acct.get("id")] = record
    _save_settings_patch({"pub_account_status": status_map})
    out = {"ok": ok, "state": state, "detail": detail,
           "account_name": record["account_name"]}
    if platform == "tiktok":
        note = ADAPTERS["tiktok"].connection_note(cfg)
        if note:
            out["note"] = note
    return out


def connection_state(platform, settings=None):
    """Per-platform card state for the Social Accounts view (no live
    call — uses the recorded last test + what's configured)."""
    st = settings if settings is not None else _settings()
    conf = platform_config(platform, st)
    state = "connected"
    if conf["missing"]:
        state = "not_configured"
    elif not conf["accounts"]:
        state = "not_configured"
    else:
        status_map = st.get("pub_account_status") or {}
        states = []
        for a in conf["accounts"]:
            last = status_map.get(a["id"]) or {}
            if not a["has_token"]:
                states.append("not_connected")
            elif last.get("state"):
                states.append(last["state"])
            else:
                states.append("not_connected")
        if "connected" in states:
            state = "connected"
        elif "token_expired" in states:
            state = "token_expired"
        elif "error" in states:
            state = "error"
        else:
            state = "not_connected"
    out = {"platform": platform, "label": PLATFORM_LABELS[platform],
           "state": state, "missing": conf["missing"],
           "accounts": conf["accounts"],
           "app_fields": [{"key": k, "label": lbl, "secret": sec,
                           "set": bool(str(st.get(k) or "").strip())}
                          for k, lbl, sec in APP_CRED_FIELDS[platform]],
           "setup_steps": setup_steps(platform),
           "base": conf["base"]}
    if platform == "tiktok":
        cfg = resolve_account_config(platform, None, st)
        note = ADAPTERS["tiktok"].connection_note(cfg)
        if note and not conf["missing"]:
            out["note"] = note
    return out


# ------------------------------------------------------------- metadata

def _clean_tags(tags, extra=()):
    out, seen = [], set()
    for t in list(tags or []) + list(extra):
        t = str(t or "").strip()
        if not t:
            continue
        t = t if t.startswith("#") else "#" + t.lstrip("#")
        t = t.replace(" ", "")
        if t.lower() not in seen and len(t) > 1:
            seen.add(t.lower())
            out.append(t)
    return out


def _first_sentence(text, cap=140):
    text = " ".join(str(text or "").split())
    for sep in (". ", "! ", "? "):
        if sep in text:
            text = text.split(sep)[0] + sep.strip()[-1]
            break
    return text[:cap].rstrip()


def _trim_title(title, cap=100):
    title = " ".join(str(title or "").split())
    if len(title) <= cap:
        return title
    cut = title[:cap].rsplit(" ", 1)[0]
    return (cut or title[:cap]).rstrip(" ,;:-")


def platform_metadata(pkg, platform, clip_title="", vertical=True,
                      visibility="public"):
    """Build ONE platform's genuinely-shaped variant from a posting
    package ({titles, description, hashtags}). Pure text work — the
    package itself comes from the existing generator (AI or offline
    extractive), so variants differ because each platform's format is
    assembled differently, not because text is copied."""
    titles = [t for t in (pkg.get("titles") or []) if str(t).strip()]
    desc = " ".join(str(pkg.get("description") or "").split())
    tags = _clean_tags(pkg.get("hashtags") or [])
    base_title = titles[0] if titles else (clip_title or "New clip")
    if platform == "youtube":
        tag_words = [t.lstrip("#") for t in tags][:15]
        full_desc = desc
        if vertical and "#Shorts" not in full_desc:
            full_desc = (full_desc + "\n\n#Shorts").strip()
        if tags:
            full_desc = (full_desc + "\n" + " ".join(tags[:5])).strip()
        return {"title": _trim_title(base_title, 100),
                "description": full_desc[:4900], "caption": "",
                "hashtags": tags, "tags": tag_words,
                "visibility": visibility}
    if platform == "facebook":
        caption = desc or base_title
        if tags:
            caption = caption + "\n\n" + " ".join(tags[:8])
        return {"title": "", "description": "",
                "caption": caption[:2200], "hashtags": tags[:8],
                "visibility": visibility}
    if platform == "instagram":
        short = _first_sentence(desc) or base_title
        ig_tags = _clean_tags(tags, ["#reels"])
        block = " ".join(ig_tags[:12])
        caption = short + ("\n\n.\n.\n" + block if block else "")
        return {"title": "", "description": "", "caption": caption,
                "hashtags": ig_tags[:12], "visibility": visibility}
    # tiktok
    hook = _trim_title(base_title, 90)
    tt_desc = hook
    tt_tags = _clean_tags(tags)
    if tt_tags:
        tt_desc = tt_desc + " " + " ".join(tt_tags[:6])
    return {"title": hook, "description": tt_desc[:2200],
            "caption": tt_desc, "hashtags": tt_tags[:6],
            "visibility": visibility}


def assistant_options(pkg, platform_tag="", vertical=True):
    """AI Publishing Assistant panel data: 5 title options, 3 caption
    options, hashtags, a recommended platform and time slots. All
    labelled suggestions in the UI; derived from the clip's own
    posting package (AI or extractive) — never invented facts."""
    titles = [str(t).strip() for t in (pkg.get("titles") or [])
              if str(t).strip()]
    desc = " ".join(str(pkg.get("description") or "").split())
    out_titles = list(titles[:3])
    hook = titles[0] if titles else ""
    if hook:
        q = hook if hook.endswith("?") else \
            "What happens next? " + hook
        out_titles.append(_trim_title(q, 100))
        out_titles.append(_trim_title("Watch till the end: " + hook,
                                      100))
    seen, titles5 = set(), []
    for t in out_titles:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            titles5.append(t)
    captions = []
    if desc:
        captions.append(desc)
        captions.append(_first_sentence(desc))
    if hook:
        captions.append(hook + (" " + desc if desc and
                                desc not in hook else ""))
    captions = [c for c in dict.fromkeys(captions) if c][:3]
    slots = {"facebook": "7:00 PM", "instagram": "6:30 PM",
             "youtube": "5:00 PM", "tiktok": "8:00 PM"}
    recommended = platform_tag if platform_tag in PLATFORMS else \
        "facebook"
    return {"titles": titles5[:5], "captions": captions,
            "hashtags": _clean_tags(pkg.get("hashtags") or []),
            "recommended_platform": recommended,
            "recommended_slots": slots,
            "note": "Suggestions from this clip's own titles, "
                    "description and hashtags — edit anything before "
                    "queueing."}


# ------------------------------------------------------------ job store

def _public_job(job):
    out = {k: job.get(k) for k in
           ("id", "clip_job_id", "render_index", "title", "file_name",
            "thumb_url", "platform", "account_id", "account_label",
            "metadata", "status", "scheduled_at", "created_at",
            "updated_at", "attempts", "last_error", "remote_id",
            "published_url", "published_at", "publish_note",
            "source", "info")}
    out["history"] = (job.get("history") or [])[-12:]
    return out


def create_job(item):
    """Create one job after the quality gate + duplicate guard.
    item: {file_path, title, clip_job_id, render_index, thumb_url,
    platform, account_id, metadata, scheduled_at, override, source}.
    Returns (job_or_None, refusal_reason_or_None)."""
    platform = item.get("platform")
    if platform not in PLATFORMS:
        return None, "Unknown platform."
    path = item.get("file_path") or ""
    ok, reasons, info = quality_gate(path, platform)
    if not ok:
        return None, " ".join(reasons)
    fhash = file_sha256(path)
    key = idempotency_key(fhash, platform, item.get("account_id") or "")
    st = _settings()
    approval = (item.get("approval") or
                st.get("pub_approval_mode") or "manual")
    scheduled_at = item.get("scheduled_at") or None

    def build(doc):
        for j in doc["jobs"]:
            if j.get("key") == key and j.get("status") in \
                    ACTIVE_STATUSES + ("published",):
                if not item.get("override"):
                    return ("dup", j)
        now = _now()
        status = "awaiting_approval" if approval != "auto" else (
            "scheduled" if scheduled_at and scheduled_at > now
            else "ready")
        acct = get_account(item.get("account_id") or "", st)
        job = {
            "id": uuid.uuid4().hex[:16], "key": key,
            "file_path": path, "file_sha256": fhash,
            "file_name": os.path.basename(path),
            "title": item.get("title") or os.path.basename(path),
            "clip_job_id": item.get("clip_job_id") or "",
            "render_index": item.get("render_index"),
            "thumb_url": item.get("thumb_url") or "",
            "platform": platform,
            "account_id": item.get("account_id") or "",
            "account_label": (acct or {}).get("label")
            or (acct or {}).get("external_id") or "",
            "metadata": item.get("metadata") or {},
            "status": status, "scheduled_at": scheduled_at,
            "created_at": now, "updated_at": now, "attempts": 0,
            "next_attempt_at": 0, "last_error": "",
            "remote_id": "", "published_url": "", "published_at": 0,
            "source": item.get("source") or "manual",
            "info": {"w": info.get("w"), "h": info.get("h"),
                     "duration": round(info.get("duration") or 0, 1),
                     "vertical": bool(info.get("h", 0)
                                      > info.get("w", 0))},
            "meta": {}, "history": []}
        _log(job, "created",
             "Queued from %s." % job["source"])
        if status == "awaiting_approval":
            _log(job, "awaiting_approval",
                 "Waiting for approval (manual approval mode).")
        doc["jobs"].append(job)
        return ("ok", job)

    kind, payload = _mutate(build)
    if kind == "dup":
        return None, ("This clip is already %s for %s on this "
                      "account (duplicate guard). Tick 'allow "
                      "duplicate' to queue it anyway."
                      % (payload.get("status"),
                         PLATFORM_LABELS[platform]))
    return _public_job(payload), None


def list_jobs(platform="", status="", limit=200):
    with _LOCK:
        doc = _read_doc()
    jobs = list(reversed(doc["jobs"]))
    if platform:
        jobs = [j for j in jobs if j.get("platform") == platform]
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    return [_public_job(j) for j in jobs[:limit]]


def get_job(job_id):
    with _LOCK:
        doc = _read_doc()
    for j in doc["jobs"]:
        if j.get("id") == job_id:
            return _public_job(j)
    return None


def _find(doc, job_id):
    for j in doc["jobs"]:
        if j.get("id") == job_id:
            return j
    return None


def job_action(job_id, action):
    """approve / pause / resume / cancel / retry. Returns the public
    job or an error string."""
    def apply(doc):
        job = _find(doc, job_id)
        if job is None:
            return "Job not found."
        st = job.get("status")
        if action == "approve":
            if st != "awaiting_approval":
                return "Only jobs awaiting approval can be approved."
            now = _now()
            if job.get("scheduled_at") and job["scheduled_at"] > now:
                _set_status(job, "scheduled", "Approved.")
            else:
                _set_status(job, "ready", "Approved.")
        elif action == "pause":
            if st in ("published", "failed", "cancelled"):
                return "This job has already finished."
            job["pre_pause_status"] = st
            _set_status(job, "paused", "Paused by user.")
        elif action == "resume":
            if st != "paused":
                return "Only a paused job can be resumed."
            prev = job.get("pre_pause_status") or "ready"
            if prev in ("uploading", "publishing"):
                prev = "ready"
            _set_status(job, prev, "Resumed.")
        elif action == "cancel":
            if st in ("published",):
                return "A published job cannot be cancelled."
            _set_status(job, "cancelled", "Cancelled by user.")
        elif action == "retry":
            if st not in ("failed", "awaiting_connection",
                          "cancelled"):
                return "Only failed or waiting jobs can be retried."
            job["attempts"] = 0
            job["last_error"] = ""
            job["next_attempt_at"] = 0
            now = _now()
            if job.get("scheduled_at") and job["scheduled_at"] > now:
                _set_status(job, "scheduled", "Retry queued.")
            else:
                _set_status(job, "ready", "Retry queued.")
        else:
            return "Unknown action."
        return _public_job(job)

    out = _mutate(apply)
    return out


def set_pause_all(paused):
    def apply(doc):
        doc["pause_all"] = bool(paused)
        return doc["pause_all"]
    return _mutate(apply)


def pause_all():
    with _LOCK:
        return bool(_read_doc().get("pause_all"))


def admin_stats():
    with _LOCK:
        doc = _read_doc()
    by_status, by_platform = {}, {}
    errors = []
    for j in doc["jobs"]:
        by_status[j.get("status")] = by_status.get(j.get("status"),
                                                   0) + 1
        p = by_platform.setdefault(j.get("platform"),
                                   {"total": 0, "published": 0,
                                    "failed": 0})
        p["total"] += 1
        if j.get("status") == "published":
            p["published"] += 1
        if j.get("status") == "failed":
            p["failed"] += 1
            errors.append({"ts": j.get("updated_at") or 0,
                           "platform": j.get("platform"),
                           "title": j.get("title"),
                           "error": j.get("last_error") or ""})
    errors.sort(key=lambda e: e["ts"], reverse=True)
    return {"total": len(doc["jobs"]), "by_status": by_status,
            "by_platform": by_platform,
            "published": by_status.get("published", 0),
            "failed": by_status.get("failed", 0),
            "pause_all": bool(doc.get("pause_all")),
            "recent_errors": errors[:10],
            "outbox": len(doc.get("outbox") or [])}


# --------------------------------------------------------------- outbox

def _emit_event(job, event):
    """Signed completion record → local outbox + optional webhook.
    Off by default: nothing leaves the machine unless the user set a
    webhook URL (and secret) in the Publishing settings."""
    st = _settings()
    payload = {"event": event, "job_id": job.get("id"),
               "platform": job.get("platform"),
               "account_id": job.get("account_id"),
               "clip": job.get("file_name"),
               "remote_id": job.get("remote_id") or "",
               "url": job.get("published_url") or "",
               "error": job.get("last_error") or "",
               "ts": _now()}
    secret = str(st.get("pub_webhook_secret") or "")
    sig = ""
    if secret:
        sig = hmac.new(secret.encode("utf-8"),
                       json.dumps(payload, sort_keys=True)
                       .encode("utf-8"),
                       hashlib.sha256).hexdigest()
    record = {"payload": payload, "signature": sig, "delivered": None}

    def add(doc):
        doc.setdefault("outbox", []).append(record)
        doc["outbox"] = doc["outbox"][-MAX_OUTBOX:]
    _mutate(add)
    url = str(st.get("pub_webhook_url") or "").strip()
    if not url:
        return
    for attempt in range(2):
        try:
            status, _h, _b = _http(
                "POST", url, json_body=payload,
                headers={"X-Itraji-Signature": sig,
                         "X-Itraji-Event": event}, timeout=15)
            if status < 300:
                record["delivered"] = True
                break
            record["delivered"] = False
        except Exception:  # noqa: BLE001 - fire-and-forget, never blocks
            record["delivered"] = False
    def mark(doc):
        for rec in doc.get("outbox", []):
            if rec.get("payload", {}).get("job_id") == job.get("id") \
                    and rec["payload"].get("event") == event:
                rec["delivered"] = record["delivered"]
    _mutate(mark)


# --------------------------------------------------------------- worker

_WORKER = {"thread": None, "stop": False}


def _daily_count(doc, account_id):
    day = time.strftime("%Y-%m-%d", time.localtime(_now()))
    n = 0
    for j in doc["jobs"]:
        if j.get("account_id") == account_id and \
                j.get("published_at") and \
                time.strftime("%Y-%m-%d",
                              time.localtime(j["published_at"])) == day:
            n += 1
    return n


def _claim_next():
    """Pick the next runnable job and mark it uploading (atomically).
    Returns a snapshot dict or None."""
    st = _settings()
    if not st.get("pub_enabled"):
        return None
    daily_limit = int(st.get("pub_daily_limit") or 0)
    delay = float(st.get("pub_delay_s") or 0)
    now = _now()
    claimed = {}

    def pick(doc):
        if doc.get("pause_all"):
            return None
        for j in doc["jobs"]:
            status = j.get("status")
            if status not in ("ready", "scheduled", "waiting",
                              "awaiting_connection"):
                continue
            if j.get("next_attempt_at", 0) > now:
                continue
            if status == "scheduled" and \
                    (j.get("scheduled_at") or 0) > now:
                continue
            acct_key = "%s:%s" % (j.get("platform"),
                                  j.get("account_id"))
            last = (doc.get("last_upload") or {}).get(acct_key, 0)
            if delay and now - last < delay:
                continue
            if daily_limit and _daily_count(doc, j.get("account_id")) \
                    >= daily_limit:
                continue
            j["attempts"] = int(j.get("attempts") or 0) + 1
            _set_status(j, "uploading",
                        "Attempt %d started." % j["attempts"])
            doc.setdefault("last_upload", {})[acct_key] = now
            claimed.update(j)
            return j.get("id")
        return None

    jid = _mutate(pick)
    return claimed if jid else None


def _update_job(job_id, fn):
    def apply(doc):
        job = _find(doc, job_id)
        if job is not None:
            fn(job)
        return job
    return _mutate(apply)


def _fail_or_retry(job, err):
    """Apply the error policy to a claimed job (mutates via _update_job
    closure over a fresh copy)."""
    def apply(j):
        if err.kind == "auth":
            j["last_error"] = err.message + \
                " Reconnect the account, then Retry."
            j["next_attempt_at"] = _now() + 60
            _set_status(j, "awaiting_connection", err.message)
            return
        if err.retryable and j.get("attempts", 0) < MAX_ATTEMPTS:
            backoff = _env_float("ITRAJI_PUB_BACKOFF", 30) * \
                (2 ** max(0, j.get("attempts", 1) - 1))
            wait = max(backoff, err.retry_after or 0)
            j["last_error"] = err.message
            j["next_attempt_at"] = _now() + wait
            _set_status(j, "waiting",
                        "Retryable error — retrying in %ds: %s"
                        % (int(wait), err.message))
            return
        j["last_error"] = err.message
        _set_status(j, "failed", err.message)
    out = _update_job(job["id"], apply)
    if out and out.get("status") == "failed":
        _emit_event(out, "video.publish.failed")
    return out


def _run_claimed(job):
    platform = job["platform"]
    cfg = resolve_account_config(platform, job.get("account_id"))
    adapter = ADAPTERS[platform]
    if cfg.get("account") is None or not cfg.get("token"):
        def mark(j):
            j["last_error"] = ("No connected %s account — connect it "
                               "in Publishing → Social Accounts."
                               % PLATFORM_LABELS[platform])
            j["next_attempt_at"] = _now() + 60
            _set_status(j, "awaiting_connection", j["last_error"])
        _update_job(job["id"], mark)
        return
    if not os.path.isfile(job["file_path"]):
        def mark(j):
            j["last_error"] = "The clip file no longer exists on disk."
            _set_status(j, "failed", j["last_error"])
        out = _update_job(job["id"], mark)
        if out:
            _emit_event(out, "video.publish.failed")
        return
    try:
        result = adapter.upload(cfg, job)
    except PubError as e:
        _fail_or_retry(job, e)
        return
    except Exception as e:  # noqa: BLE001 - adapter bug must fail honest
        _fail_or_retry(job, PubError("Unexpected adapter error: %s"
                                     % e, retryable=True))
        return

    def to_publishing(j):
        j["remote_id"] = str(result.get("remote_id") or "")
        _set_status(j, "publishing",
                    "Uploaded; waiting for %s to finish processing."
                    % PLATFORM_LABELS[platform])
    job = _update_job(job["id"], to_publishing) or job
    _poll_until_done(cfg, adapter, job)


def _poll_until_done(cfg, adapter, job):
    poll = _env_float("ITRAJI_PUB_POLL", 5)
    deadline_polls = 720  # ~1h at 5s; afterwards the reconcile pass
    # keeps checking on later worker ticks via check_once()
    for _ in range(deadline_polls):
        if _WORKER["stop"]:
            return
        try:
            state = adapter.check_status(cfg, job)
        except PubError as e:
            if e.kind == "auth" or not e.retryable:
                _fail_or_retry(job, e)
            else:
                # transient poll error: stay in publishing, the boot /
                # tick reconciler will keep checking.
                def note(j):
                    _log(j, "publishing",
                         "Status check hit a network error; will "
                         "re-check.")
                _update_job(job["id"], note)
            return
        if state.get("state") == "done":
            def done(j):
                j["published_url"] = state.get("url") or ""
                j["published_at"] = _now()
                j["last_error"] = ""
                _set_status(j, "published",
                            state.get("detail")
                            or ("Published: %s" % j["published_url"]))
                if state.get("detail"):
                    j["publish_note"] = state["detail"]
            out = _update_job(job["id"], done)
            if out:
                _emit_event(out, "video.processing.completed")
            return
        if state.get("state") == "failed":
            _fail_or_retry(job, PubError(state.get("detail")
                                         or "Processing failed."))
            return
        # IG adapter may switch remote_id (container → media) and set
        # meta flags inside check_status; persist them.
        if job.get("meta"):
            def keep(j, meta=dict(job.get("meta") or {}),
                     rid=job.get("remote_id")):
                j["meta"] = meta
                if rid:
                    j["remote_id"] = rid
            _update_job(job["id"], keep)
        time.sleep(poll)


def _reconcile_boot():
    """Jobs left in uploading/publishing by a shutdown are reconciled,
    never left stuck: with a remote id we ask the adapter; without
    one the upload never provably finished, so the job fails honestly
    rather than risking a duplicate post on a blind retry."""
    def sweep(doc):
        for j in doc["jobs"]:
            if j.get("status") == "uploading":
                if j.get("remote_id"):
                    _set_status(j, "publishing",
                                "App restarted during upload; "
                                "re-checking with the platform.")
                else:
                    j["last_error"] = ("The app closed during the "
                                       "upload and the platform "
                                       "never confirmed it. Marked "
                                       "failed instead of risking a "
                                       "duplicate post — Retry to "
                                       "upload again.")
                    _set_status(j, "failed", j["last_error"])
                    _emit_event_later.append(j.get("id"))
            elif j.get("status") == "waiting" and \
                    j.get("next_attempt_at", 0) <= _now():
                _set_status(j, "ready", "Retry due after restart.")
    _emit_event_later = []
    _mutate(sweep)
    for jid in _emit_event_later:
        job = get_job(jid)
        if job:
            _emit_event(job, "video.publish.failed")


def check_once():
    """One reconcile pass for jobs sitting in 'publishing' (used by
    the worker loop after the inline poll window and after boot)."""
    with _LOCK:
        doc = _read_doc()
    pending = [j for j in doc["jobs"]
               if j.get("status") == "publishing"]
    for job in pending:
        cfg = resolve_account_config(job["platform"],
                                     job.get("account_id"))
        if cfg.get("account") is None or not cfg.get("token"):
            continue
        adapter = ADAPTERS[job["platform"]]
        try:
            state = adapter.check_status(cfg, job)
        except PubError:
            continue
        if state.get("state") == "done":
            def done(j, state=state):
                j["published_url"] = state.get("url") or ""
                j["published_at"] = _now()
                if state.get("detail"):
                    j["publish_note"] = state["detail"]
                _set_status(j, "published",
                            state.get("detail") or "Published.")
            out = _update_job(job["id"], done)
            if out:
                _emit_event(out, "video.processing.completed")
        elif state.get("state") == "failed":
            _fail_or_retry(job, PubError(state.get("detail")
                                         or "Processing failed."))


def _worker_loop():
    try:
        _reconcile_boot()
    except Exception:  # noqa: BLE001 - worker must survive
        pass
    tick = _env_float("ITRAJI_PUB_TICK", 2)
    while not _WORKER["stop"]:
        try:
            job = _claim_next()
            if job:
                _run_claimed(job)
                continue
            check_once()
        except Exception:  # noqa: BLE001 - worker must survive
            pass
        time.sleep(tick)


def ensure_worker():
    if _WORKER["thread"] is not None and _WORKER["thread"].is_alive():
        return
    _WORKER["stop"] = False
    t = threading.Thread(target=_worker_loop, name="publishing-worker",
                         daemon=True)
    _WORKER["thread"] = t
    t.start()


def stop_worker():
    _WORKER["stop"] = True
