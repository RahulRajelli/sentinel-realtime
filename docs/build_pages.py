"""Inline `_sheet.css` into each `_body-*.html` to produce a self-contained page.

The published pages must be self-contained -- the artifact host blocks external stylesheets, so a
`<link>` would silently render an unstyled page. Pasting the same CSS into three files instead
guarantees they drift, and these three pages are the same document at three depths: if they stop
looking like one system, the reader stops treating them as one source.

So: one stylesheet, three bodies, and a build step that cannot forget one.

    python docs/build_pages.py            # rebuild all three
    python docs/build_pages.py --check    # non-zero exit if any page is stale

Output is committed, because the pages are published from disk and a reader cloning the repo
should get exactly what was published.
"""

from __future__ import annotations

import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
CSS = DOCS / "_sheet.css"

PAGES = {
    "_body-plain.html": "whitepaper-plain.html",
    "_body-bridge.html": "whitepaper.html",
    "_body-technical.html": "whitepaper-technical.html",
}


def render(body_path: Path, css: str) -> str:
    body = body_path.read_text(encoding="utf-8")
    if "</title>" not in body:
        raise SystemExit(f"{body_path.name}: needs a <title> as its first element")
    # The style block goes immediately after the title so the host's 8KB title scan still finds
    # the title first -- putting CSS above it has pushed the title out of range before.
    head, rest = body.split("</title>", 1)
    return f"{head}</title>\n\n<style>\n{css}</style>\n{rest}"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    css = CSS.read_text(encoding="utf-8")
    stale: list[str] = []

    for body_name, out_name in PAGES.items():
        body_path, out_path = DOCS / body_name, DOCS / out_name
        if not body_path.exists():
            print(f"MISSING {body_name}")
            return 2
        rendered = render(body_path, css)
        current = out_path.read_text(encoding="utf-8") if out_path.exists() else None
        if current == rendered:
            print(f"  ok      {out_name}")
            continue
        stale.append(out_name)
        if check:
            print(f"  STALE   {out_name}")
        else:
            out_path.write_text(rendered, encoding="utf-8")
            print(f"  built   {out_name}  ({len(rendered):,} bytes)")

    if check and stale:
        print(f"\n{len(stale)} page(s) stale. Run: python docs/build_pages.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
