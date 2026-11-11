# Itraji Cliper update packs ("Apply update")

Customers update by replacing specific files — never by deleting
or reinstalling the app. The seller builds a small signed `.zip`
pack; the customer picks it in **Settings → Updates → Apply
update**, and the app swaps the files in place (with a backup and
automatic rollback if anything fails the integrity check).

## What can and cannot be in a pack

Only the app's **loose** files:

- `ui/*` (pages, scripts, styles, icons)
- `fonts/*`
- `corepack.enc`
- `integrity.py` — the signed manifest; the tool below generates
  it fresh for every pack. Never hand-write it.

Compiled Python modules (`app.py`, `pipeline.py`, `local_ai.py`,
…) live **inside the exe** and cannot be replaced this way. Packs
containing them are rejected in the app with a message saying big
updates install with the Setup. Ship those as a new Setup release
as before.

## Pack format

| entry | content |
|---|---|
| `update.json` | `{"version": "2.1", "files": [{"path": "ui/app.js", "sha256": "<hex over the raw entry bytes>", "size": 123}]}` — one entry per file, `integrity.py` included |
| `update.sig` | hex Ed25519 signature over `json.dumps({"version": ..., "files": ...}, sort_keys=True).encode()` — the same private key / public key as the integrity manifest (`tools/license_private_key.txt`, `licensing.PUBLIC_KEY_HEX`) |
| `integrity.py` | new manifest covering the **full** post-update loose set, in `tools/build_integrity.py` format |
| the files | replacement bytes at app-relative paths |

## Building a pack

Prepare a folder holding the **full new loose set** (the complete
`ui/`, `fonts/` and `corepack.enc` as they should be after the
update — the manifest must cover all of it), then:

```
python3 tools/build_update_pack.py \
    --tree /path/to/new-loose-set \
    --files ui/app.js,ui/index.html \
    --version 2.1 --out update-2.1.zip
```

`--files` lists only what changed; the signed `integrity.py` is
generated and added automatically.

## How the app applies a pack

1. Verifies the pack signature — unsigned or tampered packs are
   rejected before anything on disk changes.
2. Rejects absolute/`..`/backslash paths and anything outside the
   allowlist above.
3. Verifies every file's SHA-256 and size against `update.json`.
4. Parses the carried `integrity.py` (AST only, never executed)
   and verifies its signature too.
5. Backs up every replaced file (and any previous
   `integrity_update.json`) into `update_backup/<timestamp>/`
   inside the app folder.
6. Writes the new files, and stores the new manifest as the loose
   `integrity_update.json`, which `licensing.verify_integrity`
   prefers over the compiled manifest (it is signed with the same
   key, so it is exactly as trustworthy).
7. Re-runs the integrity check on the result. If it fails, the
   backup is restored byte-for-byte and the user sees why.

On success the user is told to restart the app. The current
version is shown in Settings → Updates (`APP_VERSION` in app.py —
bump it when cutting a release that changes compiled code; packs
carry their own version string for display in the result line).
