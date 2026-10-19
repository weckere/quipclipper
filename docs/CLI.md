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
  downloads the finished files. Ctrl-C cancels a job you're waiting on.
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
| `sub_langs` | Default subtitle languages (the web app's Auto-lang box). |
| `out_dir` | Default `--out-dir` for finished jobs. |

Clip name templates use `{timestamp}`, `{end}`, `{duration}`, `{cue}`, `{title}`,
`{source}`, `{year}`, `{season}`, `{episode}` and `{date}`; `/` makes subfolders.
The server's default is `{source}/{timestamp}_{cue}_{title}`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Failed: a server error, a failed job, or no matches |
| 2 | Usage error, or the server rejected the request as invalid |
| 3 | No server configured, or the server can't be reached |
| 4 | The server refused the token |
| 5 | Refused by `--safe` |

# Commands

## server

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `server.health` (`status`) | Show the version and which media tools are installed. |  | `GET /api/health` · local | Status line at the top of every page |
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
| `item.script` | Print a file's subtitle script (every cue with its times). | `PATH` `--track` `--offset` `--fmt` | `GET /api/items/subtitles` | Script view; subtitle track picker |
| `item.reindex` | Re-read a file's subtitles (or re-fetch a YouTube transcript), clearing its cache. | `PATH` | `POST /api/items/subtitles/reindex` | ↻ Reindex subtitles (item) |
| `item.fetch-subs` | Fetch subtitles from YouTube for one library video. | `PATH` `--wait` `--out-dir` | `POST /api/media/fetch-subs` | ⬇ Fetch subtitles from YouTube (item) |

## search

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `search.item` (`search`) | Search one file's dialogue. | `QUERY` `--path` `--track` `--limit` `--min-score` `--max-span` `--subs¹` `--langs¹` | `GET /api/search` · local | Item dialogue search |
| `search.folder` | Search dialogue across every file in one or more folders. | `QUERY` `--path` `--limit` `--min-score` `--max-span` | `GET /api/search/folder` | Folder dialogue search |

## index

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `index.status` | Show how many files in a folder have cached subtitles. | `PATH` | `GET /api/search/folder/index-status` | Folder index banner |
| `index.build` | Cache the subtitles of every file in a folder (prints progress). | `PATH` `--force` | `POST /api/search/folder/index` | Index now; ↻ Reindex subtitles (folder) |
| `index.fetch-subs` | Fetch missing YouTube subtitles for every video in a folder. | `PATH` `--wait` `--out-dir` | `POST /api/search/folder/fetch-subs` | ⬇ Fetch missing YouTube subtitles; ✕ Cancel |

## clip

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `clip.create` (`clip`) | Cut a clip: around a line of dialogue (QUERY) or between --start and --end. | `QUERY` `--path` `--start` `--end` `--match-index` `--track` `--cue-text` `--before` `--after` `--kind` `--lossless` `--audio-format` `--audio-tracks` `--backend` `--chapters` `--remux-first` `--embed-subs` `--split-channels` `--split-format` `--split-groups` `--include-lfe` `--template` `--default-sub-track` `--wait` `--out-dir` `--subs¹` `--langs¹` `--pick¹` `--limit¹` `--min-score¹` `--max-span¹` `--out¹` `--yes¹` `--hwaccel¹` `--vaapi-device¹` | `POST /api/clip` · local | ✂ Make clip; ✂ on a search hit |
| `clip.batch` | Cut one clip per JSON line on stdin: search hits, bookmarks or clips-library entries (pipe from search.folder, bookmark.list or clips.list with --json). | `--path` `--kind` `--lossless` `--audio-format` `--audio-tracks` `--backend` `--chapters` `--remux-first` `--embed-subs` `--split-channels` `--split-format` `--split-groups` `--include-lfe` `--template` `--default-sub-track` `--wait` `--out-dir` | (client) | Clip selected / Export selected (search, bookmarks, clips) |

## job

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `job.list` (`jobs`) | List recent jobs (clips, downloads, YouTube). |  | `GET /api/jobs` | Job status lines |
| `job.get` | Show one job. | `JOB_ID` | `GET /api/jobs/{job_id}` | Job progress polling |
| `job.wait` | Wait for a job to finish. | `JOB_ID` `--out-dir` | (client) | Job progress polling |
| `job.cancel` | Cancel a queued or running job, or forget a finished one. | `JOB_ID` | `DELETE /api/jobs/{job_id}` | ✕ Cancel |
| `job.download` | Download a finished job's file (all files if FILENAME is omitted). | `JOB_ID` `FILENAME` `--out-dir` | `GET /api/jobs/{job_id}/download/{filename}` | ⤓ link after a clip finishes |

## bookmark

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `bookmark.list` | List bookmarks (all, or one file's). | `--path` | `GET /api/bookmarks` | Bookmarks view; item bookmark list |
| `bookmark.add` | Save a range as a bookmark. | `PATH` `--start` `--end` `--label` `--before` `--after` `--sub-track` `--audio-track` `--channel-subset` `--cue` | `POST /api/bookmarks` | ★ Save bookmark; ★ on a search hit |
| `bookmark.update` | Rename a bookmark or change its padding. | `BOOKMARK_ID` `--label` `--before` `--after` | `PATCH /api/bookmarks/{bookmark_id}` | Bookmark −/+ buffer |
| `bookmark.delete` ⚠ | Delete a bookmark. | `BOOKMARK_ID` | `DELETE /api/bookmarks/{bookmark_id}` | Bookmark ✕ |
| `bookmark.clear` ⚠ | Delete every bookmark. |  | `DELETE /api/bookmarks` | Clear all |
| `bookmark.batch` | Bookmark one search hit per JSON line on stdin (pipe from search.folder --json). | `--path` | (client) | Bookmark selected |

## clips

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `clips.list` | List finished clips (a folder of the clips library). | `FOLDER` | `GET /api/clips` | Clips view |
| `clips.download` | Download a clip from the clips library. | `CLIP_PATH` `--out-dir` | `GET /api/clips/download/{clip_path}` | Clip Download link |

## settings

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `settings.list` | Show the CLI settings. |  | (client) | — |
| `settings.get` | Show one CLI setting. | `KEY` | (client) | — |
| `settings.set` | Change a CLI setting (empty value clears it). | `KEY` `VALUE` | (client) | Name template; Auto-lang |

⚠ destructive: refused with `--safe`. ¹ local mode only.
