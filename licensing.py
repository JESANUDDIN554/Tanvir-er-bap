"""Itraji Cliper licensing: signed license keys, an encrypted core
pack, signed file integrity, and basic anti-debug.

Design (offline, no server):
- Licenses are Ed25519-signed (see ed25519.py). The app embeds only
  the PUBLIC key; keys are minted by tools/issue_license.py with a
  private key that never ships.
- The license is LOAD-BEARING: the AI prompts and caption styles the
  app cannot work without live in corepack.enc, sealed with a random
  pack key. Each license carries that pack key wrapped under
  SHA-256(core_sig || machine_code) - core_sig being the issuer's
  signature over a fixed context string. Forging a license therefore
  needs the private key; copying one to another machine fails both
  the machine check and the unwrap; patching out the activation gate
  still leaves no pack key, so detection and rendering stay dead.
- Sealing construction (no third-party crypto in the packaged build):
  HMAC-SHA256 counter keystream + encrypt-then-MAC with independent
  subkeys - see seal()/open_sealed().
"""

import base64
import hashlib
import hmac
import json
import os

import ed25519

PUBLIC_KEY_HEX = ("34fff23bf9e19566cc8a898fb835c633"
                  "6f2bc54936c03775a7c8a98a72c5c2ae")
COREPACK_CONTEXT = b"ITRAJI-COREPACK-V1"
KEY_PREFIX = "ITRAJI1"


class LicenseError(Exception):
    """A license problem with a user-readable reason."""


# ---------------------------------------------------------------------------
# HMAC-SHA256 seal (encrypt-then-MAC, counter keystream)
# ---------------------------------------------------------------------------

def _subkey(key, label):
    return hmac.new(key, label, hashlib.sha256).digest()


def _keystream(enc_key, nonce, n):
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + ctr.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def seal(key, plaintext, aad=b""):
    """key: 32 bytes. Returns nonce(16) || ciphertext || tag(32)."""
    nonce = os.urandom(16)
    enc_key = _subkey(key, b"itraji-seal-enc")
    mac_key = _subkey(key, b"itraji-seal-mac")
    ks = _keystream(enc_key, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    tag = hmac.new(mac_key, aad + nonce + ct, hashlib.sha256).digest()
    return nonce + ct + tag


def open_sealed(key, blob, aad=b""):
    """Inverse of seal(). Raises ValueError on any tampering."""
    if len(blob) < 16 + 32:
        raise ValueError("sealed blob too short")
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    mac_key = _subkey(key, b"itraji-seal-mac")
    expect = hmac.new(mac_key, aad + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, tag):
        raise ValueError("sealed data failed its integrity check")
    enc_key = _subkey(key, b"itraji-seal-enc")
    ks = _keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))


# ---------------------------------------------------------------------------
# Machine code
# ---------------------------------------------------------------------------

_machine_code_cache = None


def machine_code():
    """Short, copyable code identifying this computer.

    Windows: the MachineGuid from the registry. Elsewhere (dev/CI):
    a hash of the MAC address + hostname. Grouped XXXX-XXXX-XXXX-XXXX.
    """
    global _machine_code_cache
    if _machine_code_cache:
        return _machine_code_cache
    raw = None
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography")
            raw, _ = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
        except Exception:  # noqa: BLE001 - fall through to the fallback
            raw = None
    if not raw:
        import socket
        import uuid
        raw = "%012x:%s" % (uuid.getnode(),
                            os.environ.get("COMPUTERNAME")
                            or socket.gethostname())
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest().upper()
    _machine_code_cache = "-".join(
        digest[i:i + 4] for i in range(0, 16, 4))
    return _machine_code_cache


# ---------------------------------------------------------------------------
# License parsing + verification
# ---------------------------------------------------------------------------

def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def verify_license(key_str):
    """Verify a license key string end to end.

    Returns (payload, pack_key_bytes). Raises LicenseError with a
    plain reason for every failure mode (bad format, bad signature,
    wrong machine, expired, bad core signature, bad pack unwrap).
    """
    parts = (key_str or "").strip().split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        raise LicenseError(
            "That doesn't look like an Itraji Cliper license key.")
    try:
        payload_bytes = _b64d(parts[1])
        sig = _b64d(parts[2])
    except Exception:  # noqa: BLE001
        raise LicenseError("That license key is damaged or incomplete.")
    pub = bytes.fromhex(PUBLIC_KEY_HEX)
    if not ed25519.verify(pub, parts[1].encode("ascii"), sig):
        raise LicenseError(
            "This license key is not genuine - its signature "
            "doesn't check out.")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise LicenseError("That license key is damaged or incomplete.")
    if payload.get("machine") != machine_code():
        raise LicenseError(
            "This key was issued for a different computer. Keys are "
            "tied to one machine - ask the seller for a key for this "
            "machine code.")
    import datetime
    exp = str(payload.get("exp") or "never")
    if exp != "never" and exp < datetime.date.today().isoformat():
        raise LicenseError(
            "This license key expired on " + exp + ". Ask the seller "
            "for a renewal key.")
    try:
        core_sig = _b64d(str(payload["core_sig"]))
        wrapped = _b64d(str(payload["pk"]))
    except Exception:  # noqa: BLE001
        raise LicenseError("That license key is damaged or incomplete.")
    if not ed25519.verify(pub, COREPACK_CONTEXT, core_sig):
        raise LicenseError(
            "This license key is not genuine - its core signature "
            "doesn't check out.")
    wrap_key = hashlib.sha256(
        core_sig + machine_code().encode("ascii")).digest()
    try:
        pack_key = open_sealed(wrap_key, wrapped, aad=b"itraji-pack-key")
    except ValueError:
        raise LicenseError(
            "This license key could not unlock the app's core pack "
            "(it doesn't belong to this computer).")
    if len(pack_key) != 32:
        raise LicenseError("That license key is damaged or incomplete.")
    return payload, pack_key


