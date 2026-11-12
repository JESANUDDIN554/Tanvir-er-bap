"""In-app updates by signed file replacement ("Apply update").

Customers never reinstall: the seller ships a small .zip update
pack (built by tools/build_update_pack.py) and the customer applies
it from Settings -> Updates. Only the app's LOOSE files can be
replaced this way - ui/*, fonts/*, corepack.enc and the integrity
manifest. The compiled modules (app.py, pipeline.py, ...) live
inside the exe and CANNOT be replaced loosely; packs containing
them are rejected with a message saying big updates use the Setup.

Pack format (see tools/UPDATE_PACKS.md):
  update.json  {"version": str,
                "files": [{"path", "sha256", "size"}, ...]}
                sha256/size are over the RAW entry bytes
                (transport integrity).
  update.sig   hex Ed25519 signature over the canonical bytes
               json.dumps({"version": ..., "files": ...},
                          sort_keys=True).encode()
               - the same key/scheme as the integrity manifest.
  integrity.py a NEW signed integrity manifest covering the FULL
               post-update loose set (same format as
               tools/build_integrity.py emits). Mandatory whenever
               the pack changes any manifest-covered file.
  <files...>   the replacement bytes, at app-relative paths.

Apply order (nothing on disk changes until every check passes):
  1. verify the pack signature against the embedded public key
  2. validate every entry path against the allowlist
  3. verify every file's sha256 + size against update.json
  4. if integrity.py is included: parse it (ast, never exec),
     verify ITS signature, and remember it as the new manifest
  5. back up every file about to be replaced (and any existing
     integrity_update.json) into update_backup/<timestamp>/
  6. write the new files (tmp + os.replace), and store the new
     manifest as the loose integrity_update.json that
     licensing.verify_integrity prefers over the compiled one
  7. run licensing.verify_integrity() on the result; on failure
     roll EVERYTHING back from the backup, byte-for-byte.
"""
import ast
import hashlib
import itertools
import json
import os
import shutil
import time
import zipfile

import ed25519
import licensing

OVERRIDE_NAME = "integrity_update.json"
BACKUP_DIRNAME = "update_backup"
_backup_seq = itertools.count()
_KEEP_BACKUPS = 3


