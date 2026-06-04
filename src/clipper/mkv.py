"""MKVToolNix (mkvmerge) backend for cutting Matroska sources.

For MKV/MKA sources, mkvmerge splits losslessly by timestamp and is excellent at
it: it keeps every track, chapter and attachment, never re-encodes, and trims and
time-shifts subtitles natively (including a sidecar file added as an extra input).
This module shells out to ``mkvmerge`` and is used as an alternative to the ffmpeg
backend in :mod:`clipper.clip`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from clipper.clip import ClipRange, _timestamp_slug
from clipper.models import format_timestamp

# Containers mkvmerge can split natively (Matroska family).
MATROSKA_SUFFIXES = (".mkv", ".mka", ".mks", ".webm")


def mkvmerge_available() -> bool:
    return shutil.which("mkvmerge") is not None


def is_matroska(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MATROSKA_SUFFIXES


def _ts(seconds: float) -> str:
    # mkvmerge accepts HH:MM:SS.mmm timestamps.
    return format_timestamp(seconds)


def identify(source: str | Path) -> list[dict]:
    """Return mkvmerge's track list for `source` (id, type, codec, properties)."""
    proc = subprocess.run(
        ["mkvmerge", "-J", str(source)], capture_output=True, text=True
    )
    if proc.returncode not in (0, 1):  # 1 = warnings, still usable
        raise RuntimeError(f"mkvmerge identify failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}").get("tracks", [])


def audio_track_ids(tracks: list[dict], audio_indices: list[int] | None) -> list[int]:
    """Map ffmpeg-style a:N indices to mkvmerge global track ids (None = all)."""
    audio = [t for t in tracks if t.get("type") == "audio"]
    if audio_indices is None:
        return [t["id"] for t in audio]
    return [audio[i]["id"] for i in audio_indices if 0 <= i < len(audio)]


def build_mkvmerge_args(
    *,
    source: Path,
    rng: ClipRange,
    kind: str,
    out: Path,
    audio_ids: list[int],
    all_audio: bool,
    keep_subs: bool,
    keep_chapters: bool,
    embed_subs: Path | None,
) -> list[str]:
    """Build the mkvmerge command line for a single-range lossless cut."""
    cmd = ["mkvmerge", "-o", str(out), "--split", f"parts:{_ts(rng.start)}-{_ts(rng.end)}"]
    audio_sel = ["-a", ",".join(map(str, audio_ids))] if audio_ids else ["-A"]
    if kind == "audio":
        # audio only: drop video, subtitles, attachments, buttons
        cmd += ["-D", "-S", "-M", "-B"]
        if not all_audio:
            cmd += audio_sel
    else:  # video
        if not all_audio:
            cmd += audio_sel
        if not keep_subs:
            cmd += ["-S"]
    if not keep_chapters:
        cmd += ["--no-chapters"]
    cmd += [str(source)]
    if kind == "video" and embed_subs is not None:
        # Added as an extra input: mkvmerge muxes it and the --split cut trims and
        # shifts it to match the clip automatically.
        cmd += [str(embed_subs)]
    return cmd


def remux_to_mkv(source: str | Path, extra_inputs: list[Path], out: Path) -> Path:
    """Losslessly mux `source` (plus any extra inputs, e.g. sidecar subs) into one MKV."""
    cmd = ["mkvmerge", "-o", str(out), str(source)]
    cmd += [str(e) for e in extra_inputs]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"mkvmerge remux failed:\n{(proc.stdout + proc.stderr).strip()}")
    return out


def cut_with_mkvmerge(
    source: str | Path,
    rng: ClipRange,
    *,
    kind: str = "video",
    out: str | Path | None = None,
    audio_indices: list[int] | None = None,
    keep_subs: bool = True,
    keep_chapters: bool = True,
    embed_subs: str | Path | None = None,
    remux_first: bool = False,
) -> Path:
    """Cut a Matroska `source` losslessly with mkvmerge. Returns the path written.

    With `remux_first=True`, the source (and any `embed_subs` sidecar) is first
    muxed into a temporary full MKV with mkvmerge, and the clip is cut from that —
    a fully-mkvmerge pipeline that bypasses ffmpeg entirely, at the cost of the
    temporary file's disk space.
    """
    source = Path(source)
    if kind not in ("audio", "video"):
        raise ValueError("mkvmerge backend supports only audio or video, not " + repr(kind))
    if not mkvmerge_available():
        raise RuntimeError("mkvmerge not found on PATH. Install MKVToolNix to use this backend.")
    if not source.exists():
        raise RuntimeError(f"Video file not found: {source}")

    ext = "mka" if kind == "audio" else "mkv"
    if out is None:
        out = source.with_name(f"{source.stem}_{_timestamp_slug(rng.start)}.{ext}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    embed_subs_path = Path(embed_subs) if embed_subs else None
    work_source = source
    temp_remux: Path | None = None
    try:
        if remux_first:
            tmp = tempfile.NamedTemporaryFile(
                dir=out.parent, prefix=f".{out.stem}.remux.", suffix=".mkv", delete=False
            )
            tmp.close()
            temp_remux = Path(tmp.name)
            extras = [embed_subs_path] if (kind == "video" and embed_subs_path) else []
            remux_to_mkv(source, extras, temp_remux)
            work_source = temp_remux
            embed_subs_path = None  # now embedded in the remux

        tracks = identify(work_source)
        all_audio = audio_indices is None
        audio_ids = audio_track_ids(tracks, audio_indices)
        if not all_audio and not audio_ids:
            raise RuntimeError("None of the requested --audio-track indices exist in the source.")

        args = build_mkvmerge_args(
            source=work_source, rng=rng, kind=kind, out=out, audio_ids=audio_ids,
            all_audio=all_audio, keep_subs=keep_subs, keep_chapters=keep_chapters,
            embed_subs=embed_subs_path,
        )
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"mkvmerge failed:\n{(proc.stdout + proc.stderr).strip()}")
        # With a single retained part mkvmerge writes the exact name, but older
        # versions append -001 before the extension; normalise either way.
        if not out.exists():
            alt = out.with_name(f"{out.stem}-001{out.suffix}")
            if alt.exists():
                alt.rename(out)
    finally:
        if temp_remux is not None:
            temp_remux.unlink(missing_ok=True)
    return out
