"""FB Automation channel: Cliper Built-in AI endpoint (contract v1.1).

Server side of docs/clipper-builtin-ai-contract.md (platform repo).
The Facebook Automation platform POSTs caption requests here; every
reply that carries source "builtin_ai" was genuinely produced, for
that request, by the local model through local_ai.chat() - the same
engine path the app's own AI features use. There is no template, no
cache and no second inference stack in this module: when the model
cannot answer, the contract's failure shape goes back instead and
the platform owns the fallback decision (contract section 7.2).

Auth is the contract's scheme: header x-fbos-signature
"t=<unix_seconds>,v1=<hex HMAC-SHA256 of '<t>.<raw body>'>" keyed by
the shared Clipper-integration secret - the same secret that signs
the publishing webhooks (Publishing settings -> pub_webhook_secret).
No secret configured means no request can authenticate: the channel
stays closed (401) until the user sets one. The app server binds
127.0.0.1, so the channel is never LAN-exposed by this app.

app.py binds the settings loader + license-lock check (the same
injected-callable pattern as publishing/superadmin) and dispatches
POST PATH here BEFORE its license gate, so even a locked copy
answers in contract shape (503 engine_unavailable) rather than the
UI's locked blob. A locked copy never generates: the AI core is
license-sealed, so a valid HMAC alone is not enough to run it.
"""

import hashlib
import hmac
import json
import threading
import time

import local_ai

PATH = "/api/v1/clipper/builtin-ai/caption"
ENGINE_NAME = "clipper-builtin-ai"
MAX_BODY = 256 * 1024  # a caption request is a few hundred bytes
TIMESTAMP_TOLERANCE_S = 300  # contract section 3

TONES = ("professional", "entertaining", "emotional", "news",
         "documentary", "viral", "short_form", "educational")

_TONE_DIRECTION = {
    "professional": "professional and polished, credible, no slang",
    "entertaining": "fun and entertaining, playful energy",
    "emotional": "emotional and heartfelt, the human moment first",
    "news": "news-report style, factual and direct",
    "documentary": "documentary style, observational and understated",
    "viral": "viral short-form style, a strong hook, high energy",
    "short_form": "ultra-short and punchy, made for vertical short-form",
    "educational": "educational, pointing out what the viewer can learn",
}

_settings_loader = None
_locked_check = None

# One platform generation at a time. The contract's queue depth is
# effectively zero: a second concurrent request is engine_busy (429)
# rather than a WSGI thread parked behind a 14B generation. The app's
# own AI work is unaffected - it shares the one llama-server through
# local_ai.chat() exactly as before (no second model load).
_gen_lock = threading.Lock()


def bind_settings(settings_loader, locked_check):
    """app.py injects its settings loader + license-lock check."""
    global _settings_loader, _locked_check
    _settings_loader = settings_loader
    _locked_check = locked_check


def _secret():
    try:
        st = _settings_loader() if _settings_loader else {}
        return str((st or {}).get("pub_webhook_secret") or "")
    except Exception:  # noqa: BLE001 - no secret, no auth, fail closed
        return ""


def _is_locked():
    try:
        return bool(_locked_check()) if _locked_check else False
    except Exception:  # noqa: BLE001 - if we cannot tell, do not serve
        return True


# ------------------------------------------------------------ responses

def _error(http_status, version, request_id, code, message):
    body = {"contract_version": version, "status": "error",
            "error": {"code": code, "message": message}}
    if request_id:
        body["request_id"] = request_id
    return http_status, json.dumps(body)


def _ok(version, request_id, caption):
    body = {"contract_version": version, "status": "ok",
            "result": {"caption": caption, "source": "builtin_ai",
                       "engine": ENGINE_NAME}}
    if request_id:
        body["request_id"] = request_id
    return "200 OK", json.dumps(body)


# ---------------------------------------------------------------- auth

def _verify_signature(raw, header, secret, now=None):
    """(ok, code, message) - the contract section 3 check, mirroring
    the platform's verifyWebhook: timestamp tolerance first, then a
    timing-safe HMAC comparison over '<t>.<raw body>'."""
    if not secret:
        return False, "bad_signature", "signature verification failed"
    if not header:
        return False, "bad_signature", "missing signature header"
    parts = {}
    for piece in str(header).split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    try:
        ts = int(parts.get("t") or "")
    except ValueError:
        return False, "bad_signature", "malformed signature header"
    sig = parts.get("v1") or ""
    if not sig:
        return False, "bad_signature", "malformed signature header"
    now = int(time.time()) if now is None else now
    if abs(now - ts) > TIMESTAMP_TOLERANCE_S:
        return False, "stale_timestamp", "timestamp outside tolerance"
    expected = hmac.new(secret.encode("utf-8"),
                        str(ts).encode("utf-8") + b"." + raw,
                        hashlib.sha256).digest()
    try:
        given = bytes.fromhex(sig)
    except ValueError:
        return False, "bad_signature", "signature not hex"
    if len(given) != len(expected) or \
            not hmac.compare_digest(given, expected):
        return False, "bad_signature", "signature mismatch"
    return True, "", ""


# ------------------------------------------------------------ generation