def _prune_backups(base_dir, keep_dir):
    """Keep the newest _KEEP_BACKUPS backup dirs (names sort
    chronologically); older ones are removed. Best-effort."""
    root = os.path.join(base_dir, BACKUP_DIRNAME)
    try:
        names = sorted(d for d in os.listdir(root)
                       if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return
    keep = set(names[-_KEEP_BACKUPS:]) | {os.path.basename(keep_dir)}
    for d in names:
        if d not in keep:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
_ALLOWED_PREFIXES = ("ui/", "fonts/")
_ALLOWED_FILES = ("corepack.enc", "integrity.py")
_RESERVED = ("update.json", "update.sig")
_SETUP_MSG = ("this pack contains files that cannot be replaced "
              "in place (compiled app code) - big updates like "
              "this one install with the Setup instead")


class UpdateError(Exception):
    """A pack problem the user should see verbatim."""


def _canonical(doc):
    return json.dumps(doc, sort_keys=True).encode("utf-8")


def _check_relpath(rel):
    """Validate one pack-relative path; returns the normalized
    'a/b/c' form or raises UpdateError."""
    if not isinstance(rel, str) or not rel:
        raise UpdateError("update pack has an empty file path")
    if "\\" in rel:
        raise UpdateError(
            f"update pack path '{rel}' uses backslashes - rejected")
    if rel.startswith("/") or rel.startswith("~"):
        raise UpdateError(
            f"update pack path '{rel}' is not a relative path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UpdateError(
            f"update pack path '{rel}' tries to leave the app "
            "folder - rejected")
    if ":" in parts[0]:
        raise UpdateError(
            f"update pack path '{rel}' is not a relative path")
    norm = "/".join(parts)
    if norm in _RESERVED:
        raise UpdateError(f"update pack path '{rel}' is reserved")
    if norm in _ALLOWED_FILES or \
            norm.startswith(_ALLOWED_PREFIXES):
        return norm
    raise UpdateError(f"update pack file '{rel}': {_SETUP_MSG}")


def _parse_integrity_py(data):
    """Extract (manifest, signature_hex) from an integrity.py
    entry WITHOUT executing it: parse the AST and literal-eval the
    two top-level assignments, exactly the format
    tools/build_integrity.py emits."""
    try:
        tree = ast.parse(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise UpdateError("the pack's integrity.py is not readable")
    manifest = sig_hex = None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        try:
            if node.targets[0].id == "MANIFEST":
                manifest = ast.literal_eval(node.value)
            elif node.targets[0].id == "SIGNATURE_HEX":
                sig_hex = ast.literal_eval(node.value)
        except Exception:  # noqa: BLE001
            raise UpdateError(
                "the pack's integrity.py is malformed")
    if not isinstance(manifest, dict) or not manifest \
            or not isinstance(sig_hex, str):
        raise UpdateError("the pack's integrity.py is malformed")
    return manifest, sig_hex


def _verify_manifest_sig(manifest, sig_hex):
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    return ed25519.verify(bytes.fromhex(licensing.PUBLIC_KEY_HEX),
                          _canonical(manifest), sig)


def apply_update_pack(zip_path, base_dir):
    """Apply a signed update pack to the app folder at base_dir.
    Returns {"ok": True, "version", "files", "backup"} on success.
    Raises UpdateError (nothing changed, or fully rolled back) on
    any rejection."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception:  # noqa: BLE001
        raise UpdateError("that file is not a readable update pack "
                          "(.zip)")
    with zf:
        names = zf.namelist()
        if len(names) != len(set(names)):
            raise UpdateError("update pack contains duplicate "
                              "entries - rejected")
        for req in _RESERVED:
            if req not in names:
                raise UpdateError(f"update pack is missing {req} - "
                                  "it is not a signed Itraji update")
        try:
            doc = json.loads(zf.read("update.json").decode("utf-8"))
            sig = bytes.fromhex(
                zf.read("update.sig").decode("utf-8").strip())
        except Exception:  # noqa: BLE001
            raise UpdateError("the pack's update.json/update.sig "
                              "are unreadable - rejected")
        version = doc.get("version")
        files = doc.get("files")
        if not isinstance(version, str) or not version \
                or not isinstance(files, list):
            raise UpdateError("the pack's update.json is malformed")
        # 1. Signature FIRST: an unsigned/tampered pack changes
        #    nothing at all.
        if not ed25519.verify(
                bytes.fromhex(licensing.PUBLIC_KEY_HEX),
                _canonical({"version": version, "files": files}),
                sig):
            raise UpdateError("this update pack is not signed by "
                              "Itraji (signature check failed) - "
                              "nothing was changed")
        # 2. Allowlist + path safety for the manifest entries.
        listed = {}
        for ent in files:
            if not isinstance(ent, dict):
                raise UpdateError("the pack's update.json is "
                                  "malformed")
            rel = _check_relpath(ent.get("path"))
            if rel in listed:
                raise UpdateError(f"update pack lists '{rel}' "
                                  "twice - rejected")
            listed[rel] = ent
        # Every zip entry must be accounted for (no smuggled files).
        entry_names = {n for n in names if not n.endswith("/")}
        if entry_names != set(listed) | set(_RESERVED):
            extra = sorted(entry_names - set(listed) - set(_RESERVED))
            missing = sorted(set(listed) - entry_names)
            raise UpdateError(
                "update pack contents do not match its manifest "
                f"(extra: {extra or 'none'}, missing: "
                f"{missing or 'none'}) - rejected")
        # 3. Per-file transport integrity (raw bytes).
        payload = {}
        for rel, ent in listed.items():
            data = zf.read(rel)
            if len(data) != ent.get("size") or \
                    hashlib.sha256(data).hexdigest() != \
                    ent.get("sha256"):
                raise UpdateError(f"update pack file '{rel}' does "
                                  "not match its signed hash - "
                                  "rejected, nothing was changed")
            payload[rel] = data
        # 4. A carried integrity.py must itself be validly signed.
        new_override = None
        if "integrity.py" in payload:
            manifest, sig_hex = _parse_integrity_py(
                payload.pop("integrity.py"))
            if not _verify_manifest_sig(manifest, sig_hex):
                raise UpdateError("the pack's new integrity "
                                  "manifest is not signed by "
                                  "Itraji - rejected, nothing was "
                                  "changed")
            new_override = {"manifest": manifest,
                            "signature_hex": sig_hex}
        elif any(rel == "corepack.enc" or
                 rel.startswith(_ALLOWED_PREFIXES)
                 for rel in payload):
            raise UpdateError(
                "this pack changes protected files but carries no "
                "signed integrity.py for them - it would fail the "
                "app's integrity check, so it was not applied")

    # 5. Back up everything we are about to touch.
    # The stamp has SECOND resolution and was used as-is: two
    # packs applied within one second shared one backup dir, so a
    # rollback could restore a MIX of two generations - and if the
    # second apply failed midway, the first pack's clean backup
    # was silently overwritten. A unique suffix keeps every
    # pack's backup its own. Old backups are also pruned below
    # (they used to accumulate forever).
    stamp = time.strftime("%Y%m%d-%H%M%S") + \
        "-%d-%04d" % (os.getpid(), next(_backup_seq))
    backup_dir = os.path.join(base_dir, BACKUP_DIRNAME, stamp)
    touched = list(payload)
    if new_override is not None:
        touched.append(OVERRIDE_NAME)
    try:
        for rel in touched:
            src = os.path.join(base_dir, *rel.split("/"))
            if os.path.exists(src):
                dst = os.path.join(backup_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
    except OSError as e:
        raise UpdateError(f"could not back up the current files "
                          f"({e}) - update not applied")

    def _rollback():
        for rel in touched:
            dst = os.path.join(base_dir, *rel.split("/"))
            bak = os.path.join(backup_dir, *rel.split("/"))
            try:
                if os.path.exists(bak):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(bak, dst)
                elif os.path.exists(dst):
                    os.remove(dst)  # did not exist before the update
            except OSError:
                pass

    # 6. Write the replacements (tmp + atomic rename per file).
    try:
        for rel, data in payload.items():
            dst = os.path.join(base_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".upd-tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
        if new_override is not None:
            dst = os.path.join(base_dir, OVERRIDE_NAME)
            tmp = dst + ".upd-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new_override, f, sort_keys=True)
            os.replace(tmp, dst)
    except OSError as e:
        _rollback()
        raise UpdateError(f"writing the update failed ({e}) - "
                          "the previous files were restored")

    # 7. The result must pass the app's own integrity check,
    #    exactly as the next launch will run it.
    ok, bad = licensing.verify_integrity(base_dir)
    if not ok:
        _rollback()
        raise UpdateError(
            "the updated files failed the app's integrity check "
            f"({bad}) - the update was rolled back and nothing "
            "changed. Update packs that change protected files "
            "must carry a signed integrity.py for the new files.")
    _prune_backups(base_dir, backup_dir)
    return {"ok": True, "version": version,
            "files": sorted(payload), "backup": backup_dir}
