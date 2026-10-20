"""The GitHub social preview (1280 x 640), drawn with FigCraft from the app's dark tokens.

    python3 docs/screenshots/social-preview.py FRAME.png OUT_DIR

FRAME.png is the fixture's frame at 0:40.1 (the big bunny), e.g.
    ffmpeg -ss 40.1 -i "Large Locutus Leporid (2016).mp4" -frames:v 1 -vf scale=1008:-2 frame.png
Writes OUT_DIR/social-preview.{json,figcraft,png}. Needs figcraft-cli and the kit from the
figcraft skill (~/.claude/skills/figcraft/figcraft_kit.py).
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/skills/figcraft"))
from figcraft_kit import Script, build  # noqa: E402

# tokens.css, dark
BG, SURFACE, RAISED, LINE = "#0F1115", "#171A20", "#202530", "#2B313C"
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
CH = 16 + IH + 16 + 44 + 12 + 3 * 40 + 16   # video, map, three lines
CY = (H - CH) // 2
card = s.frame(page, "workspace card", CX, CY, CW, CH, fill=SURFACE, radius=18, stroke=LINE)
video = s.frame(card, "video", 16, 16, IW, IH, fill="#000000", radius=10)
s.image(video, frame_png, 0, 0, IW, IH)
sub = "You will become one with the Borg."
SW = 340  # fits the line at 19 px
s.rect(video, "subtitle box", (IW - SW) // 2, IH - 50, SW, 32, "#000000B8", radius=4)
s.text(video, sub, 0, IH - 46, size=19, w=IW, align="center", color="#FFFFFF", style="Medium")

# The dialogue map: a bar per line, the selection amber, the playhead blue.
MY = 16 + IH + 16
strip = s.frame(card, "dialogue map", 16, MY, IW, 44, fill=BG, radius=8, stroke=LINE)
cues = [2, 5, 9, 13.5, 17, 26, 29, 34, 38.5, 45, 52, 61, 66, 73, 86, 91, 96]
for t in cues:
    x = 10 + (IW - 20) * t / 100
    s.rect(strip, f"bar {t}", x, 8, 3, 28, ACCENT if t == 38.5 else FAINT, radius=1)
s.rect(strip, "playhead", 10 + (IW - 20) * 40.1 / 100, 4, 2, 36, PLAYHEAD)

# Three script lines, the middle one selected.
lines = [("0:34.0", "From this time forward, you will service us."),
         ("0:38.5", "You will become one with the Borg."),
         ("0:45.0", "Strength is irrelevant.")]
LY = MY + 44 + 12
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
      png=os.path.join(out_dir, "social-preview.png"))
