"""Per-license clip usage limits ("max clips per key").

The issuer can bake an allowance into a license key's signed
payload ("max_clips": N - see licensing.payload_max_clips and
tools/issue_license.py --max-clips). This module is the local
bookkeeping + integrity for that allowance:

- Counting happens on THIS machine only (the app is offline).
  The record is keyed by a SHA-256 fingerprint of the license
  KEY STRING - the key itself is never stored anywhere here.
- Each record is {max_clips, clips_used, mac} where mac is an
  HMAC-SHA256 over "fingerprint|max_clips|clips_used" keyed by
  bytes derived from the license's own Ed25519 signature
  (SHA-256 of a domain label + the raw signature bytes). The
  signature only exists inside a genuine key, so the HMAC key
  cannot be recomputed by someone who merely edits the record
  file: hand-editing clips_used (or the allowance) breaks the
  MAC, and the record then reads as TAMPERED - app.py treats
  that as limit-reached with its own honest "usage data
  problem" state. The app still opens; only new clip renders
  pause.
- Keys WITHOUT the signed field are unlimited: status() says
  so, and count_clip() never creates or touches a record for
  them - existing customers see no meter and no side effects.
- Honest limit of the design: this is an offline counter.
  Deleting the whole record file is indistinguishable from a
  fresh install (same as every offline licensing counter);
  the HMAC defeats editing, not deletion. Tamper-proof
  balances would need the online license server.
"""

import hashlib
import hmac
import json
import os
import sys
import threading

import licensing

# Data area: same resolution as admin.py / local_ai.py (env
# override wins, else the app folder / PyInstaller bundle dir).
_BASE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
DATA_DIR = os.environ.get("ITRAJI_LOCAL_DATA_DIR") or _BASE

_FILE = "usage_limits.json"
_HMAC_LABEL = b"itraji-usage-hmac-v1"

_lock = threading.Lock()


def usage_path():
    return os.path.join(DATA_DIR, _FILE)


def _fingerprint(key_str):
    """SHA-256 of the license key string - the record's key.
    The key text itself is never persisted."""
    return hashlib.sha256(
        (key_str or "").strip().encode("utf-8")).hexdigest()


def _hmac_key(key_str):
    """Bytes derived from the license signature itself: SHA-256
    over a domain label + the raw Ed25519 signature bytes from
    the key's third segment. None when the key string doesn't
    parse (cannot happen for a key verify_license accepted)."""
    try:
        parts = (key_str or "").strip().split(".")
        if len(parts) != 3 or parts[0] != licensing.KEY_PREFIX:
            return None
        sig = licensing._b64d(parts[2])
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256(_HMAC_LABEL + sig).digest()


def _mac(hk, fp, max_clips, clips_used):
    msg = ("%s|%d|%d" % (fp, int(max_clips), int(clips_used)))
    return hmac.new(hk, msg.encode("ascii"), hashlib.sha256).hexdigest()


def _read_file():
    """(doc, file_state): file_state is "absent", "ok" or "bad"
    (exists but unparseable / wrong shape)."""
    try:
        with open(usage_path(), "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, "absent"
    except Exception:  # noqa: BLE001
        return None, "bad"
    if not isinstance(doc, dict) \
            or not isinstance(doc.get("records"), dict):
        return None, "bad"
    return doc, "ok"


def _write_file(doc):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = usage_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, usage_path())


def _evaluate(key_str, payload, doc, file_state):
    """Pure status computation shared by status()/count_clip().
    Returns the status dict (see status())."""
    max_clips = licensing.payload_max_clips(payload)
    out = {"capped": max_clips is not None,
           "max_clips": max_clips,
           "used": None, "remaining": None, "state": "unlimited"}
    if max_clips is None:
        return out
    out["used"] = 0
    out["remaining"] = max_clips
    out["state"] = "ok"
    if file_state == "absent":
        return out
    if file_state == "bad":
        out["state"] = "tampered"
        out["used"] = None
        out["remaining"] = None
        return out
    rec = (doc or {}).get("records", {}).get(_fingerprint(key_str))
    if rec is None:
        return out
    hk = _hmac_key(key_str)
    try:
        used = rec["clips_used"]
        rmax = rec["max_clips"]
        mac = rec["mac"]
        valid = (hk is not None
                 and isinstance(used, int)
                 and not isinstance(used, bool)
                 and used >= 0
                 and rmax == max_clips
                 and isinstance(mac, str)
                 and hmac.compare_digest(
                     mac, _mac(hk, _fingerprint(key_str),
                               max_clips, used)))
    except Exception:  # noqa: BLE001 - malformed record shape
        valid = False
    if not valid:
        out["state"] = "tampered"
        out["used"] = None
        out["remaining"] = None
        return out
    out["used"] = used
    out["remaining"] = max(0, max_clips - used)
    if out["remaining"] == 0:
        out["state"] = "limit"
    return out


def status(key_str, payload):
    """The active key's usage state:
      {"capped": bool, "max_clips": int|None,
       "used": int|None, "remaining": int|None,
       "state": "unlimited"|"ok"|"limit"|"tampered"}
    used/remaining are None when tampered (no number is
    trustworthy then) and used is 0 for a capped key that has
    not rendered yet. Never raises."""
    try:
        with _lock:
            doc, file_state = _read_file()
            return _evaluate(key_str, payload, doc, file_state)
    except Exception:  # noqa: BLE001 - enforcement reads fail closed
        max_clips = licensing.payload_max_clips(payload)
        if max_clips is None:
            return {"capped": False, "max_clips": None, "used": None,
                    "remaining": None, "state": "unlimited"}
        return {"capped": True, "max_clips": max_clips, "used": None,
                "remaining": None, "state": "tampered"}


def count_clip(key_str, payload, n=1):
    """Record n completed clip renders against the active key
    and return the fresh status.

    Unlimited keys: pure no-op (the record file is never created
    or modified for them). Capped keys: the increment is written
    only over a record whose MAC verifies - a tampered record is
    never "repaired" by writing over it. Best-effort like the
    admin counters: a failed increment returns the pre-write
    status rather than raising into the render path."""
    max_clips = licensing.payload_max_clips(payload)
    if max_clips is None:
        return {"capped": False, "max_clips": None, "used": None,
                "remaining": None, "state": "unlimited"}
    with _lock:
        doc, file_state = _read_file()
        st = _evaluate(key_str, payload, doc, file_state)
        if st["state"] == "tampered":
            return st
        hk = _hmac_key(key_str)
        if hk is None:
            return st
        fp = _fingerprint(key_str)
        if doc is None:
            doc = {"v": 1, "records": {}}
        used = int(st["used"] or 0) + max(0, int(n))
        doc["records"][fp] = {
            "max_clips": max_clips, "clips_used": used,
            "mac": _mac(hk, fp, max_clips, used)}
        try:
            _write_file(doc)
        except Exception:  # noqa: BLE001 - never break a render
            return st
        return _evaluate(key_str, payload, doc, "ok")
