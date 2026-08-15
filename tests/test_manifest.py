"""Tamper-evidence over a bundle directory, offline.

`bundle_id` is a 16-hex content hash over the fields that make a flight that flight. Two gaps:
it is deliberately partial (airframe_id, coverage and health are excluded from identity on
purpose), and 64 bits is a fingerprint rather than a defence. This hashes file BYTES at full
length and records the set in one manifest.

The tests pin what it buys and, just as importantly, what it does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from sentinel.manifest import (
    MANIFEST_NAME, build_manifest, file_digest, verify_manifest, write_manifest,
)


@pytest.fixture()
def bundles():
    d = Path(__file__).resolve().parent / ".tmp-manifest" / uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (d / f"flight_{i}.json").write_text(
            json.dumps({"scenario": "x", "bundle_id": f"id{i}", "schema_version": 2, "n": i}),
            encoding="utf-8")
    yield d
    for f in d.glob("*"):
        f.unlink(missing_ok=True)
    d.rmdir()


def test_a_clean_directory_verifies(bundles):
    write_manifest(bundles)
    r = verify_manifest(bundles)
    assert r["ok"] and r["checked"] == 3 and not r["modified"]


def test_any_edit_is_detected(bundles):
    write_manifest(bundles)
    p = bundles / "flight_1.json"
    raw = json.loads(p.read_text())
    raw["n"] = 999
    p.write_text(json.dumps(raw), encoding="utf-8")
    r = verify_manifest(bundles)
    assert not r["ok"] and r["modified"] == ["flight_1.json"]


def test_an_edit_bundle_id_is_designed_to_ignore_is_still_detected(bundles):
    """The gap this closes: identity deliberately excludes the observing conditions."""
    write_manifest(bundles)
    p = bundles / "flight_0.json"
    raw = json.loads(p.read_text())
    raw["airframe_id"] = "someone-elses-drone"     # excluded from bundle_id by design
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert verify_manifest(bundles)["modified"] == ["flight_0.json"]


def test_a_deleted_file_is_missing_not_merely_absent(bundles):
    write_manifest(bundles)
    (bundles / "flight_2.json").unlink()
    r = verify_manifest(bundles)
    assert not r["ok"] and r["missing"] == ["flight_2.json"]


def test_a_file_the_manifest_never_saw_is_reported_unlisted(bundles):
    """Not a failure on its own, but an operator should know it appeared."""
    write_manifest(bundles)
    (bundles / "smuggled.json").write_text("{}", encoding="utf-8")
    r = verify_manifest(bundles)
    assert r["unlisted"] == ["smuggled.json"]


def test_hmac_detects_a_rewritten_manifest(bundles):
    """Without a key, re-running --write over tampered files hides the tampering."""
    write_manifest(bundles, key="secret")
    p = bundles / "flight_0.json"
    p.write_text(json.dumps({"scenario": "forged"}), encoding="utf-8")
    write_manifest(bundles, key="wrong-key")       # attacker regenerates without the real key
    assert verify_manifest(bundles, key="secret")["signature"] == "INVALID"


def test_a_valid_signature_reads_valid(bundles):
    write_manifest(bundles, key="secret")
    assert verify_manifest(bundles, key="secret")["signature"] == "valid"


def test_states_are_distinguished_not_collapsed(bundles):
    """unsigned, signed-but-unchecked and key-without-signature mean different things."""
    write_manifest(bundles)
    assert verify_manifest(bundles)["signature"] == "unsigned"
    assert verify_manifest(bundles, key="k")["signature"] == "absent_but_key_provided"
    write_manifest(bundles, key="k")
    assert verify_manifest(bundles)["signature"] == "present_but_unchecked_no_key"


def test_verification_never_repairs(bundles):
    write_manifest(bundles)
    p = bundles / "flight_1.json"
    p.write_text('{"tampered": true}', encoding="utf-8")
    before = p.read_bytes()
    verify_manifest(bundles)
    assert p.read_bytes() == before, "verification must not touch the files it inspects"


def test_missing_manifest_says_so_rather_than_passing(bundles):
    r = verify_manifest(bundles)
    assert not r["ok"] and MANIFEST_NAME in r["error"]


def test_digest_is_full_length_not_truncated():
    """64 bits is a fingerprint. This layer exists because that is not a defence."""
    p = Path(__file__)
    assert len(file_digest(p)) == 64


def test_the_manifest_excludes_itself(bundles):
    write_manifest(bundles)
    m = json.loads((bundles / MANIFEST_NAME).read_text())
    assert MANIFEST_NAME not in m["entries"]
    assert len(build_manifest(bundles)["entries"]) == 3
