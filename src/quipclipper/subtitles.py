"""Load subtitle cues from files or embedded video tracks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pysubs2
from pysubs2.exceptions import Pysubs2Error

from collections import Counter

from quipclipper.models import Cue, format_timestamp

# Subtitle/transcript extensions we look for next to a media file, in priority
# order. ``.json`` covers Podcast Namespace / Whisper-style transcripts (common
# for podcasts and audiobooks) — see ``_load_json_cues``; it sits last so a real
# subtitle format wins when both are present.
SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub", ".json")
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts")
# Audio-only containers (podcasts, audiobooks). Clippable when a sidecar
# transcript sits next to them — there's nothing to extract embedded subs from.
AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".aac", ".flac", ".opus", ".ogg", ".oga",
              ".wav", ".aiff", ".aif", ".wma")

# Strip SubRip/ASS inline markup like <i>, {\an8}, etc.
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _clean(text: str) -> str:
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace("\n", " ")
    text = _TAG_RE.sub("", text)
    return " ".join(text.split())


# --- speaker attribution -----------------------------------------------------
# WebVTT voice tag: <v Chris> / <v.loud Chris Fisher>. pysubs2 strips these when
# it loads a VTT, so we recover the speaker from the raw file.
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]+?)\s*>", re.IGNORECASE)
_CUE_TIMING_RE = re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->")
# A leading "Name:" / "First Last:" / "NARRATOR:" speaker prefix (1–3 words).
_SPEAKER_PREFIX_RE = re.compile(r"^([A-Za-z][\w.'\-]*(?: [A-Za-z][\w.'\-]*){0,2}):\s+(?=\S)")


def _norm_ts(s: str) -> str:
    """Normalize a cue timestamp string to the canonical HH:MM:SS.mmm key."""
    sec = _coerce_seconds(s)
    return format_timestamp(sec) if sec is not None else s


def _vtt_voice_speakers(path: Path) -> dict[str, str]:
    """Map each VTT cue's start time (HH:MM:SS.mmm) to its ``<v Name>`` speaker.

    pysubs2 discards voice tags, so we scan the raw file: the first text line
    after a ``-->`` timing line carries the voice tag, if any."""
    speakers: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return speakers
    start: str | None = None
    for line in text.splitlines():
        m = _CUE_TIMING_RE.match(line)
        if m:
            start = _norm_ts(m.group(1))
        elif start is not None and line.strip():
            vm = _VOICE_RE.match(line.strip())
            if vm:
                speakers[start] = vm.group(1).strip()
            start = None  # only the first text line of a cue carries the tag
    return speakers


def _apply_speaker_prefixes(cues: list[Cue]) -> list[Cue]:
    """Move a recurring ``Name:`` prefix off cue text into ``Cue.speaker``.

    Only names that recur (≥2 cues) or are ALL-CAPS are trusted, so a one-off
    sentence like "Wait: I can explain." in ordinary dialogue isn't mistaken for
    a speaker label. Cues that already have a speaker are left untouched."""
    counts: Counter[str] = Counter()
    parsed: list[tuple[Cue, str | None, str]] = []
    for c in cues:
        name, rest = None, c.text
        if not c.speaker:
            m = _SPEAKER_PREFIX_RE.match(c.text)
            if m:
                name = m.group(1).strip()
                rest = c.text[m.end():].strip()
                counts[name] += 1
        parsed.append((c, name, rest))
    trusted = {n for n, k in counts.items() if k >= 2 or n.isupper()}
    out: list[Cue] = []
    for c, name, rest in parsed:
        if name and rest and name in trusted:
            out.append(Cue(c.index, c.start, c.end, rest, speaker=name))
        else:
            out.append(c)
    return out


def _coerce_seconds(v) -> float | None:
    """A time value as seconds: a number (already seconds) or an
    ``HH:MM:SS(.mmm)`` / ``MM:SS`` / ``SS`` string. None if unparseable."""
    if isinstance(v, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.strip().replace(",", ".")
        if not v:
            return None
        try:
            sec = 0.0
            for part in v.split(":"):
                sec = sec * 60 + float(part)
            return sec
        except ValueError:
            return None
    return None


def _first_seconds(seg: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if seg.get(k) is not None:
            s = _coerce_seconds(seg[k])
            if s is not None:
                return s
    return None


def _json_segments(data):
    """Find the segment list in a parsed JSON transcript. Accepts a bare list, or
    an object keyed by ``segments`` (Podcast Namespace / Whisper), ``transcription``
    (whisper.cpp), ``results``, or ``cues``."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("segments", "transcription", "results", "cues"):
            seg = data.get(key)
            if isinstance(seg, list):
                return seg
    return None


