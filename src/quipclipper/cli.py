"""quipclipper command-line interface.

Every action in the web app is a command here (see ``quipclipper commands`` and
docs/CLI.md). Commands drive a quipclipper web server over its HTTP API when one
is configured (``--server URL`` or ``QC_URL``); ``search``, ``clip``, ``tracks``
and ``status`` also run on local files when there's no server.

Examples:
    quipclipper search "i'll be back" --subs movie.srt
    quipclipper clip "i'll be back" --video movie.mkv --type audio
    quipclipper clip "i'll be back" --video movie.mkv --split-channels --split-format wav
    quipclipper clip "you will become one" --video "Book (readaloud).epub"  # EPUB3 audiobook
    QC_URL=https://quipclipper.example ls /movies
    quipclipper --server https://quipclipper.example search.folder "smalls" --path /movies --json \\
        | quipclipper --server https://quipclipper.example clip.batch --kind audio
"""

from __future__ import annotations

import inspect
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, List, Literal, Optional

import typer

try:  # typer >= 0.27 bundles its own click and no longer installs one
    from typer import _click as click
except ImportError:  # older typer sits on the real click
    import click

from quipclipper import commands as reg
from quipclipper.client import (
    EXIT_REFUSED,
    EXIT_USAGE,
    CliError,
    Context,
    Transport,
    build_request,
    call,
    download_job_files,
    execute,
    load_settings,
    make_context,
    save_settings,
    settings_path,
    wait_job,
)
from quipclipper.clip import (
    DEFAULT_VAAPI_DEVICE,
    ClipRange,
    _timestamp_slug,
    compute_range,
    cut_clip,
    output_extension,
    probe_audio_streams,
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
from quipclipper.naming import clip_template_context, render_clip_template, resolve_template
from quipclipper.search import search
from quipclipper.subtitles import (
    AUDIO_EXTS,
    ResolvedSubtitles,
    list_embedded_tracks,
    list_streams,
    resolve_subtitles,
)
from quipclipper.transcribe import OFF_REASON as STT_OFF
from quipclipper.transcribe import enabled as stt_enabled

app = typer.Typer(
    add_completion=False,
    help="Find and cut clips from movies, shows, podcasts and audiobooks by searching "
         "their dialogue. Every web-app action is a command: run `quipclipper commands`.",
    no_args_is_help=True,
)

# Tests point the CLI at an in-process server by setting this.
TRANSPORT_OVERRIDE: Optional[Transport] = None


@app.callback()
def _main(
    ctx: typer.Context,
    server: Optional[str] = typer.Option(
        None, "--server", "-S", envvar="QC_URL",
        help="quipclipper server URL (or set QC_URL). Token: QC_API_TOKEN or "
             "QC_API_TOKEN_FILE."),
    local: bool = typer.Option(False, "--local", help="Run on local files even when a "
                                                      "server is configured."),
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    safe: bool = typer.Option(False, "--safe", help="Refuse commands that change or delete "
                                                    "data (marked ⚠). Use it for agents."),
):
    ctx.obj = make_context(server=server, local=local, json_out=json_out, safe=safe,
                           transport=TRANSPORT_OVERRIDE)


def _c(ctx: typer.Context) -> Context:
    if ctx.obj is None:  # invoked without the group callback (direct function call)
        ctx.obj = make_context(server=None, local=False, json_out=False, safe=False,
                               transport=TRANSPORT_OVERRIDE)
    return ctx.obj


def _fail(c: Context, exc: CliError) -> None:
    if c.json:
        typer.echo(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
    else:
        typer.secho(str(exc), fg="red", err=True)
    raise typer.Exit(code=exc.exit_code)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


# --- local helpers (unchanged from the original CLI) -------------------------------------


# Machine transcript lines (M21) are marked wherever they are printed.
AUTO_MARK = "[auto] "


def _echo_match(rank: int, m: Match) -> None:
    typer.echo(
        f"[{rank}] {m.start:>8.2f}s  "
        f"{typer.style(m.cues[0].start_ts, fg='cyan')}–{m.cues[-1].end_ts}  "
        f"(score {m.score:.0f})"
    )
    typer.echo(f"      {AUTO_MARK if m.machine else ''}{m.text}")


def _match_dict(rank: int, m: Match) -> dict:
    """A local match in the same shape the server's search returns."""
    return {
        "index": rank, "score": round(m.score, 1), "text": m.text,
        "start": m.start, "end": m.end,
        "start_ts": m.cues[0].start_ts, "end_ts": m.cues[-1].end_ts,
        "cue_count": len(m.cues), "machine": m.machine,
    }


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
        typer.secho(f"  auto-selected subtitle track: {label}", fg="bright_black", err=True)
    return resolved


def _local_cues(subs: Optional[Path], video: Optional[Path], track: Optional[int], langs,
                machine: str = "include") -> list:
    """A local file's cues for search: its subtitles, with the machine transcript lines
    (this machine's transcript store, and a ….whisper.srt copy next to the file) mixed
    in, left out, or alone. A file with only a transcript has no subtitles to resolve.
    While transcription is switched off (D-040) machine lines are always left out."""
    from quipclipper import transcribe as tr

    if not stt_enabled():
        machine = "exclude"
    heard = (tr.machine_cues(video, tr.TranscriptStore(tr.default_store_dir()))
             if video is not None and subs is None and machine != "exclude" else [])
    if heard and machine == "only":
        return heard
    if heard:
        try:
            real = resolve_subtitles(subs=subs, video=video, track=track, langs=langs).cues
        except (ValueError, FileNotFoundError, RuntimeError):
            real = []
        return tr.merge_cues(real, heard)
    if machine == "only":
        return []
    return _resolve(subs, video, track, langs).cues


def _parse_tracks(spec: Optional[str]) -> Optional[list[int]]:
    """Parse a comma-separated audio-track spec like "0,2" into [0, 2].

    Indices are 0-based a:N selectors, so negatives are rejected here — otherwise
    ``-1`` would slip through to ``-map 0:a:-1`` (which ffmpeg refuses) and, worse,
    index a per-stream codec list from the end.
    """
    if not spec:
        return None
    try:
        tracks = [int(x) for x in spec.replace(" ", "").split(",") if x != ""]
    except ValueError:
        raise typer.BadParameter("--audio-track must be comma-separated integers, e.g. 0,2")
    if any(t < 0 for t in tracks):
        raise typer.BadParameter("--audio-track indices must be 0 or greater (a:N is 0-based).")
    return tracks


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


def _probe_duration(path: Path) -> Optional[float]:
    """File duration in seconds via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True,
        ).stdout.strip()
        return float(out) if out and out != "N/A" else None
    except (OSError, ValueError):
        return None


# --- server-mode plumbing ------------------------------------------------------------------


def _reject_local_only(cmd: reg.Command, given: dict[str, Any]) -> None:
    for p in cmd.params:
        if p.local_only and given.get(p.name) not in (None, False, p.default):
            raise CliError(
                f"--{p.name.replace('_', '-')} only works on local files; drop it or pass "
                "--local.", code="usage", exit_code=EXIT_USAGE)


def _run_server(c: Context, cmd_id: str, values: dict[str, Any]) -> None:
    cmd = reg.BY_ID[cmd_id]
    try:
        _reject_local_only(cmd, values)
        data = execute(c, cmd, values)
    except OSError as exc:
        _fail(c, CliError(str(exc), code="io"))
    except CliError as exc:
        _fail(c, exc)
    _print(c, cmd, data)
    _exit_for(cmd, data)


def _exit_for(cmd: reg.Command, data: Any) -> None:
    """Exit 1 for a search with no matches, as a local search does (docs/CLI.md)."""
    if cmd.id in ("search.item", "search.folder") and isinstance(data, dict) \
            and not data.get("matches"):
        raise typer.Exit(code=1)


# --- output ---------------------------------------------------------------------------------


def _print(c: Context, cmd: reg.Command, data: Any) -> None:
    if c.json:
        typer.echo(_dump(data))
        return
    if cmd.response == "lines":
        return  # already streamed
    fmt = _FORMATTERS.get(cmd.id)
    if fmt is not None and isinstance(data, (dict, list)):
        fmt(data)
    elif isinstance(data, str):
        typer.echo(data.rstrip("\n"))
    else:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _fmt_entries(data: dict) -> None:
    entries = data.get("entries") or []
    if not entries:
        typer.secho("(empty)", fg="yellow")
    for e in entries:
        kind = e.get("type") or e.get("kind") or ("dir" if e.get("is_dir") else "file")
        icon = {"dir": "📁", "folder": "📁", "epub": "📚", "audio": "🎵",
                "youtube": "▶", "video": "🎬"}.get(kind, "  ")
        # The dialogue index (library.browse): "indexed", or "no subs" when indexing found none.
        state = {"indexed": typer.style("  [indexed]", fg="green"),
                 "no_subs": typer.style("  [no subs]", fg="yellow")}.get(e.get("index_state"), "")
        typer.echo(f"{icon} {e.get('name', '')}{state}  {typer.style(e.get('path', ''), dim=True)}")


def _fmt_matches(data: dict) -> None:
    matches = data.get("matches") or []
    if not matches:
        typer.secho("No matches.", fg="yellow")
        return
    if data.get("capped"):
        typer.secho(f"(searched the first {data.get('files_scanned')} files only)",
                    fg="yellow", err=True)
    for m in matches:
        where = f"{m['file']}  " if m.get("file") else ""
        typer.echo(f"[{m.get('index', '?')}] {where}"
                   f"{typer.style(m.get('start_ts', ''), fg='cyan')}–{m.get('end_ts', '')}  "
                   f"(score {m.get('score', 0):.0f})")
        typer.echo(f"      {AUTO_MARK if m.get('machine') else ''}{m.get('text', '')}")


def _fmt_job(job: dict) -> None:
    typer.echo(f"{job.get('id')}  {job.get('status')}  {job.get('label', '')}")
    if job.get("progress"):
        typer.echo(f"  progress: {job['progress']}")
    for f in job.get("files") or []:
        typer.echo(f"  file: {f.get('name')} ({human_size(f.get('size', 0))})")
    for s in job.get("saved") or []:
        typer.secho(f"  saved {s}", fg="green")
    result = job.get("result")
    if isinstance(result, dict) and "link" not in result:  # e.g. clips.backfill's counts
        typer.echo("  " + ", ".join(f"{k} {v}" for k, v in result.items()))
    if job.get("error"):
        typer.secho(f"  error: {job['error']}", fg="red")
    if job.get("job_id") and not job.get("id"):
        typer.echo(f"job {job['job_id']} {job.get('status', 'queued')} "
                   "(follow it with `quipclipper job.wait <id>`)")


def _fmt_jobs(data: dict) -> None:
    for j in data.get("jobs") or []:
        _fmt_job(j)


def _fmt_bookmarks(data: dict) -> None:
    for b in data.get("bookmarks") or []:
        typer.echo(f"{b.get('id')}  {b.get('source_name') or b.get('path')}  "
                   f"{b.get('start', 0):.2f}–{b.get('end', 0):.2f}s  {b.get('label', '')}")


def _fmt_clips(data: dict) -> None:
    if not data.get("recursive"):
        for f in data.get("folders") or []:
            typer.echo(f"📁 {f}")
    for clip in data.get("clips") or []:
        icon = {"audio": "🎵", "video": "🎬", "gif": "🎞", "set": "🗂"}.get(clip.get("kind"), "  ")
        meta = clip.get("meta") or {}
        dur = meta.get("duration")
        extra = f"{dur:.1f}s, " if isinstance(dur, (int, float)) else ""
        name = clip.get("clip") or clip.get("name")
        title = clip.get("title")
        said = f"  “{title}”" if title and meta.get("cue") else ""
        typer.echo(f"{icon} {name}  ({extra}{human_size(clip.get('size', 0))}){said}")
    if data.get("cursor"):
        typer.secho(f"cursor {data['cursor']} (--since for what's added after this)", dim=True)


def _fmt_script(data: Any) -> None:
    for cue in data if isinstance(data, list) else []:
        speaker = f"{cue['speaker']}: " if cue.get("speaker") else ""
        auto = AUTO_MARK if cue.get("source") == "machine" else ""
        typer.echo(f"{cue.get('start', 0):>9.2f}  {auto}{speaker}{cue.get('text', '')}")


def _fmt_item(data: dict) -> None:
    duration = data.get("duration")
    typer.echo(f"{data.get('name') or data.get('path')}  "
               + (f"{duration:.1f}s" if isinstance(duration, (int, float)) else ""))
    for kind in ("video", "audio", "subtitle"):
        group = [s for s in data.get("streams") or [] if s.get("kind") == kind]
        if group:
            typer.secho(f"{kind.capitalize()}:", bold=True)
            for s in group:
                typer.echo(f"  {s.get('label') or s}")


def _fmt_health(data: dict) -> None:
    tools = "  ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in (data.get("tools") or {}).items())
    where = data.get("where", "server")
    typer.echo(f"{data.get('service', 'quipclipper')} v{data.get('version', '?')} "
               f"({where}) — {tools}")


def _fmt_sessions(data: dict) -> None:
    rows = data.get("sessions") or []
    if not rows:
        typer.echo("Nothing is playing on Jellyfin.")
    for r in rows:
        what = r.get("title") or "?"
        if r.get("series"):
            what = f"{r['series']} S{r.get('season') or 0:02d}E{r.get('episode') or 0:02d} {what}"
        state = "paused" if r.get("paused") else "playing"
        typer.echo(f"{r.get('id')}  {r.get('device')} ({r.get('user')})  {state} at "
                   f"{format_clock(r.get('position') or 0)}  {what}")
        typer.secho(f"  {r.get('path') or r.get('note')}", dim=True)


def _fmt_jf_index(index: dict) -> str:
    if index.get("error"):
        return f"index: {index['error']}"
    built = index.get("built") or "not built yet"
    return (f"index: {index.get('files', 0)} files, {index.get('folders', 0)} folders, "
            f"built {built}" + (" (rebuilding)" if index.get("building") else ""))


def _fmt_lookup(data: dict) -> None:
    item = data.get("item")
    if not item:
        typer.echo(f"Jellyfin doesn't know {data.get('path')}.")
    else:
        what = item.get("title") or "?"
        if item.get("type") == "Episode" and item.get("show"):
            what = (f"{item['show']} S{item.get('season') or 0:02d}"
                    f"E{item.get('episode') or 0:02d} {what}")
        year = f" ({item['year']})" if item.get("year") else ""
        typer.echo(f"{item.get('type') or 'Item'} {item.get('id')}: {what}{year}")
        ids = "  ".join(f"{k}={v}" for k, v in (item.get("provider_ids") or {}).items())
        if ids:
            typer.echo(f"  {ids}")
        if item.get("jellyfin_path"):
            typer.secho(f"  {item['jellyfin_path']}", dim=True)
    naming = data.get("naming")
    if naming:
        typer.echo("  names: " + "  ".join(f"{{{k}}}={v}" for k, v in naming.items() if v))
    typer.secho("  " + _fmt_jf_index(data.get("index") or {}), dim=True)


def _fmt_reindex(data: dict) -> None:
    typer.echo(_fmt_jf_index(data.get("index") or {}))


def _fmt_settings(data: dict) -> None:
    values = data.get("settings", data)
    if data.get("file"):
        typer.secho(data["file"], dim=True)
    for k, v in values.items():
        typer.echo(f"{k} = {v}" if v else f"{k} (not set)")


def _fmt_link(data: dict) -> None:
    typer.echo(data.get("link") or "")


def _fmt_transcribe(data: dict) -> None:
    if "status" in data:  # a server job
        _fmt_job(data)
        data = data.get("result") or {}
    if data.get("note"):
        typer.secho(data["note"], fg="yellow")
    if "files" in data:  # a folder: one line per file
        for f in data["files"]:
            typer.echo(f"  {f.get('new_lines', 0):>4} new  {f['name']}"
                       + (f"  ({f['error']})" if f.get("error") else ""))
    new = data.get("new_lines")
    if isinstance(new, list):  # run here: the lines themselves
        for line in new:
            typer.echo(f"{line['start']:>9.2f}  {AUTO_MARK}{line['text']}")
    elif new and "files" not in data:  # a server job: how many
        typer.echo(f"{new} new machine line{'' if new == 1 else 's'}")
    t = data.get("transcript")
    if t:
        typer.secho(f"{t['lines']} machine lines over {format_clock(t['covered'])} "
                    f"({', '.join(t['models'])})", dim=True)


def _fmt_transcript(data: dict) -> None:
    t = data.get("transcript") or data.get("stored")  # run here / a server
    if t:
        typer.secho(f"Transcribed {format_clock(t['covered'])} with {', '.join(t['models'])}: "
                    f"{t['lines']} lines", bold=True)
    elif not data.get("lines"):
        typer.secho("No machine transcript.", fg="yellow")
    if data.get("library"):
        typer.secho("In the library: " + ", ".join(data["library"]), dim=True)
    for line in data.get("lines") or []:
        typer.echo(f"{line['start']:>9.2f}  {AUTO_MARK}{line['text']}")


_FORMATTERS = {
    "item.transcribe": _fmt_transcribe, "index.transcribe": _fmt_transcribe,
    "item.transcript": _fmt_transcript,
    "library.browse": _fmt_entries, "library.find": _fmt_entries,
    "search.item": _fmt_matches, "search.folder": _fmt_matches,
    "job.get": _fmt_job, "job.wait": _fmt_job, "job.list": _fmt_jobs,
    "clip.create": _fmt_job, "clip.nowplaying": _fmt_job, "clips.backfill": _fmt_job, "youtube.add": _fmt_job, "youtube.download": _fmt_job,
    "item.fetch-subs": _fmt_job, "index.fetch-subs": _fmt_job,
    "bookmark.list": _fmt_bookmarks, "clips.list": _fmt_clips,
    "item.script": _fmt_script, "item.info": _fmt_item, "server.health": _fmt_health,
    "jellyfin.sessions": _fmt_sessions, "jellyfin.lookup": _fmt_lookup,
    "jellyfin.reindex": _fmt_reindex, "item.link": _fmt_link,
    "settings.list": _fmt_settings, "settings.get": _fmt_settings,
    "settings.set": _fmt_settings,
}


# --- search / clip / tracks / status: server or local -------------------------------------


@app.command(name="search")
def search_cmd(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Dialogue text to search for."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (.srt/.vtt/.ass). Local only."),
    video: Optional[Path] = typer.Option(None, "--video", "-v", "--path", help="Media file (on the server, in server mode)."),
    track: Optional[int] = typer.Option(None, "--track", help="Subtitle track to use, by s:N index (from `tracks`)."),
    langs: Optional[str] = typer.Option(
        None, "--langs",
        help="Preferred subtitle language(s) for auto-selection, comma-separated (e.g. en,es). "
             "Default: English. Local only.",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Max matches to show."),
    min_score: float = typer.Option(
        60.0, "--min-score", help="Drop matches below this score (0-100; 100 is the words "
                                  "verbatim, 75 and under the words in any order)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
    machine: Literal["include", "exclude", "only"] = typer.Option(
        "include", "--machine", hidden=not stt_enabled(),
        help="Machine transcript lines (whisper): include them (marked [auto]), exclude "
             "them, or search only them."),
):
    """Search one file's dialogue and print ranked matches with timestamps."""
    c = _c(ctx)
    if machine == "only" and not c.remote and not stt_enabled():
        _fail(c, CliError(STT_OFF, code="usage", exit_code=EXIT_USAGE))
    if subs is not None and machine == "only":
        _fail(c, CliError("--machine only searches a file's machine transcript: give the "
                          "media file (--video), not --subs.", code="usage",
                          exit_code=EXIT_USAGE))
    if c.remote:
        if video is None:
            _fail(c, CliError("--video/--path is required with a server.", code="usage",
                              exit_code=EXIT_USAGE))
        _run_server(c, "search.item", {
            "query": query, "path": str(video), "track": track, "limit": limit,
            "min_score": min_score, "max_span": max_span, "machine": machine, "subs": subs,
            "langs": langs})
        return
    # An EPUB3 media-overlay audiobook carries its own cues (text + narration
    # timing); search those directly, like the web app does.
    if video is not None and video.suffix.lower() == ".epub" and is_media_overlay_epub(video):
        book = read_epub(video)
        if not c.json:
            typer.echo(
                f"EPUB audiobook: {book.title or video.stem} "
                f"({len(book.chapters)} chapters, {len(book.cues)} cues)"
            )
        cues = to_cues(book.cues)
    else:
        cues = _local_cues(subs, video, track, langs or c.settings.get("sub_langs"), machine)
    matches = search(query, cues, limit=limit, min_score=min_score, max_span=max_span)
    if c.json:
        typer.echo(_dump({"query": query, "count": len(matches),
                          "matches": [_match_dict(i, m) for i, m in enumerate(matches)]}))
        if not matches:
            raise typer.Exit(code=1)
        return
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
) -> list[Path]:
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
        # Default the output next to the *epub* (not next to tmp_audio, which
        # lives in the TemporaryDirectory below and would be deleted on exit).
        out_path = out if out is not None else _epub_clip_name(video, m.start, m.text, ext)
        with tempfile.TemporaryDirectory() as td:
            tmp_audio = extract_audio_member(video, first.audio, Path(td) / Path(first.audio).name)
            written.append(cut_clip(
                tmp_audio, rng, kind="audio", lossless=lossless,
                audio_codec=audio_format, out=out_path,
            ))
    for w in written:
        typer.secho(f"  wrote {w}", fg="green")
    return written


@app.command()
def clip(
    ctx: typer.Context,
    query: Optional[str] = typer.Argument(None, help="Dialogue text to locate and clip (or use --start/--end)."),
    video: Path = typer.Option(..., "--video", "-v", "--path", help="Media file to cut from (on the server, in server mode)."),
    subs: Optional[Path] = typer.Option(None, "--subs", "-s", help="Subtitle file (else sidecar/embedded). Local only."),
    track: Optional[int] = typer.Option(None, "--track", help="Subtitle track to use, by s:N index (from `tracks`)."),
    langs: Optional[str] = typer.Option(
        None, "--langs",
        help="Preferred subtitle language(s) for auto-selecting among sidecars/embedded "
             "tracks, comma-separated (e.g. en,es). Prefers the language and deprioritizes "
             "HI/forced/commentary. Default: English. Local only.",
    ),
    start: Optional[float] = typer.Option(None, "--start", help="Clip start in seconds, instead of a dialogue query."),
    end: Optional[float] = typer.Option(None, "--end", help="Clip end in seconds with --start. Default: end of file."),
    cue_text: Optional[str] = typer.Option(None, "--cue-text", help="Dialogue for the {cue} name token with --start/--end."),
    kind: str = typer.Option("video", "--type", "-t", "--kind", help="audio | video | gif."),
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
        "--audio-tracks",
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
             "Only affects video re-encodes (not lossless copies, audio, or gif). Local only.",
    ),
    vaapi_device: str = typer.Option(
        DEFAULT_VAAPI_DEVICE, "--vaapi-device", help="VAAPI render node for --hwaccel. Local only.",
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
    default_sub_track: Optional[int] = typer.Option(None, "--default-sub-track", help="Subtitle track (s:N) to mark as the default in a video clip. Server mode."),
    template: Optional[str] = typer.Option(None, "--template", help="Output name template, e.g. '{show}/S{season}E{episode}_{title}_{timestamp}' (tokens in docs/CLI.md). Default: the clip_template setting."),
    before: float = typer.Option(2.0, "--before", "-b", help="Seconds of padding before the line."),
    after: float = typer.Option(2.0, "--after", "-a", help="Seconds of padding after the line."),
    index: int = typer.Option(0, "--index", "-i", "--match-index", help="Which ranked match to cut (0 = best). Ignored with --pick."),
    pick: bool = typer.Option(
        False,
        "--pick",
        "-p",
        help="Interactively choose one or more matches to clip (multi-select, e.g. 0,2,3). Local only.",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="How many candidate matches to offer with --pick."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output path (auto-named; not allowed when clipping multiple matches). Local only."),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir", help="Server mode: download the finished clip here. Local mode: base folder for --template names."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Server mode: wait for the clip job to finish."),
    min_score: float = typer.Option(
        60.0, "--min-score", help="Drop matches below this score (0-100; 100 is the words "
                                  "verbatim, 75 and under the words in any order)."),
    max_span: int = typer.Option(3, "--max-span", help="Max consecutive captions a match may join."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirmation prompts (including the remux disk-space confirmation)."),
):
    """Cut a clip around a line of dialogue (or between --start and --end)."""
    c = _c(ctx)
    if kind not in ("audio", "video", "gif"):
        typer.secho("--type must be audio, video, or gif.", fg="red", err=True)
        raise typer.Exit(code=2)
    if query is None and start is None:
        typer.secho("Give a dialogue QUERY or --start.", fg="red", err=True)
        raise typer.Exit(code=2)
    template = (template or "").strip() or c.settings.get("clip_template") or None
    if template and not c.remote:
        template = resolve_template(template)  # an earlier default reads as today's
    if c.remote:
        if kind == "video" and video.suffix.lower() in AUDIO_EXTS:
            kind = "audio"  # as the server (and local mode) do, before picking lossless
        _run_server(c, "clip.create", {
            "query": query, "path": str(video), "start": start, "end": end,
            "match_index": index, "track": track, "cue_text": cue_text, "kind": kind,
            # Sent explicitly: the server defaults to lossless, the CLI to the
            # per-kind default (re-encode video, copy audio).
            "lossless": (kind != "video") if lossless is None else lossless,
            "audio_format": audio_format, "before": before, "after": after,
            "audio_tracks": audio_track, "backend": backend, "chapters": chapters,
            "remux_first": remux_first, "embed_subs": embed_subs,
            "split_channels": split_channels, "split_format": split_format,
            "split_groups": split_groups, "include_lfe": include_lfe,
            "template": template, "default_sub_track": default_sub_track,
            "wait": wait, "out_dir": str(out_dir) if out_dir else None,
            "subs": subs, "langs": langs, "pick": pick, "out": out,
            "hwaccel": hwaccel, "yes": False, "limit": limit, "min_score": min_score,
            "max_span": max_span, "vaapi_device": vaapi_device,
        })
        return
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
        if query is None:
            typer.secho("An EPUB audiobook is clipped by dialogue: give a QUERY.", fg="red", err=True)
            raise typer.Exit(code=2)
        written = _clip_epub(
            video, query,
            before=before, after=after, index=index, pick=pick, limit=limit,
            min_score=min_score, max_span=max_span, out=out,
            lossless=(True if lossless is None else lossless),
            audio_format=audio_format, yes=yes,
        )
        if c.json:
            typer.echo(_dump({"files": [str(w) for w in written]}))
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
    else:  # auto — mkvmerge for a lossless audio cut when available, as the server does
        use_mkvmerge = mkv_capable and mkvmerge_available()
        # Not for lossless VIDEO: mkvmerge can only end a kept range on a keyframe, so on
        # a long-GOP source it runs well past the cut (a 3 s line became 22 s). ffmpeg's
        # copy ends exactly; the start still snaps to the keyframe before (207d7c8).
        if use_mkvmerge and kind == "video":
            use_mkvmerge = False

    # Remux-first only happens on the mkvmerge path and only adds value for
    # non-Matroska sources — an MKV is already a clean container, so cutting it
    # directly with mkvmerge is just as accurate without the redundant full copy.
    do_remux = use_mkvmerge and remux_first and not is_matroska(video)

    # Hardware-encode the H.264 re-encode on an Intel iGPU (Quick Sync via VAAPI)
    # when available; only a re-encoded *video* clip hits the encoder (lossless
    # copies, audio and gif don't). --hwaccel forces it, --no-hwaccel forces
    # software; default auto-detects. cut_clip retries on libx264 if it fails.
    hw_reencode = (not lossless) and kind == "video" and (
        hwaccel if hwaccel is not None else vaapi_h264_available(vaapi_device)
    )

    resolved: Optional[ResolvedSubtitles] = None
    if start is not None:
        # An explicit range, like the web app's Start/End selection: no search.
        clip_end = end if end is not None else _probe_duration(video)
        if clip_end is None:
            typer.secho("Couldn't read the file's duration; pass --end.", fg="red", err=True)
            raise typer.Exit(code=2)
        chosen_texts = [cue_text or ""]
        ranges = [ClipRange(start=max(0.0, start - before), end=clip_end + after)]
        chosen: list[Match] = []
        if not c.json:
            typer.echo(f"Will clip {start:.2f}s–{clip_end:.2f}s ({kind}).")
        # Embedding subtitles still wants the resolved cues when a file exists.
        if embed_subs and kind == "video" and subs is not None:
            resolved = _resolve(subs, video, track, langs)
    else:
        resolved = _resolve(subs, video, track, langs or c.settings.get("sub_langs"))
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
        chosen_texts = [m.text for m in chosen]
        ranges = [compute_range(m, before=before, after=after) for m in chosen]

    if out is not None and len(ranges) > 1:
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

    if chosen:
        typer.echo(f"Will clip {len(chosen)} match(es) ({kind}, {mode}):")
        for i, m in enumerate(chosen):
            _echo_match(i, m)

    if do_remux:
        sidecar_inputs = [resolved.path] if (kind == "video" and resolved and resolved.path) else []
        estimate = estimate_remux_bytes(video, sidecar_inputs)
        per = " each (run sequentially; temp deleted between clips)" if len(ranges) > 1 else ""
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
        sub_path = resolved.path if resolved else None
        if use_mkvmerge:
            # mkvmerge muxes/trims a sidecar subtitle natively, so pass the file.
            sidecar = sub_path if (embed_subs and kind == "video") else None
            return [cut_with_mkvmerge(
                video, rng, kind=kind, out=out_path,
                audio_indices=audio_indices, keep_subs=True, keep_chapters=chapters,
                embed_subs=sidecar, remux_first=do_remux,
            )]
        # ffmpeg: embed the clip-aligned search subtitles when we have a file;
        # embedded video subtitle tracks are always kept.
        cues_to_embed = (
            resolved.cues if (resolved and embed_subs and kind == "video" and sub_path) else None
        )
        return [cut_clip(
            video, rng, kind=kind, lossless=lossless, out=out_path,
            audio_indices=audio_indices, embed_cues=cues_to_embed,
            audio_codec=(audio_format if fullmix else None),
            video_encoder="h264_vaapi" if hw_reencode else "libx264",
            vaapi_device=vaapi_device if hw_reencode else None,
        )]

    single = len(ranges) == 1

    def _auto_out(rng, claimed: set[str], cue: str) -> Path:
        """Build a unique auto-named output for a clip.

        Auto-naming rounds the start to a whole second (``_timestamp_slug``), so
        two chosen matches whose padded starts land in the same second would
        otherwise resolve to the same filename and silently clobber each other.
        Append ``_2``/``_3``… to the stem until the name is unused this run. Split
        output is a no-extension prefix (``split_audio_channels`` appends the
        channel label + extension); everything else gets a concrete extension.
        With a ``--template`` the name comes from the template, under
        ``--out-dir`` (default: the source's folder).
        """
        if template:
            rel = render_clip_template(template, clip_template_context(video, rng, cue))
            stem_path = Path(out_dir or video.parent).expanduser() / (rel or _timestamp_slug(rng.start))
            stem_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            stem_path = video.with_name(f"{video.stem}_{_timestamp_slug(rng.start)}")
        if split_channels:
            ext = ""  # prefix only; the channel label + ext are added downstream
        elif use_mkvmerge:
            ext = "mka" if kind == "audio" else "mkv"
        elif fullmix:
            ext = audio_format  # wav | flac
        else:
            codecs = None
            if lossless and kind == "audio":
                codecs = probe_audio_streams(video)
                if audio_indices:
                    codecs = [codecs[i] for i in audio_indices if i < len(codecs)]
            ext = output_extension(kind, lossless=lossless, audio_codecs=codecs)
        base = stem_path.name
        n = 1
        while (base + (f".{ext}" if ext else "")).lower() in claimed or (
                template and stem_path.with_name(base + (f".{ext}" if ext else "")).exists()):
            n += 1
            base = f"{stem_path.name}_{n}"
        name = base + (f".{ext}" if ext else "")
        claimed.add(name.lower())
        return stem_path.with_name(name)

    claimed: set[str] = set()
    written: list[str] = []
    try:
        for rng, cue in zip(ranges, chosen_texts):
            if out is not None:
                out_path: Optional[Path] = out
            elif single and not template:
                out_path = None
            else:
                out_path = _auto_out(rng, claimed, cue)
            for w in _cut_one(rng, out_path):
                written.append(str(w))
                if not c.json:
                    typer.secho(f"Wrote {w}", fg="green")
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1)
    if c.json:
        typer.echo(_dump({"files": written}))


@app.command()
def tracks(
    ctx: typer.Context,
    video: Path = typer.Argument(..., help="Media file to inspect (on the server, in server mode)."),
    langs: Optional[str] = typer.Option(None, "--langs", help="Server mode: preferred subtitle languages for picking the best track."),
):
    """List the video, audio and subtitle streams in a file.

    Audio/subtitle entries show the a:N / s:N index to pass to --audio-track / --track.
    """
    c = _c(ctx)
    if c.remote:
        _run_server(c, "item.info", {"path": str(video),
                                     "langs": langs or c.settings.get("sub_langs")})
        return
    try:
        found = list_streams(video)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2)
    if c.json:
        typer.echo(_dump({"path": str(video), "streams": [
            {"kind": s.kind, "label": s.label(), **{k: v for k, v in vars(s).items()
                                                   if isinstance(v, (str, int, float, bool))}}
            for s in found]}))
        if not found:
            raise typer.Exit(code=1)
        return
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


@app.command()
def status(ctx: typer.Context):
    """Show the version and which media tools are installed (the server's, or this machine's)."""
    c = _c(ctx)
    if c.remote:
        _run_server(c, "server.health", {})
        return
    from quipclipper import __version__

    data = {"service": "quipclipper", "version": __version__, "where": "local",
            "tools": {t: shutil.which(t) is not None
                      for t in ("ffmpeg", "ffprobe", "mkvmerge", "yt-dlp")}}
    _print(c, reg.BY_ID["server.health"], data)


# Registry ids for the four commands above (same function, both names).
app.command(name="search.item", help=search_cmd.__doc__)(search_cmd)
app.command(name="clip.create", help=clip.__doc__)(clip)
app.command(name="item.info", help=tracks.__doc__)(tracks)
app.command(name="server.health", help=status.__doc__)(status)
_HANDWRITTEN = {"search.item", "clip.create", "item.info", "server.health"}


# --- client-side commands --------------------------------------------------------------------


def _read_items(text: str) -> list[dict]:
    """Rows from stdin: one JSON document (a list, or an object holding one), or
    JSON lines. Accepts the --json output of search/bookmark/clips commands."""
    text = text.strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if doc is None:
        if "\n" not in text:
            raise CliError(f"stdin isn't JSON: {text[:80]!r}. Pipe in another command's "
                           "--json output.", code="usage", exit_code=EXIT_USAGE)
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.extend(_read_items(line))
        return rows
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        for key in ("matches", "bookmarks", "clips", "entries", "jobs"):
            if isinstance(doc.get(key), list):
                return [r for r in doc[key] if isinstance(r, dict)]
        return [doc]
    return []


def _batch_clip_values(item: dict, shared: dict, default_path: Optional[str]) -> dict:
    """One clip.create body for a stdin row, the way the web app's batch bars build it."""
    path = item.get("path") or default_path
    if not path:
        raise CliError("a row has no path; pass --path for search.item results",
                       code="usage", exit_code=EXIT_USAGE)
    values = dict(shared, path=path, wait=False)
    if "start" in item:  # a search hit or a bookmark
        values.update(start=item["start"], end=item.get("end"),
                      before=item.get("before", 0) or 0, after=item.get("after", 0) or 0,
                      cue_text=item.get("cue") or item.get("text") or None)
    else:  # a clips-library entry: the whole file
        values.update(start=0, before=0, after=0)
    return values


def _client_clip_batch(c: Context, v: dict) -> Any:
    rows = _read_items(sys.stdin.read())
    if not rows:
        raise CliError("No rows on stdin. Pipe in the --json output of search.folder, "
                       "bookmark.list or clips.list.", code="usage", exit_code=EXIT_USAGE)
    shared = {p.name: v.get(p.name) for p in reg._CLIP_OPTIONS}
    # The web app's batch bars default to a lossless copy (Exact unchecked).
    if shared.get("lossless") is None:
        shared["lossless"] = True
    shared["template"] = shared.get("template") or c.settings.get("clip_template")
    cmd = reg.BY_ID["clip.create"]
    jobs = []
    for item in rows:
        jobs.append(execute(c, cmd, _batch_clip_values(item, shared, v.get("path"))))
    if not v.get("wait", True):
        return {"jobs": jobs}
    out_dir = v.get("out_dir") or c.settings.get("out_dir")
    done, failed = [], []
    for j in jobs:
        try:
            done.append(wait_job(c, j["job_id"], out_dir=out_dir))
        except CliError as exc:
            if exc.code == "interrupted":  # Ctrl-C stops the batch, not just one job
                raise CliError(f"{exc} {len(done)} of {len(jobs)} were done; the rest "
                               "may still run on the server (quipclipper jobs).",
                               code="interrupted") from None
            failed.append({"job_id": j["job_id"], "error": str(exc)})
    result = {"jobs": done, "failed": failed}
    if failed:
        if c.json:
            typer.echo(_dump(result))
        raise CliError(f"{len(done)}/{len(jobs)} exported, {len(failed)} failed: "
                       f"{failed[0]['error']}", code="batch_failed")
    return result


def _client_bookmark_batch(c: Context, v: dict) -> Any:
    rows = _read_items(sys.stdin.read())
    if not rows:
        raise CliError("No rows on stdin. Pipe in the --json output of search.folder.",
                       code="usage", exit_code=EXIT_USAGE)
    cmd = reg.BY_ID["bookmark.add"]
    saved = []
    for item in rows:
        path = item.get("path") or v.get("path")
        if not path or "start" not in item:
            raise CliError("each row needs a path and a start (search hits)", code="usage",
                           exit_code=EXIT_USAGE)
        text = item.get("cue") or item.get("text") or ""
        saved.append(execute(c, cmd, {"path": path, "label": text, "cue": text,
                                      "start": item["start"], "end": item.get("end")}))
    return {"bookmarks": saved}


# --- LeadStinger (M14) --------------------------------------------------------------------

# Imports into a running LeadStinger copy files in the background, so the clips handed to it
# live here rather than in a temporary folder. Kept a week.
LEADSTINGER_CACHE = Path.home() / ".cache" / "quipclipper" / "leadstinger"
_CACHE_DAYS = 7


def _leadstinger_items(rows: list[dict], default_path: Optional[str],
                       before: float = 0.0, after: float = 0.0) -> list[dict]:
    """POST /api/leadstinger/set items from search hits, bookmarks or clips-library rows.

    Padding is per clip, as for clip.batch: a bookmark's saved before/after, else the
    --before/--after given for every row (D-026)."""
    items = []
    for row in rows:
        path = row.get("path") or default_path
        if not path:
            raise CliError("a row has no path; pass --path for search.item results",
                           code="usage", exit_code=EXIT_USAGE)
        label = (row.get("label") or "").strip()
        cue = (row.get("cue") or row.get("text") or "").strip()
        # A bookmark's default label is "<times> — <cue>": the line alone reads better.
        title = (cue if cue and (not label or label.endswith(cue)) else label or cue
                 or Path(row.get("name") or path).stem)
        item = {"path": path, "title": str(title)[:200]}
        if "start" in row:  # a search hit or a bookmark; a clips entry is the whole file
            item.update(start=row["start"], end=row.get("end"),
                        before=float(row["before"]) if row.get("before") else before,
                        after=float(row["after"]) if row.get("after") else after)
        items.append(item)
    return items


def _start_leadstinger_set(c: Context, v: dict, name: Optional[str]) -> dict:
    """POST the stdin rows as a set; returns ``{job_id, status}``."""
    rows = _read_items(sys.stdin.read())
    if not rows:
        raise CliError("No rows on stdin. Pipe in the --json output of search.folder, "
                       "bookmark.list or clips.list.", code="usage", exit_code=EXIT_USAGE)
    body: dict = {"items": _leadstinger_items(rows, v.get("path"), v.get("before") or 0.0,
                                              v.get("after") or 0.0)}
    if name:
        body["name"] = name
    cmd = reg.BY_ID["leadstinger.set"]
    return call(c, cmd.method, cmd.path, body=body).json()


def _client_leadstinger_set(c: Context, v: dict) -> Any:
    job = _start_leadstinger_set(c, v, v.get("name"))
    if not v.get("wait", True):
        return job
    return wait_job(c, job["job_id"], out_dir=v.get("out_dir") or c.settings.get("out_dir") or ".")


def _prune_leadstinger_cache() -> None:
    if not LEADSTINGER_CACHE.is_dir():
        return
    cutoff = time.time() - _CACHE_DAYS * 86400
    for d in LEADSTINGER_CACHE.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _leadstinger(v: dict, args: list[str], stdin: Optional[str] = None) -> Any:
    """Run leadstinger-cli with --json and return its parsed output."""
    exe = v.get("leadstinger_cli") or "leadstinger-cli"
    if shutil.which(exe) is None and not Path(exe).is_file():
        raise CliError(f"{exe} not found: install LeadStinger, or pass --leadstinger-cli PATH",
                       code="no_leadstinger", exit_code=EXIT_USAGE)
    cmd = [exe, "--json"]
    if v.get("data_dir"):
        cmd += ["--data-dir", str(Path(v["data_dir"]).expanduser())]
    proc = subprocess.run(cmd + args, input=stdin, capture_output=True, text=True)
    out = proc.stdout.strip()
    if proc.returncode != 0:
        detail = out or proc.stderr.strip() or f"exit {proc.returncode}"
        raise CliError(f"leadstinger-cli {args[0]} failed: {detail[:500]}", code="leadstinger")
    try:
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    except ValueError:
        raise CliError(f"leadstinger-cli {args[0]} answered something that isn't JSON: "
                       f"{out[:200]!r}", code="leadstinger") from None
    return lines[0] if len(lines) == 1 else lines


def _client_leadstinger_send(c: Context, v: dict) -> Any:
    _prune_leadstinger_cache()
    target_set = v.get("set")
    name = target_set or v.get("name")
    stage = LEADSTINGER_CACHE / time.strftime("%Y%m%d-%H%M%S")
    stage.mkdir(parents=True, exist_ok=True)
    job = wait_job(c, _start_leadstinger_set(c, v, name)["job_id"], out_dir=str(stage))
    saved = [Path(p) for p in job.get("saved", []) if str(p).endswith(".lsset")]
    if not saved:
        raise CliError("the server made no set file", code="leadstinger")
    lsset_file = saved[0]
    if not target_set:
        result = _leadstinger(v, ["set.import", str(lsset_file)])
        return {"imported": str(lsset_file), "leadstinger": result}

    # Append to an existing set: set.import always adds new sets, so add the clips to the
    # set and then give each new tile its title, trims and notes from the manifest.
    files = stage / "files"
    with zipfile.ZipFile(lsset_file) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        zf.extractall(files)
    tiles = [t for s in manifest.get("sets", []) for t in s.get("tiles", [])]
    root = files.resolve()
    paths = [str((files / t["file"]).resolve()) for t in tiles]
    if not all(Path(x).is_relative_to(root) for x in paths):
        raise CliError("the set file names a clip outside itself", code="leadstinger")
    added = _leadstinger(v, ["tile.add", "--params",
                             json.dumps({"paths": paths, "set": target_set})])
    ids = added.get("added", []) if isinstance(added, dict) else []
    if len(ids) != len(tiles):
        raise CliError(f"LeadStinger added {len(ids)} of {len(tiles)} clips: "
                       f"{added.get('errors') if isinstance(added, dict) else added}",
                       code="leadstinger")
    script = []
    for tile_id, t in zip(ids, tiles):
        params = {"tile": tile_id, "title": t.get("title", "")}
        if t.get("trim_start"):
            params["trimStart"] = t["trim_start"]
        if t.get("trim_end") is not None:
            params["trimEnd"] = t["trim_end"]
        if t.get("notes"):
            params["notes"] = t["notes"]
        script.append(json.dumps({"command": "tile.set", "params": params}))
    _leadstinger(v, ["run", "-"], stdin="\n".join(script) + "\n")
    return {"set": target_set, "added": ids, "lsset": str(lsset_file)}


def _open_link(link: str) -> bool:
    """Hand a leadstinger:// link to the system, as a browser would. False if nothing could."""
    if sys.platform == "win32":
        try:
            os.startfile(link)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener) is None:
        return False
    try:
        return subprocess.run([opener, link], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _deliver_link(v: dict, result: dict) -> dict:
    """Open a Send to LeadStinger link here (unless --no-open) and return it."""
    link = result.get("link")
    if not link:
        raise CliError("the server sent no leadstinger:// link", code="leadstinger")
    return {**result, "opened": bool(v.get("open", True)) and _open_link(link)}


def _client_leadstinger_quip(c: Context, v: dict) -> Any:
    method, path, query, body = build_request(reg.BY_ID["leadstinger.quip"], v)
    job = call(c, method, path, query, body).json()
    return _deliver_link(v, wait_job(c, job["job_id"]).get("result") or {})


def _client_leadstinger_link(c: Context, v: dict) -> Any:
    method, path, query, body = build_request(reg.BY_ID["leadstinger.link"], v)
    return _deliver_link(v, call(c, method, path, query, body).json())


def format_clock(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def pick_session(sessions: list[dict], want: Optional[str]) -> dict:
    """The session clip.nowplaying means: ``want`` (an id, or part of a device, user or
    client name), else the only one playing a library file. Ambiguity is an error that
    lists the choices, so an agent can ask which one."""
    playable = [s for s in sessions if s.get("path")]
    if want:
        key = want.casefold()
        hits = [s for s in sessions if s.get("id") == want] or [
            s for s in sessions
            if any(key in str(s.get(f) or "").casefold() for f in ("device", "user", "client"))]
    else:
        hits = playable
    if not hits:
        if not sessions:
            msg = "Nothing is playing on Jellyfin."
        elif want:
            msg = f"No Jellyfin session matches {want!r}."
        else:
            msg = "Nothing playing on Jellyfin is in quipclipper's library: " + "; ".join(
                f"{s.get('device')}: {s.get('note')}" for s in sessions)
        raise CliError(msg, code="not_found")
    if len(hits) > 1:
        names = "; ".join(f"{s.get('id')} = {s.get('device')} ({s.get('title')})" for s in hits)
        raise CliError(f"{len(hits)} Jellyfin sessions are playing; pass --session: {names}",
                       code="ambiguous", exit_code=EXIT_USAGE)
    if not hits[0].get("path"):
        raise CliError(f"{hits[0].get('device')}: {hits[0].get('note')}", code="not_found")
    return hits[0]


def cues_before(cues: list[dict], at: float, count: int = 1) -> list[dict]:
    """The ``count`` cues ending with the one at or before ``at``: the last cue that has
    started by then (the playhead lags, so the line just heard has started)."""
    started = [c for c in cues if isinstance(c.get("start"), (int, float)) and c["start"] <= at]
    return started[-max(count, 1):]


def _client_clip_nowplaying(c: Context, v: dict) -> Any:
    sessions = execute(c, reg.BY_ID["jellyfin.sessions"], {}).get("sessions") or []
    session = pick_session(sessions, v.get("session"))
    at = max((session.get("position") or 0) - (v.get("back") or 0), 0)
    cues = execute(c, reg.BY_ID["item.script"], {"path": session["path"]})
    lines = cues_before(cues if isinstance(cues, list) else [], at, v.get("lines") or 1)
    if not lines:
        raise CliError(f"No dialogue at or before {format_clock(at)} in {session['path']}.",
                       code="not_found")
    values = {p.name: v.get(p.name) for p in reg._CLIP_OPTIONS}
    values["template"] = values.get("template") or c.settings.get("clip_template")
    if values.get("lossless") is None:  # as `clip`: re-encode video (exact), copy audio
        values["lossless"] = (values.get("kind") or "video") != "video"
    values.update(path=session["path"], start=lines[0]["start"], end=lines[-1]["end"],
                  cue_text=" ".join(x.get("text", "") for x in lines).strip() or None,
                  before=v.get("before"), after=v.get("after"),
                  wait=v.get("wait", True), out_dir=v.get("out_dir"))
    job = execute(c, reg.BY_ID["clip.create"], values)
    line = {"start": lines[0]["start"], "end": lines[-1]["end"], "text": values["cue_text"]}
    if not c.json:
        typer.echo(f"{session.get('device')}: {format_clock(line['start'])} "
                   f"\u201c{line['text']}\u201d")
    return {**job, "session": session.get("id"), "device": session.get("device"),
            "line": line}


# --- machine transcription on this machine (M21) -----------------------------------------
# item.transcribe and the transcript commands run whisper.cpp here when no server is set:
# `whisper-cli` on PATH (or QC_WHISPER_BIN), and a ggml model in QC_WHISPER_MODEL (a path)
# or QC_WHISPER_MODELS (name=path pairs). Transcripts go to ~/.cache/quipclipper/transcripts.


def _media_arg(v: dict) -> Path:
    media = Path(str(v.get("path") or "")).expanduser()
    if not media.is_file():
        raise CliError(f"Not a file: {media}", code="not_found")
    return media.resolve()


def _store():
    from quipclipper import transcribe as tr

    return tr.TranscriptStore(tr.default_store_dir())


def _local_transcribe(c: Context, v: dict) -> Any:
    from quipclipper import transcribe as tr

    cfg = tr.WhisperConfig.from_env()
    if not cfg.binary_path():
        raise CliError(f"{cfg.binary} not found: install whisper.cpp, or set QC_WHISPER_BIN.",
                       code="missing_tool")
    if not cfg.models:
        raise CliError("No whisper model: set QC_WHISPER_MODEL to a ggml model file "
                       "(e.g. ggml-small.en.bin).", code="usage", exit_code=EXIT_USAGE)
    media = _media_arg(v)
    model = v.get("model") or cfg.default_model
    try:
        cfg.model_path(model)
    except ValueError as exc:
        raise CliError(str(exc), code="usage", exit_code=EXIT_USAGE) from None
    try:
        real = resolve_subtitles(subs=None, video=media,
                                 langs=c.settings.get("sub_langs")).cues
    except (ValueError, FileNotFoundError, RuntimeError):
        real = []
    store = _store()
    have = store.load(media)
    mode = v.get("mode") or ("range" if v.get("start") is not None else "gaps")
    try:
        ranges = tr.plan_ranges(mode, real=real, duration=_probe_duration(media),
                                done=have.ranges if have else [], start=v.get("start"),
                                end=v.get("end"), redo=bool(v.get("redo")))
    except ValueError as exc:
        raise CliError(str(exc), code="usage", exit_code=EXIT_USAGE) from None
    if not ranges:
        return {"path": str(media), "transcribed": [], "new_lines": [],
                "transcript": have.summary() if have else None,
                "note": "Nothing new to transcribe (pass --redo to do it again)."}

    def progress(done: float, total: float) -> None:
        if not c.json:
            typer.secho(f"  transcribing… {format_clock(done)} of {format_clock(total)}",
                        dim=True, err=True)

    try:
        t = tr.run(store, media, ranges, cfg=cfg, model=model, audio=int(v.get("audio") or 0),
                   real=real, duration=_probe_duration(media), vad=tr.use_vad(mode),
                   progress=progress)
    except RuntimeError as exc:
        raise CliError(str(exc), code="failed") from None
    new = [cue for cue in (t.cues if t else []) if any(a <= cue.start < b for a, b in ranges)]
    return {"path": str(media), "model": model, "transcribed": [list(r) for r in ranges],
            "new_lines": [{"start": x.start, "end": x.end, "text": x.text} for x in new],
            "transcript": t.summary() if t else None}


def _local_transcript(c: Context, v: dict) -> Any:
    from quipclipper import transcribe as tr

    media = _media_arg(v)
    store = _store()
    t = store.load(media)
    cues = tr.machine_cues(media, store)
    fmt = v.get("fmt") or "json"
    if fmt == "srt":
        return tr.cues_to_srt(cues)
    if fmt == "vtt":
        return tr.cues_to_vtt(cues)
    return {"path": str(media), "transcript": t.summary() if t else None,
            "library": [x.name for x in tr.find_machine_sidecars(media)],
            "lines": [{"start": x.start, "end": x.end, "text": x.text, "source": x.source}
                      for x in cues]}


def _local_transcript_clear(c: Context, v: dict) -> Any:
    media = _media_arg(v)
    return {"path": str(media), "deleted": _store().clear(media)}


def _local_transcript_copy(c: Context, v: dict) -> Any:
    from quipclipper import transcribe as tr

    media = _media_arg(v)
    t = _store().load(media)
    if t is None or not t.cues:
        raise CliError("This file has no machine transcript to copy.", code="not_found")
    try:
        real = resolve_subtitles(subs=None, video=media,
                                 langs=c.settings.get("sub_langs")).cues
    except (ValueError, FileNotFoundError, RuntimeError):
        real = []
    lines = tr.library_lines(real, t.cues)  # as the server: none the subtitles have
    try:
        side = tr.write_sidecar(media, lines, t.language)
    except ValueError as exc:
        raise CliError(str(exc), code="http_409") from None
    except OSError as exc:
        raise CliError(f"Can't write next to {media.name}: {exc.strerror or exc}",
                       code="io") from None
    return {"path": str(media), "sidecar": side.name, "lines": len(lines)}


def _local_transcript_uncopy(c: Context, v: dict) -> Any:
    from quipclipper import transcribe as tr

    media = _media_arg(v)
    removed = []
    for side in tr.find_machine_sidecars(media):
        side.unlink()
        removed.append(side.name)
    return {"path": str(media), "removed": removed}


_LOCAL = {
    "item.transcribe": _local_transcribe,
    "item.transcript": _local_transcript,
    "item.transcript-clear": _local_transcript_clear,
    "item.transcript-copy": _local_transcript_copy,
    "item.transcript-uncopy": _local_transcript_uncopy,
}
# Commands whose --model falls back to the stt_model setting.
_STT_MODEL = ("item.transcribe", "index.transcribe")


def _client_job_wait(c: Context, v: dict) -> Any:
    return wait_job(c, v["job_id"], out_dir=v.get("out_dir") or c.settings.get("out_dir"))


def _client_settings(c: Context, cmd_id: str, v: dict) -> Any:
    values = load_settings()
    if cmd_id == "settings.list":
        return {"file": str(settings_path()),
                "settings": {k: values.get(k, "") for k in reg.SETTINGS
                             if stt_enabled() or k not in reg.EXPERIMENTAL_SETTINGS}}
    key = v["key"]
    if key not in reg.SETTINGS:
        raise CliError(f"Unknown setting {key!r}; one of: {', '.join(reg.SETTINGS)}",
                       code="usage", exit_code=EXIT_USAGE)
    if cmd_id == "settings.get":
        return {key: values.get(key, "")}
    if v.get("value"):
        values[key] = v["value"]
    else:
        values.pop(key, None)
    save_settings(values)
    return {key: values.get(key, "")}


def _run_client(c: Context, cmd: reg.Command, v: dict) -> Any:
    if cmd.id == "clip.batch":
        return _client_clip_batch(c, v)
    if cmd.id == "bookmark.batch":
        return _client_bookmark_batch(c, v)
    if cmd.id == "leadstinger.set":
        return _client_leadstinger_set(c, v)
    if cmd.id == "leadstinger.send":
        return _client_leadstinger_send(c, v)
    if cmd.id == "leadstinger.quip":
        return _client_leadstinger_quip(c, v)
    if cmd.id == "leadstinger.link":
        return _client_leadstinger_link(c, v)
    if cmd.id == "clip.nowplaying":
        return _client_clip_nowplaying(c, v)
    if cmd.id == "job.wait":
        return _client_job_wait(c, v)
    if cmd.area == "settings":
        return _client_settings(c, cmd.id, v)
    raise CliError(f"{cmd.id} has no client handler", code="internal")


def run_command(c: Context, cmd: reg.Command, values: dict) -> Any:
    """Run any registry command from a values dict (used by generic commands and `run`)."""
    if cmd.experimental and not c.remote and not stt_enabled():
        # A server decides for itself (it answers 404 when it's off there).
        raise CliError(STT_OFF, code="usage", exit_code=EXIT_USAGE)
    if cmd.id in _STT_MODEL and not values.get("model") and c.settings.get("stt_model"):
        values = {**values, "model": c.settings["stt_model"]}
    if cmd.mode == "both" and not c.remote and cmd.id in _LOCAL:
        if cmd.destructive and c.safe:
            raise CliError(f"{cmd.id} changes or deletes data; refused with --safe.",
                           code="refused", exit_code=EXIT_REFUSED)
        return _LOCAL[cmd.id](c, values)
    if cmd.mode == "client":
        if cmd.destructive and c.safe:  # execute() checks server commands
            raise CliError(f"{cmd.id} changes or deletes data; refused with --safe.",
                           code="refused", exit_code=EXIT_REFUSED)
        return _run_client(c, cmd, values)
    _reject_local_only(cmd, values)
    return execute(c, cmd, values)


# --- generic commands, built from the registry ---------------------------------------------


def _decls(p: reg.Param) -> list[str]:
    long = "--" + p.name.replace("_", "-")
    if p.type == "bool" and (p.default is True or p.default is None or p.negative):
        neg = p.negative or "--no-" + p.name.replace("_", "-")
        return [f"{long}/{neg}"] + [f for f in p.flags if not f.startswith("-") or len(f) > 2]
    return [long, *p.flags]


def _annotation(p: reg.Param):
    base = {"str": str, "int": int, "float": float, "bool": bool}.get(p.type, str)
    if p.choices:
        base = Literal[tuple(p.choices)]  # typer renders it as a click Choice
    if p.type.startswith("list["):
        return Optional[List[str]]
    if p.type == "bool" and p.default is not None:
        return bool
    return Optional[base] if not (p.required and p.main) else base


def _typer_default(p: reg.Param):
    if p.main:
        default = ... if p.required else p.default
        return typer.Argument(default, help=p.help, show_default=False)
    return typer.Option(p.default, *_decls(p), help=p.help,
                        show_default=p.default not in (None, "", False),
                        hidden=p.experimental and not stt_enabled())


def _make_generic(cmd: reg.Command):
    sig = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY,
                             annotation=typer.Context)]
    # A wire name that is a Python keyword (clips.download's "as") gets a trailing _.
    py = {p.name: p.name + "_" if keyword.iskeyword(p.name) else p.name for p in cmd.params}
    for p in cmd.params:
        sig.append(inspect.Parameter(py[p.name], inspect.Parameter.KEYWORD_ONLY,
                                     default=_typer_default(p), annotation=_annotation(p)))

    def fn(ctx: typer.Context, **values):
        values = {name: values[py_name] for name, py_name in py.items() if py_name in values}
        c = _c(ctx)
        try:
            data = run_command(c, cmd, values)
        except OSError as exc:
            _fail(c, CliError(str(exc), code="io"))
        except CliError as exc:
            _fail(c, exc)
        _print(c, cmd, data)
        _exit_for(cmd, data)

    fn.__signature__ = inspect.Signature(sig)
    fn.__annotations__ = {prm.name: prm.annotation for prm in sig}
    fn.__name__ = cmd.id.replace(".", "_").replace("-", "_")
    warn = " Refused with --safe." if cmd.destructive else ""
    fn.__doc__ = cmd.summary + warn
    return fn


