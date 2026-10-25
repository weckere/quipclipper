#!/usr/bin/env python3
"""Turn CHANGELOG.md's Unreleased section into a dated release section.

    scripts/changelog-cut.py X.Y.Z YYYY-MM-DD [PREVIOUS_TAG]

release.sh runs this. Everything under ``## [Unreleased]`` becomes ``## [X.Y.Z] - date``,
except held-back blocks: lines from ``<!-- held back …`` to ``<!-- /held back -->`` (each
marker on a line of its own) stay in Unreleased, markers and all, for a later release
(D-040 held machine transcription back from 0.3.x this way). Removing a block's two marker lines releases
it with the next version. The compare links at the bottom are updated too. It fails, and
writes nothing, when nothing outside the held blocks is left to release.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = "https://github.com/weckere/quipclipper"
HELD = re.compile(r"^<!-- held back\b.*?^<!-- /held back -->[ \t]*\n?", re.M | re.S)


def has_entries(text: str) -> bool:
    """Whether a changelog section has anything besides headings and link definitions."""
    return any(line.strip() and not line.startswith(("#", "["))
               for line in text.splitlines())


def cut(text: str, version: str, today: str, previous: str = "") -> str:
    head = "## [Unreleased]\n"
    if head not in text:
        raise ValueError("CHANGELOG.md has no ## [Unreleased] section")
    before, after = text.split(head, 1)
    nxt = re.search(r"^## ", after, re.M)
    body, rest = (after[:nxt.start()], after[nxt.start():]) if nxt else (after, "")
    held = [m.group(0).rstrip("\n") for m in HELD.finditer(body)]
    released = HELD.sub("", body)
    if re.search(r"^<!-- /?held back", released, re.M):
        raise ValueError("CHANGELOG.md has a held-back marker without its pair")
    if not has_entries(released):
        raise ValueError("CHANGELOG.md's Unreleased section is empty: nothing to release")
    released = re.sub(r"\n{3,}", "\n\n", released).strip("\n")
    kept = "".join(f"\n{h}\n" for h in held)
    out = f"{before}{head}{kept}\n## [{version}] - {today}\n\n{released}\n\n{rest}"
    links = [f"[Unreleased]: {REPO}/compare/v{version}...dev",
             f"[{version}]: {REPO}/compare/{previous}...v{version}" if previous
             else f"[{version}]: {REPO}/releases/tag/v{version}"]
    return re.sub(r"^\[Unreleased\]: .*$", "\n".join(links), out, count=1, flags=re.M)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__.strip().splitlines()[2].strip())
    path = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    try:
        new = cut(path.read_text(), sys.argv[1], sys.argv[2],
                  sys.argv[3] if len(sys.argv) == 4 else "")
    except ValueError as exc:
        sys.exit(f"release: {exc}")
    path.write_text(new)
