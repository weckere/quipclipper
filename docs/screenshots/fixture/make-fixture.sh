#!/usr/bin/env bash
# Build the "Large Locutus Leporid (2016)" screenshot fixture:
#   docs/screenshots/fixture/make-fixture.sh OUT_DIR
#
# The footage is Big Buck Bunny (c) 2008 Blender Foundation, www.bigbuckbunny.org,
# CC BY 3.0, taken from the Blender Foundation's own upload (YouTube aqz-KE-bpKQ;
# download.blender.org sits behind a bot challenge). The subtitles are this
# folder's .srt files: a few Borg lines, for search to find.
#
# OUT_DIR gets two media roots for QC_MEDIA_ROOTS:
#   OUT_DIR/Movies/Large Locutus Leporid (2016)/   the whole 100 s section
#   OUT_DIR/Shows/The Next Leporation/Season 01/   three short "episodes" of it
# Needs ffmpeg, plus yt-dlp unless SRC names an already downloaded section
# (0:25-2:05 of the upload).
set -euo pipefail

out=${1:?usage: make-fixture.sh OUT_DIR}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out"
src=${SRC:-$out/bbb-section.mp4}
if [ ! -f "$src" ]; then
  yt-dlp --no-config -f "299+140" --download-sections "*00:25-02:05" \
    --force-keyframes-at-cuts --merge-output-format mp4 -o "$src" \
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
fi

# cut START DURATION OUT: 720p30 H.264 + AAC, light enough for any browser.
cut() {
  mkdir -p "$(dirname "$3")"
  ffmpeg -nostdin -v error -y -ss "$1" -t "$2" -i "$src" -vf scale=1280:-2,fps=30 \
    -c:v libx264 -crf 22 -preset slow -c:a aac -b:a 128k -movflags +faststart "$3"
}

movie="$out/Movies/Large Locutus Leporid (2016)"
cut 0 100 "$movie/Large Locutus Leporid (2016).mp4"
cp "$here/Large Locutus Leporid (2016).srt" "$movie/"

show="$out/Shows/The Next Leporation/Season 01"
n=0
while IFS='|' read -r start dur title; do
  n=$((n + 1))
  name="The Next Leporation - S01E0$n - $title"
  cut "$start" "$dur" "$show/$name.mp4"
  # Re-time the movie's sidecar to the episode: keep the cues inside it, shifted.
  python3 - "$here/Large Locutus Leporid (2016).srt" "$start" "$dur" > "$show/$name.srt" <<'PY'
import re, sys
text, start, dur = open(sys.argv[1]).read(), float(sys.argv[2]), float(sys.argv[3])
def secs(t):
    h, m, s = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
def fmt(x):
    ms = round(x * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
n = 0
for block in text.strip().split("\n\n"):
    lines = block.splitlines()
    a, b = (secs(t) for t in re.findall(r"[\d:,]+(?= |$)", lines[1]))
    if a >= start and b <= start + dur:
        n += 1
        print(f"{n}\n{fmt(a - start)} --> {fmt(b - start)}\n" + "\n".join(lines[2:]) + "\n")
PY
done <<'EPISODES'
0|20|Encounter at Meadowpoint
24|34|The Best of Both Burrows
58|42|Q Who Hops
EPISODES

find "$out/Movies" "$out/Shows" -type f | sort
