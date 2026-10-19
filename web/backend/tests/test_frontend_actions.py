"""Every web UI action keeps a control: the frontend is checked against the command registry.

``test_cli_parity.py`` stops a *new* UI action from skipping the CLI. This file guards the other
direction: each command that documents a Web UI entry (``Command.ui``) must still be carried
by a marked control in ``web/frontend``. A control is marked with ``data-action="<command id>"``
in ``index.html`` or in a template string in ``app.js`` (several ids may share one attribute,
separated by spaces), or with ``el.dataset.action = "<command id>"`` in ``app.js``. A redesign
can move, restyle or rebuild a control freely; it must keep the marker on whatever replaces it.

Dropping the "Reindex subtitles" button while "Index now" still calls ``index.build`` is invisible
to the parity test. Here it fails, because ``MARKED_CONTROLS`` below says how many marked places
each shared command must keep.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from quipclipper import commands as reg

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SOURCES = ("index.html", "app.js")
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                    reason="web/frontend is not part of this source tree")

# Commands with a Web UI entry that no single control carries, and why.
NOT_A_CONTROL: dict[str, str] = {
    "server.config": "read once on start (loadStatus); nothing on screen to click",
    "job.wait": "CLI-side composition of job.get; the UI polls job.get",
}

# Commands that more than one control carries, so a single surviving marker would hide a dropped
# button. command id -> minimum number of marked places (index.html + app.js occurrences).
MARKED_CONTROLS: dict[str, int] = {
    "index.build": 2,         # "Index now" and "Reindex subtitles" (folder)
    "clip.create": 2,         # the Marks-header clip button and the clip button on a search hit
    "bookmark.add": 3,        # the Marks-header bookmark button, its label form's Save, and a search hit's
    "clip.batch": 3,          # Clip selected (search) and Export selected (clips, bookmarks)
    "bookmark.delete": 2,     # item bookmark list and the Bookmarks view
    "job.download": 2,        # inline download after a clip, and the clip row's link
    "library.browse": 3,      # folder rows, breadcrumbs, Back
    "settings.set": 2,        # Name template and Auto-lang
    "job.cancel": 3,          # folder backfill Cancel, the Jobs popover's Cancel/Dismiss and Clear finished
    "youtube.download": 1,
    "youtube.delete-download": 1,
}

_ATTR = re.compile(r'data-action="([^"]+)"')
_DATASET = re.compile(r"""dataset\.action\s*=\s*["']([^"']+)["']""")


def _marker_occurrences(source: str) -> list[str]:
    """Command ids marked in ``source``, one entry per marked place."""
    out: list[str] = []
    for pattern in (_ATTR, _DATASET):
        for value in pattern.findall(source):
            out.extend(value.split())
    return out


def _all_markers() -> list[str]:
    found: list[str] = []
    for name in SOURCES:
        found.extend(_marker_occurrences((FRONTEND / name).read_text()))
    return found


def test_marker_scanner():
    src = ('<button data-action="clip.create">x</button> `<a data-action="a.b c.d">`'
           ' btn.dataset.action = "youtube.download"; el.dataset.action = \'job.get\'')
    assert _marker_occurrences(src) == ["clip.create", "a.b", "c.d", "youtube.download", "job.get"]


def test_exemptions_name_real_commands():
    for cid in NOT_A_CONTROL:
        assert cid in reg.BY_ID, f"NOT_A_CONTROL names a command that no longer exists: {cid}"
        assert reg.BY_ID[cid].ui not in ("", "—")
    for cid in MARKED_CONTROLS:
        assert cid in reg.BY_ID, f"MARKED_CONTROLS names a command that no longer exists: {cid}"


@needs_frontend
def test_every_ui_command_has_a_marked_control():
    marked = set(_all_markers())
    missing = sorted(c.id for c in reg.COMMANDS
                     if c.ui not in ("", "—") and c.id not in NOT_A_CONTROL and c.id not in marked)
    assert not missing, (
        f"Web UI actions with no control marked data-action in web/frontend: {missing}. "
        "Put data-action=\"<command id>\" on the control that carries the action "
        "(or add a NOT_A_CONTROL entry with a reason).")


@needs_frontend
def test_exempt_commands_stay_exempt_only_while_unmarked():
    marked = set(_all_markers())
    stale = sorted(cid for cid in NOT_A_CONTROL if cid in marked)
    assert not stale, f"these commands now have a marked control; drop them from NOT_A_CONTROL: {stale}"


@needs_frontend
def test_every_marker_names_a_real_command():
    unknown = sorted({m for m in _all_markers() if m not in reg.BY_ID})
    assert not unknown, f"data-action values that are not command ids: {unknown}"


@needs_frontend
def test_shared_commands_keep_each_of_their_controls():
    counts: dict[str, int] = {}
    for m in _all_markers():
        counts[m] = counts.get(m, 0) + 1
    short = {cid: (counts.get(cid, 0), need) for cid, need in MARKED_CONTROLS.items()
             if counts.get(cid, 0) < need}
    assert not short, (
        "a command lost one of its controls (found, expected at least): "
        f"{short}. A redesign may rebuild a control but must keep it.")
