"""Machine transcription with whisper.cpp (M21): find what the subtitles left out.

Everything quipclipper searches comes from subtitle cues, so a sung line the
subtitle author skipped, or a file with no subtitles at all, can't be found. This
module runs whisper.cpp's ``whisper-cli`` over a stretch of a file's audio and keeps
what it hears as *machine* cues (``Cue.source == "machine"``), which the web app and
the CLI mix into the script and search, always marked as machine-made.

One primitive does the work: transcribe a time range. The three uses are ranges:
a waveform selection, every gap between a file's subtitle lines (:func:`find_gaps`),
or the whole file. Long ranges run in chunks, so a job reports progress, can be
cancelled between chunks (or mid-chunk, by killing the process) and keeps every
finished chunk.

The audio is decoded with the waveform's anchored seek (:mod:`quipclipper.waveform`),
so a transcript time is a clip time. Every subprocess, ffmpeg included, runs at idle
priority through :mod:`quipclipper.idle`, with at most :data:`MAX_THREADS` threads:
transcription must never slow down playback on a shared host (a whisper run at
normal priority starved on-demand HLS encoding once, 2026-10-21).

Transcripts are stored apart from the media and the subtitle cache, in a
:class:`TranscriptStore` (the web app's state dir, or ``~/.cache/quipclipper``),
keyed on the media file: a new subtitle file doesn't throw away hours of CPU. An
explicit action copies one into the library as a subtitle sidecar named
``<stem>.<lang>.whisper.srt`` (:func:`machine_sidecar_path`), which quipclipper
recognises as machine-made and never picks as a file's subtitles.

The feature is experimental and off unless a deployment turns it on (D-040):
``QC_TRANSCRIBE=1`` (the NixOS module's ``transcribe.enable`` sets it). Off, nothing
transcribes, and stored transcripts and machine sidecars stay out of search and the
script (:func:`enabled`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from quipclipper import idle, waveform
from quipclipper.models import Cue, format_timestamp
from quipclipper.subtitles import SIDECAR_TOKEN, load_subtitles

MACHINE = "machine"
ENGINE = "whisper.cpp"
# The models a run may ask for, by name; the default is small.en (D-037). English-only.
MODELS = ("small.en", "medium.en")
DEFAULT_MODEL = "small.en"
# Never more threads than this, whatever the configuration says (the host is shared).
MAX_THREADS = 3
DEFAULT_THREADS = 2
SAMPLE_RATE = 16000
# Context decoded either side of a chunk (whisper hears a word cut at the edge), and
# the longest stretch handed to one whisper-cli run.
CONTEXT = 2.0
CHUNK = 180.0
# Gaps between subtitle lines shorter than this aren't worth a run.
MIN_GAP = 4.0
# The longest range a request may ask for when the file's length is unknown (a week:
# far past any media, and still only a few thousand chunks).
MAX_SECONDS = 7 * 24 * 3600.0
# Store format version (bump on an incompatible change; older files are then ignored).
STORE_VERSION = 1



# --- configuration ------------------------------------------------------------------

# The explicit switch (D-040): off unless set to one of these. A server with whisper and a
# model but no switch still reports transcription off.
SWITCH = "QC_TRANSCRIBE"
OFF_REASON = ("Machine transcription is experimental and off here: a deployment turns it "
              f"on with {SWITCH}=1 (the NixOS module's transcribe.enable).")


def enabled(env=None) -> bool:
    """Whether machine transcription is switched on (``QC_TRANSCRIBE``)."""
    e = os.environ if env is None else env
    return (e.get(SWITCH) or "").strip().lower() in ("1", "true", "yes", "on")


def _clamp_threads(n: int | str | None) -> int:
    try:
        n = int(n) if n not in (None, "") else DEFAULT_THREADS
    except (TypeError, ValueError):
        n = DEFAULT_THREADS
    return max(1, min(MAX_THREADS, n))


def _models(raw: str | None, default: str | None) -> dict[str, str]:
    """``QC_WHISPER_MODELS`` (``small.en=/path/ggml-small.en.bin,medium.en=/path…``),
    plus ``QC_WHISPER_MODEL`` when that is a path rather than a name."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        name, sep, path = part.partition("=")
        if sep and name.strip() and path.strip():
            out[name.strip()] = path.strip()
    if default and ("/" in default or default.endswith(".bin")):
        name = model_name_from_path(default)
        out.setdefault(name, default)
    return out


def model_name_from_path(path: str) -> str:
    """``…/ggml-small.en.bin`` → ``small.en``."""
    stem = Path(path).name
    stem = re.sub(r"\.bin$", "", stem)
    return re.sub(r"^ggml-", "", stem) or stem