def _clean_caption(text):
    """Trim the model's framing without inventing text: outer
    whitespace and one pair of wrapping quotes come off; the words
    are the model's own."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("\"", "'"):
        t = t[1:-1].strip()
    return t


def _generate_messages(inp):
    title = inp["title"]
    lines = [f"Video title: {title}"]
    if isinstance(inp.get("page_name"), str) and inp["page_name"]:
        lines.append(f"Facebook page: {inp['page_name']}")
    dur = inp.get("duration_sec")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) \
            and dur > 0:
        lines.append(f"Clip length: {dur:g} seconds")
    tone = inp.get("tone")
    style = _TONE_DIRECTION.get(tone) if tone in TONES else \
        "engaging and natural"
    lines.append(f"Caption style: {style}.")
    system = (
        "You write captions for short-form social videos "
        "(Facebook Reels). Reply with ONLY the caption text: one or "
        "two short lines, then a few relevant hashtags. No quotes, "
        "no explanation, no preamble, no markdown.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


def _rewrite_messages(inp):
    lines = ["Caption to rewrite:", inp["text"], "",
             f"New style: {_TONE_DIRECTION[inp['tone']]}."]
    if isinstance(inp.get("title"), str) and inp["title"]:
        lines.append(f"Video title: {inp['title']}")
    if isinstance(inp.get("page_name"), str) and inp["page_name"]:
        lines.append(f"Facebook page: {inp['page_name']}")
    system = (
        "You restyle existing social-media captions. Reply with "
        "ONLY the rewritten caption text: keep the meaning and the "
        "facts, change only the style. No quotes, no explanation, "
        "no preamble, no markdown.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


def _run_generation(messages):
    """(caption, None) or (None, (http_status, code, message)).

    Every failure is a contract failure - this function never
    substitutes text of its own for the model's."""
    if _is_locked():
        return None, ("503 Service Unavailable", "engine_unavailable",
                      "Built-in AI is unavailable while this copy "
                      "is locked")
    try:
        downloading = bool(local_ai.download_state().get("active"))
    except Exception:  # noqa: BLE001
        downloading = False
    if downloading or not local_ai.model_ready():
        return None, ("503 Service Unavailable", "engine_unavailable",
                      "Built-in AI engine is not running")
    if not _gen_lock.acquire(blocking=False):
        return None, ("429 Too Many Requests", "engine_busy",
                      "Built-in AI engine is busy")
    try:
        reply = local_ai.chat(messages)
    except RuntimeError as e:
        return None, ("503 Service Unavailable", "engine_unavailable",
                      f"Built-in AI request failed: {e}"[:300])
    except Exception as e:  # noqa: BLE001 - never a stack to the caller
        return None, ("503 Service Unavailable", "engine_unavailable",
                      f"Built-in AI request failed: {e}"[:300])
    finally:
        _gen_lock.release()
    caption = _clean_caption(reply)
    if not caption:
        return None, ("503 Service Unavailable", "engine_unavailable",
                      "Built-in AI returned an empty caption")
    return caption, None


# --------------------------------------------------------------- handler

def handle(environ):
    """WSGI route handler -> (status_line, json_body_str).

    Called by app.py for POST PATH only. Auth runs before the body
    is trusted (same order as the platform's test double); every
    outcome is one of the contract's shapes."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > MAX_BODY:
        return _error("400 Bad Request", "1.0", None, "bad_request",
                      "missing or oversized request body")
    raw = environ["wsgi.input"].read(length)

    ok, code, message = _verify_signature(
        raw, environ.get("HTTP_X_FBOS_SIGNATURE"), _secret())
    if not ok:
        return _error("401 Unauthorized", "1.0", None, code, message)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _error("400 Bad Request", "1.0", None, "bad_request",
                      "request body is not valid JSON")
    if not isinstance(body, dict):
        return _error("400 Bad Request", "1.0", None, "bad_request",
                      "request body must be a JSON object")

    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        request_id = None

    version = body.get("contract_version")
    major = None
    if isinstance(version, str):
        head, _, _tail = version.partition(".")
        if head.isdigit():
            major = int(head)
    if major != 1:
        return _error("400 Bad Request", "1.0", request_id,
                      "bad_request",
                      f"unsupported contract_version: {version!r}")

    operation = body.get("operation")
    if operation == "caption.generate":
        resp_version = "1.0"  # generate answers on the 1.0 envelope
    elif operation == "caption.rewrite":
        resp_version = "1.1"  # a rewrite MUST be answered >= 1.1
    else:
        return _error("400 Bad Request", "1.0", request_id,
                      "bad_request",
                      f"unknown operation: {operation!r}")

    inp = body.get("input")
    if not isinstance(inp, dict):
        return _error("400 Bad Request", resp_version, request_id,
                      "bad_request", "input must be an object")

    if operation == "caption.generate":
        title = inp.get("title")
        if not isinstance(title, str) or not title.strip():
            return _error("400 Bad Request", resp_version, request_id,
                          "bad_request",
                          "input.title is required")
        messages = _generate_messages(inp)
    else:
        text = inp.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("400 Bad Request", resp_version, request_id,
                          "bad_request", "input.text is required")
        if inp.get("tone") not in TONES:
            return _error("400 Bad Request", resp_version, request_id,
                          "bad_request",
                          "input.tone must be one of: "
                          + ", ".join(TONES))
        messages = _rewrite_messages(inp)

    caption, failure = _run_generation(messages)
    if failure is not None:
        http_status, fcode, fmessage = failure
        return _error(http_status, resp_version, request_id,
                      fcode, fmessage)
    return _ok(resp_version, request_id, caption)