for _cmd in reg.COMMANDS:
    if _cmd.id in _HANDWRITTEN:
        continue
    _fn = _make_generic(_cmd)
    # Experimental commands (D-040) still run; they only stay out of the help while off.
    app.command(name=_cmd.id, hidden=_cmd.experimental and not stt_enabled())(_fn)
    if _cmd.shortcut:
        app.command(name=_cmd.shortcut, help=f"Shortcut for {_cmd.id}. {_cmd.summary}")(_fn)


# --- built-ins -----------------------------------------------------------------------------


@app.command(name="commands")
def commands_cmd(
    ctx: typer.Context,
    filter_: Optional[str] = typer.Option(None, "--filter", help="Only ids containing this."),
    markdown: bool = typer.Option(False, "--markdown", help="Print the docs/CLI.md tables."),
):
    """List every command (the registry)."""
    c = _c(ctx)
    if markdown:
        typer.echo(reg.render_markdown())
        return
    rows = [x for x in reg.COMMANDS if (not filter_ or filter_ in x.id)
            and (stt_enabled() or not x.experimental)]
    if c.json:
        typer.echo(_dump([{
            "id": x.id, "shortcut": x.shortcut, "summary": x.summary, "mode": x.mode,
            "method": x.method, "path": x.path, "destructive": x.destructive,
            "response": x.response, "ui": x.ui,
            "params": [{"name": p.name, "type": p.type, "where": p.where,
                        "required": p.required, "main": p.main, "default": p.default,
                        "choices": list(p.choices) if p.choices else None,
                        "local_only": p.local_only, "help": p.help} for p in x.params
                       if stt_enabled() or not p.experimental],
        } for x in rows]))
        return
    width = max(len(x.id) for x in rows) if rows else 0
    for x in rows:
        short = f" ({x.shortcut})" if x.shortcut else ""
        flag = " ⚠" if x.destructive else ""
        typer.echo(f"{x.id.ljust(width)}  {x.summary}{short}{flag}")