@dataclass(frozen=True)
class WhisperConfig:
    """Where whisper.cpp and its models are. From the environment (the NixOS module
    sets these): ``QC_WHISPER_BIN`` (default ``whisper-cli`` on PATH),
    ``QC_WHISPER_MODELS`` (name=path pairs), ``QC_WHISPER_MODEL`` (the default
    model's name, or a path), ``QC_WHISPER_VAD_MODEL`` (Silero, optional),
    ``QC_WHISPER_THREADS`` (2, at most 3) and ``QC_WHISPER_LANGUAGE`` (en). Nothing
    runs unless ``QC_TRANSCRIBE`` switches the feature on (``enabled``)."""

    binary: str = "whisper-cli"
    models: dict[str, str] = field(default_factory=dict)
    default_model: str = DEFAULT_MODEL
    vad_model: str | None = None
    threads: int = DEFAULT_THREADS
    language: str = "en"
    enabled: bool = False

    @classmethod
    def from_env(cls, env=None) -> "WhisperConfig":
        e = os.environ if env is None else env
        default = (e.get("QC_WHISPER_MODEL") or "").strip()
        models = _models(e.get("QC_WHISPER_MODELS"), default)
        if default and ("/" in default or default.endswith(".bin")):
            default = model_name_from_path(default)
        if not default or default not in models:
            default = DEFAULT_MODEL if DEFAULT_MODEL in models or not models else next(iter(models))
        return cls(
            binary=(e.get("QC_WHISPER_BIN") or "whisper-cli").strip(),
            models=models,
            default_model=default,
            vad_model=(e.get("QC_WHISPER_VAD_MODEL") or "").strip() or None,
            threads=_clamp_threads(e.get("QC_WHISPER_THREADS")),
            language=(e.get("QC_WHISPER_LANGUAGE") or "en").strip() or "en",
            enabled=enabled(e),
        )

    def binary_path(self) -> str | None:
        return shutil.which(self.binary)

    def model_path(self, name: str | None = None) -> str:
        """The model file for ``name`` (None: the default). Raises ValueError for a model
        this server doesn't have."""
        name = name or self.default_model
        path = self.models.get(name)
        if not path:
            have = ", ".join(sorted(self.models)) or "none"
            raise ValueError(f"No whisper model {name!r} here (have: {have}).")
        return path

    @property
    def available(self) -> bool:
        return (self.enabled and bool(self.binary_path())
                and any(Path(p).is_file() for p in self.models.values()))

    def describe(self) -> dict:
        """For /api/health and /api/config: what's installed, never secrets."""
        models = []
        for name, path in sorted(self.models.items()):
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = None
            models.append({"name": name, "size": size, "present": size is not None})
        return {
            "enabled": self.enabled,
            "available": self.available,
            "engine": ENGINE,
            "binary": bool(self.binary_path()),
            "models": models,
            "default_model": self.default_model,
            "threads": self.threads,
            "max_threads": MAX_THREADS,
            "vad": bool(self.vad_model and Path(self.vad_model).is_file()),
            "language": self.language,
        }


# --- ranges ---------------------------------------------------------------------------

Range = tuple[float, float]


def union(ranges: Iterable[Range], join: float = 0.0) -> list[Range]:
    """Merge overlapping (or within ``join`` seconds) ranges; sorted, empty ones dropped."""
    out: list[list[float]] = []
    for s, e in sorted((float(s), float(e)) for s, e in ranges if e > s):
        if out and s <= out[-1][1] + join:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in out]


def subtract(want: Iterable[Range], done: Iterable[Range], min_len: float = 0.5) -> list[Range]:
    """The parts of ``want`` not covered by ``done`` (pieces shorter than ``min_len`` go)."""
    done = union(done)
    out: list[Range] = []
    for s, e in union(want):
        cur = s
        for ds, de in done:
            if de <= cur or ds >= e:
                continue
            if ds > cur:
                out.append((cur, ds))
            cur = max(cur, de)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return [(round(s, 3), round(e, 3)) for s, e in out if e - s >= min_len]


def covered(ranges: Iterable[Range]) -> float:
    return sum(e - s for s, e in union(ranges))


def find_gaps(cues: list[Cue], duration: float | None, min_gap: float = MIN_GAP) -> list[Range]:
    """Every stretch between subtitle lines (and before the first and after the last)
    at least ``min_gap`` seconds long. Machine cues don't count as subtitles."""
    real = union((c.start, c.end) for c in cues if not c.machine)
    end = duration if duration and duration > 0 else (real[-1][1] if real else 0.0)
    gaps: list[Range] = []
    cur = 0.0
    for s, e in real:
        if s - cur >= min_gap:
            gaps.append((cur, s))
        cur = max(cur, e)
    if end - cur >= min_gap:
        gaps.append((cur, end))
    return [(round(s, 3), round(e, 3)) for s, e in gaps]


def chunks(start: float, end: float, size: float | None = None) -> list[Range]:
    """Split a range into pieces of at most ``size`` seconds (the last may be shorter;
    a short tail is folded into the piece before it)."""
    size = size or CHUNK
    out: list[Range] = []
    s = start
    while end - s > size * 1.25:
        out.append((s, s + size))
        s += size
    out.append((s, end))
    return out


# --- whisper output ------------------------------------------------------------------

