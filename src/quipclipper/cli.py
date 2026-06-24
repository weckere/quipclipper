"""quipclipper command-line interface.

Examples:
    quipclipper search "i'll be back" --subs movie.srt
    quipclipper clip "i'll be back" --video movie.mkv --type audio
    quipclipper clip "i'll be back" --video movie.mkv --type video --before 5 --after 3
    quipclipper clip "i'll be back" --video movie.mkv --audio-track 0
    quipclipper clip "i'll be back" --video movie.mkv --type audio --audio-format wav
    quipclipper clip "i'll be back" --video movie.mkv --split-channels --split-format wav
    quipclipper clip "you will become one" --video "Book (readaloud).epub"  # EPUB3 audiobook
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Optional

import typer

from quipclipper.clip import (
    DEFAULT_VAAPI_DEVICE,
    ClipRange,
    _timestamp_slug,
    compute_range,
    cut_clip,
    split_audio_channels,
    vaapi_h264_available,
)
from quipclipper.epub import (
    extract_audio_member,
    is_media_overlay_epub,
    read_epub,
    to_cues,
)
from quipclipper.mkv import (
    cut_with_mkvmerge,
    estimate_remux_bytes,
    human_size,
    is_matroska,
    mkvmerge_available,
)
from quipclipper.models import Match
from quipclipper.search import search
from quipclipper.subtitles import (
    AUDIO_EXTS,
    ResolvedSubtitles,
    list_embedded_tracks,
    list_streams,
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


def _resolve(
    subs: Optional[Path], video: Optional[Path], track: Optional[int], langs=None,
) -> ResolvedSubtitles:
    """Resolve cues, mapping engine errors to clean CLI exits.

    When several embedded subtitle tracks *or* sidecar files exist and no
    ``--track`` is given, the engine auto-selects the best dialogue source for
    ``langs`` (preferred language > others; full dialogue > SDH > forced >
    commentary; see ``best_track``/``find_sidecar``). We echo which embedded track
    it landed on; pass ``--track`` to override or ``--langs`` to set the preference.
    """
    try:
        resolved = resolve_subtitles(subs=subs, video=video, track=track, langs=langs)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)
    if track is None and video and resolved.track is not None:
        tracks = list_embedded_tracks(video)
        label = next(
            (t.label() for t in tracks if t.index == resolved.track),
            f"s:{resolved.track}",
        )
        typer.secho(f"  auto-selected subtitle track: {label}", fg="bright_black")
    return resolved


def _parse_tracks(spec: Optional[str]) -> Optional[list[int]]:
    """Parse a comma-separated audio-track spec like "0,2" into [0, 2]."""
    if not spec:
        return None
    try:
        return [int(x) for x in spec.replace(" ", "").split(",") if x != ""]
    except ValueError:
        raise typer.BadParameter("--audio-track must be comma-separated integers, e.g. 0,2")


def _select_matches(candidates: list[Match]) -> list[Match]:
    """Interactively pick one or more matches (non-exclusive multi-select).

    A single candidate is auto-selected. The prompt reads from stdin, so piping a
    selection works too; an empty Enter selects the top match.
    """
    if len(candidates) == 1:
        return list(candidates)
    raw = typer.prompt(
        "Select matches to clip (comma-separated indices, or 'all')", default="0"
    ).strip().lower()
    if raw in ("all", "*"):
        return list(candidates)
    picks: list[Match] = []
    seen: set[int] = set()
    for tok in raw.replace(" ", "").split(","):
        if tok == "":
            continue
        if not tok.isdigit() or int(tok) >= len(candidates):
            raise typer.BadParameter(f"invalid selection: {tok!r}")
        i = int(tok)
        if i not in seen:
            seen.add(i)
            picks.append(candidates[i])
    if not picks:
        raise typer.BadParameter("no matches selected")
    return picks


@app.command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Dialogue text to search for."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (.srt/.vtt/.ass)."),
    video: Optional[Path] = typer.Option(None, "--video", "-v", help="Video file (for sidecar/embedded subs)."),
    track: Optional[int] = typer.Option(None, "--track", help="Subtitle track to use, by s:N index (from `tracks`)."),
    langs: Optional[str] = typer.Option(
        None, "--langs",
        help="Preferred subtitle language(s) for auto-selection, comma-separated (e.g. en,es). "
             "Default: English.",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Max matches to show."),
    min_score: float = typer.Option(60.0, "--min-score", help="Drop matches below this score (0-100)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
):
    """Search subtitles and print ranked matches with timestamps."""
    # An EPUB3 media-overlay audiobook carries its own cues (text + narration
    # timing); search those directly, like the web app does.
    if video is not None and video.suffix.lower() == ".epub" and is_media_overlay_epub(video):
        book = read_epub(video)
        typer.echo(
            f"EPUB audiobook: {book.title or video.stem} "
            f"({len(book.chapters)} chapters, {len(book.cues)} cues)"
        )
        cues = to_cues(book.cues)
    else:
        cues = _resolve(subs, video, track, langs).cues
    matches = search(query, cues, limit=limit, min_score=min_score, max_span=max_span)
    if not matches:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)
    for rank, m in enumerate(matches):
        _echo_match(rank, m)


def _epub_clip_name(epub: Path, start: float, text: str, ext: str) -> Path:
    """Auto-name an EPUB clip ``<book>_<timestamp>_<cue>.<ext>``, next to the epub."""
    slug = re.sub(r"\s+", "_", re.sub(r"[^\w\s'-]", "", text)[:60].strip())
    stem = f"{epub.stem}_{_timestamp_slug(start)}" + (f"_{slug}" if slug else "")
    return epub.with_name(f"{stem}.{ext}")


def _clip_epub(
    video: Path, query: str, *,
    before: float, after: float, index: int, pick: bool, limit: int,
    min_score: float, max_span: int, out: Optional[Path],
    lossless: bool, audio_format: Optional[str], yes: bool,
) -> None:
    """Search an EPUB3 media-overlay audiobook and cut the matched line(s) straight
    out of the embedded narration audio."""
    try:
        book = read_epub(video)
    except ValueError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)
    typer.echo(
        f"EPUB audiobook: {book.title or video.stem}"
        + (f" — {book.author}" if book.author else "")
        + f" ({len(book.chapters)} chapters, {len(book.cues)} cues)"
    )
    candidates = search(
        query, to_cues(book.cues),
        limit=(limit if pick else max(index + 1, 5)),
        min_score=min_score, max_span=max_span,
    )
    if not candidates:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)
    if pick:
        typer.echo(f"{len(candidates)} match(es):")
        for i, m in enumerate(candidates):
            _echo_match(i, m)
        chosen = _select_matches(candidates)
    else:
        if index >= len(candidates):
            typer.secho(
                f"Only {len(candidates)} match(es); index {index} out of range.",
                fg="red", err=True,
            )
            raise typer.Exit(code=2)
        chosen = [candidates[index]]
    if out is not None and len(chosen) > 1:
        typer.secho("--out can't be used when clipping multiple matches.", fg="red", err=True)
        raise typer.Exit(code=2)

    ext = audio_format or ("mka" if lossless else "mp3")
    mode = (
        f"full-mix lossless {audio_format}" if audio_format
        else "lossless copy" if lossless else "re-encode (mp3)"
    )
    typer.echo(f"Will clip {len(chosen)} match(es) (audio, {mode}):")
    for i, m in enumerate(chosen):
        _echo_match(i, m)
    if not yes:
        typer.confirm("Proceed?", default=True, abort=True)

    single = len(chosen) == 1
    written: list[Path] = []
    for m in chosen:
        first = book.cues[m.cues[0].index]
        last = book.cues[m.cues[-1].index]
        if last.audio != first.audio:
            typer.secho(
                "  note: this match spans two audio files; clipping from the first.",
                fg="bright_black",
            )
        rng = ClipRange(start=max(0.0, m.start - before), end=m.end + after)
        out_path = out if single else _epub_clip_name(video, m.start, m.text, ext)
        with tempfile.TemporaryDirectory() as td:
            tmp_audio = extract_audio_member(video, first.audio, Path(td) / Path(first.audio).name)
            written.append(cut_clip(
                tmp_audio, rng, kind="audio", lossless=lossless,
                audio_codec=audio_format, out=out_path,
            ))
    for w in written:
        typer.secho(f"  wrote {w}", fg="green")


@app.command()
def clip(
    query: str = typer.Argument(..., help="Dialogue text to locate and clip."),
    video: Path = typer.Option(..., "--video", "-v", help="Video file to cut from."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (else sidecar/embedded)."),
    track: Optional[int] = typer.Option(None, "--track", help="Subtitle track to use, by s:N index (from `tracks`)."),
    langs: Optional[str] = typer.Option(
        None, "--langs",
        help="Preferred subtitle language(s) for auto-selecting among sidecars/embedded "
             "tracks, comma-separated (e.g. en,es). Prefers the language and deprioritizes "
             "HI/forced/commentary. Default: English.",
    ),
    kind: str = typer.Option("video", "--type", "-t", help="audio | video | gif."),
    lossless: Optional[bool] = typer.Option(
        None,
        "--lossless/--no-lossless",
        help="Default matches the web app: video re-encodes for a frame-exact clip; "
             "audio is stream-copied (already exact). --lossless forces a stream copy "
             "(keyframe-aligned start); --no-lossless forces a re-encode.",
    ),
    audio_track: Optional[str] = typer.Option(
        None,
        "--audio-track",
        "-A",
        help="Audio stream(s) to keep, by a:N index, comma-separated (e.g. 0,2). Default: all.",
    ),
    audio_format: Optional[str] = typer.Option(
        None,
        "--audio-format",
        help="Full-mix lossless audio: wav | flac. Re-encodes one audio stream keeping ALL channels (5.1 stays 5.1) into a single file. Audio only; not with --split-channels. Default: passthrough (--lossless) / mp3 (--no-lossless).",
    ),
    split_channels: bool = typer.Option(
        False,
        "--split-channels",
        help="Split a surround audio track into per-group files (stereo pairs + centre/LFE). Audio only.",
    ),
    split_format: str = typer.Option(
        "wav",
        "--split-format",
        help="Format for --split-channels: wav | flac (lossless, no re-encode) | original (re-encode to source codec).",
    ),
    split_groups: Optional[str] = typer.Option(
        None,
        "--split-groups",
        help="Which channel groups to export with --split-channels: comma-separated subset of front,center,surround,lfe (default: all). E.g. --split-groups center exports only the centre channel.",
    ),
    include_lfe: bool = typer.Option(
        True,
        "--include-lfe/--no-lfe",
        help="Include the LFE channel as its own file when splitting (default). --no-lfe drops it.",
    ),
    backend: str = typer.Option(
        "auto",
        "--backend",
        help="Cutting backend: auto | ffmpeg | mkvmerge. auto uses mkvmerge for lossless MKV cuts.",
    ),
    chapters: bool = typer.Option(
        True,
        "--chapters/--no-chapters",
        help="Keep chapters in mkvmerge output (default). --no-chapters drops them.",
    ),
    hwaccel: Optional[bool] = typer.Option(
        None,
        "--hwaccel/--no-hwaccel",
        help="Hardware-encode a re-encoded video clip on an Intel iGPU (Quick Sync "
             "via VAAPI). Default: auto-detect; --no-hwaccel forces software libx264. "
             "Only affects video re-encodes (not lossless copies, audio, or gif).",
    ),
    vaapi_device: str = typer.Option(
        DEFAULT_VAAPI_DEVICE, "--vaapi-device", help="VAAPI render node for --hwaccel.",
    ),
    remux_first: bool = typer.Option(
        False,
        "--remux-first/--no-remux-first",
        help="Remux source (+ sidecar subs) to a temp MKV with mkvmerge first, then cut — bypasses ffmpeg but copies the full source to a temp file. Off by default; use --remux-first when you want maximum accuracy and have local disk space.",
    ),
    embed_subs: bool = typer.Option(
        True,
        "--embed-subs/--no-embed-subs",
        help="Mux the subtitle file into video clips (lossless). Embedded video subs are always kept.",
    ),
    before: float = typer.Option(2.0, "--before", "-b", help="Seconds of padding before the line."),
    after: float = typer.Option(2.0, "--after", "-a", help="Seconds of padding after the line."),
    index: int = typer.Option(0, "--index", "-i", help="Which ranked match to cut (0 = best). Ignored with --pick."),
    pick: bool = typer.Option(
        False,
        "--pick",
        "-p",
        help="Interactively choose one or more matches to clip (multi-select, e.g. 0,2,3).",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="How many candidate matches to offer with --pick."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output path (auto-named; not allowed when clipping multiple matches)."),
    min_score: float = typer.Option(60.0, "--min-score", help="Drop matches below this score (0-100)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirmation prompts (including the remux disk-space confirmation)."),
):
    """Find the dialogue and cut a clip around it."""
    if kind not in ("audio", "video", "gif"):
        typer.secho("--type must be audio, video, or gif.", fg="red", err=True)
        raise typer.Exit(code=2)
    # An EPUB3 media-overlay book (e.g. Storyteller) is a self-contained
    # text+narration archive: cut a matched line straight out of its embedded
    # audio. Always an audio clip — there's no video and no surround to split.
    if video.suffix.lower() == ".epub" and is_media_overlay_epub(video):
        if split_channels:
            typer.secho("--split-channels doesn't apply to an EPUB audiobook.", fg="red", err=True)
            raise typer.Exit(code=2)
        if kind == "gif":
            typer.secho("Can't cut a gif from an EPUB audiobook.", fg="red", err=True)
            raise typer.Exit(code=2)
        if audio_format is not None and audio_format not in ("wav", "flac"):
            typer.secho("--audio-format must be wav or flac.", fg="red", err=True)
            raise typer.Exit(code=2)
        _clip_epub(
            video, query,
            before=before, after=after, index=index, pick=pick, limit=limit,
            min_score=min_score, max_span=max_span, out=out,
            lossless=(True if lossless is None else lossless),
            audio_format=audio_format, yes=yes,
        )
        return
    # An audio-only source (podcast/audiobook) has no video stream; quietly cut an
    # audio clip instead of asking ffmpeg to map a video that isn't there.
    if kind == "video" and video.suffix.lower() in AUDIO_EXTS:
        kind = "audio"
        typer.secho(f"{video.name} is audio-only — cutting an audio clip.", fg="yellow")
    # Default cut mode mirrors the web app: re-encode video for a frame-exact clip
    # (a lossless copy can only start on a keyframe → runs long on long-GOP
    # sources), but keep audio a lossless stream copy (it's already exact). gif is
    # always re-encoded regardless. Explicit --lossless/--no-lossless overrides.
    if lossless is None:
        lossless = kind != "video"
    if split_channels and kind != "audio":
        typer.secho("--split-channels only applies to --type audio.", fg="red", err=True)
        raise typer.Exit(code=2)
    if split_format not in ("wav", "flac", "original"):
        typer.secho("--split-format must be wav, flac, or original.", fg="red", err=True)
        raise typer.Exit(code=2)
    split_categories: Optional[list[str]] = None
    if split_groups is not None:
        if not split_channels:
            typer.secho("--split-groups only applies with --split-channels.", fg="red", err=True)
            raise typer.Exit(code=2)
        valid = {"front", "center", "surround", "lfe"}
        split_categories = [g.strip().lower() for g in split_groups.split(",") if g.strip()]
        bad = [g for g in split_categories if g not in valid]
        if bad or not split_categories:
            typer.secho(
                f"--split-groups must be a comma-separated subset of {', '.join(sorted(valid))}.",
                fg="red", err=True,
            )
            raise typer.Exit(code=2)
    if audio_format is not None:
        if audio_format not in ("wav", "flac"):
            typer.secho("--audio-format must be wav or flac.", fg="red", err=True)
            raise typer.Exit(code=2)
        if kind != "audio":
            typer.secho("--audio-format only applies to --type audio.", fg="red", err=True)
            raise typer.Exit(code=2)
        if split_channels:
            typer.secho("--audio-format can't be combined with --split-channels.", fg="red", err=True)
            raise typer.Exit(code=2)
    if backend not in ("auto", "ffmpeg", "mkvmerge"):
        typer.secho("--backend must be auto, ffmpeg, or mkvmerge.", fg="red", err=True)
        raise typer.Exit(code=2)
    audio_indices = _parse_tracks(audio_track)

    # Full-mix lossless audio (one WAV/FLAC, all channels) is an ffmpeg re-encode,
    # so it bypasses the mkvmerge/passthrough path.
    fullmix = kind == "audio" and not split_channels and audio_format is not None

    # Decide the backend. mkvmerge only does lossless audio/video cuts (no gif,
    # no re-encode, no channel split, no full-mix); ffmpeg handles everything else.
    mkv_capable = lossless and kind in ("audio", "video") and not split_channels and not fullmix
    if backend == "ffmpeg":
        use_mkvmerge = False
    elif backend == "mkvmerge":
        if not mkv_capable:
            typer.secho(
                "--backend mkvmerge supports only lossless audio/video cuts "
                "(not gif, --no-lossless, or --split-channels).",
                fg="red", err=True,
            )
            raise typer.Exit(code=2)
        if not mkvmerge_available():
            typer.secho("mkvmerge not found on PATH. Install MKVToolNix.", fg="red", err=True)
            raise typer.Exit(code=2)
        use_mkvmerge = True
    else:  # auto — prefer mkvmerge for any lossless audio/video cut when available
        use_mkvmerge = mkv_capable and mkvmerge_available()

    # Remux-first (default) only happens on the mkvmerge path and only adds value
    # for non-Matroska sources — an MKV is already a clean container, so cutting it
    # directly with mkvmerge is just as accurate without the redundant full copy.
    do_remux = use_mkvmerge and remux_first and not is_matroska(video)

    # Hardware-encode the H.264 re-encode on an Intel iGPU (Quick Sync via VAAPI)
    # when available; only a re-encoded *video* clip hits the encoder (lossless
    # copies, audio and gif don't). --hwaccel forces it, --no-hwaccel forces
    # software; default auto-detects. cut_clip retries on libx264 if it fails.
    hw_reencode = (not lossless) and kind == "video" and (
        hwaccel if hwaccel is not None else vaapi_h264_available(vaapi_device)
    )

    resolved = _resolve(subs, video, track, langs)
    candidates = search(
        query, resolved.cues,
        limit=(limit if pick else max(index + 1, 5)),
        min_score=min_score, max_span=max_span,
    )
    if not candidates:
        typer.secho("No matches.", fg="yellow")
        raise typer.Exit(code=1)

    if pick:
        typer.echo(f"{len(candidates)} match(es):")
        for i, m in enumerate(candidates):
            _echo_match(i, m)
        chosen = _select_matches(candidates)
    else:
        if index >= len(candidates):
            typer.secho(
                f"Only {len(candidates)} match(es); index {index} out of range.",
                fg="red", err=True,
            )
            raise typer.Exit(code=2)
        chosen = [candidates[index]]

    if out is not None and len(chosen) > 1:
        typer.secho("--out can't be used when clipping multiple matches.", fg="red", err=True)
        raise typer.Exit(code=2)

    is_copy = lossless and kind != "gif" and not split_channels and not fullmix
    if split_channels:
        mode = f"channel split ({split_format})"
    elif fullmix:
        mode = f"full-mix lossless {audio_format} (all channels)"
    elif use_mkvmerge:
        mode = "lossless copy (mkvmerge, remux-first)" if do_remux else "lossless copy (mkvmerge)"
    else:
        mode = "lossless copy (ffmpeg)" if is_copy else "re-encode"

    ranges = [compute_range(m, before=before, after=after) for m in chosen]
    typer.echo(f"Will clip {len(chosen)} match(es) ({kind}, {mode}):")
    for i, m in enumerate(chosen):
        _echo_match(i, m)

    if do_remux:
        sidecar_inputs = [resolved.path] if (kind == "video" and resolved.path) else []
        estimate = estimate_remux_bytes(video, sidecar_inputs)
        per = " each (run sequentially; temp deleted between clips)" if len(chosen) > 1 else ""
        typer.secho(
            f"  remux-first: muxing the source to a temporary MKV for best accuracy.\n"
            f"  estimated scratch space: ~{human_size(estimate)}{per} "
            f"(temp file, deleted afterward).",
            fg="yellow",
        )
    elif mkv_capable and not use_mkvmerge and not mkvmerge_available():
        # Wanted mkvmerge accuracy but it isn't installed.
        typer.secho(
            "  note: mkvmerge not found — cutting with ffmpeg instead. Install "
            "MKVToolNix for the most accurate cuts.",
            fg="yellow",
        )
    elif mkv_capable and use_mkvmerge and not do_remux and not is_matroska(video):
        # Non-MKV source with remux-first off (the default): brief note.
        typer.secho(
            "  note: non-MKV source — cutting directly. Use --remux-first for "
            "maximum accuracy (copies the full source to a temp MKV first).",
            fg="bright_black",
        )

    if is_copy and not use_mkvmerge:
        typer.secho(
            "  note: ffmpeg lossless start snaps to the nearest keyframe, so the clip "
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
    if fullmix:
        typer.secho(
            "  note: full-mix re-encodes one audio stream to "
            f"{audio_format}, keeping every channel (5.1 stays 5.1). Lossless "
            "from the decode; not a stream copy.",
            fg="bright_black",
        )
    if hw_reencode:
        typer.secho(
            f"  note: hardware-encoding H.264 on the iGPU (h264_vaapi, {vaapi_device}); "
            "falls back to libx264 if it fails.",
            fg="bright_black",
        )
    if not yes:
        typer.confirm("Proceed?", default=True, abort=True)

    def _cut_one(rng, out_path: Optional[Path]) -> list[Path]:
        if split_channels:
            return split_audio_channels(
                video, rng,
                audio_index=(audio_indices[0] if audio_indices else 0),
                fmt=split_format, include_lfe=include_lfe,
                categories=split_categories, out=out_path,
            )
        if use_mkvmerge:
            # mkvmerge muxes/trims a sidecar subtitle natively, so pass the file.
            sidecar = resolved.path if (embed_subs and kind == "video") else None
            return [cut_with_mkvmerge(
                video, rng, kind=kind, out=out_path,
                audio_indices=audio_indices, keep_subs=True, keep_chapters=chapters,
                embed_subs=sidecar, remux_first=do_remux,
            )]
        # ffmpeg: embed the clip-aligned search subtitles when we have a file;
        # embedded video subtitle tracks are always kept.
        cues_to_embed = (
            resolved.cues if (embed_subs and kind == "video" and resolved.path) else None
        )
        return [cut_clip(
            video, rng, kind=kind, lossless=lossless, out=out_path,
            audio_indices=audio_indices, embed_cues=cues_to_embed,
            audio_codec=(audio_format if fullmix else None),
            video_encoder="h264_vaapi" if hw_reencode else "libx264",
            vaapi_device=vaapi_device if hw_reencode else None,
        )]

    single = len(chosen) == 1
    try:
        for rng in ranges:
            for w in _cut_one(rng, out if single else None):
                typer.secho(f"Wrote {w}", fg="green")
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1)


@app.command()
def tracks(
    video: Path = typer.Argument(..., help="Video file to inspect."),
):
    """List the video, audio and subtitle streams in a container.

    Audio/subtitle entries show the a:N / s:N index to pass to --audio-track / --track.
    """
    try:
        found = list_streams(video)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)
    if not found:
        typer.secho("No streams found.", fg="yellow")
        raise typer.Exit(code=1)
    for kind in ("video", "audio", "subtitle"):
        group = [s for s in found if s.kind == kind]
        if not group:
            continue
        typer.secho(f"{kind.capitalize()}:", bold=True)
        for s in group:
            typer.echo(f"  {s.label()}")


if __name__ == "__main__":
    app()
