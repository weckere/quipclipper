"""Core data types: a subtitle Cue and a search Match."""

from __future__ import annotations

from dataclasses import dataclass


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    if seconds < 0:
        seconds = 0.0
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


@dataclass(frozen=True)
class Cue:
    """A single subtitle entry with a time span (in seconds) and its text."""

    index: int
    start: float  # seconds
    end: float  # seconds
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def start_ts(self) -> str:
        return format_timestamp(self.start)

    @property
    def end_ts(self) -> str:
        return format_timestamp(self.end)


@dataclass(frozen=True)
class Match:
    """A ranked search hit pointing at one or more consecutive cues."""

    score: float  # 0-100, higher is better
    cues: tuple[Cue, ...]  # one cue, or several when a phrase spans captions
    text: str  # joined text of the matched cues

    @property
    def start(self) -> float:
        return self.cues[0].start

    @property
    def end(self) -> float:
        return self.cues[-1].end

    @property
    def index(self) -> int:
        return self.cues[0].index
