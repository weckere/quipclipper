# quipclipper command reference

Everything the web app can do, the `quipclipper` command can do. Each action in the
web UI is a command here, and the test suite fails if the web app gains an action
that has no command (`web/backend/tests/test_cli_parity.py`). The only exceptions
are playback: streaming video to the browser's player has no meaning on a command
line.

The tables below are generated from the command registry,
`src/quipclipper/commands.py`; edit that file, then regenerate this one with
`python -m quipclipper.commands > docs/CLI.md`. A test fails if this file is stale.

## Two ways to run

**Against a server.** Most commands drive a quipclipper web server over its HTTP
API, so you can search and clip a library that lives on another machine:

```sh
export QC_URL=https://quipclipper.example.net      # or --server URL
export QC_API_TOKEN_FILE=~/.config/quipclipper/token # or QC_API_TOKEN=...
quipclipper ls /movies
quipclipper search.folder "you're killing me" --path /movies
quipclipper clip "you're killing me" -v "/movies/The Sandlot (1993)/The.Sandlot.mkv" \
    -t audio --out-dir ~/clips
```

Paths are the server's paths. The token is the server's `QC_API_TOKEN`; the CLI
sends it as `X-API-Key` for the app and as basic-auth user `api` for the nginx
password gate in front of it.

**On local files.** `search`, `clip`, `tracks` and `status` also work with no
server, on files on this machine (this is the original CLI, unchanged):

```sh
quipclipper search "i'll be back" --subs movie.srt
quipclipper clip "i'll be back" --video movie.mkv --type audio
```

Which one runs: `--local` wins, then `--server`, then `QC_URL`, then the `server`
setting; with none of them, commands run locally, and commands that need a server
exit with code 3.

## Names, output and scripting

- Commands are named `<area>.<action>` (`bookmark.add`). The common ones also have
  a short name: `status`, `ls`, `tracks`, `search`, `clip`, `jobs`.
  `quipclipper commands` lists them all; `quipclipper help <command>` shows one.
- Output is readable text. Add `--json` (before the command name) for one line of
  JSON, the same shape the HTTP API returns. Errors in JSON mode are
  `{"error": {"code": ..., "message": ...}}`.
- Jobs (clips, YouTube adds and downloads, subtitle backfills) are followed until
  they finish; `--no-wait` returns the job id at once, and `--out-dir DIR`
  downloads the finished files. Ctrl-C cancels a job you're waiting on (a clip cut
  that has started can't stop: it finishes on the server), and stops a batch.
- `--safe` refuses every command marked ⚠ (it changes or deletes data). Use it for
  agents.
- Batch commands read the `--json` output of another command on stdin, the way the
  web app's "selected" bars work:

  ```sh
  quipclipper --json search.folder "baby" --path /movies \
    | quipclipper --json clip.batch --kind audio --out-dir ~/clips
  ```

- `quipclipper run script.jsonl` runs one command per line,
  `{"command": "bookmark.add", "params": {...}}`, and prints one JSON result per line.

## Settings

`quipclipper settings.set <key> <value>` stores defaults in
`~/.config/quipclipper/cli.json`. They are the CLI's version of the web app's
per-browser preferences.

