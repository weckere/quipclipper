"""The dialogue map shows any selection (M20): its amber band is the range the transport
plays, ``wfSel()`` (the selected lines first, else a free range: a waveform drag, I / O, a
bookmark or a link's span), with Before / After, not only a line selection.

There's no JS runtime in the tests, so these read ``app.js``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _function(name: str) -> str:
    js = (FRONTEND / "app.js").read_text()
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", js, re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_the_amber_band_is_the_transports_range():
    sel = _function("updateMapSelection")
    assert "wfSel()" in sel
    # The line-only readers would hide a free range.
    for line_only in ("hasClipRange()", "bufStart()", "bufEnd()"):
        assert line_only not in sel, line_only


def test_every_selection_change_redraws_the_band_even_without_a_waveform():
    on_sel = _function("wfOnSelection")
    # Before the wf.path guard: a file with no waveform yet still has a map.
    assert on_sel.index("updateMapSelection()") < on_sel.index("if (!wf.path) return;")
    # A free range made or cleared, lines selected, Before / After typed or dragged.
    for fn in ("showClipTarget", "updateClipUI", "padsChanged"):
        assert "wfOnSelection()" in _function(fn), fn