# Markup whisper copies from the subtitles it learnt from: <i>…</i>, {\an8}.
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
# Parenthesised or bracketed sound descriptions: "(dramatic music)", "[Music]", "[BLANK_AUDIO]".
_SOUND_RE = re.compile(r"[\[(][^\])]{0,60}[\])]")
# A segment with nothing but music notes, punctuation and space left.
_NO_WORDS_RE = re.compile(r"^[\W_♪♫]*$")
# Lines whisper is known to invent over silence and credits.
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "thank you so much for watching",
    "please subscribe", "like and subscribe", "subtitles by", "subtitled by",
    "transcribed by", "captions by", "amara.org", "see you in the next video",
    "translated by",
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


def clean_text(text: str) -> str:
    """Whisper text as a line: sound descriptions dropped, markup and space collapsed."""
    text = _SOUND_RE.sub(" ", _TAG_RE.sub("", text))
    text = " ".join(text.split())
    if text.startswith("- ") and " - " not in text:  # a lone dialogue dash
        text = text[2:]
    return text


# Sounds, not words: a line of nothing else ("♪ Mmm ♪", "Oh, oh") is no use to a search.
_FILLERS = frozenset({"mm", "mmm", "mmmm", "hmm", "hm", "uh", "um", "uhm", "oh", "ah", "ooh",
                      "aah", "huh", "la", "na", "da", "doo", "ba", "whoa", "yeah"})


# The same lines as :func:`_norm` writes them ("amara.org" -> "amara org").
_HALLUCINATION_WORDS = tuple(_norm(h) for h in _HALLUCINATIONS)


def is_hallucination(text: str) -> bool:
    n = _norm(text)
    if not n:
        return True
    if all(w in _FILLERS for w in n.split()):
        return True
    # Only a line that is one, or opens or closes with one: "Thanks for watching!",
    # "Subtitles by the Amara.org community". "It was translated by my mother" is dialogue.
    padded = f" {n} "
    return len(n.split()) <= 12 and any(
        padded.startswith(f" {h} ") or padded.endswith(f" {h} ")
        or (h == "amara org" and f" {h} " in padded)  # a credit wherever it sits
        for h in _HALLUCINATION_WORDS)


def parse_whisper_json(data: dict, *, offset: float = 0.0,
                       keep: Range | None = None, model: str | None = None) -> list[Cue]:
    """Cues from whisper.cpp's ``-oj``/``-ojf`` JSON (``transcription[].offsets`` in ms).

    ``offset`` is added to every time (the decode started there). With ``keep``, only
    segments whose midpoint falls inside it stay: the context decoded around a chunk
    belongs to its neighbour. Sound descriptions, empty and music-only lines, repeats
    of the line before and whisper's stock outro lines are dropped."""
    segs = data.get("transcription") if isinstance(data, dict) else None
    out: list[Cue] = []
    prev = ""
    for seg in segs or []:
        if not isinstance(seg, dict):
            continue
        off = seg.get("offsets") or {}
        try:
            s = float(off.get("from")) / 1000.0 + offset
            e = float(off.get("to")) / 1000.0 + offset
        except (TypeError, ValueError):
            continue
        if e < s:
            e = s
        text = clean_text(str(seg.get("text") or ""))
        if not text or _NO_WORDS_RE.match(text) or is_hallucination(text):
            continue
        n = _norm(text)
        if n == prev:
            continue
        prev = n
        mid = (s + e) / 2
        if keep is not None and not keep[0] <= mid < keep[1]:
            continue
        out.append(Cue(index=len(out), start=round(s, 3), end=round(e, 3), text=text,
                       source=MACHINE))
    return out


def merge_cues(real: list[Cue], machine: list[Cue], *, overlap: float = 0.5,
               dup_ratio: float = 85.0) -> list[Cue]:
    """Subtitle cues with the machine lines that add something, sorted and renumbered.

    A machine line stays only where no subtitle line covers it: less than ``overlap``
    of its length is under subtitle cues, and it isn't a fuzzy duplicate
    (``ratio >= dup_ratio``) of a subtitle line it touches. Subtitle lines always
    win. Nothing stores cue indexes (bookmarks keep times; a search ``index`` is a
    rank), so renumbering is safe."""
    real = [c for c in real if not c.machine]
    spans = union((c.start, c.end) for c in real)
    kept: list[Cue] = []
    for m in machine:
        length = max(m.end - m.start, 0.001)
        under = sum(max(0.0, min(e, m.end) - max(s, m.start)) for s, e in spans)
        if under / length >= overlap:
            continue
        near = [c for c in real if c.start <= m.end + 1.0 and c.end >= m.start - 1.0]
        mn = _norm(m.text)
        if any(fuzz.ratio(mn, _norm(c.text)) >= dup_ratio
               or (len(mn) > 12 and fuzz.partial_ratio(mn, _norm(c.text)) >= 95)
               for c in near):
            continue
        kept.append(m)
    both = sorted([*real, *kept], key=lambda c: (c.start, c.end))
    return [Cue(i, c.start, c.end, c.text, c.speaker, c.source) for i, c in enumerate(both)]


