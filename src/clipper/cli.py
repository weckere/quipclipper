"""clipper command-line interface.

Examples:
    clipper search "i'll be back" --subs movie.srt
    clipper clip "i'll be back" --video movie.mkv --type audio
    clipper clip "i'll be back" --video movie.mkv --type video --before 5 --after 3
    clipper clip "i'll be back" --video movie.mkv --audio-track 0
    clipper clip "i'll be back" --video movie.mkv --split-channels --split-format wav
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from clipper.clip import compute_range, cut_clip, split_audio_channels
from clipper.models import Match
from clipper.search import search
from clipper.subtitles import (
    ResolvedSubtitles,
    list_embedded_tracks,
    resolve_subtitles,
)

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


def _resolve(subs: Optional[Path], video: Optional[Path], track: Optional[int]) -> ResolvedSubtitles:
    try:
        return resolve_subtitles(subs=subs, video=video, track=track)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)


def _parse_tracks(spec: Optional[str]) -> Optional[list[int]]:
    """Parse a comma-separated audio-track spec like "0,2" into [0, 2]."""
    if not spec:
        return None
    try:
        return [int(x) for x in spec.replace(" ", "").split(",") if x != ""]
    except ValueError:
        raise typer.BadParameter("--audio-track must be comma-separated integers, e.g. 0,2")


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
    cues = _resolve(subs, video, track).cues
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
    lossless: bool = typer.Option(
        True,
        "--lossless/--no-lossless",
        help="Stream-copy with no re-encode (default). --no-lossless re-encodes for exact boundaries.",
    ),
    audio_track: Optional[str] = typer.Option(
        None,
        "--audio-track",
        "-A",
        help="Audio stream(s) to keep, by a:N index, comma-separated (e.g. 0,2). Default: all.",
    ),
    split_channels: bool = typer.Option(
        False,
        "--split-channels",
        help="Split a surround audio track into per-group files (stereo pairs + centre/LFE). Audio only.",
    ),
    split_format: str = typer.Option(
        "wav",
        "--split-format",
        help="Format for --split-channels: wav | flac (lossless) | original (re-encode to source codec).",
    ),
    embed_subs: bool = typer.Option(
        True,
        "--embed-subs/--no-embed-subs",
        help="Mux the subtitle file into video clips (lossless). Embedded video subs are always kept.",
    ),
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
    if split_channels and kind != "audio":
        typer.secho("--split-channels only applies to --type audio.", fg="red", err=True)
        raise typer.Exit(code=2)
    if split_format not in ("wav", "flac", "original"):
        typer.secho("--split-format must be wav, flac, or original.", fg="red", err=True)
        raise typer.Exit(code=2)
    audio_indices = _parse_tracks(audio_track)

    resolved = _resolve(subs, video, track)
    matches = search(query, resolved.cues, limit=max(index + 1, 5), min_score=min_score, max_span=max_span)
    if not matches:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)
    if index >= len(matches):
        typer.secho(f"Only {len(matches)} match(es); index {index} out of range.", fg="red", err=True)
        raise typer.Exit(code=2)

    chosen = matches[index]
    rng = compute_range(chosen, before=before, after=after)

    is_copy = lossless and kind != "gif" and not split_channels
    if split_channels:
        mode = f"channel split ({split_format})"
    else:
        mode = "lossless copy" if is_copy else "re-encode"
    typer.echo("Selected match:")
    _echo_match(index, chosen)
    typer.echo(
        f"Clip range: {rng.start:.2f}s → {rng.end:.2f}s "
        f"({rng.duration:.2f}s, {kind}, {mode})"
    )
    if is_copy:
        typer.secho(
            "  note: lossless start snaps to the nearest keyframe, so the clip "
            "may begin a little earlier (the end is exact). Use --no-lossless "
            "for frame-exact boundaries.",
            fg="bright_black",
        )
    if split_channels:
        typer.secho(
            "  note: splitting channels requires decoding the surround mix, so it "
            "is not a stream copy; wav/flac are lossless from the decode.",
            fg="bright_black",
        )
    if not yes:
        typer.confirm("Cut this clip?", default=True, abort=True)

    try:
        if split_channels:
            written = split_audio_channels(
                video,
                rng,
                audio_index=(audio_indices[0] if audio_indices else 0),
                fmt=split_format,
                out=out,
            )
            for w in written:
                typer.secho(f"Wrote {w}", fg="green")
        else:
            # Embed the (clip-aligned) search subtitles only when we have an
            # external file; embedded video subtitle tracks are always kept.
            cues_to_embed = (
                resolved.cues if (embed_subs and kind == "video" and resolved.path) else None
            )
            written = cut_clip(
                video, rng, kind=kind, lossless=lossless, out=out,
                audio_indices=audio_indices, embed_cues=cues_to_embed,
            )
            typer.secho(f"Wrote {written}", fg="green")
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1)


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
