"""The command registry: every action the web UI offers, as a CLI command.

One plain-data table drives the CLI (``quipclipper <id>``), the generated
reference ``docs/CLI.md`` and the parity tests in ``web/backend/tests``. The
tests fail when the web app gains a route or the frontend gains an API call
that no command here covers, so a new UI action has to be added here first.

No web framework is imported, so the CLI package stays free of FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Where a parameter goes on the wire.
PATH, QUERY, BODY, CLIENT = "path", "query", "body", "client"


@dataclass(frozen=True)
class Param:
    """One command parameter.

    ``name`` is the wire name (the API's query/body/path field). The CLI flag is
    ``--name`` with underscores as hyphens, plus ``flags``; ``main`` makes it the
    positional argument. ``where="client"`` parameters never reach the server.
    ``local_only`` ones are accepted only when the command runs locally.
    """

    name: str
    type: str  # str | int | float | bool | list[str] | list[int]
    help: str
    where: str = QUERY
    required: bool = False
    default: Any = None
    main: bool = False
    flags: tuple[str, ...] = ()
    choices: tuple[str, ...] | None = None
    local_only: bool = False
    # bool params only: the "--x/--no-x" pair, when the off form isn't "--no-<name>".
    negative: str | None = None


@dataclass(frozen=True)
class Command:
    """One CLI command and the API endpoint it drives.

    mode:
      server  needs a quipclipper server (``--server`` / ``QC_URL``)
      local   runs on this machine only
      both    calls the server when one is configured, else runs locally
      client  CLI-side composition of other commands (batch, wait, settings)
    response:
      json    print the JSON reply
      job     the reply is ``{job_id}``; follow it with ``--wait``
      lines   stream text lines as they arrive
      file    save the reply body to a file
    """

    id: str
    summary: str
    method: str | None
    path: str | None
    params: tuple[Param, ...] = ()
    mode: str = "server"
    response: str = "json"
    destructive: bool = False
    shortcut: str | None = None
    ui: str = ""
    area: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "area", self.id.split(".", 1)[0])

    @property
    def main_param(self) -> Param | None:
        return next((p for p in self.params if p.main), None)


# --- shared parameter sets ------------------------------------------------

_SEARCH_TUNING = (
    Param("min_score", "float", "Drop matches scoring below this (0-100).", default=60.0),
    Param("max_span", "int", "Most consecutive captions one match may join.", default=3),
)

# Clip options shared by clip.create and clip.batch (ClipRequest fields).
_CLIP_OPTIONS = (
    Param("kind", "str", "Clip type.", where=BODY, default="video",
          choices=("audio", "video", "gif"), flags=("--type", "-t")),
    Param("lossless", "bool", "Stream-copy instead of re-encoding. Default: re-encode "
          "video (frame-exact), copy audio.", where=BODY),
    Param("audio_format", "str", "Full-mix lossless audio: one file keeping every "
          "channel. Audio clips only.", where=BODY, choices=("wav", "flac")),
    Param("audio_tracks", "list[int]", "Audio stream(s) to keep, by a:N index, "
          "comma-separated (e.g. 0,2). Default: all.", where=BODY,
          flags=("--audio-track", "-A")),
    Param("backend", "str", "Cutting backend; auto uses mkvmerge for lossless MKV cuts.",
          where=BODY, default="auto", choices=("auto", "ffmpeg", "mkvmerge")),
    Param("chapters", "bool", "Keep chapters in mkvmerge output.", where=BODY, default=True),
    Param("remux_first", "bool", "Remux the source to a temporary MKV before cutting.",
          where=BODY, default=False),
    Param("embed_subs", "bool", "Mux the subtitle file into video clips.", where=BODY,
          default=True),
    Param("split_channels", "bool", "Split a surround track into per-group files.",
          where=BODY, default=False),
    Param("split_format", "str", "Format for --split-channels.", where=BODY, default="wav",
          choices=("wav", "flac", "original")),
    Param("split_groups", "list[str]", "Channel groups to export when splitting, "
          "comma-separated subset of front,center,surround,lfe. Default: all.",
          where=BODY),
    Param("include_lfe", "bool", "Include the LFE channel when splitting.", where=BODY,
          default=True, negative="--no-lfe"),
    Param("template", "str", "Output name template, e.g. "
          "'{source}/{timestamp}_{cue}_{title}'. Default: the settings value.",
          where=BODY),
    Param("default_sub_track", "int", "Subtitle track (s:N) to mark as the default "
          "in the saved video clip.", where=BODY),
)

_JOB_FOLLOW = (
    Param("wait", "bool", "Wait for the job to finish and show its result.",
          where=CLIENT, default=True),
    Param("out_dir", "str", "After waiting, download the job's files into this "
          "folder. Default: the settings value.", where=CLIENT),
)


COMMANDS: tuple[Command, ...] = (
    # --- server ---------------------------------------------------------------
    Command("server.health", "Show the version and which media tools are installed.",
            "GET", "/api/health", mode="both", shortcut="status",
            ui="Status line at the top of every page"),
    Command("server.config", "Show the server's configuration (media roots, limits, "
            "subtitle languages).", "GET", "/api/config", ui="Loaded on start"),

    # --- library ----------------------------------------------------------------
    Command("library.roots", "List the media roots.", "GET", "/api/library/roots",
            ui="Library home"),
    Command("library.browse", "List a folder (no path: the media roots; 'yt:': YouTube "
            "items; an EPUB: its chapters).", "GET", "/api/library/browse",
            params=(Param("path", "str", "Folder, EPUB or 'yt:'.", main=True),),
            shortcut="ls", ui="Folder rows, breadcrumbs, Back"),
    Command("library.find", "Find files and folders by name.", "GET", "/api/library/search",
            params=(Param("query", "str", "Name to look for.", main=True, required=True),
                    Param("path", "str", "Only look inside this folder."),
                    Param("limit", "int", "Most results to return (max 200).", default=50)),
            ui="Library filter box"),

    # --- youtube -----------------------------------------------------------------
    Command("youtube.add", "Add a YouTube video by URL (fetches its transcript, not the "
            "video).", "POST", "/api/youtube",
            params=(Param("url", "str", "YouTube watch URL.", where=BODY, main=True,
                          required=True),) + _JOB_FOLLOW,
            response="job", ui="YouTube Add button"),
    Command("youtube.remove", "Remove a YouTube item (and any downloaded copy).",
            "DELETE", "/api/youtube/{video_id}",
            params=(Param("video_id", "str", "YouTube video id.", where=PATH, main=True,
                          required=True),),
            destructive=True, ui="YouTube row ✕"),
    Command("youtube.download", "Download a YouTube item's full video to the server.",
            "POST", "/api/youtube/{video_id}/download",
            params=(Param("video_id", "str", "YouTube video id.", where=PATH, main=True,
                          required=True),) + _JOB_FOLLOW,
            response="job", ui="⬇ Download video"),
    Command("youtube.delete-download", "Delete a YouTube item's downloaded video (the "
            "item stays).", "DELETE", "/api/youtube/{video_id}/download",
            params=(Param("video_id", "str", "YouTube video id.", where=PATH, main=True,
                          required=True),),
            destructive=True, ui="💾 Remove download"),

    # --- item ----------------------------------------------------------------------
    Command("item.info", "Show a file's streams, duration and subtitle tracks.",
            "GET", "/api/items",
            params=(Param("path", "str", "Media file.", main=True, required=True),
                    Param("langs", "str", "Preferred subtitle languages for picking the "
                          "best track, comma-separated (e.g. en,es).")),
            mode="both", shortcut="tracks", ui="Opening an item; stream selector"),
    Command("item.script", "Print a file's subtitle script (every cue with its times).",
            "GET", "/api/items/subtitles",
            params=(Param("path", "str", "Media file.", main=True, required=True),
                    Param("track", "int", "Subtitle track (s:N). Default: the best one."),
                    Param("offset", "float", "Shift every cue by this many seconds.",
                          default=0.0),
                    Param("fmt", "str", "json (cues with speakers) or vtt.", default="json",
                          choices=("json", "vtt"))),
            ui="Script view; subtitle track picker"),
    Command("item.reindex", "Re-read a file's subtitles (or re-fetch a YouTube "
            "transcript), clearing its cache.", "POST", "/api/items/subtitles/reindex",
            params=(Param("path", "str", "Media file or yt:<id>.", main=True,
                          required=True),),
            ui="↻ Reindex subtitles (item)"),
    Command("item.fetch-subs", "Fetch subtitles from YouTube for one library video.",
            "POST", "/api/media/fetch-subs",
            params=(Param("path", "str", "Media file.", main=True, required=True),)
            + _JOB_FOLLOW,
            response="job", ui="⬇ Fetch subtitles from YouTube (item)"),

    # --- search ------------------------------------------------------------------------
    Command("search.item", "Search one file's dialogue.", "GET", "/api/search",
            params=(Param("query", "str", "Dialogue to look for.", main=True, required=True),
                    Param("path", "str", "Media file (or subtitle file's video).",
                          flags=("--video", "-v")),
                    Param("track", "int", "Subtitle track (s:N). Default: the best one."),
                    Param("limit", "int", "Most matches to return.", default=10,
                          flags=("-n",)),
                    *_SEARCH_TUNING,
                    Param("subs", "str", "Subtitle file to search instead of the video's.",
                          where=CLIENT, flags=("-s",), local_only=True),
                    Param("langs", "str", "Preferred subtitle languages, comma-separated.",
                          where=CLIENT, local_only=True)),
            mode="both", shortcut="search", ui="Item dialogue search"),
    Command("search.folder", "Search dialogue across every file in one or more folders.",
            "GET", "/api/search/folder",
            params=(Param("query", "str", "Dialogue to look for.", main=True, required=True),
                    Param("path", "list[str]", "Folder(s) to search (repeat for several).",
                          required=True),
                    Param("limit", "int", "Most matches per file.", default=5,
                          flags=("-n",)),
                    *_SEARCH_TUNING),
            ui="Folder dialogue search"),

    # --- index -------------------------------------------------------------------------
    Command("index.status", "Show how many files in a folder have cached subtitles.",
            "GET", "/api/search/folder/index-status",
            params=(Param("path", "str", "Folder.", main=True, required=True),),
            ui="Folder index banner"),
    Command("index.build", "Cache the subtitles of every file in a folder (prints "
            "progress).", "POST", "/api/search/folder/index",
            params=(Param("path", "str", "Folder.", main=True, required=True),
                    Param("force", "bool", "Re-read files already cached.", default=False)),
            response="lines", ui="Index now; ↻ Reindex subtitles (folder)"),
    Command("index.fetch-subs", "Fetch missing YouTube subtitles for every video in a "
            "folder.", "POST", "/api/search/folder/fetch-subs",
            params=(Param("path", "str", "Folder.", main=True, required=True),)
            + _JOB_FOLLOW,
            response="job", ui="⬇ Fetch missing YouTube subtitles; ✕ Cancel"),

    # --- clip -----------------------------------------------------------------------------
    Command("clip.create", "Cut a clip: around a line of dialogue (QUERY) or between "
            "--start and --end.", "POST", "/api/clip",
            params=(Param("query", "str", "Dialogue to locate and clip.", where=BODY,
                          main=True),
                    Param("path", "str", "Media file to cut from.", where=BODY,
                          required=True, flags=("--video", "-v")),
                    Param("start", "float", "Clip start in seconds (instead of a query).",
                          where=BODY),
                    Param("end", "float", "Clip end in seconds. Default: end of file.",
                          where=BODY),
                    Param("match_index", "int", "Which ranked match to cut (0 = best).",
                          where=BODY, default=0, flags=("--index", "-i")),
                    Param("track", "int", "Subtitle track (s:N) to search.", where=BODY),
                    Param("cue_text", "str", "Dialogue for the {cue} name token when "
                          "clipping by --start/--end.", where=BODY),
                    Param("before", "float", "Seconds of padding before the line.",
                          where=BODY, default=2.0, flags=("-b",)),
                    Param("after", "float", "Seconds of padding after the line.",
                          where=BODY, default=2.0, flags=("-a",)),
                    *_CLIP_OPTIONS,
                    *_JOB_FOLLOW,
                    Param("subs", "str", "Subtitle file (else sidecar/embedded).",
                          where=CLIENT, flags=("-s",), local_only=True),
                    Param("langs", "str", "Preferred subtitle languages, comma-separated.",
                          where=CLIENT, local_only=True),
                    Param("pick", "bool", "Choose one or more matches interactively.",
                          where=CLIENT, default=False, flags=("-p",), local_only=True),
                    Param("limit", "int", "How many matches to offer with --pick.",
                          where=CLIENT, default=10, flags=("-n",), local_only=True),
                    *(Param(p.name, p.type, p.help, where=CLIENT, default=p.default,
                            local_only=True) for p in _SEARCH_TUNING),
                    Param("out", "str", "Output path (one clip only).", where=CLIENT,
                          flags=("-o",), local_only=True),
                    Param("yes", "bool", "Skip confirmation prompts.", where=CLIENT,
                          default=False, flags=("-y",), local_only=True),
                    Param("hwaccel", "bool", "Hardware-encode video re-encodes (VAAPI). "
                          "Default: auto-detect.", where=CLIENT, local_only=True),
                    Param("vaapi_device", "str", "VAAPI render node for --hwaccel.",
                          where=CLIENT, default="/dev/dri/renderD128", local_only=True)),
            mode="both", response="job", shortcut="clip",
            ui="✂ Make clip; ✂ on a search hit"),
    Command("clip.batch", "Cut one clip per JSON line on stdin: search hits, bookmarks "
            "or clips-library entries (pipe from search.folder, bookmark.list or "
            "clips.list with --json).", None, None,
            params=(Param("path", "str", "Media file for hits that don't carry one "
                          "(search.item results).", where=CLIENT),
                    *_CLIP_OPTIONS, *_JOB_FOLLOW),
            mode="client", ui="Clip selected / Export selected (search, bookmarks, clips)"),

    # --- jobs ------------------------------------------------------------------------------
    Command("job.list", "List recent jobs (clips, downloads, YouTube).", "GET", "/api/jobs",
            shortcut="jobs", ui="Job status lines"),
    Command("job.get", "Show one job.", "GET", "/api/jobs/{job_id}",
            params=(Param("job_id", "str", "Job id.", where=PATH, main=True,
                          required=True),),
            ui="Job progress polling"),
    Command("job.wait", "Wait for a job to finish.", None, None,
            params=(Param("job_id", "str", "Job id.", where=CLIENT, main=True,
                          required=True),
                    Param("out_dir", "str", "Download the job's files into this folder.",
                          where=CLIENT)),
            mode="client", ui="Job progress polling"),
    Command("job.cancel", "Cancel a queued or running job, or forget a finished one.",
            "DELETE", "/api/jobs/{job_id}",
            params=(Param("job_id", "str", "Job id.", where=PATH, main=True,
                          required=True),),
            ui="✕ Cancel"),
    Command("job.download", "Download a finished job's file (all files if FILENAME is "
            "omitted).", "GET", "/api/jobs/{job_id}/download/{filename}",
            params=(Param("job_id", "str", "Job id.", where=PATH, main=True,
                          required=True),
                    Param("filename", "str", "File name from the job's result.",
                          where=PATH, main=True),
                    Param("out_dir", "str", "Folder to save into.", where=CLIENT,
                          default=".")),
            response="file", ui="⤓ link after a clip finishes"),

    # --- bookmarks -------------------------------------------------------------------------
    Command("bookmark.list", "List bookmarks (all, or one file's).", "GET",
            "/api/bookmarks",
            params=(Param("path", "str", "Only this file's bookmarks."),),
            ui="Bookmarks view; item bookmark list"),
    Command("bookmark.add", "Save a range as a bookmark.", "POST", "/api/bookmarks",
            params=(Param("path", "str", "Media file.", where=BODY, main=True,
                          required=True),
                    Param("start", "float", "Range start in seconds.", where=BODY,
                          required=True),
                    Param("end", "float", "Range end in seconds.", where=BODY,
                          required=True),
                    Param("label", "str", "Name for the bookmark.", where=BODY,
                          default=""),
                    Param("before", "float", "Saved padding before.", where=BODY,
                          default=0.0),
                    Param("after", "float", "Saved padding after.", where=BODY,
                          default=0.0),
                    Param("sub_track", "int", "Subtitle track (s:N) to remember.",
                          where=BODY),
                    Param("audio_track", "int", "Audio track (a:N) to remember.",
                          where=BODY),
                    Param("channel_subset", "str", "Channel group to remember.",
                          where=BODY),
                    Param("cue", "str", "The dialogue, for the {cue} name token.",
                          where=BODY, default="")),
            ui="★ Save bookmark; ★ on a search hit"),
    Command("bookmark.update", "Rename a bookmark or change its padding.", "PATCH",
            "/api/bookmarks/{bookmark_id}",
            params=(Param("bookmark_id", "str", "Bookmark id.", where=PATH, main=True,
                          required=True),
                    Param("label", "str", "New name.", where=BODY),
                    Param("before", "float", "New padding before.", where=BODY),
                    Param("after", "float", "New padding after.", where=BODY)),
            ui="Bookmark −/+ buffer"),
    Command("bookmark.delete", "Delete a bookmark.", "DELETE",
            "/api/bookmarks/{bookmark_id}",
            params=(Param("bookmark_id", "str", "Bookmark id.", where=PATH, main=True,
                          required=True),),
            destructive=True, ui="Bookmark ✕"),
    Command("bookmark.clear", "Delete every bookmark.", "DELETE", "/api/bookmarks",
            destructive=True, ui="Clear all"),
    Command("bookmark.batch", "Bookmark one search hit per JSON line on stdin (pipe "
            "from search.folder --json).", None, None,
            params=(Param("path", "str", "Media file for hits that don't carry one.",
                          where=CLIENT),),
            mode="client", ui="Bookmark selected"),

    # --- clips library ---------------------------------------------------------------------
    Command("clips.list", "List finished clips (a folder of the clips library).",
            "GET", "/api/clips",
            params=(Param("folder", "str", "Subfolder of the clips library.", main=True),),
            ui="Clips view"),
    Command("clips.download", "Download a clip from the clips library.", "GET",
            "/api/clips/download/{clip_path}",
            params=(Param("clip_path", "str", "Path inside the clips library.", where=PATH,
                          main=True, required=True),
                    Param("out_dir", "str", "Folder to save into.", where=CLIENT,
                          default=".")),
            response="file", ui="Clip Download link"),

    # --- settings (the web UI's per-browser preferences) -------------------------------------
    Command("settings.list", "Show the CLI settings.", None, None, mode="client",
            ui="—"),
    Command("settings.get", "Show one CLI setting.", None, None,
            params=(Param("key", "str", "Setting name.", where=CLIENT, main=True,
                          required=True),),
            mode="client", ui="—"),
    Command("settings.set", "Change a CLI setting (empty value clears it).", None, None,
            params=(Param("key", "str", "Setting name.", where=CLIENT, main=True,
                          required=True),
                    Param("value", "str", "New value (omit to clear).", where=CLIENT,
                          default="", main=True)),
            mode="client", ui="Name template; Auto-lang"),
)

BY_ID: dict[str, Command] = {c.id: c for c in COMMANDS}
SHORTCUTS: dict[str, str] = {c.shortcut: c.id for c in COMMANDS if c.shortcut}

# CLI settings, and the web UI preference each one mirrors.
SETTINGS: dict[str, str] = {
    "server": "Server URL used when --server and QC_URL are not set.",
    "token_file": "File holding the API token (instead of QC_API_TOKEN).",
    "clip_template": "Default --template for clips (the web UI's Name box, "
                     "localStorage qc_clip_template).",
    "sub_langs": "Default subtitle languages (the web UI's Auto-lang box, "
                 "localStorage qc_sub_langs).",
    "out_dir": "Default --out-dir for downloading finished jobs.",
}
UI_SETTINGS: dict[str, str] = {
    "qc_clip_template": "clip_template",
    "qc_sub_langs": "sub_langs",
}

# API routes no command covers, and why. All are playback: the browser's player
# streams media from them, which has no meaning on a command line.
EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/media"): "playback: raw media stream for the player",
    ("GET", "/api/media/keyframe"): "playback: seek alignment for the player",
    ("GET", "/api/media/transcode"): "playback: live transcode for the player",
    ("GET", "/api/media/hls"): "playback: HLS playlist for the player",
    ("GET", "/api/media/hls/{token}/{name}"): "playback: HLS segments for the player",
    ("GET", "/api/clips/stream/{clip_path}"): "playback: inline clip stream "
                                              "(clips.download saves the same file)",
}
# Web UI preferences with no CLI meaning.
EXEMPT_UI_SETTINGS: dict[str, str] = {
    "qcYtPlayer": "playback: chooses the YouTube player in the browser",
}


def route_key(method: str, path: str) -> tuple[str, str]:
    """Normalise a route for comparison: FastAPI's ``{x:path}`` becomes ``{x}``."""
    import re

    return method.upper(), re.sub(r"\{(\w+):\w+\}", r"{\1}", path)


def render_markdown() -> str:
    """The generated half of docs/CLI.md: one table per area."""
    out: list[str] = []
    area = None
    for c in COMMANDS:
        if c.area != area:
            area = c.area
            out += ["", f"## {area}", "",
                    "| Command | What it does | Arguments | API | Web UI |",
                    "|---|---|---|---|---|"]
        name = f"`{c.id}`" + (f" (`{c.shortcut}`)" if c.shortcut else "")
        if c.destructive:
            name += " ⚠"
        args = []
        for p in c.params:
            label = p.name.upper() if p.main else "--" + p.name.replace("_", "-")
            if p.local_only:
                label += "¹"
            args.append(f"`{label}`")
        api = f"`{c.method} {c.path}`" if c.method else f"({c.mode})"
        if c.mode == "both":
            api += " · local"
        out.append(f"| {name} | {c.summary} | {' '.join(args)} | {api} | {c.ui} |")
    out += ["", "⚠ destructive: refused with `--safe`. ¹ local mode only.", ""]
    return "\n".join(out)


if __name__ == "__main__":  # python -m quipclipper.commands > docs/CLI.md
    from pathlib import Path as _Path

    _head = _Path(__file__).resolve().parents[2] / "docs" / "CLI.head.md"
    print(_head.read_text() + render_markdown(), end="")
