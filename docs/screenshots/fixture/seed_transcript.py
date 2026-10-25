"""Machine transcription for the screenshots: a stand-in whisper.cpp and a few machine lines.

    FIXTURE=<make-fixture.sh OUT_DIR> python3 seed_transcript.py

Big Buck Bunny has no dialogue for whisper to hear, so the lines are written straight into
the server's transcript store ($FIXTURE/state/transcripts), as a gaps run of the movie
would have kept them: in the stretches between its subtitle lines. It also writes a
whisper-cli that does nothing and an empty model file (into $FIXTURE/whisper), so the
server, started with QC_WHISPER_BIN and QC_WHISPER_MODEL pointing at them, has
transcription on (D-053) and shows the transcript bar. Nothing here can transcribe.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from quipclipper import transcribe as tr  # noqa: E402
from quipclipper.models import Cue  # noqa: E402

FIXTURE = Path(os.environ["FIXTURE"])
MOVIE = FIXTURE / "Movies/Large Locutus Leporid (2016)/Large Locutus Leporid (2016).mp4"

# (start, end, text) in the movie's gaps between subtitle lines; each gap is a range done.
LINES = {
    (19.0, 26.0): [(19.8, 24.6, "♪ Hop along, little leporid, the hive is calling ♪")],
    (41.5, 45.0): [(41.9, 44.6, "♪ Carrots are irrelevant ♪")],
    (48.0, 52.0): [(48.4, 51.6, "Fine. We'll hop to the cube.")],
    (55.5, 61.0): [(56.0, 60.4, "♪ One with the burrow, one with the Borg ♪")],
}

whisper = FIXTURE / "whisper"
whisper.mkdir(exist_ok=True)
exe = whisper / "whisper-cli"
exe.write_text("#!/bin/sh\necho 'screenshot fixture: no whisper here' >&2\nexit 1\n")
exe.chmod(0o755)
(whisper / "ggml-small.en.bin").write_bytes(b"")

store = tr.TranscriptStore(FIXTURE / "state" / "transcripts")
store.clear(MOVIE)
for done, lines in LINES.items():
    store.add(MOVIE, done, [Cue(0, s, e, t, source=tr.MACHINE) for s, e, t in lines],
              model="small.en")
print(f"seeded {sum(map(len, LINES.values()))} machine lines; start the server with")
print(f"  QC_WHISPER_BIN={exe} QC_WHISPER_MODEL={whisper / 'ggml-small.en.bin'}")
