"""clipper command-line interface.

Examples:
    clipper search "i'll be back" --subs movie.srt
    clipper clip "i'll be back" --video movie.mkv --type audio
    clipper clip "i'll be back" --video movie.mkv --type video --before 5 --after 3
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from clipper.clip import compute_range, cut_clip
from clipper.models import Match
from clipper.search import search
from clipper.subtitles import list_embedded_tracks, resolve_cues

app = typer.Typer(
    add_completion=False,
    help="Find and cut clips from movies/TV by searching the subtitle dialogue.",
    no_args_is_help=True,
)


def _echo_match(rank: int, m: Match) -> None:
    typer.echo(
        f"[{rank}] {m.start:>8.2f}s  "
        f"{typer.style(m.cues[0].start_ts, fg='cyan')}–{m.cues[-1].end_ts}  "
        f"(score {m.score:.0f})"
    )
    typer.echo(f"      {m.text}")


def _resolve(subs: Optional[Path], video: Optional[Path], track: Optional[int]):
    try:
        return resolve_cues(subs=subs, video=video, track=track)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)


@app.command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Dialogue text to search for."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (.srt/.vtt/.ass)."),
    video: Optional[Path] = typer.Option(None, "--video", "-v", help="Video file (for sidecar/embedded subs)."),
    track: Optional[int] = typer.Option(None, "--track", help="Embedded subtitle stream index."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max matches to show."),
    min_score: float = typer.Option(60.0, "--min-score", help="Drop matches below this score (0-100)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
):
    """Search subtitles and print ranked matches with timestamps."""
    cues = _resolve(subs, video, track)
    matches = search(query, cues, limit=limit, min_score=min_score, max_span=max_span)
    if not matches:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)
    for rank, m in enumerate(matches):
        _echo_match(rank, m)


@app.command()
def clip(
    query: str = typer.Argument(..., help="Dialogue text to locate and clip."),
    video: Path = typer.Option(..., "--video", "-v", help="Video file to cut from."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (else sidecar/embedded)."),
    track: Optional[int] = typer.Option(None, "--track", help="Embedded subtitle stream index."),
    kind: str = typer.Option("audio", "--type", "-t", help="audio | video | gif."),
    before: float = typer.Option(0.5, "--before", "-b", help="Seconds of padding before the line."),
    after: float = typer.Option(0.5, "--after", "-a", help="Seconds of padding after the line."),
    index: int = typer.Option(0, "--index", "-i", help="Which ranked match to cut (0 = best)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output path (auto-named if omitted)."),
    min_score: float = typer.Option(60.0, "--min-score", help="Drop matches below this score (0-100)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation preview."),
):
    """Find the dialogue and cut a clip around it."""
    if kind not in ("audio", "video", "gif"):
        typer.secho("--type must be audio, video, or gif.", fg="red", err=True)
        raise typer.Exit(code=2)

    cues = _resolve(subs, video, track)
    matches = search(query, cues, limit=max(index + 1, 5), min_score=min_score, max_span=max_span)
    if not matches:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)
    if index >= len(matches):
        typer.secho(f"Only {len(matches)} match(es); index {index} out of range.", fg="red", err=True)
        raise typer.Exit(code=2)

    chosen = matches[index]
    rng = compute_range(chosen, before=before, after=after)

    typer.echo("Selected match:")
    _echo_match(index, chosen)
    typer.echo(
        f"Clip range: {rng.start:.2f}s → {rng.end:.2f}s "
        f"({rng.duration:.2f}s, {kind})"
    )
    if not yes:
        typer.confirm("Cut this clip?", default=True, abort=True)

    try:
        written = cut_clip(video, rng, kind=kind, out=out)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1)
    typer.secho(f"Wrote {written}", fg="green")


@app.command()
def tracks(
    video: Path = typer.Argument(..., help="Video file to inspect."),
):
    """List embedded subtitle tracks in a video container."""
    found = list_embedded_tracks(video)
    if not found:
        typer.secho("No embedded subtitle tracks.", fg="yellow")
        raise typer.Exit(code=1)
    for t in found:
        typer.echo(t.label())


if __name__ == "__main__":
    app()
