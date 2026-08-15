#!/usr/bin/env python3
"""Write or verify a tamper-evidence manifest over a bundle directory.

    python scripts/manifest.py --write            # after capturing flights
    python scripts/manifest.py --verify           # before trusting them as evidence

Set SENTINEL_MANIFEST_KEY to add an HMAC. That proves the manifest was written by a holder of the
key -- it is a shared secret, not a signature, so anyone who can verify it can also forge it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sentinel.manifest import verify_manifest, write_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.write == args.verify:
        print("choose exactly one of --write / --verify", file=sys.stderr)
        return 2

    if args.write:
        p = write_manifest(args.bundles)
        print(f"wrote {p}")
        return 0

    r = verify_manifest(args.bundles)
    if "error" in r:
        print(r["error"], file=sys.stderr)
        return 1

    print(f"checked {r['checked']} files   signature: {r['signature']}")
    for label in ("modified", "missing", "unlisted"):
        for name in r[label]:
            print(f"  {label.upper():<9} {name}")
    print(f"\n{r['note']}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
