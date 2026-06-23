"""Adapters between the quipclipper engine and the web layer.

Turns the engine's stream/subtitle data into JSON-friendly dicts and renders
cues as WebVTT for the in-browser player. All ffmpeg/ffprobe work happens inside
the engine functions called here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from quipclipper.models import Cue, format_timestamp
from quipclipper.clip import channels_for_layout, group_channels
from quipclipper.subtitles import StreamInfo, best_track, find_sidecar, list_streams


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


def probe_keyframe_before(path: Path, target: float) -> float:
    """Return the PTS of the last keyframe at or before *target* seconds.

    When ffmpeg uses ``-ss`` before ``-i`` with ``-c:v copy``, it starts
    from this keyframe.  Knowing the actual start lets us shift subtitles
    accurately instead of using the requested (approximate) seek time.

    Falls back to *target* if ffprobe fails or no keyframe is found.
    """
    # Read a window around the target; go back far enough to catch sparse
    # keyframe intervals (up to ~15 s for some encodes).
    window_start = max(0, target - 20)
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            "-read_intervals", f"{window_start}%{target + 0.5}",
            str(path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    best = None
    for line in result.stdout.splitlines():
        if ",K" not in line:
            continue
        pts_str = line.split(",", 1)[0]
        try:
            pts = float(pts_str)
        except ValueError:
            continue
        if pts <= target + 0.01:  # small epsilon for floating-point
            if best is None or pts > best:
                best = pts
    return best if best is not None else target


def stream_dict(s: StreamInfo) -> dict:
    d = {
        "kind": s.kind,
        "index": s.type_index,
        "selector": s.selector,
        "codec": s.codec,
        "language": s.language,
        "title": s.title,
        "channels": s.channels,
        "channel_layout": s.channel_layout,
        "forced": s.forced,
        "hearing_impaired": s.hearing_impaired,
        "attached_pic": s.attached_pic,
        "label": s.label(),
    }
    # For multichannel audio, the selectable channel groups (front/center/lfe/
    # side/back) so the stream selector can offer a channel subset (B17). Derived
    # ONLY from a recognized channel-layout tag — never guessed from the channel
    # count — so the Channels dropdown can only ever offer channels that actually
    # exist (B18). Unknown layout => no groups => the dropdown is hidden.
    if s.kind == "audio":
        chans = channels_for_layout(s.channel_layout, None)
        d["groups"] = [lbl for lbl, _ in group_channels(chans)] if chans else []
    return d


def item_info(path: Path, langs=None) -> dict:
    """Probe a video file: all streams, its subtitle tracks, and sidecar status.

    ``langs`` is the subtitle-language preference for auto-selection (B14)."""
    streams = list_streams(path)
    sub_streams = [s for s in streams if s.kind == "subtitle"]
    subtitle_tracks = [stream_dict(s) for s in sub_streams]
    # The auto-selected subtitle track, by the engine's single source of truth.
    # The frontend preselects this so its request matches the cache key that
    # pre-indexing (which auto-selects) warmed — no duplicate extraction.
    chosen = best_track(sub_streams, langs)
    return {
        "name": path.name,
        "path": str(path),
        "size": path.stat().st_size if path.exists() else None,
        "duration": probe_duration(path),
        "streams": [stream_dict(s) for s in streams],
        "subtitle_tracks": subtitle_tracks,
        "best_track": chosen.type_index if chosen is not None else None,
        "has_sidecar": find_sidecar(path) is not None,
    }


def cues_to_vtt(cues: list[Cue], offset: float = 0) -> str:
    """Render cues as a WebVTT document.

    When *offset* is non-zero, shift all timestamps backward by that amount
    (i.e. subtract *offset* from each cue start/end).  Cues that fall entirely
    before the offset are dropped; cues that straddle it are clamped to 0.
    """
    lines = ["WEBVTT", ""]
    for c in cues:
        end = c.end - offset
        if end <= 0:
            continue
        start = max(0, c.start - offset)
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        # Re-emit the speaker as a WebVTT voice tag so the player caption shows it.
        speaker = getattr(c, "speaker", None)
        lines.append(f"<v {speaker}>{c.text}" if speaker else c.text)
        lines.append("")
    return "\n".join(lines)
