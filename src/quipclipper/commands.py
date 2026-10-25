"""The command registry: every action the web UI offers, as a CLI command.

One plain-data table drives the CLI (``quipclipper <id>``), the generated
reference ``docs/CLI.md`` and the parity tests in ``web/backend/tests``. The
tests fail when the web app gains a route or the frontend gains an API call
that no command here covers, so a new UI action has to be added here first.

No web framework is imported, so the CLI package stays free of FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    # Part of machine transcription (M21): left out of the CLI's help while transcription is
    # off there (no whisper-cli and model, or QC_TRANSCRIBE=0; D-053), marked in docs/CLI.md.
    transcription: bool = False


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

    ``transcription`` commands (machine transcription, M21) are hidden from the CLI's
    help and ``commands``, and refused locally, while transcription is off there (no
    whisper-cli and model, or QC_TRANSCRIBE=0; D-053); a server with it off answers them
    404. docs/CLI.md lists them with a marker.
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
    transcription: bool = False
    area: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "area", self.id.split(".", 1)[0])

    @property
    def main_param(self) -> Param | None:
        return next((p for p in self.params if p.main), None)


# --- shared parameter sets ------------------------------------------------

_SEARCH_TUNING = (
    Param("min_score", "float", "Drop matches scoring below this (0-100; 100 is the words "
          "verbatim, 75 and under the words in any order).", default=60.0),
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
          "'{show}/S{season}E{episode}_{title}_{timestamp}' (tokens in docs/CLI.md). "
          "Default: the settings value, else '{source}/{timestamp}_{cue}_{folder}'.",
          where=BODY),
    Param("default_sub_track", "int", "Subtitle track (s:N) to mark as the default "
          "in the saved video clip.", where=BODY),
)

# Orphaned machine transcripts (M21): also those of a media file replaced since.
_STALE = Param("stale", "bool", "Also transcripts of media files replaced since they were "
               "made (a re-encode or a new release under the same name).", default=False)

# Machine transcript lines (M21) in a script or search: mixed in (marked), left out, or alone.
_MACHINE = Param("machine", "str", "Machine transcript lines (whisper): include them "
                 "(marked as machine-made), exclude them, or search only them.",
                 default="include", choices=("include", "exclude", "only"), transcription=True)

_JOB_FOLLOW = (
    Param("wait", "bool", "Wait for the job to finish and show its result.",
          where=CLIENT, default=True),
    Param("out_dir", "str", "After waiting, download the job's files into this "
          "folder. Default: the settings value.", where=CLIENT),
)


# Send to LeadStinger by link: open it here, or only print it (an agent, or no desktop).
_OPEN_LINK = (
    Param("open", "bool", "Open the link on this machine (LeadStinger must be installed "
          "here); --no-open only prints it.", where=CLIENT, default=True),
)


COMMANDS: tuple[Command, ...] = (
    # --- server ---------------------------------------------------------------
    Command("server.health", "Show the version and which media tools are installed.",
            "GET", "/api/health", mode="both", shortcut="status",
            ui="Status line at the foot of every page"),
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
                          choices=("json", "vtt")),
                    _MACHINE),
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
    Command("item.link", "A link that opens a file in the web app at a moment, with a range "
            "selected (needs QC_PUBLIC_URL on the server).", "GET", "/api/items/link",
            params=(Param("path", "str", "Media file (or yt:<id>).", main=True, required=True),
                    Param("at", "float", "Seek to this many seconds. Default: the start of "
                          "--sel."),
                    Param("sel", "str", "Range to select, START-END in seconds "
                          "(e.g. 61.2-64.8)."),
                    Param("track", "int", "Subtitle track (s:N) to show.")),
            ui="Copy link (workspace selection; search hit)"),
    Command("item.peaks", "Waveform peaks of a span: min/max pairs as base64 int16 (raw "
            "samples when zoomed past one per bin). Long spans come from a cached overview "
            "built in the background (202 while it builds).", "GET", "/api/items/peaks",
            params=(Param("path", "str", "Media file, yt:<id> or <epub>#seg=N.", main=True,
                          required=True),
                    Param("start", "float", "Span start in seconds.", default=0.0),
                    Param("end", "float", "Span end in seconds. Default: the end of the file."),
                    Param("bins", "int", "How many min/max pairs (1-8192).", default=1000),
                    Param("audio", "int", "Audio stream (a:N).", default=0),
                    Param("keyframes", "bool", "Also list the video keyframes in the span "
                          "(spans up to 60 s): where a lossless cut can start.",
                          default=False)),
            ui="Waveform strip (workspace): drawing, zoom and pan"),
    Command("item.snap", "The nearest zero crossing or quiet point to a time: where to put "
            "a cut so it starts without a click.", "GET", "/api/items/snap",
            params=(Param("path", "str", "Media file, yt:<id> or <epub>#seg=N.", main=True,
                          required=True),
                    Param("t", "float", "Time in seconds.", required=True, flags=("--at",)),
                    Param("mode", "str", "zero: nearest zero crossing; silence: nearest point "
                          "where the level drops below --db.", default="zero",
                          choices=("zero", "silence")),
                    Param("radius", "float", "How far to look, in seconds. Default: 0.01 for "
                          "zero, 1 for silence."),
                    Param("db", "float", "Quiet threshold in dBFS (silence mode).",
                          default=-45.0),
                    Param("audio", "int", "Audio stream (a:N).", default=0)),
            ui="Waveform strip: Snap to zero crossing / silence while dragging a handle"),
    # Machine transcription (M21, D-037): only ever on request, at idle priority.
    Command("item.transcribe", "Transcribe a file's audio with whisper.cpp, for lines the "
            "subtitles leave out: a range (--start/--end), every gap between subtitle lines, "
            "or the whole file. Runs as a background job at idle priority; lines it hears are "
            "searchable and marked as machine-made.", "POST", "/api/items/transcribe",
            params=(Param("path", "str", "Media file.", where=BODY, main=True, required=True),
                    Param("mode", "str", "range (needs --start/--end), gaps (between subtitle "
                          "lines) or file (all of it). Default: range with --start, else gaps.",
                          where=BODY, choices=("range", "gaps", "file")),
                    Param("start", "float", "Range start in seconds.", where=BODY),
                    Param("end", "float", "Range end in seconds.", where=BODY),
                    Param("model", "str", "Whisper model. Default: the settings value, else "
                          "the server's (small.en).", where=BODY, choices=("small.en", "medium.en")),
                    Param("audio", "int", "Audio stream (a:N).", where=BODY, default=0),
                    Param("redo", "bool", "Transcribe again parts done before (e.g. with a "
                          "bigger model).", where=BODY, default=False)) + _JOB_FOLLOW,
            mode="both", response="job",
            ui="Transcribe selection (waveform); Fill gaps / Transcribe file (Transcript card)", transcription=True),
    Command("item.transcript", "Show a file's machine transcript: the lines whisper heard, "
            "with what was transcribed.", "GET", "/api/items/transcript",
            params=(Param("path", "str", "Media file.", main=True, required=True),
                    Param("fmt", "str", "json, or srt / vtt to export the lines.",
                          default="json", choices=("json", "srt", "vtt"))),
            mode="both", ui="Transcript card (workspace)", transcription=True),
    Command("item.transcript-clear", "Delete a file's machine transcript (not a copy in the "
            "library).", "DELETE", "/api/items/transcript",
            params=(Param("path", "str", "Media file.", main=True, required=True),),
            mode="both", destructive=True, ui="Transcript card: Delete transcript", transcription=True),
    Command("item.transcript-copy", "Copy a file's machine transcript into the library as a "
            "subtitle sidecar, <name>.<lang>.whisper.srt, next to the file (writable folders "
            "only). From then on it also shows in the player's captions and in Jellyfin.",
            "POST", "/api/items/transcript/library",
            params=(Param("path", "str", "Media file.", where=QUERY, main=True,
                          required=True),),
            mode="both", destructive=True, ui="Transcript card: Copy to library", transcription=True),
    Command("item.transcript-uncopy", "Remove the machine transcript sidecar (…whisper.srt) "
            "from the library; the transcript itself stays.", "DELETE",
            "/api/items/transcript/library",
            params=(Param("path", "str", "Media file.", main=True, required=True),),
            mode="both", destructive=True, ui="Transcript card: Remove from library", transcription=True),

    # --- search ------------------------------------------------------------------------
    Command("search.item", "Search one file's dialogue.", "GET", "/api/search",
            params=(Param("query", "str", "Dialogue to look for.", main=True, required=True),
                    Param("path", "str", "Media file (or subtitle file's video).",
                          flags=("--video", "-v")),
                    Param("track", "int", "Subtitle track (s:N). Default: the best one."),
                    Param("limit", "int", "Most matches to return.", default=10,
                          flags=("-n",)),
                    *_SEARCH_TUNING,
                    _MACHINE,
                    Param("subs", "str", "Subtitle file to search instead of the video's.",
                          where=CLIENT, flags=("-s",), local_only=True),
                    Param("langs", "str", "Preferred subtitle languages, comma-separated.",
                          where=CLIENT, local_only=True)),
            mode="both", shortcut="search", ui="Top-bar search, scope This video"),
    Command("search.folder", "Search dialogue across every file in one or more folders.",
            "GET", "/api/search/folder",
            params=(Param("query", "str", "Dialogue to look for.", main=True, required=True),
                    Param("path", "list[str]", "Folder(s) to search (repeat for several).",
                          required=True),
                    Param("limit", "int", "Most matches per file.", default=5,
                          flags=("-n",)),
                    *_SEARCH_TUNING,
                    _MACHINE),
            ui="Top-bar search, scope This folder / Whole library"),

    # --- index -------------------------------------------------------------------------
    Command("index.status", "Show how many files in a folder have cached subtitles.",
            "GET", "/api/search/folder/index-status",
            params=(Param("path", "str", "Folder.", main=True, required=True),),
            ui="Dialogue index card"),
    Command("index.build", "Cache the subtitles of every file in a folder (prints "
            "progress).", "POST", "/api/search/folder/index",
            params=(Param("path", "str", "Folder.", main=True, required=True),
                    Param("force", "bool", "Re-read files already cached.", default=False)),
            response="lines", ui="Index N files; Re-index all (Dialogue index card)"),
    Command("index.library", "Index every file under the library roots (or one folder of "
            "any size) that isn't indexed yet, as a background job: one file at a time at "
            "idle priority, cancellable, and a rerun resumes where the last one stopped "
            "(files indexed, or known to have no subtitles, are skipped). Only ever on "
            "request.", "POST", "/api/search/library/index",
            params=(Param("path", "str", "Folder to index instead of the whole library.",
                          main=True),
                    Param("wait", "bool", "Wait for the job to finish and show its result "
                          "(a whole library takes many hours; Ctrl-C cancels it).",
                          where=CLIENT, default=False)),
            response="job",
            ui="Index the whole library (Dialogue index card, library root); Index N files "
               "in the background (a folder over 500 files); ✕ Cancel"),
    Command("index.library-status", "Show the library index job: the one running, and "
            "how far the last run got (kept across restarts).", "GET",
            "/api/search/library/index",
            ui="Dialogue index card (library root)"),
    Command("index.file", "Re-index files whose subtitles changed (what the Bazarr, "
            "Sonarr and Radarr hooks call): media or subtitle files, in the caller's own "
            "paths (the server's QC_PATH_MAP maps them). Queued, each once, and re-indexed "
            "by one job at idle priority; a file already current is left alone.", "POST",
            "/api/index/file",
            # In the body: hundreds of paths in a query string meet HTTP 414 (PA-5).
            params=(Param("path", "list[str]", "Media or subtitle file(s); a subtitle "
                          "file's media file is found next to it.", where=BODY,
                          main=True, required=True),
                    Param("force", "bool", "Re-read files even when their index is "
                          "current.", where=BODY, default=False)),
            ui="Re-index them (the changed-subtitles banner in a folder and in search "
               "results)"),
    Command("index.file-status", "Show the re-index hooks' queue: the job draining it, "
            "the files waiting, and the last results.", "GET", "/api/index/file",
            ui="—"),
    Command("index.changes", "Show the files whose subtitles changed since they were "
            "indexed, as the hourly timestamp-only check (and the folders viewed since) "
            "found them. It only checks: nothing is re-indexed until you ask.", "GET",
            "/api/index/changes", ui="Index out of date: N files (library home)"),
    Command("index.reindex-changed", "Re-index every file `index.changes` lists, in the "
            "background at idle priority, one at a time.", "POST",
            "/api/index/changes/reindex",
            ui="Re-index them (the Index out of date badge)"),
    Command("index.fetch-subs", "Fetch missing YouTube subtitles for every video in a "
            "folder.", "POST", "/api/search/folder/fetch-subs",
            params=(Param("path", "str", "Folder.", main=True, required=True),)
            + _JOB_FOLLOW,
            response="job", ui="⬇ Fetch missing YouTube subtitles; ✕ Cancel"),
    Command("index.transcribe", "Transcribe a folder's files with whisper.cpp, one after "
            "another at idle priority: whole files that have no subtitles (audio without a "
            "transcript included), or the gaps between every file's subtitle lines. Only ever "
            "on request; cancel it like any job.", "POST", "/api/search/folder/transcribe",
            params=(Param("path", "str", "Folder.", where=BODY, main=True, required=True),
                    Param("mode", "str", "nosubs: whole files with no subtitles; gaps: every "
                          "file's gaps between subtitle lines.", where=BODY, default="nosubs",
                          choices=("nosubs", "gaps")),
                    Param("model", "str", "Whisper model. Default: the settings value, else "
                          "the server's (small.en).", where=BODY,
                          choices=("small.en", "medium.en"))) + _JOB_FOLLOW,
            response="job",
            ui="Transcribe files without subtitles; Fill gaps in every file (Dialogue index "
               "card); ✕ Cancel", transcription=True),
    Command("index.transcript-orphans", "List the stored machine transcripts no file needs "
            "any more: their media file was deleted (with --stale, also replaced since), or "
            "they can't be read. Deletes nothing.", "GET", "/api/transcripts/orphans",
            params=(_STALE,), mode="both",
            ui="Library home: the orphaned transcripts count", transcription=True),
    Command("index.transcript-clean", "Delete the stored machine transcripts "
            "`index.transcript-orphans` lists. Never runs on its own; copies in the library "
            "stay. A disk that isn't mounted counts as nothing deleted.", "POST",
            "/api/transcripts/clean", params=(_STALE,), mode="both", destructive=True,
            ui="Library home: Remove orphaned transcripts", transcription=True),
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
                    # The search --index counts in, tuned as search.item's.
                    *(replace(p, where=BODY) for p in _SEARCH_TUNING),
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
    Command("clip.nowplaying", "Clip the line just heard on a Jellyfin client: the cue at or "
            "before its playhead (less --back seconds), cut like clip.create. Needs Jellyfin "
            "on the server.", None, None,
            params=(Param("session", "str", "Which session: its id, or part of its device, "
                          "user or client name. Default: the only one playing a library "
                          "file.", where=CLIENT, flags=("-S",)),
                    Param("back", "float", "Seconds before the playhead to look for the "
                          "line (the playhead lags a few seconds while playing).",
                          where=CLIENT, default=0.0),
                    Param("lines", "int", "How many lines to clip, ending with that one.",
                          where=CLIENT, default=1, flags=("-n",)),
                    Param("before", "float", "Seconds of padding before the line.",
                          where=CLIENT, default=2.0, flags=("-b",)),
                    Param("after", "float", "Seconds of padding after the line.",
                          where=CLIENT, default=2.0, flags=("-a",)),
                    *(Param(p.name, p.type, p.help, where=CLIENT, default=p.default,
                            choices=p.choices, flags=p.flags, negative=p.negative)
                      for p in _CLIP_OPTIONS),
                    *_JOB_FOLLOW),
            mode="client", response="job",
            ui="Now playing card and scope: open at the playhead, then ✂ Make clip"),

    # --- jellyfin --------------------------------------------------------------------------
    Command("jellyfin.sessions", "What each Jellyfin client is playing: device, title, "
            "playhead and the file in the library.", "GET", "/api/jellyfin/sessions",
            shortcut="nowplaying", ui="Now playing card (library) and scope (search box)"),
    Command("jellyfin.lookup", "What Jellyfin knows about a library file or folder: its "
            "title, series, season, episode, year, ids and artwork, and the values its "
            "clip names get.", "GET", "/api/jellyfin/lookup",
            params=(Param("path", "str", "File or folder under the media roots.", main=True,
                          required=True),),
            ui="Posters and titles on library rows, the workspace header and Now playing"),
    Command("jellyfin.reindex", "Rebuild the path index from Jellyfin now (it is rebuilt "
            "hourly on its own), e.g. after adding media.", "POST", "/api/jellyfin/index",
            ui="—"),

    # --- jobs ------------------------------------------------------------------------------
    Command("job.list", "List recent jobs (clips, downloads, YouTube).", "GET", "/api/jobs",
            shortcut="jobs", ui="Jobs popover"),
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
            destructive=True,
            ui="Cancel / Dismiss in the Jobs popover; ✕ Cancel (folder subtitle fetch)"),
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
            destructive=True, ui="Bookmark −/+ buffer"),
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
    Command("clips.list", "List finished clips and LeadStinger set files (a folder of the "
            "clips library, or everything under it), each with its metadata: source, line, "
            "show and episode, codec, channels, duration.",
            "GET", "/api/clips",
            params=(Param("folder", "str", "Subfolder of the clips library.", main=True),
                    Param("recursive", "bool", "Everything under the folder, not just its "
                          "own files.", default=False, flags=("-r",)),
                    Param("q", "str", "Only entries whose line, title, show or file name "
                          "has every word of this.", flags=("--query",)),
                    Param("kind", "str", "Only this kind of entry.",
                          choices=("audio", "video", "gif", "set")),
                    Param("since", "str", "Only entries added after this cursor (the "
                          "cursor of an earlier listing)."),
                    Param("sort", "str", "Order: by path, or newest first.", default="name",
                          choices=("name", "newest"))),
            ui="Clips view"),
    Command("clips.download", "Download a clip or set file from the clips library.", "GET",
            "/api/clips/download/{clip_path}",
            params=(Param("clip_path", "str", "Path inside the clips library.", where=PATH,
                          main=True, required=True),
                    Param("as", "str", "Convert the clip (no cut) to a full-mix FLAC "
                          "keeping its channels; not for set files.", choices=("flac",)),
                    Param("out_dir", "str", "Folder to save into.", where=CLIENT,
                          default=".")),
            response="file", ui="Clip Download link"),
    Command("clips.backfill", "Give clips cut before metadata sidecars existed a best-effort "
            "one (ffprobe, the file name, Jellyfin); re-attach moved clips' sidecars and "
            "drop those of deleted clips. Safe to run again.", "POST", "/api/clips/backfill",
            params=_JOB_FOLLOW, response="job", destructive=True, ui="—"),

    # --- LeadStinger (M14): hand clips to the soundboard ------------------------------------
    Command("leadstinger.set", "Make a LeadStinger set file (.lsset) from JSON lines on stdin: "
            "search hits, bookmarks or clips-library entries, each cut as FLAC with its "
            "padding and trimmed to its line.", "POST", "/api/leadstinger/set",
            params=(Param("name", "str", "The set's name in LeadStinger. Default: the movie "
                          "or show, else quipclipper and the date.", where=BODY, main=True),
                    Param("before", "float", "Seconds of padding before each line (a "
                          "bookmark's own padding wins); kept as handle audio around the "
                          "tile's trim.", where=CLIENT, default=0.0),
                    Param("after", "float", "Seconds of padding after each line.",
                          where=CLIENT, default=0.0),
                    Param("path", "str", "Media file for hits that don't carry one "
                          "(search.item results).", where=CLIENT),
                    *_JOB_FOLLOW),
            mode="client", response="job",
            ui="Send to LeadStinger button beside Make clip (when the server can't name "
               "its own address), or the toggle beside Clip / Export selected"),
    Command("leadstinger.send", "Put the clips on stdin straight into LeadStinger on this "
            "machine: a new set, or appended to an existing one with --set.", None, None,
            params=(Param("name", "str", "The new set's name (ignored with --set). "
                          "Default: the movie or show, else quipclipper and the date.",
                          where=CLIENT, main=True),
                    Param("set", "str", "Append to this existing LeadStinger set instead of "
                          "importing a new one.", where=CLIENT),
                    Param("before", "float", "Seconds of padding before each line (a "
                          "bookmark's own padding wins); kept as handle audio around the "
                          "tile's trim.", where=CLIENT, default=0.0),
                    Param("after", "float", "Seconds of padding after each line.",
                          where=CLIENT, default=0.0),
                    Param("path", "str", "Media file for hits that don't carry one.",
                          where=CLIENT),
                    Param("leadstinger_cli", "str", "The leadstinger-cli program.",
                          where=CLIENT, default="leadstinger-cli"),
                    Param("data_dir", "str", "LeadStinger library folder (its --data-dir).",
                          where=CLIENT)),
            mode="client", ui="—"),

    Command("leadstinger.quip", "Send one line to LeadStinger: cut it as FLAC with its padding "
            "into the clips library (LeadStinger/) and open its leadstinger://quip link, so "
            "LeadStinger downloads it into a tile trimmed to the line. The link names "
            "QC_PUBLIC_URL, else the advertised URL, else the address this CLI reached the "
            "server at.", "POST", "/api/leadstinger/quip",
            params=(Param("path", "str", "Media file the line is in.", where=BODY, main=True,
                          required=True),
                    Param("start", "float", "Where the line starts (seconds).", where=BODY,
                          required=True),
                    Param("end", "float", "Where the line ends (seconds).", where=BODY,
                          required=True),
                    Param("title", "str", "The tile's title, usually the line.", where=BODY),
                    Param("before", "float", "Seconds of padding before the line, kept as "
                          "handle audio around the tile's trim.", where=BODY, default=2.0),
                    Param("after", "float", "Seconds of padding after the line.", where=BODY,
                          default=2.0),
                    Param("set", "str", "LeadStinger set to put the tile in (made if missing; "
                          "default: its current set).", where=BODY),
                    *_OPEN_LINK),
            mode="client", response="job",
            ui="Send to LeadStinger button beside Make clip, or the toggle with Clip / "
               "Export selected on one line"),
    Command("leadstinger.link", "The leadstinger://quip link for a clip already in the clips "
            "library, opened here so LeadStinger downloads it into a new tile. The server it "
            "names is chosen as for leadstinger.quip.", "GET", "/api/leadstinger/link",
            params=(Param("clip", "str", "Path inside the clips library (as clips.download "
                          "takes it).", main=True, required=True),
                    Param("title", "str", "The tile's title (default: the file name)."),
                    Param("start", "float", "Trim start inside the clip (seconds)."),
                    Param("end", "float", "Trim end inside the clip (seconds)."),
                    Param("set", "str", "LeadStinger set to put the tile in (made if missing; "
                          "default: its current set)."),
                    *_OPEN_LINK),
            mode="client",
            ui="Send to LeadStinger on a clip row (Clips library), or the toggle with "
               "Export selected on one clip"),

    Command("leadstinger.connect", "Open LeadStinger's Connect prompt for this server (a "
            "leadstinger://pair link): LeadStinger asks to connect, then pairs, so nobody "
            "pastes a token. The server is QC_PUBLIC_URL, else the advertised URL, else the "
            "address this CLI reached it at.", None, None,
            params=_OPEN_LINK, mode="client",
            ui="Connect LeadStinger (Settings ▸ Connected apps)"),

    # --- pairing: an app such as LeadStinger asks for access, a person lets it in ----------
    Command("pair.list", "List the apps waiting for access (pairing requests): their name, "
            "host, code, address and expiry.", "GET", "/api/pair",
            ui="The pairing banner on every page"),
    Command("pair.approve", "Let an app in: approve its pairing request. Check first that "
            "the code is the one the app shows. Its next poll collects its own client token.",
            "POST", "/api/pair/{pair_id}/approve",
            params=(Param("pair_id", "str", "Request id (from pair.list).", where=PATH,
                          main=True, required=True),),
            destructive=True, ui="Allow on the pairing banner"),
    Command("pair.deny", "Turn down an app's pairing request.", "POST",
            "/api/pair/{pair_id}/deny",
            params=(Param("pair_id", "str", "Request id (from pair.list).", where=PATH,
                          main=True, required=True),),
            destructive=True, ui="Deny on the pairing banner"),

    # --- connected apps (paired clients) ------------------------------------------------------
    Command("clients.list", "List the connected apps: name, host, when they paired and when "
            "they were last used.", "GET", "/api/clients",
            ui="Settings ▸ Connected apps"),
    Command("clients.revoke", "Disconnect an app: revoke its client token. 'self' revokes "
            "the client token this CLI is using.", "DELETE", "/api/clients/{client_id}",
            params=(Param("client_id", "str", "Client id (from clients.list), or self.",
                          where=PATH, main=True, required=True),),
            destructive=True, ui="Revoke (Settings ▸ Connected apps)"),

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
            mode="client", destructive=True,
            ui="Name field (clip bar); Subtitle languages (Settings)"),
)

BY_ID: dict[str, Command] = {c.id: c for c in COMMANDS}
SHORTCUTS: dict[str, str] = {c.shortcut: c.id for c in COMMANDS if c.shortcut}

# CLI settings, and the web UI preference each one mirrors.
SETTINGS: dict[str, str] = {
    "server": "Server URL used when --server and QC_URL are not set.",
    "token_file": "File holding the API token (instead of QC_API_TOKEN).",
    "clip_template": "Default --template for clips (the web UI's Name box, "
                     "localStorage qc_clip_template).",
    "sub_langs": "Default subtitle languages (the web UI's Subtitle languages setting, "
                 "localStorage qc_sub_langs).",
    "out_dir": "Default --out-dir for downloading finished jobs.",
    "stt_model": "Default --model for transcription (small.en or medium.en; the web UI's "
                 "Transcription model setting, localStorage qc_stt_model).",
}
# Settings of machine transcription (D-053): left out of settings.list while it's off.
TRANSCRIPTION_SETTINGS = ("stt_model",)
UI_SETTINGS: dict[str, str] = {
    "qc_clip_template": "clip_template",
    "qc_sub_langs": "sub_langs",
    "qc_stt_model": "stt_model",
}

# API routes no command covers, and why. Playback (the browser's player streams media
# from them) and presentation (artwork) have no meaning on a command line.
EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/media"): "playback: raw media stream for the player",
    ("GET", "/api/media/keyframe"): "playback: seek alignment for the player",
    ("GET", "/api/media/transcode"): "playback: live transcode for the player",
    ("GET", "/api/media/hls"): "playback: HLS playlist for the player",
    ("GET", "/api/media/hls/{token}/{name}"): "playback: HLS segments for the player",
    ("GET", "/api/clips/stream/{clip_path}"): "playback: inline clip stream "
                                              "(clips.download saves the same file)",
    ("GET", "/api/jellyfin/image/{item_id}"): "presentation: a poster for the browser "
                                              "(jellyfin.lookup names the image)",
    # Pairing: the app's side. A person answers with pair.approve / pair.deny.
    ("POST", "/api/pair"): "the app's side of pairing: LeadStinger asks for access",
    ("GET", "/api/pair/{pair_id}"): "the app's side of pairing: LeadStinger polls its own "
                                    "request with that request's poll secret",
    ("GET", "/api/clients/check"): "the nginx password gate asks whether a client token is "
                                   "current (clients.list shows the clients)",
}
# Web UI preferences with no CLI meaning.
EXEMPT_UI_SETTINGS: dict[str, str] = {
    "qcYtPlayer": "playback: chooses the YouTube player in the browser",
    "qc_theme": "presentation: light or dark appearance in the browser",
    "qc_sub_size": "presentation: subtitle size on the video in the browser",
    "qc_sub_bg": "presentation: solid background behind the video's subtitles in the browser",
    "qc_stt_open": "presentation: whether the machine transcript bar is open in the browser",
}


def route_key(method: str, path: str) -> tuple[str, str]:
    """Normalise a route for comparison: FastAPI's ``{x:path}`` becomes ``{x}``."""
    import re

    return method.upper(), re.sub(r"\{(\w+):\w+\}", r"{\1}", path)


