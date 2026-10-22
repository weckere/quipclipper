"""The colour tokens meet WCAG 2.2 AA contrast, in dark and light, normal and high contrast (M16).

``web/frontend/tokens.css`` defines every colour the UI uses. This test reads it and checks the
pairs the stylesheet actually puts together:

* text colours (``--text``, ``--dim``, ``--faint``, ``--accent-text``, ``--ok``, ``--danger``,
  ``--playhead`` for speaker names, ``--machine`` for machine transcript lines) on every ground (``--bg``, ``--surface``, ``--raised``,
  ``--sunken``): 4.5:1 (SC 1.4.3);
* text on the selection, playhead and transcript-bar tints (``--accent-soft``, ``--playhead-soft``,
  ``--machine-soft`` laid over a ground), machine lines included, and ``--accent-ink`` on ``--accent`` (the primary buttons): 4.5:1;
* UI colours on every ground: input and button borders (``--line-strong``), the switch and the
  selection edges (``--accent``), the focus ring (``--accent-text``), the playhead, the job marks
  and the dialogue map's bars (``--faint``): 3:1 (SC 1.4.11).

A change to a token that breaks a pair fails here with the pair and its ratio.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parents[2] / "frontend" / "tokens.css"
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not TOKENS.exists(), reason="web/frontend is not part of this source tree")

TEXT = ("text", "dim", "faint", "accent-text", "ok", "danger", "playhead", "machine")
GROUNDS = ("bg", "surface", "raised", "sunken")
TINTS = ("accent-soft", "playhead-soft", "machine-soft")
UI = ("line-strong", "accent", "accent-text", "playhead", "ok", "danger", "faint", "machine")


def _decls(body: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", body))


def _blocks(css: str) -> list[tuple[str, str, dict[str, str]]]:
    """(media query or "", selector, declarations) for every rule that sets custom properties."""
    out = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"@media\s*([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}|([^{}@]+)\{([^{}]*)\}", css):
        if m.group(1) is not None:
            for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", m.group(2)):
                out.append((m.group(1).strip(), sel.strip(), _decls(body)))
        else:
            out.append(("", m.group(3).strip(), _decls(m.group(4))))
    return [b for b in out if b[2]]


def themes() -> dict[str, dict[str, str]]:
    """The four resolved palettes: dark, light, and each with prefers-contrast: more."""
    blocks = _blocks(TOKENS.read_text())
    pick = lambda media, sel: next(d for m, s, d in blocks if m == media and s == sel)  # noqa: E731
    dark = pick("", ":root")
    light_forced = pick("", ':root[data-theme="light"]')
    light_os = pick("(prefers-color-scheme: light)", ":root:not([data-theme])")
    assert light_os == light_forced, "the OS-light and forced-light token blocks have drifted apart"
    light = {**dark, **light_forced}
    hc_dark = pick("(prefers-contrast: more)", ":root")
    hc_light = pick("(prefers-contrast: more)", ':root[data-theme="light"]')
    hc_light_os = pick("(prefers-contrast: more) and (prefers-color-scheme: light)", ":root:not([data-theme])")
    assert hc_light == hc_light_os, "the OS-light and forced-light high-contrast blocks have drifted apart"
    return {
        "dark": dark, "light": light,
        "dark, more contrast": {**dark, **hc_dark}, "light, more contrast": {**light, **hc_light},
    }


def rgba(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#") and len(value) == 7:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), 1.0)
    m = re.fullmatch(r"rgba\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)", value)
    assert m, f"unparsed colour {value!r}"
    r, g, b, a = (float(x) for x in m.groups())
    return (r, g, b, a)


def over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def luminance(c) -> float:
    def ch(x: float) -> float:
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def failures(t: dict[str, str]) -> list[str]:
    c = {k: rgba(v) for k, v in t.items() if v.strip().startswith(("#", "rgba"))}
    bad = []

    def need(fg_name, fg, bg_name, bg, minimum):
        r = ratio(fg, bg)
        if r < minimum:
            bad.append(f"{fg_name} on {bg_name}: {r:.2f} < {minimum}")

    for f in TEXT:
        for g in GROUNDS:
            need(f, c[f], g, c[g], 4.5)
    for tint in TINTS:
        for g in GROUNDS:
            for f in ("text", "accent-text", "machine"):
                need(f, c[f], f"{tint} over {g}", over(c[tint], c[g]), 4.5)
    need("accent-ink", c["accent-ink"], "accent", c["accent"], 4.5)
    for f in UI:
        for g in GROUNDS:
            need(f, c[f], g, c[g], 3.0)
    return bad


def test_contrast_maths():
    assert round(ratio(rgba("#000000"), rgba("#FFFFFF")), 2) == 21.0
    assert round(ratio(rgba("#767676"), rgba("#FFFFFF")), 2) == 4.54  # the classic AA grey
    assert over(rgba("rgba(0, 0, 0, 0.5)"), rgba("#FFFFFF"))[:3] == (127.5, 127.5, 127.5)


@needs_frontend
@pytest.mark.parametrize("theme", ["dark", "light", "dark, more contrast", "light, more contrast"])
def test_tokens_meet_wcag_aa(theme):
    bad = failures(themes()[theme])
    assert not bad, f"{theme}: " + "; ".join(bad)
