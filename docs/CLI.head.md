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
  they finish (`index.library` returns at once unless `--wait`: a library run takes
  hours); `--no-wait` returns the job id at once, and `--out-dir DIR`
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
