"""Tamper-evidence for a set of captured flights (Phase E5).

**What `bundle_id` already does, and what it does not.** It is a content hash over the fields
that make a flight *that flight*, truncated to 16 hex characters, and the code says plainly of
its sibling `params_hash`: *"identifies a config, it does not defend against an adversary."* Two
gaps follow:

  * **It is deliberately partial.** `airframe_id`, `detector_coverage` and `monitor_health` are
    excluded from identity on purpose -- they describe the observing conditions, not what the
    aircraft did. So a bundle can be edited in ways `bundle_id` is designed not to notice.
  * **64 bits is a fingerprint.** Fine against accident and typos, not against someone motivated.

This module hashes the **file bytes** with full-length SHA-256, which catches any change at all,
and records every bundle in one manifest so the set is verifiable rather than each file
separately.

**What this buys, stated precisely, because security claims rot into folklore.**

  * With the manifest from a trusted channel: any modification to any bundle is detected.
  * With `SENTINEL_MANIFEST_KEY` set, the manifest carries an HMAC, so a manifest that has itself
    been rewritten is detected too.
  * It is **not a signature**. HMAC is a shared secret: it proves the manifest was written by
    someone holding the key, not by a specific person, and anyone who can verify can also forge.
    Publishing a manifest to a third party who should not be able to forge it needs asymmetric
    signing, which needs a key-management story this project does not have.

Verification never repairs. A file that fails is reported and left alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"
KEY_ENV = "SENTINEL_MANIFEST_KEY"
MANIFEST_VERSION = 1


def file_digest(path: str | Path) -> str:
    """Full-length SHA-256 of the file bytes.

    Bytes rather than parsed content, deliberately: it covers every field, including the ones
    `bundle_id` excludes by design, plus formatting. A re-serialised bundle with identical content
    is a different file, and for tamper-evidence that is the answer you want -- "this is the file
    I saw" rather than "this is a file that means the same thing".
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sign(payload: str, key: str | None) -> str | None:
    if not key:
        return None
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _canonical(entries: dict[str, Any], created: str) -> str:
    return json.dumps({"version": MANIFEST_VERSION, "created_utc": created,
                       "entries": entries}, sort_keys=True, separators=(",", ":"))


def build_manifest(directory: str | Path, key: str | None = None,
                   created_utc: str | None = None) -> dict[str, Any]:
    """Digest every bundle in a directory into one manifest."""
    d = Path(directory)
    entries: dict[str, Any] = {}
    for p in sorted(d.glob("*.json")):
        if p.name == MANIFEST_NAME:
            continue
        entry: dict[str, Any] = {"sha256": file_digest(p), "bytes": p.stat().st_size}
        # bundle_id is recorded for cross-checking, not for verification. A reader comparing the
        # two can tell "the file changed" from "the flight changed", which are different problems.
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            entry["bundle_id"] = raw.get("bundle_id")
            entry["schema_version"] = raw.get("schema_version")
        except Exception:
            entry["bundle_id"] = None
            entry["schema_version"] = None
        entries[p.name] = entry

    created = created_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "created_utc": created,
        "entries": entries,
        "note": ("SHA-256 over file bytes. Verify with `python scripts/manifest.py --verify`. "
                 "An HMAC proves the manifest was written by a holder of the shared key; it is "
                 "not a signature and anyone who can verify it can also forge it."),
    }
    sig = _sign(_canonical(entries, created), key if key is not None else os.environ.get(KEY_ENV))
    if sig:
        manifest["hmac_sha256"] = sig
    return manifest


def write_manifest(directory: str | Path, key: str | None = None) -> Path:
    d = Path(directory)
    m = build_manifest(d, key=key)
    p = d / MANIFEST_NAME
    p.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return p


def verify_manifest(directory: str | Path, key: str | None = None) -> dict[str, Any]:
    """Check every recorded file against the manifest. Never repairs anything.

    Reports four states separately, because they mean different things to whoever is asking:
    modified, missing, unlisted (a file the manifest never saw), and an unsigned or badly signed
    manifest.
    """
    d = Path(directory)
    p = d / MANIFEST_NAME
    if not p.exists():
        return {"ok": False, "error": f"no {MANIFEST_NAME} in {d}",
                "note": "nothing to verify against; write one first"}

    m = json.loads(p.read_text(encoding="utf-8"))
    entries: dict[str, Any] = m.get("entries", {})
    modified: list[str] = []
    missing: list[str] = []

    for name, entry in sorted(entries.items()):
        f = d / name
        if not f.exists():
            missing.append(name)
            continue
        if file_digest(f) != entry.get("sha256"):
            modified.append(name)

    on_disk = {q.name for q in d.glob("*.json")} - {MANIFEST_NAME}
    unlisted = sorted(on_disk - set(entries))

    effective_key = key if key is not None else os.environ.get(KEY_ENV)
    stored_sig = m.get("hmac_sha256")
    if effective_key and stored_sig:
        expected = _sign(_canonical(entries, m.get("created_utc", "")), effective_key)
        # Constant-time: a timing side channel here would be a silly way to lose the property.
        signature = "valid" if hmac.compare_digest(expected or "", stored_sig) else "INVALID"
    elif stored_sig:
        signature = "present_but_unchecked_no_key"
    elif effective_key:
        signature = "absent_but_key_provided"
    else:
        signature = "unsigned"

    ok = not modified and not missing and signature in ("valid", "unsigned",
                                                        "present_but_unchecked_no_key")
    return {
        "ok": ok,
        "checked": len(entries),
        "modified": modified,
        "missing": missing,
        "unlisted": unlisted,
        "signature": signature,
        "note": ("Every listed file matches the manifest."
                 if ok else
                 "At least one file does not match what the manifest recorded. Nothing has been "
                 "changed on disk; decide what to do with these before using them as evidence."),
    }