def dedupe_machine(*groups: list[Cue]) -> list[Cue]:
    """Machine cues from several places (the store and a library sidecar copy of it),
    with a line that appears in two kept once."""
    out: list[Cue] = []
    for c in sorted((c for g in groups for c in g), key=lambda c: (c.start, c.end)):
        if out and abs(out[-1].start - c.start) < 1.0 and _norm(out[-1].text) == _norm(c.text):
            continue
        out.append(c)
    return [Cue(i, c.start, c.end, c.text, c.speaker, MACHINE) for i, c in enumerate(out)]


def filter_machine(cues: list[Cue], mode: str) -> list[Cue]:
    """``include`` (all), ``exclude`` (subtitle lines only) or ``only`` (machine only)."""
    if mode == "exclude":
        return [c for c in cues if not c.machine]
    if mode == "only":
        return [c for c in cues if c.machine]
    return cues


# --- running whisper.cpp ---------------------------------------------------------------


# Kept under these names for the transcription code and its tests.
idle_argv = idle.wrap
idle_env = idle.child_env


def extract_argv(source: str | Path, out_wav: str | Path, *, start: float, duration: float,
                 audio: int = 0, src_rate: int | None = None,
                 input_flags: tuple[str, ...] = ()) -> list[str]:
    """ffmpeg arguments that write ``start..start+duration`` of audio stream ``a:audio``
    as a 16 kHz mono WAV, decoded the way the waveform decodes (anchored seek, so the
    first sample is at ``start`` exactly)."""
    # Decode at the stream's own rate and resample to 16 kHz inside the filter chain. An
    # output -ar after the chain's aresample=first_pts=0 starts the audio seconds late
    # (4 s on Darkplace S01E06's AC3, after an anchored -ss): every line came out early.
    cmd = waveform.decode_args(source, audio=audio, rate=None, start=start,
                               duration=duration, input_flags=input_flags, src_rate=src_rate)
    assert cmd[-5:] == ["-f", "s16le", "-acodec", "pcm_s16le", "-"], cmd[-5:]
    af = cmd.index("-af") + 1
    cmd[af] += f",aresample={SAMPLE_RATE}"
    return cmd[:-5] + ["-f", "wav", "-acodec", "pcm_s16le", "-y", str(out_wav)]


def whisper_argv(cfg: WhisperConfig, model_path: str, wav: str | Path, out_base: str | Path,
                 *, prompt: str | None = None, vad: bool = False) -> list[str]:
    """``whisper-cli`` arguments: JSON out, English, capped threads, no console text.

    ``vad`` (Silero, when a VAD model is configured) skips what it hears as non-speech.
    Only whole-file runs use it (:func:`use_vad`): on wizrock it dropped every sung line
    of the Darkplace song, the very lines a range or a gap is transcribed for. A seed
    ``prompt`` of the file's names made small.en write a single "." there, so runs
    don't use one (M21 phase 0)."""
    binary = cfg.binary_path() or cfg.binary
    if os.sep in binary:
        binary = os.path.abspath(binary)
    cmd = [binary, "-m", os.path.abspath(model_path), "-f", os.path.abspath(wav),
           "-t", str(_clamp_threads(cfg.threads)), "-l", cfg.language,
           "-oj", "-of", os.path.abspath(out_base), "-np"]
    if vad and cfg.vad_model:
        cmd += ["--vad", "-vm", os.path.abspath(cfg.vad_model)]
    if prompt:
        cmd += ["--prompt", prompt[:600]]
    return cmd


def use_vad(mode: str) -> bool:
    """Whether a run of ``mode`` uses voice activity detection: a whole file (a podcast's
    silences) yes; a range or the gaps, which are mostly songs and music, no."""
    return mode == "file"


class Cancelled(Exception):
    """The run was cancelled; the chunks finished before that are kept."""


def _run(argv: list[str], *, cancelled: Callable[[], bool] | None, timeout: float,
         cwd: str | Path | None = None) -> str:
    """Run one idle-priority subprocess; kill it if ``cancelled()`` turns true, or if this
    process stops waiting for any other reason (Ctrl-C: the child is in its own session,
    so the terminal's SIGINT never reaches it). ``cwd`` should be a directory of ours:
    ``python -m`` puts the working directory first on the import path."""
    proc = subprocess.Popen(idle_argv(argv), stdout=subprocess.DEVNULL, env=idle_env(),
                            stderr=subprocess.PIPE, start_new_session=True,
                            cwd=cwd or tempfile.gettempdir())
    err: list[bytes] = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancelled is not None and cancelled():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(f"{Path(argv[0]).name} took over {timeout:.0f} s")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
    stderr = b"".join(err).decode(errors="replace")
    if proc.returncode != 0:
        tail = [ln for ln in stderr.strip().splitlines() if ln.strip()][-1:] or ["no output"]
        raise RuntimeError(f"{Path(argv[0]).name} failed ({proc.returncode}): {tail[0]}")
    return stderr


