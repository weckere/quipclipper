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
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from quipclipper import waveform
from quipclipper.models import Cue, format_timestamp
from quipclipper.subtitles import SIDECAR_TOKEN, is_machine_sidecar, load_subtitles

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
# Store format version (bump on an incompatible change; older files are then ignored).
STORE_VERSION = 1



# --- configuration ------------------------------------------------------------------


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
    ``QC_WHISPER_THREADS`` (2, at most 3) and ``QC_WHISPER_LANGUAGE`` (en)."""

    binary: str = "whisper-cli"
    models: dict[str, str] = field(default_factory=dict)
    default_model: str = DEFAULT_MODEL
    vad_model: str | None = None
    threads: int = DEFAULT_THREADS
    language: str = "en"

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
        return bool(self.binary_path()) and any(Path(p).is_file() for p in self.models.values())

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


def is_hallucination(text: str) -> bool:
    n = _norm(text)
    if not n:
        return True
    if all(w in _FILLERS for w in n.split()):
        return True
    return any(h in n for h in _HALLUCINATIONS) and len(n.split()) <= 12


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


def idle_argv(argv: list[str]) -> list[str]:
    """``argv`` run through :mod:`quipclipper.idle` (idle CPU and I/O priority)."""
    return [sys.executable, "-m", "quipclipper.idle", *argv]


def idle_env() -> dict[str, str]:
    """The environment for :func:`idle_argv`: this process's import path as PYTHONPATH.
    A packaged app (Nix's buildPythonApplication) sets its path up inside its own entry
    script, so the bare interpreter it runs on wouldn't find ``quipclipper.idle``."""
    paths = [p for p in sys.path if p and os.path.isdir(p)]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


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
    cmd = [cfg.binary_path() or cfg.binary, "-m", str(model_path), "-f", str(wav),
           "-t", str(_clamp_threads(cfg.threads)), "-l", cfg.language,
           "-oj", "-of", str(out_base), "-np"]
    if vad and cfg.vad_model:
        cmd += ["--vad", "-vm", cfg.vad_model]
    if prompt:
        cmd += ["--prompt", prompt[:600]]
    return cmd


def use_vad(mode: str) -> bool:
    """Whether a run of ``mode`` uses voice activity detection: a whole file (a podcast's
    silences) yes; a range or the gaps, which are mostly songs and music, no."""
    return mode == "file"


class Cancelled(Exception):
    """The run was cancelled; the chunks finished before that are kept."""


def _run(argv: list[str], *, cancelled: Callable[[], bool] | None, timeout: float) -> str:
    """Run one idle-priority subprocess; kill it if ``cancelled()`` turns true."""
    proc = subprocess.Popen(idle_argv(argv), stdout=subprocess.DEVNULL, env=idle_env(),
                            stderr=subprocess.PIPE, start_new_session=True)
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
    lo = max(0.0, start - CONTEXT)
    hi = end + CONTEXT
    if media_duration:
        hi = min(hi, media_duration)
    with tempfile.TemporaryDirectory(prefix="qc-stt-", dir=workdir) as tmp:
        wav = Path(tmp) / "a.wav"
        out = Path(tmp) / "out"
        # The decode's first sample is at the anchored start, which can differ from lo
        # by under a sample: count from where it really starts.
        t0 = waveform.anchored_start(lo, src_rate) if (lo and src_rate) else lo
        _run(extract_argv(source, wav, start=lo, duration=hi - lo, audio=audio,
                          src_rate=src_rate), cancelled=cancelled, timeout=600)
        if not wav.exists() or wav.stat().st_size <= 44:
            return []
        _run(whisper_argv(cfg, model_path, wav, out, prompt=prompt, vad=vad),
             cancelled=cancelled, timeout=max(600.0, (hi - lo) * 20))
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


class TranscriptStore:
    """Machine transcripts in ``<dir>/<sha256(path)>.json``, one per media file.

    Keyed on the media file and checked against its mtime and size, not the subtitle
    source's, so changing a file's subtitles keeps its transcript; replacing the media
    retires it. Writes are atomic (temp file + rename)."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self._lock = threading.Lock()

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
        try:
            mtime, size = _stat(Path(media))
        except OSError:
            return t  # the media is gone or unreadable: still show what we have
        if abs(mtime - t.media_mtime) > 1e-3 or (t.media_size and size != t.media_size):
            return None
        return t

    def has(self, media: str | Path) -> bool:
        return self.load(media) is not None

    def exists(self, media: str | Path) -> bool:
        """Cheap: a transcript file for ``media`` exists (not read, not checked for
        staleness). For listings that check many files."""
        return self._file(media).exists()

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
    def exclusive(self) -> Iterator[None]:
        """One transcription at a time per store, across processes (a CLI run and the
        server sharing a state dir wait for each other)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as fh:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows: in-process only
                with self._lock:
                    yield
                return
            fcntl.flock(fh, fcntl.LOCK_EX)
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
    with store.exclusive():
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


def find_machine_sidecars(media: str | Path) -> list[Path]:
    """The machine sidecars next to ``media`` (same stem, ``….whisper.srt``/``.vtt``)."""
    media = Path(media)
    try:
        siblings = list(media.parent.iterdir())
    except OSError:
        return []
    return sorted(p for p in siblings
                  if p.suffix.lower() in (".srt", ".vtt") and is_machine_sidecar(p, media.stem))


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


def write_sidecar(media: str | Path, cues: list[Cue], language: str = "en") -> Path:
    """Write ``cues`` as the machine sidecar next to ``media`` (atomic; replaces an
    older machine sidecar of the same name, never any other file)."""
    target = machine_sidecar_path(media, language)
    if not cues:
        raise ValueError("The transcript has no lines to copy.")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".qc-", suffix=".srt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cues_to_srt(cues))
        os.chmod(tmp, 0o666 & ~_umask())  # as any new file here: the umask decides
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def machine_cues(media: str | Path, store: TranscriptStore | None, *,
                 library: bool = True, stored: bool = True) -> list[Cue]:
    """A file's machine lines: its stored transcript and/or the machine sidecars copied
    into the library next to it (read as machine lines, deduplicated)."""
    groups: list[list[Cue]] = []
    if stored and store is not None:
        t = store.load(media)
        if t is not None:
            groups.append(t.cues)
    if library:
        for p in find_machine_sidecars(media):
            with contextlib.suppress(OSError, ValueError, FileNotFoundError):
                groups.append([Cue(c.index, c.start, c.end, c.text, c.speaker, MACHINE)
                               for c in load_subtitles(p)])
    return dedupe_machine(*groups) if groups else []