| Setting | Meaning |
|---|---|
| `server` | Server URL when `--server` and `QC_URL` aren't set. |
| `token_file` | File holding the API token. |
| `clip_template` | Default `--template` (the web app's Name box). |
| `sub_langs` | Default subtitle languages (the web app's Subtitle languages setting). |
| `out_dir` | Default `--out-dir` for finished jobs. |

Clip name templates use these tokens; `/` makes subfolders, and a token with no value
is dropped along with the separators around it.

| Token | Value |
|---|---|
| `{timestamp}`, `{end}` | The clip's start and end, as `HH-MM-SS` |
| `{duration}` | Its length in whole seconds |
| `{cue}` | The line of dialogue (first 80 characters) |
| `{title}` | The episode's or movie's title from Jellyfin; else the folder name |
| `{show}` | The series from Jellyfin (a movie's title for a movie); else the folder name |
| `{season}`, `{episode}` | Two digits, from Jellyfin; else from an `S03E26` in the file name |
| `{year}` | The production year from Jellyfin; else a year in the file name |
| `{folder}` | The folder name: the show folder past a `Season N` folder, or the movie's |
| `{source}` | The source file's name without its extension |
| `{date}` | Today, as `YYYY-MM-DD` |

The Jellyfin values need Jellyfin on the server (`jellyfin.lookup PATH` shows what it
knows about a file); a file Jellyfin doesn't know, and a local-mode cut, use the
parsed values. The server's default is `{source}/{timestamp}_{cue}_{folder}`. The
earlier default `{source}/{timestamp}_{cue}_{title}` is read as it, so clips named
by default land where they always did.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Failed: a server error, a failed job, or no matches |
| 2 | Usage error, or the server rejected the request as invalid |
| 3 | No server configured, the server can't be reached, or it redirects (use the URL it redirects to) |
| 4 | The server refused the token |
| 5 | Refused by `--safe` |

# Commands

## server

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `server.health` (`status`) | Show the version and which media tools are installed. |  | `GET /api/health` · local | Status line at the foot of every page |
| `server.config` | Show the server's configuration (media roots, limits, subtitle languages). |  | `GET /api/config` | Loaded on start |

## library

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `library.roots` | List the media roots. |  | `GET /api/library/roots` | Library home |
| `library.browse` (`ls`) | List a folder (no path: the media roots; 'yt:': YouTube items; an EPUB: its chapters). | `PATH` | `GET /api/library/browse` | Folder rows, breadcrumbs, Back |
| `library.find` | Find files and folders by name. | `QUERY` `--path` `--limit` | `GET /api/library/search` | Library filter box |

## youtube

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `youtube.add` | Add a YouTube video by URL (fetches its transcript, not the video). | `URL` `--wait` `--out-dir` | `POST /api/youtube` | YouTube Add button |
| `youtube.remove` ⚠ | Remove a YouTube item (and any downloaded copy). | `VIDEO_ID` | `DELETE /api/youtube/{video_id}` | YouTube row ✕ |
| `youtube.download` | Download a YouTube item's full video to the server. | `VIDEO_ID` `--wait` `--out-dir` | `POST /api/youtube/{video_id}/download` | ⬇ Download video |
| `youtube.delete-download` ⚠ | Delete a YouTube item's downloaded video (the item stays). | `VIDEO_ID` | `DELETE /api/youtube/{video_id}/download` | 💾 Remove download |

## item

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `item.info` (`tracks`) | Show a file's streams, duration and subtitle tracks. | `PATH` `--langs` | `GET /api/items` · local | Opening an item; stream selector |
| `item.script` | Print a file's subtitle script (every cue with its times). | `PATH` `--track` `--offset` `--fmt` `--machine` | `GET /api/items/subtitles` | Script view; subtitle track picker |
| `item.reindex` | Re-read a file's subtitles (or re-fetch a YouTube transcript), clearing its cache. | `PATH` | `POST /api/items/subtitles/reindex` | ↻ Reindex subtitles (item) |
| `item.fetch-subs` | Fetch subtitles from YouTube for one library video. | `PATH` `--wait` `--out-dir` | `POST /api/media/fetch-subs` | ⬇ Fetch subtitles from YouTube (item) |
| `item.link` | A link that opens a file in the web app at a moment, with a range selected (needs QC_PUBLIC_URL on the server). | `PATH` `--at` `--sel` `--track` | `GET /api/items/link` | Copy link (workspace selection; search hit) |
| `item.peaks` | Waveform peaks of a span: min/max pairs as base64 int16 (raw samples when zoomed past one per bin). Long spans come from a cached overview built in the background (202 while it builds). | `PATH` `--start` `--end` `--bins` `--audio` `--keyframes` | `GET /api/items/peaks` | Waveform strip (workspace): drawing, zoom and pan |
| `item.snap` | The nearest zero crossing or quiet point to a time: where to put a cut so it starts without a click. | `PATH` `--t` `--mode` `--radius` `--db` `--audio` | `GET /api/items/snap` | Waveform strip: Snap to zero crossing / silence while dragging a handle |
| `item.transcribe` | Transcribe a file's audio with whisper.cpp, for lines the subtitles leave out: a range (--start/--end), every gap between subtitle lines, or the whole file. Runs as a background job at idle priority; lines it hears are searchable and marked as machine-made. | `PATH` `--mode` `--start` `--end` `--model` `--audio` `--redo` `--wait` `--out-dir` | `POST /api/items/transcribe` · local | Transcribe selection (waveform); Fill gaps / Transcribe file (Transcript card) |
| `item.transcript` | Show a file's machine transcript: the lines whisper heard, with what was transcribed. | `PATH` `--fmt` | `GET /api/items/transcript` · local | Transcript card (workspace) |
| `item.transcript-clear` ⚠ | Delete a file's machine transcript (not a copy in the library). | `PATH` | `DELETE /api/items/transcript` · local | Transcript card: Delete transcript |
| `item.transcript-copy` ⚠ | Copy a file's machine transcript into the library as a subtitle sidecar, <name>.<lang>.whisper.srt, next to the file (writable folders only). From then on it also shows in the player's captions and in Jellyfin. | `PATH` | `POST /api/items/transcript/library` · local | Transcript card: Copy to library |
| `item.transcript-uncopy` ⚠ | Remove the machine transcript sidecar (…whisper.srt) from the library; the transcript itself stays. | `PATH` | `DELETE /api/items/transcript/library` · local | Transcript card: Remove from library |

## search

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `search.item` (`search`) | Search one file's dialogue. | `QUERY` `--path` `--track` `--limit` `--min-score` `--max-span` `--machine` `--subs¹` `--langs¹` | `GET /api/search` · local | Top-bar search, scope This video |
| `search.folder` | Search dialogue across every file in one or more folders. | `QUERY` `--path` `--limit` `--min-score` `--max-span` `--machine` | `GET /api/search/folder` | Top-bar search, scope This folder / Whole library |

## index

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `index.status` | Show how many files in a folder have cached subtitles. | `PATH` | `GET /api/search/folder/index-status` | Dialogue index card |
| `index.build` | Cache the subtitles of every file in a folder (prints progress). | `PATH` `--force` | `POST /api/search/folder/index` | Index N files; Re-index all (Dialogue index card) |
| `index.fetch-subs` | Fetch missing YouTube subtitles for every video in a folder. | `PATH` `--wait` `--out-dir` | `POST /api/search/folder/fetch-subs` | ⬇ Fetch missing YouTube subtitles; ✕ Cancel |
| `index.transcribe` | Transcribe a folder's files with whisper.cpp, one after another at idle priority: whole files that have no subtitles (audio without a transcript included), or the gaps between every file's subtitle lines. Only ever on request; cancel it like any job. | `PATH` `--mode` `--model` `--wait` `--out-dir` | `POST /api/search/folder/transcribe` | Transcribe files without subtitles; Fill gaps in every file (Dialogue index card); ✕ Cancel |

## clip

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `clip.create` (`clip`) | Cut a clip: around a line of dialogue (QUERY) or between --start and --end. | `QUERY` `--path` `--start` `--end` `--match-index` `--track` `--cue-text` `--before` `--after` `--kind` `--lossless` `--audio-format` `--audio-tracks` `--backend` `--chapters` `--remux-first` `--embed-subs` `--split-channels` `--split-format` `--split-groups` `--include-lfe` `--template` `--default-sub-track` `--wait` `--out-dir` `--subs¹` `--langs¹` `--pick¹` `--limit¹` `--min-score¹` `--max-span¹` `--out¹` `--yes¹` `--hwaccel¹` `--vaapi-device¹` | `POST /api/clip` · local | ✂ Make clip; ✂ on a search hit |
| `clip.batch` | Cut one clip per JSON line on stdin: search hits, bookmarks or clips-library entries (pipe from search.folder, bookmark.list or clips.list with --json). | `--path` `--kind` `--lossless` `--audio-format` `--audio-tracks` `--backend` `--chapters` `--remux-first` `--embed-subs` `--split-channels` `--split-format` `--split-groups` `--include-lfe` `--template` `--default-sub-track` `--wait` `--out-dir` | (client) | Clip selected / Export selected (search, bookmarks, clips) |
| `clip.nowplaying` | Clip the line just heard on a Jellyfin client: the cue at or before its playhead (less --back seconds), cut like clip.create. Needs Jellyfin on the server. | `--session` `--back` `--lines` `--before` `--after` `--kind` `--lossless` `--audio-format` `--audio-tracks` `--backend` `--chapters` `--remux-first` `--embed-subs` `--split-channels` `--split-format` `--split-groups` `--include-lfe` `--template` `--default-sub-track` `--wait` `--out-dir` | (client) | Now playing card and scope: open at the playhead, then ✂ Make clip |

## jellyfin

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `jellyfin.sessions` (`nowplaying`) | What each Jellyfin client is playing: device, title, playhead and the file in the library. |  | `GET /api/jellyfin/sessions` | Now playing card (library) and scope (search box) |
| `jellyfin.lookup` | What Jellyfin knows about a library file or folder: its title, series, season, episode, year, ids and artwork, and the values its clip names get. | `PATH` | `GET /api/jellyfin/lookup` | Posters and titles on library rows, the workspace header and Now playing |
| `jellyfin.reindex` | Rebuild the path index from Jellyfin now (it is rebuilt hourly on its own), e.g. after adding media. |  | `POST /api/jellyfin/index` | — |

## job

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `job.list` (`jobs`) | List recent jobs (clips, downloads, YouTube). |  | `GET /api/jobs` | Jobs popover |
| `job.get` | Show one job. | `JOB_ID` | `GET /api/jobs/{job_id}` | Job progress polling |
| `job.wait` | Wait for a job to finish. | `JOB_ID` `--out-dir` | (client) | Job progress polling |
| `job.cancel` ⚠ | Cancel a queued or running job, or forget a finished one. | `JOB_ID` | `DELETE /api/jobs/{job_id}` | Cancel / Dismiss in the Jobs popover; ✕ Cancel (folder subtitle fetch) |
| `job.download` | Download a finished job's file (all files if FILENAME is omitted). | `JOB_ID` `FILENAME` `--out-dir` | `GET /api/jobs/{job_id}/download/{filename}` | ⤓ link after a clip finishes |

## bookmark

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `bookmark.list` | List bookmarks (all, or one file's). | `--path` | `GET /api/bookmarks` | Bookmarks view; item bookmark list |
| `bookmark.add` | Save a range as a bookmark. | `PATH` `--start` `--end` `--label` `--before` `--after` `--sub-track` `--audio-track` `--channel-subset` `--cue` | `POST /api/bookmarks` | ★ Save bookmark; ★ on a search hit |
| `bookmark.update` ⚠ | Rename a bookmark or change its padding. | `BOOKMARK_ID` `--label` `--before` `--after` | `PATCH /api/bookmarks/{bookmark_id}` | Bookmark −/+ buffer |
| `bookmark.delete` ⚠ | Delete a bookmark. | `BOOKMARK_ID` | `DELETE /api/bookmarks/{bookmark_id}` | Bookmark ✕ |
| `bookmark.clear` ⚠ | Delete every bookmark. |  | `DELETE /api/bookmarks` | Clear all |
| `bookmark.batch` | Bookmark one search hit per JSON line on stdin (pipe from search.folder --json). | `--path` | (client) | Bookmark selected |

## clips

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `clips.list` | List finished clips and LeadStinger set files (a folder of the clips library, or everything under it), each with its metadata: source, line, show and episode, codec, channels, duration. | `FOLDER` `--recursive` `--q` `--kind` `--since` `--sort` | `GET /api/clips` | Clips view |
| `clips.download` | Download a clip or set file from the clips library. | `CLIP_PATH` `--as` `--out-dir` | `GET /api/clips/download/{clip_path}` | Clip Download link |
| `clips.backfill` ⚠ | Give clips cut before metadata sidecars existed a best-effort one (ffprobe, the file name, Jellyfin); re-attach moved clips' sidecars and drop those of deleted clips. Safe to run again. | `--wait` `--out-dir` | `POST /api/clips/backfill` | — |

## leadstinger

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `leadstinger.set` | Make a LeadStinger set file (.lsset) from JSON lines on stdin: search hits, bookmarks or clips-library entries, each cut as FLAC with its padding and trimmed to its line. | `NAME` `--before` `--after` `--path` `--wait` `--out-dir` | `POST /api/leadstinger/set` | Send to LeadStinger button beside Make clip (no QC_PUBLIC_URL), or the toggle beside Clip / Export selected |
| `leadstinger.send` | Put the clips on stdin straight into LeadStinger on this machine: a new set, or appended to an existing one with --set. | `NAME` `--set` `--before` `--after` `--path` `--leadstinger-cli` `--data-dir` | (client) | — |
| `leadstinger.quip` | Send one line to LeadStinger: cut it as FLAC with its padding into the clips library (LeadStinger/) and open its leadstinger://quip link, so LeadStinger downloads it into a tile trimmed to the line. Needs QC_PUBLIC_URL on the server. | `PATH` `--start` `--end` `--title` `--before` `--after` `--set` `--open` | `POST /api/leadstinger/quip` | Send to LeadStinger button beside Make clip, or the toggle with Clip / Export selected on one line (needs QC_PUBLIC_URL) |
| `leadstinger.link` | The leadstinger://quip link for a clip already in the clips library, opened here so LeadStinger downloads it into a new tile. Needs QC_PUBLIC_URL on the server. | `CLIP` `--title` `--start` `--end` `--set` `--open` | `GET /api/leadstinger/link` | Send to LeadStinger on a clip row (Clips library), or the toggle with Export selected on one clip (needs QC_PUBLIC_URL) |

## settings

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `settings.list` | Show the CLI settings. |  | (client) | — |
| `settings.get` | Show one CLI setting. | `KEY` | (client) | — |
| `settings.set` ⚠ | Change a CLI setting (empty value clears it). | `KEY` `VALUE` | (client) | Name field (clip bar); Subtitle languages, Transcription model (Settings) |

⚠ destructive: refused with `--safe`. ¹ local mode only.