def transcribe_range(source: str | Path, start: float, end: float, *, cfg: WhisperConfig,
                     model: str | None = None, audio: int = 0, src_rate: int | None = None,
                     prompt: str | None = None, vad: bool = False,
                     cancelled: Callable[[], bool] | None = None,
                     media_duration: float | None = None, workdir: str | Path | None = None
                     ) -> list[Cue]:
    """Transcribe one stretch (at most a chunk or so) of ``source``'s audio. Returns its
    machine cues in file time. Decodes ``CONTEXT`` seconds either side and keeps only
    the lines that belong inside ``start..end``."""
    model_path = cfg.model_path(model)
    source = os.path.abspath(source)
    lo = max(0.0, start - CONTEXT)
    hi = end + CONTEXT
    if media_duration:
        hi = min(hi, media_duration)
    with tempfile.TemporaryDirectory(prefix="qc-stt-", dir=workdir) as tmp:
        tmp = os.path.abspath(tmp)
        wav = Path(tmp) / "a.wav"
        out = Path(tmp) / "out"
        # The decode's first sample is at the anchored start, which can differ from lo
        # by under a sample: count from where it really starts.
        t0 = waveform.anchored_start(lo, src_rate) if (lo and src_rate) else lo
        _run(extract_argv(source, wav, start=lo, duration=hi - lo, audio=audio,
                          src_rate=src_rate), cancelled=cancelled, timeout=600, cwd=tmp)
        if not wav.exists() or wav.stat().st_size <= 44:
            return []
        _run(whisper_argv(cfg, model_path, wav, out, prompt=prompt, vad=vad),
             cancelled=cancelled, timeout=max(600.0, (hi - lo) * 20), cwd=tmp)
        js = out.with_suffix(".json")
        try:
            data = json.loads(js.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"whisper-cli wrote no readable transcript: {exc}") from exc
    return parse_whisper_json(data, offset=t0, keep=(start, end),
                              model=model or cfg.default_model)


# --- the transcript store -------------------------------------------------------------


@dataclass
class Transcript:
    """A file's machine transcript: what was transcribed (``ranges``) and what it said."""

    path: str
    media_mtime: float
    media_size: int
    language: str = "en"
    ranges: list[Range] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    updated: float = 0.0

    def to_json(self) -> dict:
        return {
            "version": STORE_VERSION, "engine": ENGINE, "path": self.path,
            "media_mtime": self.media_mtime, "media_size": self.media_size,
            "language": self.language, "models": self.models, "updated": self.updated,
            "ranges": [list(r) for r in self.ranges],
            "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in self.cues],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Transcript":
        cues = [Cue(i, float(c["start"]), float(c["end"]), str(c["text"]), source=MACHINE)
                for i, c in enumerate(d.get("cues") or [])]
        return cls(path=d["path"], media_mtime=float(d["media_mtime"]),
                   media_size=int(d.get("media_size") or 0),
                   language=d.get("language") or "en",
                   ranges=[(float(s), float(e)) for s, e in d.get("ranges") or []],
                   cues=cues, models=list(d.get("models") or []),
                   updated=float(d.get("updated") or 0))

    def summary(self) -> dict:
        return {"ranges": [list(r) for r in self.ranges], "covered": round(covered(self.ranges), 1),
                "lines": len(self.cues), "models": self.models, "language": self.language,
                "updated": self.updated, "engine": ENGINE}


def _stat(path: Path) -> tuple[float, int]:
    st = path.stat()
    return st.st_mtime, st.st_size


def _current(media: str | Path, mtime: float, size: int) -> bool:
    """Whether ``media`` is still the file a transcript of ``(mtime, size)`` was made of.
    A file that's gone or unreadable counts as current: still show what we have."""
    try:
        now_mtime, now_size = _stat(Path(media))
    except OSError:
        return True
    return abs(now_mtime - mtime) <= 1e-3 and not (size and now_size != size)


# A store file's media stamp: the (mtime, size) of the file it transcribes, or None when it
# isn't a transcript this version reads.
Stamp = tuple[float, int] | None
# Why :meth:`TranscriptStore.orphans` lists a store file.
GONE, STALE, UNREADABLE, PARTIAL = "gone", "stale", "unreadable", "partial"
# A temp file a save left behind is a leftover only once it's this old (seconds).
PARTIAL_AGE = 3600.0


@dataclass(frozen=True)
class Orphan:
    """A store file no current transcript needs (:meth:`TranscriptStore.orphans`)."""

    file: Path
    path: str | None  # the media file it transcribed, when it can be read
    reason: str  # GONE, STALE (the media changed since), UNREADABLE or PARTIAL

    def to_json(self) -> dict:
        return {"file": self.file.name, "path": self.path, "reason": self.reason}


def _read_header(f: Path) -> dict | None:
    """A store file's JSON when it is a transcript this version reads (its name is its
    path's hash), else None."""
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or d.get("version") != STORE_VERSION:
        return None
    path = d.get("path")
    if not isinstance(path, str) or hashlib.sha256(path.encode()).hexdigest() + ".json" != f.name:
        return None
    return d


def _read_stamp(f: Path) -> Stamp:
    d = _read_header(f)
    try:
        return (float(d["media_mtime"]), int(d.get("media_size") or 0)) if d else None
    except (KeyError, TypeError, ValueError):
        return None


def _root_present(root: Path, mounted: dict[Path, bool]) -> bool:
    """``root`` is there and has something in it (an unmounted disk's mount point is an
    empty folder)."""
    if root not in mounted:
        try:
            with os.scandir(root) as it:
                mounted[root] = next(it, None) is not None
        except OSError:
            mounted[root] = False
    return mounted[root]


def _orphan_reason(f: Path, roots: list[Path] | None, mounted: dict[Path, bool],
                   stale: bool) -> tuple[str | None, str | None]:
    """Why store file ``f`` is an orphan (GONE, STALE, UNREADABLE), or None; and the
    media path it names."""
    d = _read_header(f)
    if d is None:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, json.JSONDecodeError):
            return UNREADABLE, None
        if isinstance(raw, dict) and isinstance(raw.get("version"), int) \
                and raw["version"] > STORE_VERSION:
            return None, raw.get("path")  # a newer quipclipper's: not ours to judge
        return UNREADABLE, raw.get("path") if isinstance(raw, dict) else None
    media = Path(d["path"])
    if media.exists():
        if stale:
            stamp = _read_stamp(f)
            if stamp is None or not _current(media, *stamp):
                return STALE, str(media)
        return None, str(media)
    if roots is None:
        known = media.parent.is_dir()
    else:
        known = any(media.is_relative_to(r) and _root_present(r, mounted) for r in roots)
    return (GONE if known else None), str(media)