def load_corepack(base_dir, pack_key):
    """Decrypt corepack.enc with the license-derived pack key.
    Returns the pack dict. Raises LicenseError on failure."""
    path = os.path.join(base_dir, "corepack.enc")
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        raise LicenseError(
            "The app's core pack file (corepack.enc) is missing - "
            "reinstall the app, then activate again.")
    try:
        data = open_sealed(pack_key, blob, aad=b"itraji-corepack-v1")
        return json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise LicenseError(
            "The app's core pack could not be unlocked with this "
            "key - the copy may be damaged or mismatched.")


# ---------------------------------------------------------------------------
# Runtime license state
# ---------------------------------------------------------------------------

_state = {"unlocked": False, "payload": None, "reason": "not activated",
          "key_fp": None, "tampered": False}


def state():
    return dict(_state)


def try_unlock(key_str, base_dir, load_pack):
    """Verify + unlock + load the core pack (via the caller's loader).
    Memoized per key fingerprint. Returns the state dict."""
    import hashlib as _h
    fp = _h.sha256((key_str or "").encode()).hexdigest()[:16]
    if _state["unlocked"] and _state["key_fp"] == fp:
        return state()
    if not (key_str or "").strip():
        _state.update(unlocked=False, payload=None,
                      reason="not activated", key_fp=fp)
        return state()
    try:
        payload, pack_key = verify_license(key_str)
        load_pack(load_corepack(base_dir, pack_key))
    except LicenseError as e:
        _state.update(unlocked=False, payload=None, reason=str(e),
                      key_fp=fp)
        return state()
    _state.update(unlocked=True, payload=payload, reason="", key_fp=fp)
    return state()


def require_unlocked():
    """Per-operation re-check. Returns None when the app is fully
    unlocked, else a plain reason string. Machine and expiry are
    re-evaluated cheaply; the signature verdict comes from the
    unlock-time verification (per process)."""
    if _state.get("tampered"):
        return "This copy of Itraji Cliper has been modified."
    if debugger_present():
        _state["tampered"] = True
        return "This copy of Itraji Cliper has been modified."
    if not _state["unlocked"] or not _state.get("payload"):
        return _state.get("reason") or "not activated"
    payload = _state["payload"]
    if payload.get("machine") != machine_code():
        return "This key was issued for a different computer."
    import datetime
    exp = str(payload.get("exp") or "never")
    if exp != "never" and exp < datetime.date.today().isoformat():
        return "This license key has expired."
    return None


def mark_tampered():
    _state["tampered"] = True
    _state["unlocked"] = False
    _state["reason"] = "This copy of Itraji Cliper has been modified."


# ---------------------------------------------------------------------------
# Anti-debug (Windows only, single well-known API, dev-safe)
# ---------------------------------------------------------------------------

def debugger_present():
    if os.name != "nt" or os.environ.get("ITRAJI_DEV") == "1":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.IsDebuggerPresent())
    except Exception:  # noqa: BLE001 - never fatal
        return False


# ---------------------------------------------------------------------------
# Signed integrity manifest over the loose distribution files
# ---------------------------------------------------------------------------

def verify_integrity(base_dir):
    """Returns (ok, bad_relpath_or_None). Skipped when ITRAJI_DEV=1.
    The manifest + its Ed25519 signature live in integrity.py, signed
    by the issuer - editing a covered file breaks the hash, and the
    manifest itself cannot be re-signed without the private key."""
    if os.environ.get("ITRAJI_DEV") == "1":
        return (True, None)
    try:
        import integrity as _integ
        manifest = _integ.MANIFEST
        sig = bytes.fromhex(_integ.SIGNATURE_HEX)
    except Exception:  # noqa: BLE001
        # no manifest module: fine for a source checkout, not for the
        # packaged app
        import sys
        if getattr(sys, "frozen", False):
            return (False, "integrity.py")
        return (True, None)
    blob = json.dumps(manifest, sort_keys=True).encode("utf-8")
    if not ed25519.verify(bytes.fromhex(PUBLIC_KEY_HEX), blob, sig):
        return (False, "integrity.py")
    _TEXT_EXTS = (".html", ".js", ".css", ".md", ".txt", ".json")
    for rel, want in manifest.items():
        p = os.path.join(base_dir, rel)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return (False, rel)
        if rel.lower().endswith(_TEXT_EXTS):
            # Windows build checkouts rewrite text files with CRLF line
            # endings; the signed manifest is over the LF originals, so
            # normalize line endings before hashing text files. Real
            # content edits still change the hash and fail the check.
            data = data.replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != want:
            return (False, rel)
    return (True, None)