def _load_json_cues(path: Path) -> list[Cue]:
    """Parse a JSON transcript into Cue objects.

    Tolerant of the common podcast/transcription shapes:
    - **Podcast Namespace** (``application/json``): ``segments[].startTime/endTime``
      (seconds) + ``body``.
    - **Whisper**: ``segments[].start/end`` (seconds) + ``text``.
    - **whisper.cpp**: ``transcription[].offsets.from/to`` (milliseconds) + ``text``.
    Times may also be ``HH:MM:SS.mmm`` strings. Raises ValueError (the type callers
    already handle) when the file isn't a recognizable transcript."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"Could not parse JSON transcript {path.name}: {exc}") from exc
    segments = _json_segments(data)
    if segments is None:
        raise ValueError(
            f"Unrecognized JSON transcript {path.name}: expected a list of segments, "
            f"or an object with a 'segments' array (Podcast Namespace / Whisper style)."
        )
    cues: list[Cue] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = _clean(str(seg.get("body") or seg.get("text") or seg.get("transcript") or ""))
        if not text:
            continue
        start = _first_seconds(seg, ("startTime", "start", "begin"))
        end = _first_seconds(seg, ("endTime", "end", "stop"))
        # whisper.cpp puts millisecond offsets under "offsets": {"from", "to"}.
        off = seg.get("offsets")
        if start is None and isinstance(off, dict):
            fr, to = off.get("from"), off.get("to")
            if isinstance(fr, (int, float)) and not isinstance(fr, bool):
                start = float(fr) / 1000.0
            if isinstance(to, (int, float)) and not isinstance(to, bool):
                end = float(to) / 1000.0
        if start is None:
            continue
        if end is None or end < start:
            end = start
        spk = seg.get("speaker")
        spk = str(spk).strip() if spk not in (None, "") else None
        cues.append(Cue(index=len(cues), start=start, end=end, text=text, speaker=spk))
    if not cues:
        raise ValueError(f"No usable cues in JSON transcript {path.name}.")
    # Pick up a "Name:" prefix when there's no explicit speaker field.
    cues = _apply_speaker_prefixes(cues)
    cues.sort(key=lambda c: c.start)
    return [Cue(i, c.start, c.end, c.text, c.speaker) for i, c in enumerate(cues)]


def load_subtitles(path: str | Path) -> list[Cue]:
    """Parse a subtitle/transcript file into a list of Cue objects (text cleaned
    of markup). ``.json`` is parsed as a transcript (see ``_load_json_cues``);
    everything else goes through pysubs2 (srt/vtt/ass/ssa/sub)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Subtitle file not found: {path}")
    if path.suffix.lower() == ".json":
        return _load_json_cues(path)
    try:
        subs = pysubs2.load(str(path))
    except (Pysubs2Error, UnicodeDecodeError) as exc:
        # An empty or non-text subtitle (e.g. an image/PGS track ffmpeg wrote out
        # as an empty SRT, or a malformed/garbage file) can't be format-detected
        # or decoded. Normalize to ValueError — the single exception type callers
        # already handle (folder search skips the file; /api/search → 409) — so
        # one unparseable file doesn't surface as a 500.
        raise ValueError(f"Could not parse subtitle file {path.name}: {exc}") from exc
    # WebVTT voice tags (<v Name>) are stripped by pysubs2 — recover them from
    # the raw file, keyed by cue start time.
    voice = _vtt_voice_speakers(path) if path.suffix.lower() == ".vtt" else {}
    cues: list[Cue] = []
    for i, line in enumerate(subs):
        text = _clean(line.text)
        if not text:
            continue
        start = line.start / 1000.0  # pysubs2 stores milliseconds
        cues.append(
            Cue(
                index=i,
                start=start,
                end=line.end / 1000.0,
                text=text,
                speaker=voice.get(format_timestamp(start)),
            )
        )
    cues = _apply_speaker_prefixes(cues)
    cues.sort(key=lambda c: c.start)
    # Re-number after sorting so indexes are stable, contiguous positions.
    return [Cue(i, c.start, c.end, c.text, c.speaker) for i, c in enumerate(cues)]


