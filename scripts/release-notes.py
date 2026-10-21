#!/usr/bin/env python3
"""Print the GitHub Release notes for a version: its CHANGELOG.md section.

    scripts/release-notes.py X.Y.Z

GitHub renders every newline in release notes as a line break, so the changelog's
wrapped paragraphs and list items are joined back into single lines; headings, list
items, table rows, quotes, fenced code and blank lines stay as they are. release.yml uses this; so can a person
fixing a published Release (gh release edit vX.Y.Z --notes-file ...).
"""
import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("v")
text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
found = re.search(rf"^## \[{re.escape(version)}\] - .*?\n(.*?)(?=^## |\Z)", text, re.M | re.S)
if not found:
    sys.exit(f"CHANGELOG.md has no dated section for {version}")

starts_block = re.compile(r"\s*([-*+]\s|\d+\.\s|#|\[[^\]]+\]:\s|```|<|\||>)")
stands_alone = re.compile(r"\s*(#|\||```)")  # a line nothing is joined onto
out: list[str] = []
fenced = False
for line in found.group(1).splitlines():
    if line.lstrip().startswith("```"):
        fenced = not fenced
        out.append(line)
        continue
    if fenced:
        out.append(line)
        continue
    if re.match(r"\[[^\]]+\]: ", line):  # link reference definitions
        continue
    prev = out[-1] if out else ""
    if line.strip() and prev.strip() and not starts_block.match(line) and not stands_alone.match(prev):
        out[-1] = prev.rstrip() + " " + line.strip()
    else:
        out.append(line)
print("\n".join(out).strip())
