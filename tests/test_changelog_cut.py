"""scripts/changelog-cut.py: how release.sh turns Unreleased into a release (D-040)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "changelog-cut.py"
_spec = importlib.util.spec_from_file_location("changelog_cut", SCRIPT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

LOG = """# Changelog

## [Unreleased]

<!-- held back (D-040): later. -->

### Later feature

- Held line.

<!-- /held back -->

### Now

- Shipping line.

### Fixed

- A fix, mentioning `<!-- held back -->` in passing.

## [0.2.0] - 2026-10-21

- Old.

[Unreleased]: https://github.com/weckere/quipclipper/compare/v0.2.0...dev
[0.2.0]: https://github.com/weckere/quipclipper/compare/v0.1.1...v0.2.0
"""


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^## ", text[start + len(heading):], re.M)
    return text[start:start + len(heading) + nxt.start()] if nxt else text[start:]


def test_held_blocks_stay_in_unreleased():
    out = cc.cut(LOG, "0.3.0", "2026-10-23", "v0.2.0")
    unreleased = _section(out, "## [Unreleased]")
    assert "<!-- held back (D-040)" in unreleased and "- Held line." in unreleased
    assert "<!-- /held back -->" in unreleased and "Shipping line" not in unreleased
    release = _section(out, "## [0.3.0] - 2026-10-23")
    assert "Held line" not in release and "Later feature" not in release
    assert release.startswith("## [0.3.0] - 2026-10-23\n\n### Now\n\n- Shipping line.")
    assert "- A fix, mentioning `<!-- held back -->` in passing." in release
    assert "## [0.2.0] - 2026-10-21\n\n- Old." in out
    assert "[Unreleased]: https://github.com/weckere/quipclipper/compare/v0.3.0...dev\n" in out
    assert "[0.3.0]: https://github.com/weckere/quipclipper/compare/v0.2.0...v0.3.0\n" in out
    # The next release carries the block on again.
    again = cc.cut(out.replace("## [0.3.0]", "### Next\n\n- More.\n\n## [0.3.0]", 1),
                   "0.3.1", "2026-10-30", "v0.3.0")
    assert "- Held line." in _section(again, "## [Unreleased]")
    assert "- More." in _section(again, "## [0.3.1] - 2026-10-30")


def test_without_held_blocks_it_is_the_old_cut():
    log = LOG.replace("<!-- held back (D-040): later. -->\n\n", "").replace(
        "<!-- /held back -->\n\n", "")
    out = cc.cut(log, "0.3.0", "2026-10-23", "v0.2.0")
    assert out.startswith("# Changelog\n\n## [Unreleased]\n\n## [0.3.0] - 2026-10-23\n\n"
                          "### Later feature\n\n- Held line.\n\n### Now")


@pytest.mark.parametrize("log, why", [
    (LOG.split("### Now")[0] + "## [0.2.0] - 2026-10-21\n", "nothing to release"),
    (LOG.replace("<!-- /held back -->\n", ""), "without its pair"),
    (LOG.replace("## [Unreleased]\n", ""), "no ## [Unreleased]"),
])
def test_refusals(log, why):
    with pytest.raises(ValueError, match=re.escape(why)):
        cc.cut(log, "0.3.0", "2026-10-23", "v0.2.0")


def test_the_real_changelog_releases_without_its_held_blocks():
    text = (ROOT / "CHANGELOG.md").read_text()
    if not cc.has_entries(cc.HELD.sub("", _section(text, "## [Unreleased]"))):
        # Right after a release only the held-back blocks are left: nothing to cut yet.
        with pytest.raises(ValueError, match="nothing to release"):
            cc.cut(text, "99.0.0", "2099-01-01", "v0.2.0")
        return
    out = cc.cut(text, "99.0.0", "2099-01-01", "v0.2.0")
    release = _section(out, "## [99.0.0] - 2099-01-01")
    for block in cc.HELD.findall(_section(text, "## [Unreleased]")):
        for line in block.splitlines():
            if line.startswith("- "):
                assert line not in release


def test_the_script_writes_nothing_when_it_fails(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "changelog-cut.py").write_text(SCRIPT.read_text())
    log = LOG.split("### Now")[0] + "## [0.2.0] - 2026-10-21\n"
    (tmp_path / "CHANGELOG.md").write_text(log)
    r = subprocess.run([sys.executable, str(tmp_path / "scripts" / "changelog-cut.py"),
                        "0.3.0", "2026-10-23", "v0.2.0"], capture_output=True, text=True)
    assert r.returncode != 0 and "nothing to release" in r.stderr
    assert (tmp_path / "CHANGELOG.md").read_text() == log
