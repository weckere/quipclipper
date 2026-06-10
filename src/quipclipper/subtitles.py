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

from quipclipper.models import Cue

# Subtitle extensions we look for next to a video file, in priority order.
SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts")

# Strip SubRip/ASS inline markup like <i>, {\an8}, etc.
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _clean(text: str) -> str:
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace("\n", " ")
    text = _TAG_RE.sub("", text)
    return " ".join(text.split())


def load_subtitles(path: str | Path) -> list[Cue]:
    """Parse a subtitle file into a list of Cue objects (text cleaned of markup)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Subtitle file not found: {path}")
    subs = pysubs2.load(str(path))
    cues: list[Cue] = []
    for i, line in enumerate(subs):
        text = _clean(line.text)
        if not text:
            continue
        cues.append(
            Cue(
                index=i,
                start=line.start / 1000.0,  # pysubs2 stores milliseconds
                end=line.end / 1000.0,
                text=text,
            )
        )
    cues.sort(key=lambda c: c.start)
    # Re-number after sorting so indexes are stable, contiguous positions.
    return [Cue(index=i, start=c.start, end=c.end, text=c.text) for i, c in enumerate(cues)]


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
        ":stream_disposition=forced,hearing_impaired",
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
    # Also accept stem-prefixed names like "movie.en.srt".
    matches = sorted(
        p
        for p in video_path.parent.glob(f"{video_path.stem}*")
        if p.suffix.lower() in SUBTITLE_EXTS
    )
    return matches[0] if matches else None


@dataclass(frozen=True)
class ResolvedSubtitles:
    """Cues plus where they came from.

    `path` is the external subtitle file used (explicit `--subs` or a sidecar),
    or None when the cues were extracted from an embedded track — in which case
    the subtitle is already inside the video and need not be muxed in separately.
    """

    cues: list[Cue]
    path: Path | None


def resolve_subtitles(
    *,
    subs: str | Path | None,
    video: str | Path | None,
    track: int | None = None,
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
        if len(tracks) == 1:
            track = tracks[0].index
        else:
            # Score English tracks: full dialogue (non-SDH, non-forced) >
            # SDH > forced.  Forced tracks contain only foreign-language
            # portions (minimal dialogue), so they rank below SDH.
            _SDH_RE = re.compile(r"sdh|hearing|impaired|\bcc\b", re.IGNORECASE)
            _FORCED_RE = re.compile(r"forced", re.IGNORECASE)
            eng = [t for t in tracks if t.language and t.language.lower() in ("eng", "en", "english")]

            def _score(t: SubtitleTrack) -> int:
                if t.forced or _FORCED_RE.search(t.title or ""):
                    return -50
                if t.hearing_impaired or _SDH_RE.search(t.title or ""):
                    return -10
                return 0

            best = max(eng, key=_score) if eng else None
            if best is not None:
                track = best.index
            else:
                labels = "\n  ".join(t.label() for t in tracks)
                raise ValueError(
                    "Multiple embedded subtitle tracks found; choose one with "
                    f"--track <index>:\n  {labels}"
                )
    return ResolvedSubtitles(extract_embedded(video, track), None)


def resolve_cues(
    *,
    subs: str | Path | None,
    video: str | Path | None,
    track: int | None = None,
) -> list[Cue]:
    """Resolve cues from the best available source (see `resolve_subtitles`)."""
    return resolve_subtitles(subs=subs, video=video, track=track).cues