@dataclass(frozen=True)
class SubtitleTrack:
    """A subtitle stream embedded in a video container."""

    index: int  # subtitle-relative index (s:N), matching `quipclipper tracks`
    codec: str
    language: str | None
    title: str | None
    forced: bool = False
    hearing_impaired: bool = False

    def label(self) -> str:
        parts = [f"s:{self.index}", self.codec]
        if self.language:
            parts.append(self.language)
        if self.title:
            parts.append(f"'{self.title}'")
        return " ".join(parts)


def list_embedded_tracks(video_path: str | Path) -> list[SubtitleTrack]:
    """Use ffprobe to enumerate subtitle streams in a video container."""
    video_path = Path(video_path)
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found on PATH. Install ffmpeg to read embedded subtitles.")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title"
        ":stream_disposition=forced,hearing_impaired",
        "-of", "json", str(video_path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(
            f"ffprobe could not read {video_path}:\n{out.stderr.strip()}"
        )
    streams = json.loads(out.stdout or "{}").get("streams", [])
    tracks: list[SubtitleTrack] = []
    for rel, s in enumerate(streams):  # rel = subtitle-relative index (s:N)
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        tracks.append(
            SubtitleTrack(
                index=rel,
                codec=s.get("codec_name", "?"),
                language=tags.get("language"),
                title=tags.get("title"),
                forced=bool(disp.get("forced")),
                hearing_impaired=bool(disp.get("hearing_impaired")),
            )
        )
    return tracks


@dataclass(frozen=True)
class StreamInfo:
    """A media stream in a container, with its type-relative (a:N/s:N/v:N) index."""

    kind: str  # "video" | "audio" | "subtitle"
    type_index: int
    codec: str
    language: str | None
    title: str | None
    channels: int | None
    channel_layout: str | None
    forced: bool = False
    hearing_impaired: bool = False
    attached_pic: bool = False  # a still cover-art image (mp3/m4a), not real video

    @property
    def selector(self) -> str:
        return {"video": "v", "audio": "a", "subtitle": "s"}.get(self.kind, "?") + f":{self.type_index}"

    def label(self) -> str:
        parts = [self.selector, self.codec]
        if self.channel_layout:
            parts.append(self.channel_layout)
        elif self.channels:
            parts.append(f"{self.channels}ch")
        if self.language and self.language not in ("und", ""):
            parts.append(self.language)
        if self.title:
            parts.append(f"'{self.title}'")
        return "  ".join(parts)


def list_streams(video_path: str | Path) -> list[StreamInfo]:
    """List all video/audio/subtitle streams with their per-type index."""
    video_path = Path(video_path)
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found on PATH. Install ffmpeg to inspect streams.")
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,channels,channel_layout"
        ":stream_tags=language,title"
        ":stream_disposition=forced,hearing_impaired,attached_pic",
        "-of", "json", str(video_path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(
            f"ffprobe could not read {video_path}:\n{out.stderr.strip()}"
        )
    streams = json.loads(out.stdout or "{}").get("streams", [])
    counts: dict[str, int] = {}
    infos: list[StreamInfo] = []
    for s in streams:
        kind = s.get("codec_type", "")
        if kind not in ("video", "audio", "subtitle"):
            continue
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        idx = counts.get(kind, 0)
        counts[kind] = idx + 1
        infos.append(
            StreamInfo(
                kind=kind,
                type_index=idx,
                codec=s.get("codec_name", "?"),
                language=tags.get("language"),
                title=tags.get("title"),
                channels=s.get("channels"),
                channel_layout=s.get("channel_layout"),
                forced=bool(disp.get("forced")),
                hearing_impaired=bool(disp.get("hearing_impaired")),
                attached_pic=bool(disp.get("attached_pic")),
            )
        )
    return infos


def extract_embedded(video_path: str | Path, stream_index: int) -> list[Cue]:
    """Extract one embedded subtitle stream to a temp .srt and parse it."""
    video_path = Path(video_path)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to extract embedded subtitles.")
    with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video_path),
            "-map", f"0:s:{stream_index}",  # subtitle-relative index (s:N)
            "-f", "srt", str(tmp_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Could not extract subtitle track s:{stream_index} as text — image "
                f"subtitles (e.g. PGS) aren't supported; supply an .srt with --subs."
            )
        return load_subtitles(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def find_sidecar(video_path: str | Path) -> Path | None:
    """Find a subtitle file sitting next to the video (same stem)."""
    video_path = Path(video_path)
    for ext in SUBTITLE_EXTS:
        candidate = video_path.with_suffix(ext)
        if candidate.exists():
            return candidate
    # Also accept stem-prefixed names like "movie.en.srt". Skip yt-dlp's
    # "*.info.json" metadata, which shares the stem but isn't a transcript.
    matches = sorted(
        p
        for p in video_path.parent.glob(f"{video_path.stem}*")
        if p.suffix.lower() in SUBTITLE_EXTS and not p.name.lower().endswith(".info.json")
    )
    return matches[0] if matches else None


_SDH_RE = re.compile(r"sdh|hearing|impaired|\bcc\b", re.IGNORECASE)
_FORCED_RE = re.compile(r"forced", re.IGNORECASE)
# Commentary subtitles transcribe a commentary track, not the film's dialogue, so
# they must never be the default (e.g. Apollo 13 ships a "Commentary" eng SRT
# alongside the main eng SRT).
_COMMENTARY_RE = re.compile(r"comment", re.IGNORECASE)

# Bitmap subtitle codecs — these are images, not text, so they can't be
# extracted to SRT/VTT, searched, or shown in the browser player. The engine
# only handles text subtitles; image tracks are deprioritised in auto-select
# and filtered out of the web picker.
IMAGE_SUBTITLE_CODECS = frozenset({
    "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
    "dvb_subtitle", "dvbsub", "xsub",
})


def is_text_subtitle(t) -> bool:
    """True when a track is a text subtitle (extractable to SRT/VTT)."""
    return (getattr(t, "codec", "") or "").lower() not in IMAGE_SUBTITLE_CODECS


# Default subtitle-language preference for auto-selection (B14). "en" matches
# eng/en/english. Configurable per call (web: QC_SUBTITLE_LANGS + per-request).
DEFAULT_SUB_LANGS = ("en",)


def normalize_langs(langs) -> tuple[str, ...]:
    """Normalize a language-preference iterable (or comma string) to lowercase
    prefixes; empty/None falls back to the English default."""
    if langs is None:
        return DEFAULT_SUB_LANGS
    if isinstance(langs, str):
        langs = langs.split(",")
    out = tuple(s.strip().lower() for s in langs if s and s.strip())
    return out or DEFAULT_SUB_LANGS


def _lang_rank(lang: str | None, langs: tuple[str, ...]) -> int | None:
    """Index of the best-matching preference for a track's language, or None.
    Untagged ("", und) counts as the top preference (single-language releases)."""
    lang = (lang or "").lower()
    if not lang or lang == "und":
        return 0
    for i, pref in enumerate(langs):
        if lang.startswith(pref) or pref.startswith(lang):
            return i
    return None


def _track_score(t, langs: tuple[str, ...] = DEFAULT_SUB_LANGS) -> int:
    """Score a subtitle track for dialogue quality, given a language preference.

    Text full dialogue > text SDH > text forced > any image track.  Image
    (PGS/VOBSUB) subtitles can't be turned into searchable/displayable text, so
    they rank below every text track and are only chosen when nothing else
    exists.  Among text tracks: a preferred-language track outranks other
    languages (earlier in ``langs`` ranks higher; untagged counts as the top
    preference); within a language, forced (foreign-portion-only, minimal
    dialogue) ranks below SDH ranks below full dialogue, and a commentary track
    (a transcript of the commentary, not the film) ranks below them all.
    """
    if not is_text_subtitle(t):
        return -1000
    score = 0
    title = t.title or ""
    rank = _lang_rank(getattr(t, "language", None), langs)
    if rank is not None:
        score += (len(langs) - rank) * 100
    if getattr(t, "forced", False) or _FORCED_RE.search(title):
        score -= 50
    elif getattr(t, "hearing_impaired", False) or _SDH_RE.search(title):
        score -= 10
    # A commentary track isn't the film's dialogue — rank it below every normal
    # text track of the same language, so a plain dialogue track always wins.
    if _COMMENTARY_RE.search(title):
        score -= 60
    return score


def best_track(tracks, langs=None):
    """Pick the best dialogue track from SubtitleTrack or StreamInfo objects.

    This is the single source of truth for auto-selection — the web frontend
    picker, the search/index cache, and clip subtitle embedding all follow it,
    so cached extractions are shared across those paths.  ``langs`` is an ordered
    language preference (default English).  Ties keep the first track (container
    order).  Returns None for an empty list.
    """
    if not tracks:
        return None
    langs = normalize_langs(langs)
    return max(tracks, key=lambda t: _track_score(t, langs))


@dataclass(frozen=True)
class ResolvedSubtitles:
    """Cues plus where they came from.

    `path` is the external subtitle file used (explicit `--subs` or a sidecar),
    or None when the cues were extracted from an embedded track — in which case
    the subtitle is already inside the video and need not be muxed in separately.
    `track` is the embedded track index the cues were extracted from (None when
    they came from an external file), so callers know which track auto-selection
    landed on.
    """

    cues: list[Cue]
    path: Path | None
    track: int | None = None


def resolve_subtitles(
    *,
    subs: str | Path | None,
    video: str | Path | None,
    track: int | None = None,
    langs=None,
) -> ResolvedSubtitles:
    """Resolve cues from the best available source, reporting the file used.

    Priority: explicit ``subs`` file > sidecar next to ``video`` > embedded track.
    """
    if subs:
        return ResolvedSubtitles(load_subtitles(subs), Path(subs))
    if not video:
        raise ValueError("Provide either --subs or --video.")
    if not Path(video).exists():
        raise FileNotFoundError(f"Video file not found: {video}")

    sidecar = find_sidecar(video)
    if sidecar:
        return ResolvedSubtitles(load_subtitles(sidecar), sidecar)

    tracks = list_embedded_tracks(video)
    if not tracks:
        raise FileNotFoundError(
            f"No sidecar subtitle file and no embedded subtitle tracks found for {video}."
        )
    if track is None:
        track = best_track(tracks, langs).index
    return ResolvedSubtitles(extract_embedded(video, track), None, track)


def resolve_cues(
    *,
    subs: str | Path | None,
    video: str | Path | None,
    track: int | None = None,
) -> list[Cue]:
    """Resolve cues from the best available source (see `resolve_subtitles`)."""
    return resolve_subtitles(subs=subs, video=video, track=track).cues