class TranscriptStore:
    """Machine transcripts in ``<dir>/<sha256(path)>.json``, one per media file.

    Keyed on the media file and checked against its mtime and size, not the subtitle
    source's, so changing a file's subtitles keeps its transcript; replacing the media
    retires it (nothing reads it any more, and :meth:`clean` can delete it on request).
    Writes are atomic (temp file + rename)."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self._lock = threading.Lock()
        # file name -> ((its mtime_ns, size), its media stamp): each store file is read
        # once per change, so a listing can tell a stale transcript without reading it.
        self._stamps: dict[str, tuple[tuple[int, int], Stamp]] = {}
        self._stamps_lock = threading.Lock()

    def _file(self, media: str | Path) -> Path:
        return self.dir / (hashlib.sha256(str(media).encode()).hexdigest() + ".json")

    def load(self, media: str | Path) -> Transcript | None:
        """The current transcript of ``media``, or None (none, unreadable, or stale)."""
        f = self._file(media)
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("version") != STORE_VERSION or d.get("path") != str(media):
                return None
            t = Transcript.from_json(d)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        return t if _current(media, t.media_mtime, t.media_size) else None

    def has(self, media: str | Path) -> bool:
        return self.load(media) is not None

    def exists(self, media: str | Path, names: dict[str, Stamp] | None = None) -> bool:
        """Cheap: ``media`` has a current transcript (one stat of the media, the store
        file read only when it changed). For listings that check many files; ``names``
        (from :meth:`names`) spares a stat of the store file each."""
        name = self._file(media).name
        if names is None:
            names = self._stamp_of(name)
        if names.get(name) is None:
            return False
        mtime, size = names[name]
        return _current(media, mtime, size)

    def names(self) -> dict[str, Stamp]:
        """The store's transcripts: file name -> the media stamp it was made of, for
        :meth:`exists` over many files."""
        out: dict[str, Stamp] = {}
        try:
            entries = [e for e in os.scandir(self.dir) if e.name.endswith(".json")]
        except OSError:
            return out
        with self._stamps_lock:
            for e in entries:
                with contextlib.suppress(OSError):
                    out[e.name] = self._stamp(Path(e.path), e.stat())
            for gone in set(self._stamps) - set(out):
                del self._stamps[gone]
        return out

    def _stamp_of(self, name: str) -> dict[str, Stamp]:
        f = self.dir / name
        try:
            st = f.stat()
        except OSError:
            return {}
        with self._stamps_lock:
            return {name: self._stamp(f, st)}

    def _stamp(self, f: Path, st: os.stat_result) -> Stamp:
        """``f``'s media stamp, read again only when ``f`` changed. Hold _stamps_lock."""
        key = (st.st_mtime_ns, st.st_size)
        hit = self._stamps.get(f.name)
        if hit is None or hit[0] != key:
            hit = (key, _read_stamp(f))
            self._stamps[f.name] = hit
        return hit[1]

    def orphans(self, roots: Iterable[str | Path] | None = None, *,
                stale: bool = False) -> list[Orphan]:
        """Store files no current transcript needs: transcripts of media files that are
        gone, unreadable ones (an older store format, a broken file), a save's leftover
        temp files, and with ``stale`` those of media files replaced since.

        A media file counts as gone only where that can be told: with ``roots`` (the
        library), it is under a root that is there and not empty (a disk that isn't
        mounted doesn't delete its transcripts) and the file isn't; a transcript of a
        file under no root is left alone. Without ``roots``, its folder is still there
        and the file isn't. Nothing is deleted here (:meth:`clean` does that)."""
        live = None if roots is None else [Path(r) for r in roots]
        mounted: dict[Path, bool] = {}
        out: list[Orphan] = []
        try:
            entries = sorted(os.scandir(self.dir), key=lambda e: e.name)
        except OSError:
            return out
        now = time.time()
        for e in entries:
            f = Path(e.path)
            if e.name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    if now - e.stat().st_mtime > PARTIAL_AGE:
                        out.append(Orphan(f, None, PARTIAL))
                continue
            if not e.name.endswith(".json"):
                continue
            reason, path = _orphan_reason(f, live, mounted, stale)
            if reason:
                out.append(Orphan(f, path, reason))
        return out

    def clean(self, roots: Iterable[str | Path] | None = None, *,
              stale: bool = False) -> list[Orphan]:
        """Delete what :meth:`orphans` lists (each checked again just before it goes, so
        a transcript saved meanwhile stays). Returns what was deleted. Only ever on a
        user's request (D-037): nothing calls this on a timer or a scan."""
        roots = None if roots is None else [Path(r) for r in roots]
        removed: list[Orphan] = []
        for o in self.orphans(roots, stale=stale):
            with self._lock:
                if o.reason != PARTIAL:
                    again, _ = _orphan_reason(o.file, roots, {}, stale)
                    if again != o.reason:
                        continue
                try:
                    o.file.unlink()
                except FileNotFoundError:
                    continue
            removed.append(o)
        return removed

    def save(self, t: Transcript) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(t.to_json(), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self._file(t.path))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def add(self, media: str | Path, done: Range, cues: list[Cue], *, model: str,
            language: str = "en") -> Transcript:
        """Record that ``done`` was transcribed with ``model``, saying ``cues``. Lines the
        store held inside ``done`` are replaced (a re-run with a better model wins)."""
        media = Path(media)
        with self._lock:
            t = self.load(media)
            if t is None:
                mtime, size = _stat(media)
                t = Transcript(path=str(media), media_mtime=mtime, media_size=size,
                               language=language)
            s, e = done
            keep = [c for c in t.cues if not (s <= (c.start + c.end) / 2 < e)]
            merged = sorted([*keep, *cues], key=lambda c: (c.start, c.end))
            t.cues = [Cue(i, c.start, c.end, c.text, source=MACHINE) for i, c in enumerate(merged)]
            t.ranges = union([*t.ranges, done], join=0.05)
            if model not in t.models:
                t.models.append(model)
            t.updated = time.time()
            self.save(t)
            return t

    def clear(self, media: str | Path) -> bool:
        try:
            self._file(media).unlink()
            return True
        except FileNotFoundError:
            return False

    def all(self) -> Iterator[Transcript]:
        for f in sorted(self.dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("version") == STORE_VERSION:
                    yield Transcript.from_json(d)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

    @contextlib.contextmanager
    def exclusive(self, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        """One transcription at a time per store, across processes (a CLI run and the
        server sharing a state dir wait for each other). Raises :class:`Cancelled` if
        ``cancelled()`` turns true while waiting."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as fh:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows: in-process only
                with self._lock:
                    yield
                return
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if cancelled is not None and cancelled():
                        raise Cancelled() from None
                    time.sleep(0.5)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def default_store_dir() -> Path:
    """Where the CLI keeps transcripts when it runs whisper itself."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "quipclipper" / "transcripts"


# --- running a request ------------------------------------------------------------------


def plan_ranges(mode: str, *, real: list[Cue], duration: float | None, done: list[Range],
                start: float | None = None, end: float | None = None,
                redo: bool = False) -> list[Range]:
    """What a request still has to transcribe.

    ``range``: ``start..end``; ``gaps``: every gap between subtitle lines; ``file``:
    the whole file. Unless ``redo``, parts transcribed before are skipped."""
    if mode == "range":
        if start is None or end is None or end <= start:
            raise ValueError("A range needs a start before its end.")
        if end > MAX_SECONDS:
            raise ValueError("That range ends past any media file's length.")
        hi = min(end, duration) if duration else end
        want = [(max(0.0, start), hi)]
    elif mode == "gaps":
        want = find_gaps(real, duration)
    elif mode == "file":
        if not duration:
            raise ValueError("Can't tell how long this file is.")
        want = [(0.0, duration)]
    else:
        raise ValueError(f"Unknown mode {mode!r}: range, gaps or file.")
    return union(want) if redo else subtract(want, done)


def run(store: TranscriptStore, media: str | Path, ranges: list[Range], *, cfg: WhisperConfig,
        model: str | None = None, audio: int = 0, real: list[Cue] | None = None,
        duration: float | None = None, vad: bool = False,
        progress: Callable[[float, float], None] | None = None,
        cancelled: Callable[[], bool] | None = None) -> Transcript | None:
    """Transcribe ``ranges`` of ``media`` chunk by chunk, saving each chunk as it finishes.

    ``progress(done_seconds, total_seconds)`` is called after each chunk. Raises
    :class:`Cancelled` when ``cancelled()`` turns true (finished chunks are kept)."""
    model = model or cfg.default_model
    cfg.model_path(model)  # fail before decoding anything
    media = Path(media)
    pieces = [c for s, e in ranges for c in chunks(s, e)]
    total = sum(e - s for s, e in pieces)
    src_rate = waveform.native_rate(media, audio)
    done = 0.0
    t = None
    if progress:
        progress(0.0, total)
    with store.exclusive(cancelled):
        for s, e in pieces:
            if cancelled is not None and cancelled():
                raise Cancelled()
            cues = transcribe_range(media, s, e, cfg=cfg, model=model, audio=audio,
                                    src_rate=src_rate, vad=vad, cancelled=cancelled,
                                    media_duration=duration)
            t = store.add(media, (s, e), cues, model=model, language=cfg.language)
            done += e - s
            if progress:
                progress(done, total)
    return t if t is not None else store.load(media)


# --- the library sidecar ----------------------------------------------------------------


def machine_sidecar_path(media: str | Path, language: str = "en") -> Path:
    """Where a transcript copied to the library goes: ``<stem>.<lang>.whisper.srt`` next
    to the media. The ``whisper`` token marks it machine-made: Jellyfin shows it as its
    own subtitle track, and quipclipper never picks it as the file's subtitles."""
    media = Path(media)
    lang = re.sub(r"[^a-z]", "", (language or "en").lower())[:3] or "en"
    return media.with_name(f"{media.stem}.{lang}.{SIDECAR_TOKEN}.srt")


def find_machine_sidecars(media: str | Path, siblings: list[Path] | None = None) -> list[Path]:
    """The machine sidecars of ``media``: ``<stem>.<lang>.whisper.srt``/``.vtt`` (or
    ``<stem>.whisper.srt``) next to it, nothing else. ``Show.Part2.en.whisper.srt`` is
    Show.Part2.mkv's, not Show.mkv's: reading or removing it as Show's would be wrong.
    ``siblings`` is the folder's listing (its subtitle files are enough) when the caller
    already has it."""
    media = Path(media)
    if siblings is None:
        try:
            siblings = list(media.parent.iterdir())
        except OSError:
            return []
    token = f".{SIDECAR_TOKEN}."
    siblings = [p for p in siblings if token in p.name.lower()]
    if not siblings:
        return []  # the common case, without compiling a pattern per file
    own = re.compile(re.escape(media.stem.lower()) + r"(\.[a-z]{1,3})?\."
                     + SIDECAR_TOKEN + r"\.(srt|vtt)")
    return sorted(p for p in siblings if own.fullmatch(p.name.lower()) and p.is_file())


def cues_to_srt(cues: list[Cue]) -> str:
    """SubRip text for ``cues`` (renumbered from 1)."""
    def ts(t: float) -> str:
        return format_timestamp(t).replace(".", ",")
    blocks = [f"{i}\n{ts(c.start)} --> {ts(max(c.end, c.start + 0.2))}\n{c.text}\n"
              for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def cues_to_vtt(cues: list[Cue]) -> str:
    """WebVTT text for ``cues`` (machine lines exported as plain captions)."""
    blocks = [f"{format_timestamp(c.start)} --> {format_timestamp(max(c.end, c.start + 0.2))}\n"
              f"{c.text}\n" for c in cues]
    return "WEBVTT\n\n" + "\n".join(blocks)


def library_lines(real: list[Cue], machine: list[Cue]) -> list[Cue]:
    """What copying a transcript to the library writes: the machine lines the script
    shows as machine lines, i.e. none a subtitle line already has."""
    return [c for c in merge_cues(real, machine) if c.machine]


def write_sidecar(media: str | Path, cues: list[Cue], language: str = "en") -> Path:
    """Write ``cues`` as the machine sidecar next to ``media`` (atomic; replaces an
    older machine sidecar of the same name, never any other file)."""
    target = machine_sidecar_path(media, language)
    if not cues:
        raise ValueError("The transcript has no lines to copy.")
    # Mode 0o666 at creation, so the umask decides as for any new file here (os.umask()
    # to read it would change it for every thread of the server meanwhile).
    tmp = target.parent / f".qc-{secrets.token_hex(6)}.srt.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cues_to_srt(cues))
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def machine_cues(media: str | Path, store: TranscriptStore | None, *,
                 library: bool = True, stored: bool = True,
                 siblings: list[Path] | None = None) -> list[Cue]:
    """A file's machine lines: its stored transcript and/or the machine sidecars copied
    into the library next to it (read as machine lines, deduplicated). ``siblings``
    spares listing the folder (see :func:`find_machine_sidecars`)."""
    groups: list[list[Cue]] = []
    if stored and store is not None:
        t = store.load(media)
        if t is not None:
            groups.append(t.cues)
    if library:
        for p in find_machine_sidecars(media, siblings):
            with contextlib.suppress(OSError, ValueError, FileNotFoundError):
                groups.append([Cue(c.index, c.start, c.end, c.text, c.speaker, MACHINE)
                               for c in load_subtitles(p)])
    return dedupe_machine(*groups) if groups else []
