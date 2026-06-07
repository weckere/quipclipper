"""Adapters between the quipclipper engine and the web layer.

Turns the engine's stream/subtitle data into JSON-friendly dicts and renders
cues as WebVTT for the in-browser player. All ffmpeg/ffprobe work happens inside
the engine functions called here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from quipclipper.models import Cue, format_timestamp
from quipclipper.subtitles import StreamInfo, find_sidecar, list_streams


def probe_duration(path: Path) -> float | None:
    """Get file duration in seconds via ffprobe, or None on failure."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    val = result.stdout.strip()
    if val and val != "N/A":
        try:
            return float(val)
        except ValueError:
            pass
    return None


def stream_dict(s: StreamInfo) -> dict:
    return {
        "kind": s.kind,
        "index": s.type_index,
        "selector": s.selector,
        "codec": s.codec,
        "language": s.language,
        "title": s.title,
        "channels": s.channels,
        "channel_layout": s.channel_layout,
        "label": s.label(),
    }


def item_info(path: Path) -> dict:
    """Probe a video file: all streams, its subtitle tracks, and sidecar status."""
    streams = list_streams(path)
    subtitle_tracks = [stream_dict(s) for s in streams if s.kind == "subtitle"]
    return {
        "name": path.name,
        "path": str(path),
        "size": path.stat().st_size if path.exists() else None,
        "duration": probe_duration(path),
        "streams": [stream_dict(s) for s in streams],
        "subtitle_tracks": subtitle_tracks,
        "has_sidecar": find_sidecar(path) is not None,
    }


def cues_to_vtt(cues: list[Cue]) -> str:
    """Render cues as a WebVTT document (absolute timestamps)."""
    lines = ["WEBVTT", ""]
    for c in cues:
        lines.append(f"{format_timestamp(c.start)} --> {format_timestamp(c.end)}")
        lines.append(c.text)
        lines.append("")
    return "\n".join(lines)
