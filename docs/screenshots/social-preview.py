"""The GitHub social preview (1280 x 640), drawn with FigCraft from the app's dark tokens.

    python3 docs/screenshots/social-preview.py FRAME.png OUT_DIR

FRAME.png is the fixture's frame at 0:40.1 (the big bunny), e.g.
    ffmpeg -ss 40.1 -i "Large Locutus Leporid (2016).mp4" -frames:v 1 -vf scale=1008:-2 frame.png
Writes OUT_DIR/social-preview.{json,figcraft,png}. Needs figcraft-cli and the kit from the
figcraft skill (~/.claude/skills/figcraft/figcraft_kit.py).
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/skills/figcraft"))
from figcraft_kit import Script, build  # noqa: E402

# tokens.css, dark
BG, SURFACE, RAISED, SUNKEN = "#0F1115", "#171A20", "#202530", "#0B0D10"
LINE, LINE_STRONG = "#2B313C", "#6A7282"
TEXT, DIM, FAINT = "#E9EBEF", "#A6ACB8", "#8890A0"
ACCENT, ACCENT_INK, PLAYHEAD = "#F2B544", "#1A1303", "#5BB5FF"
ACCENT_SOFT = "#F2B54429"
MONO = "Liberation Mono"

frame_png, out_dir = sys.argv[1], sys.argv[2]
W, H = 1280, 640
s = Script(font="Inter", text_color=TEXT)
page = s.frame(None, "social preview", 0, 0, W, H, fill=BG)


def scissors(scale):
    """The logo mark (index.html's 24-unit path), scaled; circles as two arcs."""
    k = scale
    def circle(cx, cy, r):
        return (f"M{(cx + r) * k} {cy * k} A{r * k} {r * k} 0 1 1 {(cx - r) * k} {cy * k} "
                f"A{r * k} {r * k} 0 1 1 {(cx + r) * k} {cy * k} Z")
    return (circle(6, 6, 2.5) + " " + circle(6, 18, 2.5) +
            f" M{8 * k} {7.6 * k} L{20 * k} {18.5 * k} M{8 * k} {16.4 * k} L{20 * k} {5.5 * k}")


# --- left: the name and what it does ---------------------------------------------------
X = 80
logo = s.frame(page, "logo", X, 104, 88, 88, fill=ACCENT, radius=22)
s.vector(logo, "mark", 14, 14, 60, 60, scissors(2.5), fill=None, stroke=ACCENT_INK,
         strokeWeight=5.2, strokeCap="round", strokeJoin="round")
s.text(page, "quipclipper", X, 222, size=76, style="Bold", letterSpacing="-2%")
s.text(page, "Find a moment by its dialogue,\nand cut it.", X, 330, size=34, w=540,
       color=DIM, lineHeight="130%")

chips = ["Self-hosted web app", "CLI", "Lossless clips"]
cx = X
for label in chips:
    w = 22 + int(len(label) * 10.2) + 22
    chip = s.frame(page, f"chip {label}", cx, 456, w, 44, fill=SURFACE, radius=22, stroke=LINE)
    s.text(chip, label, 0, 11, size=18, w=w, align="center", style="Medium", color=TEXT)
    cx += w + 12
s.text(page, "github.com/weckere/quipclipper", X, 548, size=20, color=FAINT)

# --- right: a slice of the workspace ---------------------------------------------------
CX, CW = 700, 500
IW, IH = CW - 32, int((CW - 32) * 568 / 1008)
SH = 96                                     # the waveform strip
CH = 16 + IH + 16 + SH + 12 + 3 * 40 + 16   # video, waveform, three lines
CY = (H - CH) // 2
card = s.frame(page, "workspace card", CX, CY, CW, CH, fill=SURFACE, radius=18, stroke=LINE)
video = s.frame(card, "video", 16, 16, IW, IH, fill="#000000", radius=10)
s.image(video, frame_png, 0, 0, IW, IH)
sub = "You will become one with the Borg."
SW = 340  # fits the line at 19 px
s.rect(video, "subtitle box", (IW - SW) // 2, IH - 50, SW, 32, "#000000B8", radius=4)
s.text(video, sub, 0, IH - 46, size=19, w=IW, align="center", color="#FFFFFF", style="Medium")

# The waveform strip, as the app draws it (app.js wfDraw): the subtitle lines as spans
# along the top, the wave, the selection shaded between its amber in and out handles,
# the playhead blue, a time ruler along the bottom. Zoomed to the selected line.
MY = 16 + IH + 16
V0, V1 = 36.6, 43.6
CUE_LANE, RULER = 18, 16
strip = s.frame(card, "waveform", 16, MY, IW, SH, fill=SUNKEN, radius=8, stroke=LINE)


def wx(t):
    return IW * (t - V0) / (V1 - V0)


TOP, BOTTOM = CUE_LANE, SH - RULER
MID, HALF = (TOP + BOTTOM) / 2, (BOTTOM - TOP) / 2 - 2
SEL_IN, SEL_OUT = 38.3, 41.9
s.rect(strip, "selection", wx(SEL_IN), 0, wx(SEL_OUT) - wx(SEL_IN), BOTTOM, ACCENT_SOFT)

cues = [(34.0, 37.5, "From this time forward, you will service us."),
        (38.5, 41.5, "You will become one with the Borg."),
        (45.0, 48.0, "Strength is irrelevant.")]
for a, b, text in cues:
    if b < V0 or a > V1:
        continue
    l, r = max(0, wx(a)), min(IW, wx(b))
    s.rect(strip, f"cue {a}", l, 2, r - l, CUE_LANE - 4, RAISED)
    for x in (wx(a), wx(b) - 1):
        if 0 <= x <= IW - 1:
            s.rect(strip, f"cue edge {x:.0f}", x, 2, 1, CUE_LANE - 4, LINE_STRONG)
            s.rect(strip, f"cue line {x:.0f}", x, TOP, 1, BOTTOM - TOP, LINE)
    span = s.frame(strip, f"cue label {a}", l + 4, 2, r - l - 8, CUE_LANE - 4)
    s.text(span, text, 0, 0, size=10, color=TEXT)


def level(t):
    """A made-up speech envelope: syllables inside the lines, room tone between them."""
    amp = 0.04
    for a, b, _ in cues:
        if a - 0.05 <= t <= b + 0.2:
            edge = min(1, (t - a + 0.05) / 0.12, (b + 0.2 - t) / 0.3)
            # Syllables of uneven length and stress, from a few incommensurate rates.
            syll = (0.55 + 0.45 * math.sin(t * 9.7 + 1.3)) * abs(math.sin(t * 13.1 + math.sin(t * 3.7)))
            amp = max(amp, edge * (0.12 + 0.85 * syll ** 0.8))
    return min(1, amp)


rng = random.Random(1701)  # fixed, so the picture doesn't change between runs
s.rect(strip, "centre line", 0, round(MID), IW, 1, LINE)
xs = [i * 1.5 for i in range(int(IW / 1.5) + 1)]
peaks = [level(V0 + (V1 - V0) * x / IW) for x in xs]
upper = [(x, MID - p * (0.7 + 0.3 * rng.random()) * HALF) for x, p in zip(xs, peaks)]
lower = [(x, MID + p * (0.7 + 0.3 * rng.random()) * HALF) for x, p in zip(xs, peaks)]
wave = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in upper + lower[::-1]) + " Z"
s.vector(strip, "wave", 0, 0, IW, SH, wave, fill=DIM)

for t in range(37, 44):
    x = wx(t)
    s.rect(strip, f"tick {t}", x, BOTTOM, 1, 4, LINE_STRONG)
    s.text(strip, f"0:{t}", x + 3, BOTTOM + 2, size=10, font=MONO, color=FAINT)

s.rect(strip, "playhead", wx(40.1) - 1, 0, 2, BOTTOM, PLAYHEAD)
for name, t, grip_x in (("in", SEL_IN, 0), ("out", SEL_OUT, -12)):
    x = wx(t) - 1
    s.rect(strip, f"{name} handle", x, 0, 2, BOTTOM, ACCENT)
    s.rect(strip, f"{name} grip", x + grip_x, BOTTOM - 26, 14, 26, ACCENT, radius=3,
           stroke=ACCENT_INK, strokeWeight=1)

# Three script lines, the middle one selected.
lines = [("0:34.0", "From this time forward, you will service us."),
         ("0:38.5", "You will become one with the Borg."),
         ("0:45.0", "Strength is irrelevant.")]
LY = MY + SH + 12
for i, (t, text) in enumerate(lines):
    y = LY + i * 40
    sel = i == 1
    if sel:
        s.rect(card, "selection", 0, y, CW, 40, ACCENT_SOFT)
        s.rect(card, "selection edge", 0, y, 4, 40, ACCENT)
    s.text(card, t, 20, y + 11, size=15, font=MONO, color=ACCENT if sel else FAINT)
    s.text(card, text, 92, y + 10, size=16, w=CW - 110, color=TEXT,
           style="SemiBold" if sel else "Regular")

build(s, os.path.join(out_dir, "social-preview.figcraft"),
      png=os.path.join(out_dir, "social-preview.png"), scale=1)