@app.command(name="help")
def help_cmd(ctx: typer.Context, command: str = typer.Argument(..., help="Command id or shortcut.")):
    """Show the help for one command."""
    name = reg.SHORTCUTS.get(command, command) if command not in reg.SHORTCUTS.values() else command
    group = typer.main.get_command(app)
    sub = group.get_command(ctx, name) or group.get_command(ctx, command)
    if sub is None:
        typer.secho(f"No command {command!r}. Try `quipclipper commands`.", fg="red", err=True)
        raise typer.Exit(code=EXIT_USAGE)
    with click.Context(sub, info_name=name, parent=ctx.parent) as sub_ctx:
        typer.echo(sub.get_help(sub_ctx))


@app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    script: str = typer.Argument(..., help="JSON-lines file, or - for stdin. Each line: "
                                           '{"command": "<id>", "params": {...}}.'),
    keep_going: bool = typer.Option(False, "--keep-going", help="Continue after a failed line."),
):
    """Run a script of commands (one JSON object per line)."""
    c = _c(ctx)
    try:
        text = sys.stdin.read() if script == "-" else Path(script).read_text()
    except OSError as exc:
        _fail(c, CliError(f"Can't read {script}: {exc.strerror or exc}", code="usage",
                          exit_code=EXIT_USAGE))
    failed = 0
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            step = json.loads(line)
            if not isinstance(step, dict) or not isinstance(step.get("params") or {}, dict):
                raise CliError('each line is {"command": …, "params": {…}}', code="usage",
                               exit_code=EXIT_USAGE)
            cmd_id = reg.SHORTCUTS.get(step["command"], step["command"])
            cmd = reg.BY_ID[cmd_id]
            if cmd.mode == "both" and not c.remote:
                raise CliError(f"{cmd_id} runs locally only as `quipclipper {cmd_id}`; "
                               "`run` drives a server.", code="usage", exit_code=EXIT_USAGE)
            data = run_command(c, cmd, step.get("params") or {})
            typer.echo(_dump({"line": n, "command": cmd_id, "ok": True, "result": data}))
        except (CliError, KeyError, ValueError) as exc:
            failed += 1
            code = exc.code if isinstance(exc, CliError) else "usage"
            typer.echo(_dump({"line": n, "ok": False,
                              "error": {"code": code, "message": str(exc)}}))
            if not keep_going:
                raise typer.Exit(code=exc.exit_code if isinstance(exc, CliError) else EXIT_USAGE)
    if failed:
        raise typer.Exit(code=1)


def main(argv: Optional[list[str]] = None, transport: Optional[Transport] = None) -> int:
    """Run the CLI in-process (tests drive it against a TestClient this way)."""
    global TRANSPORT_OVERRIDE
    previous = TRANSPORT_OVERRIDE
    TRANSPORT_OVERRIDE = transport
    try:
        rv = app(argv if argv is not None else sys.argv[1:], standalone_mode=False)
        # With standalone_mode off, click returns a typer.Exit's code instead of exiting.
        return rv if isinstance(rv, int) else 0
    except typer.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    finally:
        TRANSPORT_OVERRIDE = previous


if __name__ == "__main__":
    app()