def render_markdown() -> str:
    """The generated half of docs/CLI.md: one table per area. Machine transcription's
    commands and parameters are marked ², since they exist only where it is on (D-053)."""
    out: list[str] = []
    area = None
    for c in COMMANDS:
        if c.area != area:
            area = c.area
            out += ["", f"## {area}", "",
                    "| Command | What it does | Arguments | API | Web UI |",
                    "|---|---|---|---|---|"]
        name = f"`{c.id}`" + (f" (`{c.shortcut}`)" if c.shortcut else "")
        if c.transcription:
            name += " ²"
        if c.destructive:
            name += " ⚠"
        args = []
        for p in c.params:
            label = p.name.upper() if p.main else "--" + p.name.replace("_", "-")
            if p.local_only:
                label += "¹"
            if p.transcription:
                label += "²"
            args.append(f"`{label}`")
        api = f"`{c.method} {c.path}`" if c.method else f"({c.mode})"
        if c.mode == "both":
            api += " · local"
        out.append(f"| {name} | {c.summary} | {' '.join(args)} | {api} | {c.ui} |")
    out += ["", "⚠ destructive: refused with `--safe`. ¹ local mode only. ² machine "
            "transcription: only where whisper-cli and a model are installed, see "
            "[TRANSCRIPTION.md](TRANSCRIPTION.md).", ""]
    return "\n".join(out)


if __name__ == "__main__":  # python -m quipclipper.commands > docs/CLI.md
    from pathlib import Path as _Path

    _head = _Path(__file__).resolve().parents[2] / "docs" / "CLI.head.md"
    print(_head.read_text() + render_markdown(), end="")
