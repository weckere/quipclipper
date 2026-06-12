"""Compute clip boundaries and cut them out with ffmpeg.

Lossless mode (the default for audio/video) uses ffmpeg stream copy (``-c copy``)
so the original encoded bytes are copied straight through — no re-encode, no
quality loss, and very fast. The tradeoff, inherent to every codec, is that a
copy can only start at a keyframe: with ``-ss`` placed before ``-i`` ffmpeg snaps
the start to the nearest keyframe at or before the requested time, so a lossless
clip may begin a fraction early. For dialogue clips that is just a little extra
lead-in (and we already pad the start), so it is a non-issue. When you need
frame-exact boundaries or a specific codec/container, pass ``lossless=False`` to
re-encode. GIF output is always a re-encode.

Channel splitting (:func:`split_audio_channels`) is the one operation that cannot
be a stream copy: pulling individual channels out of a surround mix requires
decoding it. It writes one file per channel group (stereo pairs plus mono centre/
LFE), as lossless WAV/FLAC or re-encoded back to the source codec.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from quipclipper.models import Cue, Match, format_timestamp

# Source audio codec -> a container extension that can hold it via stream copy.
LOSSLESS_AUDIO_EXT = {
    "aac": "m4a",
    "alac": "m4a",
    "ac3": "ac3",
    "eac3": "eac3",
    "mp3": "mp3",
    "opus": "opus",
    "vorbis": "ogg",
    "flac": "flac",
    "dts": "dts",
    "truehd": "thd",
    "pcm_s16le": "wav",
    "pcm_s24le": "wav",
    "pcm_s32le": "wav",
}
# Matroska holds essentially any codec, so it is the safe lossless fallback.
FALLBACK_AUDIO_EXT = "mka"
LOSSLESS_VIDEO_EXT = "mkv"
# Extensions used when re-encoding instead of stream-copying.
REENCODE_EXT = {"audio": "mp3", "video": "mp4"}

# Full-mix lossless audio re-encode: decode one audio stream and write all its
# channels (e.g. 5.1) to a single WAV/FLAC file. Unlike stream-copy passthrough
# this re-wraps to a lossless PCM/FLAC codec, and unlike --split-channels it
# keeps the surround mix intact in one file. (encoder, extension) per format.
FULLMIX_AUDIO = {
    "wav": ("pcm_s24le", "wav"),
    "flac": ("flac", "flac"),
}

# Codecs we can re-encode back to when splitting with --split-format original.
ORIGINAL_ENCODER_OK = {"ac3", "eac3", "aac", "mp3", "flac", "opus", "vorbis"}

# Channel names for the common ffmpeg layouts, used to split surround audio.
LAYOUT_CHANNELS = {
    "mono": ["FC"],
    "stereo": ["FL", "FR"],
    "2.1": ["FL", "FR", "LFE"],
    "3.0": ["FL", "FR", "FC"],
    "3.0(back)": ["FL", "FR", "BC"],
    "4.0": ["FL", "FR", "FC", "BC"],
    "quad": ["FL", "FR", "BL", "BR"],
    "quad(side)": ["FL", "FR", "SL", "SR"],
    "4.1": ["FL", "FR", "FC", "LFE", "BC"],
    "5.0": ["FL", "FR", "FC", "BL", "BR"],
    "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "6.0": ["FL", "FR", "FC", "BC", "SL", "SR"],
    "6.1": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    "7.0": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"],
    "7.1(wide-side)": ["FL", "FR", "FC", "LFE", "SL", "SR", "FLC", "FRC"],
}
# Fallback when only a channel count is known.
COUNT_LAYOUT = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 6: "5.1", 8: "7.1"}

# Stereo pairs to keep together when splitting, in preferred order.
CHANNEL_PAIRS = [
    ("front", ("FL", "FR")),
    ("front_center", ("FLC", "FRC")),
    ("side", ("SL", "SR")),
    ("back", ("BL", "BR")),
]
# Friendly labels for standalone (mono) channels.
MONO_LABELS = {"FC": "center", "LFE": "lfe", "BC": "back_center"}


@dataclass(frozen=True)
class ClipRange:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def compute_range(
    match: Match,
    *,
    before: float = 0.5,
    after: float = 0.5,
) -> ClipRange:
    """Clip the matched cues' own span, padded by `before`/`after` seconds.

    `before` extends the start earlier; `after` extends the end later. The start
    is clamped to 0 so padding before the opening line never goes negative.
    """
    start = max(0.0, match.start - before)
    end = match.end + after
    return ClipRange(start=start, end=end)


# --- probing -----------------------------------------------------------------


def probe_audio_streams(source: str | Path) -> list[str]:
    """Return the codec name of every audio stream, in order (empty if unknown)."""
    return [s.get("codec_name", "") for s in _probe_streams(source, "a")]


def probe_channel_layout(source: str | Path, audio_index: int = 0) -> list[str] | None:
    """Return the channel names of one audio stream (e.g. FL, FR, FC, LFE, ...)."""
    streams = _probe_streams(source, f"a:{audio_index}", fields="channels,channel_layout")
    if not streams:
        return None
    info = streams[0]
    layout = (info.get("channel_layout") or "").strip()
    if layout in LAYOUT_CHANNELS:
        return list(LAYOUT_CHANNELS[layout])
    count = info.get("channels")
    if isinstance(count, int) and count in COUNT_LAYOUT:
        return list(LAYOUT_CHANNELS[COUNT_LAYOUT[count]])
    return None


def _probe_streams(source: str | Path, selector: str, fields: str = "codec_name") -> list[dict]:
    if shutil.which("ffprobe") is None:
        return []
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", selector,
        "-show_entries", f"stream={fields}",
        "-of", "json", str(source),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return json.loads(proc.stdout or "{}").get("streams", [])


# --- output naming -----------------------------------------------------------


def output_extension(
    kind: str, *, lossless: bool, audio_codecs: list[str] | None = None
) -> str:
    """Pick a file extension appropriate for the kind and copy/encode mode.

    Lossless audio uses the source codec's natural single-stream container only
    when there is exactly one audio stream of a known codec; with multiple audio
    streams (e.g. a 5.1 EAC3 track plus a stereo commentary) it falls back to
    Matroska (.mka), which can hold any number of streams of any codec losslessly.
    """
    if kind == "gif":
        return "gif"
    if not lossless:
        return REENCODE_EXT[kind]
    if kind == "video":
        return LOSSLESS_VIDEO_EXT
    codecs = audio_codecs or []
    if len(codecs) == 1 and codecs[0] in LOSSLESS_AUDIO_EXT:
        return LOSSLESS_AUDIO_EXT[codecs[0]]
    return FALLBACK_AUDIO_EXT


def _timestamp_slug(seconds: float) -> str:
    return format_timestamp(seconds).replace(":", "-").replace(".", "_")


def _srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_clip_srt(cues: list[Cue], *, window_start: float, window_end: float) -> str:
    """Render an SRT covering `cues` overlapping the window, shifted to start at 0.

    `window_start` is the clip's reference time (the keyframe the copy begins on),
    so subtitles line up with the lossless video.
    """
    blocks: list[str] = []
    n = 0
    for c in cues:
        if c.end <= window_start or c.start >= window_end:
            continue
        start = max(c.start, window_start) - window_start
        end = min(c.end, window_end) - window_start
        n += 1
        blocks.append(f"{n}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{c.text}\n")
    return "\n".join(blocks)


# --- channel grouping --------------------------------------------------------


def group_channels(channels: list[str], *, include_lfe: bool = True) -> list[tuple[str, list[str]]]:
    """Group channels into stereo pairs plus standalone mono channels.

    e.g. 5.1 [FL,FR,FC,LFE,BL,BR] ->
        [("front",[FL,FR]), ("back",[BL,BR]), ("center",[FC]), ("lfe",[LFE])]

    With `include_lfe=False` the low-frequency-effects channel is dropped.
    """
    chans = [c for c in channels if include_lfe or c != "LFE"]
    present = set(chans)
    used: set[str] = set()
    groups: list[tuple[str, list[str]]] = []
    for label, (left, right) in CHANNEL_PAIRS:
        if left in present and right in present:
            groups.append((label, [left, right]))
            used.update((left, right))
    for ch in chans:  # remaining channels, in original order, as mono files
        if ch in used:
            continue
        groups.append((MONO_LABELS.get(ch, ch.lower()), [ch]))
        used.add(ch)
    return groups


def _audio_map_args(audio_indices: list[int] | None, *, optional: bool) -> list[str]:
    if audio_indices:
        out: list[str] = []
        for i in audio_indices:
            out += ["-map", f"0:a:{i}"]
        return out
    return ["-map", "0:a?" if optional else "0:a"]


# --- single-file cut ---------------------------------------------------------


def _ffmpeg_args(
    *,
    source: Path,
    rng: ClipRange,
    kind: str,
    out: Path,
    lossless: bool,
    fps: int,
    width: int,
    audio_indices: list[int] | None = None,
    embed_subs: Path | None = None,
    audio_codec: str | None = None,
) -> list[str]:
    head = ["ffmpeg", "-y", "-v", "error"]
    inputs = ["-ss", f"{rng.start:.3f}", "-i", str(source)]
    subs_input = None
    if kind == "video" and lossless and embed_subs is not None:
        # The sidecar SRT is pre-trimmed and time-shifted to the clip (see
        # cut_clip), so it is muxed WITHOUT -ss — ffmpeg's text-subtitle seeking
        # is unreliable, and these timestamps already align with the output.
        inputs += ["-i", str(embed_subs)]
        subs_input = 1
    dur = ["-t", f"{rng.duration:.3f}"]

    if kind == "gif":
        # Scale and cap fps; height -1 preserves aspect ratio. Always re-encoded.
        vf = f"fps={fps},scale={width}:-1:flags=lanczos"
        return head + inputs + dur + ["-an", "-vf", vf, str(out)]

    if kind == "audio" and audio_codec is not None:
        # Full-mix lossless re-encode of ONE audio stream, all channels kept
        # (no -ac downmix), so a 5.1 source becomes a 5.1 WAV/FLAC. WAV/FLAC
        # containers hold a single stream, so map exactly one (first selected
        # or a:0). ffmpeg writes WAVE_FORMAT_EXTENSIBLE with the channel mask.
        enc = FULLMIX_AUDIO[audio_codec][0]
        idx = audio_indices[0] if audio_indices else 0
        return head + inputs + dur + ["-map", f"0:a:{idx}", "-c:a", enc, str(out)]

    if lossless:
        # -avoid_negative_ts make_zero cleans up timestamps after a keyframe seek.
        # -map 0:a copies ALL audio streams (every language/commentary track) and
        # -c copy preserves each one's exact bitstream and channel layout (5.1/7.1).
        common = ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if kind == "audio":
            return head + inputs + dur + _audio_map_args(audio_indices, optional=False) + common + [str(out)]
        # video: keep all video, audio and (embedded) subtitle tracks
        maps = ["-map", "0:v?"] + _audio_map_args(audio_indices, optional=True) + ["-map", "0:s?"]
        if subs_input is not None:
            maps += ["-map", f"{subs_input}:0"]
        return head + inputs + dur + maps + common + [str(out)]

    # re-encode (no subtitle handling here — that is a lossless feature)
    if kind == "audio":
        maps = _audio_map_args(audio_indices, optional=False) if audio_indices else ["-vn"]
        return head + inputs + dur + maps + [str(out)]
    maps = ["-map", "0:v:0?"] + _audio_map_args(audio_indices, optional=True)
    return head + inputs + dur + maps + ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(out)]


def cut_clip(
    source: str | Path,
    rng: ClipRange,
    *,
    kind: str = "audio",
    lossless: bool = True,
    out: str | Path | None = None,
    fps: int = 15,
    width: int = 480,
    audio_indices: list[int] | None = None,
    embed_cues: list[Cue] | None = None,
    audio_codec: str | None = None,
) -> Path:
    """Cut `source` between `rng.start` and `rng.end` into the chosen `kind`.

    With `lossless=True` (default) audio/video are stream-copied (no quality
    loss); GIF is always re-encoded. `audio_indices` selects which audio streams
    (by a:N index) to keep; None keeps them all. `embed_cues`, when given for a
    lossless video clip, are rendered to a clip-aligned SRT and muxed in (in
    addition to any embedded subtitle tracks the source already has).

    `audio_codec` (``"wav"`` or ``"flac"``, audio kind only) overrides copy/encode
    mode with a **full-mix lossless re-encode**: one audio stream is decoded and
    written with every channel kept (a 5.1 source → a 5.1 WAV/FLAC), to a single
    file. This differs from passthrough (`lossless=True`, original codec) and from
    `split_audio_channels` (one file per channel group). Returns the path written.
    Requires ffmpeg.
    """
    source = Path(source)
    if kind not in ("audio", "video", "gif"):
        raise ValueError(f"kind must be audio, video, or gif, got {kind!r}")
    if audio_codec is not None and audio_codec not in FULLMIX_AUDIO:
        raise ValueError(f"audio_codec must be one of {sorted(FULLMIX_AUDIO)}, got {audio_codec!r}")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to cut clips.")
    if not source.exists():
        raise RuntimeError(f"Video file not found: {source}")

    if out is None:
        if kind == "audio" and audio_codec is not None:
            ext = FULLMIX_AUDIO[audio_codec][1]
        else:
            codecs = None
            if lossless and kind == "audio":
                codecs = probe_audio_streams(source)
                if audio_indices:  # name by the codecs of the selected streams only
                    codecs = [codecs[i] for i in audio_indices if i < len(codecs)]
            ext = output_extension(kind, lossless=lossless, audio_codecs=codecs)
        out = source.with_name(f"{source.stem}_{_timestamp_slug(rng.start)}.{ext}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Render a clip-aligned sidecar SRT if asked to embed cues into a video clip.
    # Generate it relative to rng.start (the -ss seek point): the muxer applies
    # the same -avoid_negative_ts make_zero shift to the subtitle as to the
    # video, so the keyframe lead-in cancels out and the two stay in sync.
    embed_subs: Path | None = None
    if kind == "video" and lossless and embed_cues:
        srt = render_clip_srt(embed_cues, window_start=rng.start, window_end=rng.end)
        if srt.strip():
            tmp = tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False, encoding="utf-8")
            tmp.write(srt)
            tmp.close()
            embed_subs = Path(tmp.name)

    try:
        args = _ffmpeg_args(
            source=source, rng=rng, kind=kind, out=out, lossless=lossless, fps=fps,
            width=width, audio_indices=audio_indices, embed_subs=embed_subs,
            audio_codec=audio_codec,
        )
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.strip()}")
    finally:
        if embed_subs is not None:
            embed_subs.unlink(missing_ok=True)
    return out


# --- surround channel split --------------------------------------------------


def _split_codec(fmt: str, source: str | Path, audio_index: int) -> tuple[str, str]:
    """Return (ffmpeg encoder, file extension) for a --split-format value."""
    if fmt == "wav":
        return "pcm_s24le", "wav"
    if fmt == "flac":
        return "flac", "flac"
    if fmt == "original":
        codecs = probe_audio_streams(source)
        codec = codecs[audio_index] if audio_index < len(codecs) else ""
        if codec not in ORIGINAL_ENCODER_OK:
            raise RuntimeError(
                f"Cannot re-encode split channels to {codec or 'unknown'!r}; "
                "use --split-format wav or flac instead."
            )
        return codec, LOSSLESS_AUDIO_EXT.get(codec, FALLBACK_AUDIO_EXT)
    raise ValueError(f"split format must be wav, flac, or original, got {fmt!r}")


def split_audio_channels(
    source: str | Path,
    rng: ClipRange,
    *,
    audio_index: int = 0,
    fmt: str = "wav",
    include_lfe: bool = True,
    out: str | Path | None = None,
) -> list[Path]:
    """Split one surround audio stream into per-group files for the clip range.

    Produces a stereo file for each L/R pair (front, side, back, ...) and a mono
    file for each standalone channel (centre, LFE — drop LFE with
    `include_lfe=False`). This decodes the surround mix (it cannot be a stream
    copy); `fmt` is "wav"/"flac" (lossless, no re-encode of the decoded audio) or
    "original" (re-encode to the source codec). Returns the list of files written.
    """
    source = Path(source)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to split channels.")
    if not source.exists():
        raise RuntimeError(f"Video file not found: {source}")

    channels = probe_channel_layout(source, audio_index)
    if not channels:
        raise RuntimeError(
            f"Could not determine the channel layout of audio stream a:{audio_index}."
        )
    encoder, ext = _split_codec(fmt, source, audio_index)
    groups = group_channels(channels, include_lfe=include_lfe)

    base = Path(out) if out else source.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    slug = _timestamp_slug(rng.start)

    # Accurate two-stage seek: fast-seek to just before the start, then trim
    # precisely. Splitting re-encodes anyway, so we can be sample-exact.
    preroll = min(rng.start, 5.0)

    written: list[Path] = []
    for label, chans in groups:
        target = Path(f"{base}_{slug}_{label}.{ext}")
        pan = (
            f"pan=stereo|c0={chans[0]}|c1={chans[1]}"
            if len(chans) == 2
            else f"pan=mono|c0={chans[0]}"
        )
        args = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{rng.start - preroll:.3f}", "-i", str(source),
            "-ss", f"{preroll:.3f}", "-t", f"{rng.duration:.3f}",
            "-map", f"0:a:{audio_index}",
            "-filter:a", pan, "-c:a", encoder, str(target),
        ]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed splitting {label}:\n{proc.stderr.strip()}")
        written.append(target)
    return written
