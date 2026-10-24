# quipclipper Web API

The quipclipper web app is backed by a plain HTTP+JSON API — the same one the
browser UI runs on. It's designed to be driven by agents and scripts, not just
the frontend: browse the library, search dialogue, cut clips, and download them,
all over HTTP.

- **Base URL:** wherever the app is served, e.g. `http://mediabox:8896`. All
  endpoints are under `/api/`.
- **Format:** requests and responses are JSON (file downloads stream bytes).
  Errors are `{"detail": "..."}` with a 4xx/5xx status; a request that fails
  validation (a 422 from a parameter's limits) has `detail` as a list of
  `{loc, msg, type}` field errors instead.
- **Times** are seconds (floats); `*_ts` fields are `HH:MM:SS.mmm` strings.
- **Paths** are in-container paths under a configured media root
  (`QC_MEDIA_ROOTS`), e.g. `/media/movies/Heat (1995)/Heat.mkv` — exactly the
  `path` values that `/api/library/browse` returns.

## Authentication

The backend itself has no login; access is controlled at two layers.

**1. The gate (nginx).** When `QC_PASSWORD` is set the whole site is behind HTTP
basic auth. A programmatic client gets through it in one of two ways:

- **API token as the basic-auth password** (recommended, one secret):
  set `QC_API_TOKEN`, then `curl -u api:<token>`. nginx accepts the built-in
  `api` user and the backend recognises the token.
- **A normal basic-auth user** (`-u quip:<password>`) plus the token as
  `X-API-Key`, if you'd rather not use the `api` user. (Not `Bearer`: the gate
  needs the `Authorization` header for its own basic auth.)

If `QC_PASSWORD` is unset the site is open and no basic auth is needed.

A **hook token** (`QC_HOOK_TOKEN`) is a low-privilege token for the
[re-index hooks](#re-index-hooks) (Bazarr, Sonarr, Radarr): the backend accepts it as
`X-API-Key`, `Bearer` or a Basic password, but only on `/api/index/file`. Behind the password
gate send it as `-u hook:<token>`; the gate's `hook` login knows only the first of several
tokens.

**2. Token identification (backend).** Present `QC_API_TOKEN` as **any** of:

| Method | Header / form |
|---|---|
| API key header | `X-API-Key: <token>` |
| Bearer token | `Authorization: Bearer <token>` |
| Basic password | `Authorization: Basic base64(api:<token>)` (i.e. `-u api:<token>`) |

A recognised token marks the request as a **machine client**, which **exempts it
from the CSRF header** below. An *explicit* but wrong `X-API-Key`/`Bearer` is
rejected with `401`. Set several comma-separated tokens for rotation
(`QC_API_TOKEN=old,new`); the backend accepts all of them, but the gate's `api`
basic-auth user knows only the first, so behind the gate send a later one as
`X-API-Key` with a normal user.

**Client tokens (paired apps).** An app such as LeadStinger can [pair](#pairing) instead of
being handed `QC_API_TOKEN`: a person approves it, and it gets a client token of its own,
`qcc_` and 43 URL-safe characters. A client token is accepted wherever `QC_API_TOKEN` is
(`X-API-Key`, `Bearer`, or the Basic password of the `api` user) and marks a machine client
in the same way, **even when no `QC_API_TOKEN` is set**. An explicit (`X-API-Key` /
`Bearer`) `qcc_…` value that isn't a current client token is rejected with `401`, so an app
learns that it was revoked. A client token can't approve, deny or list pairing requests, or
list or revoke clients (`403`). Behind the password gate, nginx lets a request with a client
token in `X-API-Key` or `Bearer` through after asking the backend whether it is current
(`GET /api/clients/check`); the gate's `api` login still knows only `QC_API_TOKEN`.

**CSRF header (browser clients only).** State-changing requests
(`POST`/`PUT`/`PATCH`/`DELETE`) that are **not** token-authenticated must carry
`X-Quipclipper: 1`. This stops a malicious web page from riding your cached
basic-auth cookie. **If you authenticate with an API token you don't need it** —
tokens are the intended path for scripts.

**Proxy secret (optional).** If `QC_PROXY_SECRET` is set, nginx injects it and
the backend refuses any request that arrives without it (except `/api/health`).
It's a transport gate between nginx and the backend; normal clients going through
nginx never see it. Direct-to-backend clients must send
`X-Quip-Proxy-Secret: <secret>`.

## Interactive & machine-readable docs

FastAPI publishes the live schema, served behind the same gate:

- `GET /openapi.json` — the OpenAPI 3 contract (feed this to an agent/codegen).
- `GET /docs` — Swagger UI. `GET /redoc` — ReDoc.

## Endpoint reference

### Library & items
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/library/roots` | List configured media roots. |
| GET | `/api/library/browse?path=` | List a folder (or an EPUB book's segments). Omit `path` for the roots. |
| GET | `/api/library/search?query=&path=` | Filename search across the library (or under `path`). |
| GET | `/api/items?path=&langs=` | Probe one media item: streams, duration, subtitle tracks. |
| GET | `/api/items/link?path=&at=&sel=&track=` | A [deep link](#deep-links) that opens the file in the web app at `at` seconds with `sel` (`START-END`) selected: `{path, at, sel, track, link}`. 503 without `QC_PUBLIC_URL`; the path is checked like `/api/items` (403 outside the roots, 404 missing). |
| GET | `/api/items/peaks?path=&start=&end=&bins=&audio=&keyframes=` | [Waveform](#waveform-peaks-and-snap) peaks of `start..end` on audio stream `a:audio`, mixed to mono. 202 while the overview of a long span is still being built. |
| GET | `/api/items/snap?path=&t=&mode=&radius=&db=&audio=` | The nearest zero crossing (`mode=zero`) or quiet point (`mode=silence`) to `t`: where a cut starts cleanly. |
| GET | `/api/items/subtitles?path=&track=&offset=&fmt=` | Subtitles as WebVTT (`fmt=vtt`, default — the player's captions) or JSON cues `{start, end, text, speaker}` (`fmt=json` — what the script view uses). |
| POST | `/api/items/subtitles/reindex?path=` | Rebuild the subtitle cache for one file. |
| POST | `/api/media/fetch-subs?path=` | Backfill one YouTube-sourced library video's subtitles (async job — poll `job_id`). The video's id is read from its `[<id>]` filename or a sibling `.info.json`; a `.vtt` sidecar is written next to it. 400 = no id found or the folder is read-only (needs `writableMediaRoots`). Job `progress` ends `ok`/`skipped`/`none`. |
| POST | `/api/search/folder/fetch-subs?path=` | Backfill every id'd, subtitle-less YouTube video in a folder (async job with `progress`; cancellable via `DELETE /api/jobs/{id}`). Skips videos that already have subtitles. `{job_id: null, candidates: 0}` = nothing to do. 400 = read-only folder. |

`/api/library/browse` entries are `{name, path, is_dir, is_video, is_audio, is_book,
has_sidecar, index_state}` (plus `jellyfin` when Jellyfin knows the file).
`index_state` is the file's place in the dialogue index (the subtitle cache that
`/api/search/folder/index` fills): `"indexed"` (its subtitles are cached and current, so a
search reads them without extracting anything), `"no_subs"` (indexing found no text
subtitles: no sidecar, no text track) or `"not_indexed"` (not yet, or the subtitles changed
since). It is `null` for folders, books and the roots, and `/api/library/search` and the
YouTube folder leave it out. Working it out costs one stat per file; the folder is listed once.

Both backfill endpoints appear in the UI only for YouTube-sourced videos in
**writable** roots. `/api/items` flags such a file with `youtube_id` and
`backfill_writable: true` so the frontend can offer the action.

### Waveform: peaks and snap

`/api/items/peaks` and `/api/items/snap` take a library file, a `yt:<id>` ref (the download
when there is one, else the resolved stream) or an `<epub>#seg=N` segment, with the same
path checks as `/api/items` (403 outside the roots, 404 missing, 422 with no such audio
stream). Both decode with ffmpeg, seeking the way a clip cut does, so a time here is a time
in the clip.

**Peaks** (`bins` 1–8192, default 1000; `end` defaults to the end of the file) returns
`{path, start, end, bins, audio, duration, rate, source, kind, data, peak, overview}`:

- `source: "overview"`: from a peak pyramid built once per file, audio stream and mtime in the
  background (8 kHz mono, 20 ms buckets of min/max, cached in the state dir as
  `peaks/*.qcpk`, about 1.4 MB per 2-hour film, 512 MB kept in all). It serves spans over 2
  minutes, and, once built, shorter ones zoomed out to 40 ms or more per bin (`rate` is
  then 8000). Until then such a request answers **202** with
  `kind: "pending"` and `overview: {state, progress}` (`queued`, `building`, `error`).
- `source: "detail"`: a decode of just the span, for spans up to 2 minutes, at the stream's
  own rate (capped at 48 kHz) up to 15 s and 16 kHz beyond.
- `kind: "peaks"`: `data` is base64 little-endian int16, `bins` interleaved (min, max) pairs
  (full scale ±32768). `kind: "samples"`: zoomed past one sample per bin, `data` is the raw
  mono samples, `count` of them, the first at `t0`, `rate` apart. Detail decodes and snaps
  are *anchored*: they seek to a whole 10 s mark less 1 s before the span and count samples
  from there, as an exact (re-encoded) clip cut does, so the drawing, `snapped` and the
  cut agree on the sample even where `-ss` alone wouldn't (DTS in Matroska carries
  timestamps too coarse for it). `t0` is the sample nearest `start`. Sample 0 of a
  decode is at its file time even when the audio stream starts later than the video.
- `peak`: the largest sample in the file (from the overview), else in the span.
- `keyframes=true` (video, spans up to 60 s) adds `keyframes`: the times a lossless video cut
  can start at.

**Snap** returns `{path, t, mode, radius, rate, found, snapped, delta}`. `mode=zero` (default
radius 0.01 s, at most 0.5) finds the nearest sign change of the mono mix and returns the
sample of the pair nearer zero (`value`; on a stereo or surround stream the mix crosses
zero where each channel need not, so a multichannel WAV/FLAC cut there can still click:
`mode=silence` is the safer snap for those); `mode=silence` (default radius 1 s, at most 10)
finds the nearest 20 ms window whose RMS is below `db` dBFS (default −45) and returns its
centre (`level_db`); with none, `found` is false and `snapped` is the quietest point.

### YouTube sources

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/youtube` | Add a video: `{"url": "<watch URL>"}` → yt-dlp fetches subtitles + metadata (no video download). Asynchronous: returns `{job_id, status}`; poll the job like a clip job (a yt-dlp failure shows up as the job's `error`). Already added: `{job_id: null, status: "done", entry}`. 400 = not a YouTube URL. |
| DELETE | `/api/youtube/{video_id}` | Remove an added video (metadata + transcript + any download). |
| POST | `/api/youtube/{video_id}/download` | Download the full video to the server (async — poll the returned `job_id` like a clip job; `job_id: null` = already downloaded). Once present, playback/keyframe/clipping all use the local file, and the stream-source restrictions below no longer apply. |
| DELETE | `/api/youtube/{video_id}/download` | Delete the local video file; the transcript and metadata stay. |

Added videos are addressed by the **virtual ref `yt:<video-id>`** everywhere a
`path` is accepted: `/api/items`, `/api/items/subtitles` (+`/reindex`, which
re-fetches the transcript), `/api/search`, `/api/bookmarks`,
`/api/media/keyframe` (echoes the requested time until downloaded), and `POST
/api/clip`. The pseudo-folder itself is `path=yt:` — browse it for the listing,
or pass it to `/api/search/folder` to dialogue-search every added video.
Preview streams via `/api/media/transcode?path=yt:<id>` (raw `/api/media` and
HLS don't apply); the web UI can alternatively embed the official YouTube
player, which is purely client-side — no API involved. Clips cut straight from
resolved stream URLs, so `split_channels` and `backend=mkvmerge` return 400 for
`yt:` refs. Requires `yt-dlp` on the backend PATH (reported by `/api/health`).

### Dialogue search
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/search?path=&query=&track=&limit=&min_score=&max_span=` | Rank dialogue matches in one file. |
| GET | `/api/search/folder?path=&query=&limit=` | Search dialogue across a folder (repeat `path=` for several; `yt:` and EPUB books count). |
| GET | `/api/search/folder/index-status?path=` | How much of a folder's subtitles are cached, for a folder of any size (a listing per folder and the cache's names: no extraction, no probe): `{total, indexed, no_subs, changed, changed_files, skipped, cap}`. `changed` counts files indexed before their subtitles changed (listed, up to 1000, in `changed_files`; see [below](#changed-subtitles)). `no_subs` counts files indexing found to have no subtitles at all (no sidecar, no text track); they are never offered for indexing. `cap` (500) is the most files `POST …/index` takes in one request; a bigger folder is indexed with `POST /api/search/library/index?path=`. `skipped` is always `false` now (kept for older clients). |
| POST | `/api/search/folder/index?path=&force=` | Pre-extract subtitles for a folder of at most `cap` files (streams progress; 400 over it). |
| POST | `/api/search/library/index?path=` | Start the **library index**: every media file under the library roots (or under `path`, a folder of any size) that isn't indexed yet, as a background job. Returns `{job_id, existing, paths}`; while one runs, this returns it with `existing: true` (one at a time). 400 when `path` isn't a folder. See below. |
| GET | `/api/search/library/index` | `{job, last}`: the running library index job (as `GET /api/jobs/{id}`, or null), and `last`: the last run's `result` (the current one's while it runs) plus `updated`, kept in the state dir across restarts; `status` is `scanning` or `indexing` while it runs, then `done`, `cancelled`, `failed`, or `interrupted` when a restart (or a server stop) cut it short; null before the first run. |
| GET | `/api/index/changes` | Files whose subtitles changed since indexing, as the hourly check and the views found them. See [below](#changed-subtitles). |
| POST | `/api/index/changes/reindex` | Queue all of them for the re-index job. |
| POST | `/api/index/file?path=&force=` | Re-index files whose subtitles changed (the [re-index hooks](#re-index-hooks)): repeat `path=`, or send a JSON body. Queued; 202 (422 when no path could be queued, 400 for more than 1000 paths or a body that isn't JSON). |
| GET | `/api/index/file` | The re-index hooks' queue: `{job_id, job, pending, recent, token}`. |

`/api/search` returns `{query, count, matches: [{index, score, text, speaker,
start, end, start_ts, end_ts, cue_count, link}]}`. The `index` is what you pass back as
`match_index` when clipping. `/api/search/folder` returns `{query, folders,
files_scanned, not_indexed, changed, changed_files, books_not_searched, capped, count, truncated, matches: [{file, path, score,
text, speaker, start, end, start_ts, end_ts, cue_count, link}]}`: `limit` is the most matches
per file (default 5, at most 20); `count` is every match found, `matches` the best 500 of
them (`truncated` when there were more). `query` is at most 300 characters (422 beyond:
scoring grows with the square of the query's words, over every indexed line). Hidden folders
(`.snapshots`) are not searched, and a linked file counts once. It searches only **indexed** files, those whose subtitles
are cached (opened once, or indexed with `POST …/index`), however many there are, and
never extracts subtitles itself. `files_scanned` counts the files searched, `not_indexed` the media files
left out because they aren't indexed, and `books_not_searched` the read-along EPUB books
found in the folders (a book has no index; pass a book itself as `path` to search it).
`changed` counts the files left out because their subtitles changed since they were
indexed (up to 1000 listed in `changed_files`, for a re-index), and `not_indexed` only files
never indexed. `capped: true` means any of these is above 0, so a line not found may be in a
file it didn't search. Its hits have no `index`: clip one with its
`path`, `start`, `end` and `text` as `cue_text`. `link` (here, on `/api/search/folder` hits, bookmarks and
clips-library entries) is a [deep link](#deep-links) to that line, present only when
`QC_PUBLIC_URL` is set.

The **library index** (M20) is how a whole library becomes searchable. It runs only when
someone starts it (`POST /api/search/library/index`, `quipclipper index.library`, or the
library page's **Index the whole library**): no timer, no startup or scan hook, no setting
starts it. It indexes one file at a time, and every ffprobe and ffmpeg it runs is at idle
priority (`SCHED_IDLE`, nice 19, the idle I/O class, through `quipclipper.idle`), so a
player's HLS encodes always come first. A file whose subtitles are cached and current, or
that is known to have none (`no_subs`), is skipped, so a rerun after a cancel, a failure or
a restart picks up where the last one stopped. `DELETE /api/jobs/{id}` cancels it, killing
the extraction in flight; what is done stays. The job's `result` is `{paths, whole_library, status
(scanning | indexing | done | cancelled | failed), files, already, todo, done, indexed,
no_subs, failed, current, eta_s, files_per_hour, errors, summary}`: `files` media files were
found, `already` of them needed nothing, and `done` of the `todo` rest are finished, with
`eta_s` a rough estimate of the seconds left. A file it couldn't read is counted in
`failed` (up to 20 listed in `errors`) and tried again next run.

### Re-index hooks

How Bazarr, Sonarr and Radarr (or anything) tell quipclipper that a file's subtitles
changed, so it re-indexes that file instead of leaving it out of search. Setup, the hook
script and the troubleshooting table: [HOOKS.md](HOOKS.md).

`POST /api/index/file` takes the files as `path=` query parameters (repeat it; at most 1000),
or a JSON body: `{"paths": [...], "force": false}`, or a Sonarr or Radarr webhook body as it
comes (the paths in `episodeFile`, `episodeFiles`, `movieFile`, `renamedEpisodeFiles` and
`renamedMovieFiles`; a `Test` event answers 200 with `queued: 0`). Each path is a media file
or a subtitle file (its media file is the one in the same folder whose name, up to its
extension, starts the subtitle's name; the longest such name wins), in the caller's own
paths: `QC_PATH_MAP` (`/tv=/media/shows:/movies=/media/movies`, `:` or `;` between pairs,
longest prefix first) maps them onto this server's before they are checked against the media
roots. It returns at once with `{job_id, queued, files: [{path, media, status}], event}` (`queued` counts media files, so an
episode and its subtitle count once), a
`status` per path: `queued`, `already_queued` (waiting already: Bazarr calls once per
subtitle), `outside_roots` (with the `mapped` path), `not_found`, `no_media` (a subtitle with
no media file beside it), `not_media` or `invalid`. 202 when any path was queued, 422 (same
body plus `detail`) when none was, 400 with no path at all.

The queued files are re-indexed by one job (label "Re-index changed subtitles (low
priority)") in the library index's single-worker pool, so it never runs beside a library run;
a running library run takes the queued files between its own instead. Each file is read at
idle priority, like the library index, and only if its index entry is out of date
(`force=true` re-reads it anyway). `GET /api/index/file` lists the files `pending` and the
last 50 `recent` results, each `{path, status, at, error?}` with `status` `indexed`,
`current`, `no_subs`, `missing`, `failed` or `cancelled`, and `token`: whether this request's
token was accepted (false when the server has no token set, so a hook's POST would meet the
CSRF guard). Cancelling the job (`DELETE /api/jobs/{id}`) drops the files still waiting; one
cancelled before it started leaves them for the next request's job. While a library run is going,
it takes the queued files between its own, and once it has emptied the queue it cancels the
re-index job waiting behind it; if the run is cancelled while it re-indexes a hook's file,
that file goes back to the front of the queue. A path already under a media root is used as
it is; only other paths go through `QC_PATH_MAP`.

<a id="changed-subtitles"></a>**Changed subtitles.** A file's index entry belongs to one version
of its subtitle source (its sidecar's date, or the file's own for embedded subtitles), so
`index_state` (library rows) is `changed` for a file that was indexed but whose subtitles
changed since, as opposed to `not_indexed` for one never indexed. Index-status and folder
search count and list such files, and remember them; so does a background pass over the
library roots every `QC_INDEX_CHECK_INTERVAL` seconds (3600, the first two minutes after a
start; `0` = off), which lists folders and reads file dates only, at the idle I/O class (its
CPU priority stays normal: the thread holds the interpreter lock, and parked at idle CPU it
would stall every request), never extracting or reading a subtitle file. It is skipped while
the previous one still runs and while a library run or a re-index job is going, and a media
root that is missing or empty (an unmounted pool) keeps the last result and reports the
error. `GET /api/index/changes` returns
`{changed, files, last_check: {at, files, changed, duration_s, error?}, interval_s,
checking}` (up to 1000 `files`), and `POST /api/index/changes/reindex` queues every one of
them for the re-index job (as `POST /api/index/file` answers; 200 with `queued: 0` when
there are none). Nothing is ever re-indexed by the checks themselves. A file leaves the list
when it is re-indexed (by that job, a library run or `POST /api/items/subtitles/reindex`)
or when a check sees it current.

The hooks call `/api/index/file` with an API token, or with a **hook token**
(`QC_HOOK_TOKEN`, comma-separated to rotate, or a file named by `QC_HOOK_TOKEN_FILE`) sent the
same ways (`X-API-Key`, `Bearer`, or `-u hook:<token>`). A hook token opens `GET` and
`POST /api/index/file` and nothing else: anywhere else it gets 403. Behind a password gate,
the Docker image's nginx accepts the `hook` login when `QC_HOOK_TOKEN` is set on the `web`
service. A Basic login named `hook` is refused (403) on every other route whatever its
password, so a token the server doesn't know (unset, or rotated on one side only) never passes
as a person. With only hook tokens set (no `QC_API_TOKEN`), an unknown `Bearer`/`X-API-Key`
gets 401 on `/api/index/file` only, and is ignored elsewhere. Without a token (the web app's **Re-index them**), a POST needs the CSRF header like
any other.

`score` (0 to 100, one decimal) is 100 only when the line holds the query's words verbatim:
in order and contiguous, after case, punctuation, apostrophes and subtitle markup are
normalised away. The words in order with a typo, a word missing or an extra word score up to
99, and the words in any order at most 75, less the more other words lie between them
(D-041). A word the query gives twice needs to be in the line twice. Text beyond ASCII is
NFKC-normalised, and combining marks (accents, Indic vowel signs) belong to their word.
Matches come best first; at an equal score
the line closest to the whole query comes first, then (folder search) by path and time, so
equal hits keep one order. `min_score` (default 60)
drops matches below it; `min_score=100` keeps only verbatim lines.

### Media (preview/streaming)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/media?path=` | Stream the raw file. |
| GET | `/api/media/keyframe?path=&time=` | Nearest keyframe at/just before `time`. |
| GET | `/api/media/transcode?path=&...` | On-the-fly transcode stream (desktop preview). |
| GET | `/api/media/hls?path=&venc=` | HLS playlist (iOS preview). Segment/init URIs inside it point at `/api/media/hls/{token}/{name}` — clients just follow the playlist. |

### Clipping & jobs
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/clip` | Start a clip job (JSON body, see below). Returns `{job_id, status}`. |
| GET | `/api/jobs` | Recent jobs. |
| GET | `/api/jobs/{id}` | One job's status/result: `files` it wrote (`{name, size, clip}`, where `clip` is the file's path in the clips library, as `/api/clips/download/{clip}` takes it) and, for some jobs, a `result` object (the link of a Send to LeadStinger job). |
| DELETE | `/api/jobs/{id}` | Cancel a queued job / prune a finished one → `{cancelled}`; a running job that can stop (folder subtitle backfill, a LeadStinger set, the clips backfill) → `{cancelling}`; any other running job (a clip cut) → 409, and it finishes. |
| GET | `/api/jobs/{id}/download/{filename}` | Download a finished clip by name. |

A job is `{id, status, label, created[, progress, started, finished, elapsed, files, result, error]}`
where `status` is `queued|running|done|failed|cancelled`, `files` is
`[{name, size, clip}]` once done, `result` is what some jobs hand back (above), and
`error` is the failure message on a failed job.

### Clips library
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/clips?folder=&recursive=&q=&kind=&since=&sort=` | List the clips library: a folder's subfolders, clips and set files, or everything under it, each with its metadata. Each clip has a `link` that opens it in the workspace (with `QC_PUBLIC_URL`). The fields are a versioned contract: [Clips library contract](#clips-library-contract). |
| GET | `/api/clips/download/{path}?as=flac` | Download a clip or set file; `as=flac` converts a clip to a full-mix FLAC first. |
| GET | `/api/clips/stream/{path}` | Stream a finished clip (HTTP Range works, for previews). |
| POST | `/api/clips/backfill` | Give clips cut before sidecars existed their metadata (job). |

### LeadStinger set files
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/leadstinger/set` | Cut lines as FLAC with their padding and zip them as a [LeadStinger](https://github.com/weckere/leadstinger) set file (job). |

The body is `{name?, items: [{path, start?, end?, title?, notes?, track?, before?, after?}]}`
(1–200 items). Each item is a search hit, a bookmark or a clips-library file (no `start`:
the whole file, no padding). `before`/`after` are the clip's padding as for `/api/clip`
(0–60 s, default 0). The job cuts `[start − before, end + after]` as a full-mix FLAC
(every channel kept) and writes `LeadStinger/<name>.lsset` in the clips library:
`manifest.json` (LeadStinger's starter-pack format) plus one folder for the set. Each
tile's `trim_start`/`trim_end` sit on the line, so the padding is handle audio and the cut
points can still be moved in LeadStinger. Without `name`, the set is named after the
movie or show when every clip comes from it, else `quipclipper <date>`; LeadStinger
numbers a name that's already taken. Download it with
`GET /api/jobs/{id}/download/<name>.lsset`; import it with LeadStinger's File ▸ Import
Set…, a drop on its window, or `leadstinger-cli set.import`.

### Send to LeadStinger (links)
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/leadstinger/quip` | Cut one line as FLAC with its padding into the clips library and make its `leadstinger://quip` link (job). |
| GET | `/api/leadstinger/link?clip=&title=&start=&end=&set=` | The `leadstinger://quip` link for a clip already in the clips library. |

The link's `server` is `QC_PUBLIC_URL` (this server's base URL as LeadStinger reaches it);
without it, the URL the [LAN advertiser](#lan-advertising) announces (`state_dir/advertise.json`);
without that, the base URL the request reached the app at: `X-Forwarded-Proto`,
`X-Forwarded-Host` and `X-Forwarded-Prefix` from a proxy in front when present, else
`X-Quip-Host` (the `Host` the browser sent, port included, as the bundled nginx passes it
on), else the `Host` header, and the connection's scheme. The bundled nginx (Docker and
NixOS) leaves the `X-Forwarded-*` headers to a proxy in front of it, so the web UI's links
name the address the browser is using. When none of these
gives a usable URL, both answer 503. `/api/config`'s `link_server` says which URL that is
for the asking browser. The link names this server and a clip path, never a token:
LeadStinger downloads `GET /api/clips/download/{clip}` itself with the token it got by
[pairing](#pairing) or was connected with (`leadstinger-cli quip.connect <url> --token -`),
and asks before it trusts a server it doesn't know yet. [Deep links](#deep-links) still need
`QC_PUBLIC_URL`. The format is LeadStinger's own
(decision D-076 there, its `docs/QUIPCLIPPER.md`):

```text
leadstinger://quip?server=<QC_PUBLIC_URL>&clip=<clip path>&title=<text>&start=<s>&end=<s>&set=<name>
```

Every value is percent-encoded as `encodeURIComponent` does (a `+` goes out as `%2B`);
`title`, `start`, `end` and `set` are left out when empty. `title` is cut to one line of
200 characters and `set` to 100. `start`/`end` are plain decimals (at most 3 places).

`POST /api/leadstinger/quip` takes `{path, start, end, title?, before?, after?, set?}`: the
line's span in the source, the tile's title (usually the line), the padding kept either
side as handle audio around the tile's trim (`before`/`after`, default 2 s each, at most
60; 0 = none) and the LeadStinger set to put the tile in (made there if missing; default
its current set). The job cuts `[start − before, end + after]`, clamped to the file (a
line near either end gets a shorter handle), as a full-mix FLAC
into `LeadStinger/` in the clips library. The finished job carries
`result: {link, clip, start, end}`, where `start`/`end` are the line *inside* the clip:

```json
{"id": "…", "status": "done", "files": [{"name": "S03E26_00-33-20_Resistance_is_futile.flac", "size": 412345}],
 "result": {"link": "leadstinger://quip?server=https%3A%2F%2Fquip.example&clip=LeadStinger%2FS03E26_00-33-20_Resistance_is_futile.flac&title=Resistance%20is%20futile.&start=2&end=3.6&set=Borg",
            "clip": "LeadStinger/S03E26_00-33-20_Resistance_is_futile.flac", "start": 2.0, "end": 3.6}}
```

`GET /api/leadstinger/link` takes `clip` as `/api/clips/download/{path}` does (relative to
the clips library) and returns `{link, clip}`; without `start`/`end` the tile plays the
whole clip. Open the link in a browser or with `open`/`xdg-open` on the machine with
LeadStinger.

### Pairing

Protocol `quipclipper.pair/1`: an app gets a client token without anyone copying a secret.
The app sends only who it is; a person approves it in the web UI (the banner "LeadStinger on
laptop wants access, code 7F3K. Allow / Deny") or with the CLI (`pair.list`, `pair.approve`,
`pair.deny`); the app's next poll collects its token, once. `QC_PAIRING=0` turns every
`/api/pair…` route into `404` (clients already paired keep working).

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/api/pair` | the app (CSRF header, no token) | Ask for access: `{name, host}` → `{id, code, poll_secret, expires_in}`. |
| GET | `/api/pair/{id}` | the app (`X-Pair-Secret`) | Poll: `pending`, `approved` with `token` and `client_id` (once), `denied` or `expired`. |
| GET | `/api/pair` | a person | The requests waiting: `{requests: [{id, name, host, code, address, created, expires_at}]}`. |
| POST | `/api/pair/{id}/approve` | a person | Let it in: `{status: "approved", client_id}`. |
| POST | `/api/pair/{id}/deny` | a person | Turn it down: `{status: "denied"}`. |
| GET | `/api/clients` | a person | The paired apps: `{clients: [{id, name, host, created, last_used}]}`. |
| DELETE | `/api/clients/{id}` | a person | Revoke one: `{removed: true\|false}`. |
| DELETE | `/api/clients/self` | the app (its client token) | Revoke its own token: `{removed: true}`; `401` without a client token. |
| GET | `/api/clients/check` | the password gate | `200 {client_id}` for a current client token, else `401`. |

"A person" is a browser request (the CSRF header on POST and DELETE) or the server's own
`QC_API_TOKEN`; a client token gets `403` there, so a paired app can't let others in or
throw them out. Times are UTC ISO 8601 with `Z`.

- **Asking:** `name` 1 to 60 characters, `host` 0 to 100; no control or invisible characters
  (line breaks, bidi overrides, zero-width joiners), else `422`. `id` is 22 URL-safe
  characters, `poll_secret` 43; `code` is 4 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ`
  (no 0/O, 1/I/L), shown by the app and in the banner so the person can compare them.
  `expires_in` is 600 seconds. At most 5 requests wait at once and at most 10 new ones a
  minute come from one address (`X-Real-IP` from the bundled nginx, else the connection's);
  past either, `429 {"detail": …}`.
- **Polling:** every 2 s, with `X-Pair-Secret: <poll_secret>`. An unknown id and a wrong or
  missing secret both answer `404`, so a poll reveals nothing. `approved` hands out the
  token once and the request is gone after that (a later poll is `404`); `denied` and
  `expired` answer for a minute, then `404`. An approval nobody collects within a minute
  (and before the request's expiry) is dropped with its client.
- **Storage:** requests live in memory (a restart forgets them; the app asks again). Clients
  live in `state_dir/clients.json` (0600, written atomically): `{clients: [{id, name, host,
  token_sha256, created, last_used}]}`. The token itself is never stored. `last_used` is
  written at most once a minute.
- **Behind the password gate** (`QC_PASSWORD` / `passwordFile`), nginx lets exactly
  `POST /api/pair` and `GET /api/pair/<id>` through without the password. Listing, approving
  and denying stay behind it.

```sh
# The app's side
curl -s -X POST -H 'X-Quipclipper: 1' -H 'Content-Type: application/json' \
  -d '{"name": "LeadStinger", "host": "laptop"}' https://quip.example/api/pair
# {"id": "Qk…", "code": "7F3K", "poll_secret": "x6…", "expires_in": 600}
curl -s -H 'X-Pair-Secret: x6…' https://quip.example/api/pair/Qk…
# {"status": "pending"} … then {"status": "approved", "token": "qcc_…", "client_id": "V-2…"}
```

`/api/config` says whether a server pairs and who it is:

```json
{"pairing": true, "server_id": "8c1f0b2e…", "server_name": "nas", "link_server": "http://nas.local:8896"}
```

`server_id` is 32 hex characters made once and kept in `state_dir/server-id`, so an app can
tell that `nas.local:8896` and `192.168.1.20:8896` are one server. `server_name` is
`QC_SERVER_NAME`, else the name the LAN advertiser saw (in Docker the app's own host name is
the container's), else the host name without `.local`; at most 63 characters.

### LAN advertising

Opt-in (`QC_ADVERTISE=1`): `quipclipper-web advertise` announces the server over
mDNS/Bonjour until stopped, so LeadStinger can list it without anyone typing an address.

| Field | Value |
|---|---|
| Service | `_quipclipper._tcp.local.`, instance name `server_name` (dots become `-`) |
| Port | the port browsers use: `--port`, `QC_ADVERTISE_PORT`, else `QC_PUBLIC_URL`'s |
| TXT `v` | `1` |
| TXT `url` | the canonical base URL: `QC_PUBLIC_URL`, else `http://<host>.local:<port>` |
| TXT `id` | `server_id` |

It announces on the LAN interfaces only (not loopback, container bridges, VPNs or the
tailnet) and writes `state_dir/advertise.json` (`{url, name}`), which Send to LeadStinger
links fall back to; the file is removed when it stops. Docker runs it as its own service on
the host's network (multicast doesn't cross Docker's bridge); the NixOS module advertises
with Avahi instead (`advertise.enable`), and its record leaves out `id` (LeadStinger reads it
from `/api/config`). **Tailnet-only installs should not advertise.**

### Now playing (Jellyfin)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/jellyfin/sessions` | What each Jellyfin client is playing or has paused, its playhead and the file in the library. |

Needs `QC_JELLYFIN_URL` and an API key (503 without them; 502 when Jellyfin can't be
reached or refuses the key). The reply, newest report first:

```json
{"sessions": [{"id": "5f2…", "user": "wiz", "device": "Living room TV", "client": "Jellyfin Android TV",
  "item_id": "a41…", "type": "Episode", "title": "The Best of Both Worlds", "series": "Star Trek: TNG",
  "season": 3, "episode": 26, "year": 1990, "duration": 2716.4,
  "position": 2003.1, "reported_position": 1998.0, "reported_at": "2026-10-20T01:02:03.123456Z",
  "paused": false, "jellyfin_path": "/shows/Star Trek TNG/Season 3/S03E26.mkv",
  "path": "/srv/shows/Star Trek TNG/Season 3/S03E26.mkv", "note": null}]}
```

`position` is the playhead in seconds: the client's last report (`reported_position`,
sent about every 10 s while playing) plus the time since (`reported_at`), at most 30 s
more; a paused client is exact. `path` is the file under the media roots
(`QC_JELLYFIN_PATH_MAP` turns Jellyfin's paths into them), or `null` with a `note` saying
why. Sessions with nothing playing are left out. To clip the line just heard, take the
cue at or before `position` from `/api/items/subtitles` and `POST /api/clip` it; the CLI's
`clip.nowplaying` does exactly that. Each session also has `art`, its poster
(`{"id", "type", "tag"}`, the series' for an episode; see the image proxy below), or `null`.

### Titles and artwork (Jellyfin)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/jellyfin/lookup?path=` | What Jellyfin knows about a library file or folder, and the values its clip names get. |
| POST | `/api/jellyfin/index` | Rebuild the path index now; returns `{index}` (its status, as below). |
| GET | `/api/jellyfin/image/{item_id}?type=Primary\|Thumb&w=&tag=` | An item's poster (`Primary`) or still (`Thumb`), at most `w` px wide (default 300, capped at 800), through the server so the API key stays there. |

The server keeps a **path index**: one listing of every movie, episode, track and
audiobook in Jellyfin (`GET /Items?Recursive=true`, read in pages of 5,000), mapped
through `QC_JELLYFIN_PATH_MAP` onto the media roots and cached in
`QC_STATE_DIR/jellyfin-index.json`. It is built when the server starts and rebuilt in the
background once an hour old; lookups never wait for Jellyfin. A file it doesn't hold
(Jellyfin hasn't scanned it, or its folder has no path map entry) gets the names parsed
from its path, as before.

```json
{"path": "/srv/shows/Star Trek TNG/Season 3/S03E26.mkv", "known": true,
 "item": {"id": "a41…", "type": "Episode", "title": "The Best of Both Worlds",
   "show": "Star Trek: The Next Generation", "season": 3, "episode": 26, "year": 1990,
   "provider_ids": {"Tvdb": "…", "Imdb": "…"}, "jellyfin_path": "/shows/Star Trek TNG/Season 3/S03E26.mkv",
   "art": {"id": "a41…", "type": "Primary", "tag": "21d…", "wide": true}},
 "naming": {"title": "The_Best_of_Both_Worlds", "show": "Star_Trek_The_Next_Generation",
   "season": "03", "episode": "26", "year": "1990", "folder": "Star_Trek_TNG"},
 "index": {"built": "2026-10-21T01:00:00Z", "files": 25992, "folders": 2711, "building": false, "error": null}}
```

For a folder, `item` is the show, season or movie it holds (`type` `Series`, `Season` or
`Movie`) and `naming` is `null`. `art.wide` marks a 16:9 still rather than a 2:3 poster.
The same `{id, type, title, …, art}` rides on library rows (`/api/library/browse`,
`/api/library/search`) and `/api/items` as `jellyfin`, when Jellyfin knows the file or
folder. The image proxy takes Jellyfin item ids and image tags (32 hex digits) only, passes
on JPEG, PNG, WebP and GIF only, and with `tag` is cached by the browser for good (a new
image gets a new tag); without it, for a day.

**The clips library scan:** with a Jellyfin library whose folder is the clips folder (or
holds it; it needs a path map entry like the media folders), each clip or LeadStinger set
that lands there asks Jellyfin to scan that library (`POST /Items/{id}/Refresh`), ten
seconds after the last one, so a batch is one scan and new clips show up on the TV.

### Bookmarks & meta
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/bookmarks` | List (optionally filtered by `?path=`) / create bookmarks. |
| PATCH/DELETE | `/api/bookmarks/{id}` | Edit / delete one. `DELETE /api/bookmarks` clears all. |
| GET | `/api/health` | Liveness + tool presence (no auth-secret required) and `jellyfin` (Now playing, titles and artwork configured). |
| GET | `/api/config` | Non-secret config the UI needs, including `public_url` (`QC_PUBLIC_URL`, or `null`), `link_server` (the server [Send to LeadStinger links](#send-to-leadstinger-links) name for this request, or `null`), `jellyfin` (true when Now playing is configured), and [pairing](#pairing)'s `pairing`, `server_id` and `server_name`. |

## Clips library contract

**Version `quipclipper.clips/1`** (the listing's `contract` field). LeadStinger's
QuipClipper browser builds on it, so within a version fields are only ever added: a client
ignores fields it doesn't know, and a removal or a change of meaning bumps the version.
Authentication is the API token as everywhere ([above](#authentication)); nothing here
writes except the backfill.

### `GET /api/clips`

| Param | Default | Meaning |
|---|---|---|
| `folder` | the library's top | A folder of the clips library (relative; it can't leave the library: 403; missing: 404). |
| `recursive` | `false` | `true`/`1`: every clip and set under `folder`, not just its own files. |
| `q` | — | Only entries with **every word** of it (case-insensitive, `_` counts as a space) in the file name or path, the line (`meta.cue`), the source's name, the Jellyfin show or title, a set's name or its tiles' titles. |
| `kind` | — | `audio`, `video`, `gif` or `set`. |
| `since` | — | A `cursor` from an earlier listing: only entries added after it. A malformed cursor is 400. |
| `sort` | `name` | `name` (by path in the library) or `newest` (newest first). |

The reply (an `ETag` header carries `etag`; send it back as `If-None-Match` and an unchanged
listing answers `304` with no body):

| Field | Meaning |
|---|---|
| `contract` | `"quipclipper.clips/1"`. |
| `folder`, `recursive` | As asked. |
| `folders` | The folder's own subfolders that hold clips or sets (names). |
| `clips` | The entries (below), sorted by `sort`. |
| `count` | How many entries. |
| `cursor` | `v1.<n>`: the newest entry's time (the newest file of this listing's `folder`/`recursive`/`q`/`kind`, never older than `since`). Pass it as `since` to poll for new clips. It follows the file's modification time, so a re-cut clip counts as new; backfilling a sidecar doesn't. A clip quipclipper cuts is listed only once the file and its sidecar are both written, stamped with that moment, so a poll never sees a half-written clip or one without its `meta`. A file copied into the library keeping an older modification time isn't new to a cursor: list without `since` to catch up. |
| `etag` | Changes when any listed file or its sidecar changes (or the folders do). |

A clip is any file a cut writes: `.mkv`, `.mp4`, `.m4v`, `.webm`, `.gif`, or audio as
`.flac`, `.wav`, `.mka`, `.mp3`, `.ogg`, or a lossless cut's codec-matched `.m4a`, `.ac3`,
`.eac3`, `.opus`, `.dts`, `.thd`; plus `.lsset` sets.

Each entry:

| Field | Meaning |
|---|---|
| `name` | The file name. |
| `clip` | Its path in the clips library: what `/api/clips/download/{path}` and `/api/clips/stream/{path}` take. |
| `folder` | The folder it is in, relative to the library (`""` at the top). |
| `kind` | `audio`, `video`, `gif` or `set` (a LeadStinger `.lsset` set file). |
| `title` | A display title: the line (`meta.cue`) for a clip, the set's name for a set, else the file name with `_` as spaces. |
| `size`, `mtime`, `modified` | Bytes; modification time (Unix seconds, and ISO 8601 UTC). |
| `download_url` | The file (as it is). With `QC_CLIPS_URL_PREFIX` it points at the front proxy. |
| `stream_url` | Inline playback with Range; `null` for a set. |
| `flac_url` | `/api/clips/download/{clip}?as=flac` for `audio` and `video` clips, else `null`. |
| `link` | A deep link that opens the clip in the workspace (with `QC_PUBLIC_URL`; not for sets). |
| `path` | The file's path on the server. |
| `meta` | The clip's metadata sidecar (below), or `null` when it has none yet. |

### The metadata sidecar

Every file quipclipper writes into the clips library (a clip job, a batch export,
clip.nowplaying, Send to LeadStinger, a set file) gets `.<file name>.qc.json` beside it.
It's a dot-file, so listings, the library browser and Jellyfin skip it. Clips cut before
sidecars existed get one from `POST /api/clips/backfill` (`clips.backfill`), marked
`"backfilled": true`; fields it can't know are `null` or missing, so treat every field but
`schema`, `version`, `file`, `kind` and `created` as optional.

| Field | Meaning |
|---|---|
| `schema`, `version` | `"quipclipper.clip-meta"`, `1`. A new version only adds fields. |
| `file` | The clip's file name. |
| `kind` | `audio`, `video`, `gif` or `set`. |
| `origin` | `clip` (a clip job), `leadstinger.quip` (Send to LeadStinger), `leadstinger.set`, or `backfill`. |
| `created` | When it was cut (ISO 8601 UTC). |
| `source` | `{path, name, url}`: the source as quipclipper names it (a library path, a `yt:` or EPUB ref), its display name (the movie or show), and a YouTube page URL. Backfill adds `stem`, the source name read from the clip's folder or file name. |
| `line` | `{start, end}`: the line's span in the source, in seconds. |
| `clip` | `{start, end}`: the span cut from the source (the line plus its padding). |
| `padding` | `{before, after}`: seconds of handle audio either side of the line. |
| `trim` | `{start, end}`: where the line sits inside the clip file, in seconds. Use it as `quip.import`'s `start`/`end`. (A lossless video cut starts on the keyframe before, so for those it is approximate.) |
| `cue` | The line's text. |
| `codec`, `channels`, `sample_rate`, `video_codec`, `duration` | From ffprobe after the cut (the first audio stream). |
| `jellyfin` | `{id, type, show, season, episode, title, year}` when Jellyfin knows the source, else `null`. |
| `lossless`, `audio_format`, `set`, `split`, `size` | The cut's options, the LeadStinger set a sent line went to, whether a channel split wrote it, its size in bytes. |
| `name`, `items` | Sets only: the set's name and its tiles (`title`, `notes`, `source`, `line`, `padding`, `trim_start`, `trim_end`). |
| `parsed` | Backfill only: `{season, episode, year}` read from the names. |

### `GET /api/clips/download/{path}?as=flac`

Converts the clip (no cut) to FLAC in D-024's format: the first audio stream, every
channel kept (a 5.1 clip stays 5.1), `audio/flac`, saved as `<stem>.flac`. The result is
cached in the state folder by the clip's path, modification time and size, so a second
download is instant and a re-cut clip converts again (its old copy goes an hour later).
The copies are kept under 4 GB in all, the least recently downloaded going first, and two
conversions run at a time. A FLAC clip
comes as it is. A set file or a GIF is `400`; a clip ffmpeg can't convert (no audio) is
`422`. The same path checks as without `as` (outside the library: 403).

### `POST /api/clips/backfill`

A job (follow it at `/api/jobs/{id}`). For every clip and set without a sidecar it writes
one from ffprobe, the file name (source, timestamp, line) and, when the source's name
matches one file Jellyfin knows, Jellyfin's show and episode. A sidecar left behind by a
clip moved outside quipclipper is re-attached to the file with the same name and size; one
whose clip is gone is deleted, and so are FLAC copies of deleted clips. The job's `result`
counts them (`clips`, `written`, `kept`, `reattached`, `pruned`, `jellyfin`, `errors`,
`flac_pruned`). Safe to run again.

```bash
# Everything new since the last poll, newest first, with its metadata
curl -s -u "api:$TOKEN" "$BASE/api/clips?recursive=1&sort=newest&since=$CURSOR" \
  | jq '.cursor, (.clips[] | {clip, kind, title, show: .meta.jellyfin.show, trim: .meta.trim})'
# A clip as FLAC for LeadStinger
curl -s -u "api:$TOKEN" -OJ "$BASE/api/clips/download/LeadStinger/a_00-01-05_Engage.mka?as=flac"
```

## Deep links

Every view of the web app has a URL, so a page survives a reload, can be bookmarked or sent
to someone, and Back / Forward move between views. The route lives in the fragment, so the
server and nginx see only `/`:

| Route | Opens |
|---|---|
| `#/library?path=…` | A folder (no `path`: the library home). |
| `#/search?q=…&scope=library` | Dialogue search across the whole library. |
| `#/search?q=…&scope=folder&path=…` | Dialogue search in a folder (`path` repeats for several). |
| `#/search?q=…&scope=item&path=…` | Search in one file: the workspace with its Matches tab. |
| `#/item?path=…&t=…[&sel=A-B][&track=N]` | The workspace: seeks to `t` seconds, selects the lines covering `A`–`B`, shows subtitle track `N`. |
| `#/bookmarks` | Every bookmark. |
| `#/clips?folder=…` | The clips library (or one of its folders). |
| `#/nowplaying` | What Jellyfin is playing: the file at its playhead when one client is playing, else the Now playing card. |

Values are percent-encoded (`/` and `:` are left as they are) and times are seconds with up
to three decimals, e.g.
`https://quip.example/#/item?path=/media/Movies/T2%20(1991)/T2.mkv&t=3621.2&sel=3621.2-3624.8`.
A path outside the roots, or one that no longer exists, opens the library with a message
instead. **Copy link** in the workspace (the selection, else the playhead) and on a search hit
copies one. A link names lines, not a cut: `sel` is the unpadded range and selects the lines
covering it, so Before / After and in and out points set on the waveform stay with the
person who set them; `GET /api/items/link` and the `link` fields build them from `QC_PUBLIC_URL`, and
the CLI's `item.link PATH --at T --sel A-B` prints one.

## `POST /api/clip` body

Minimal: a `path` plus a range — either `start` (with optional `end`) **or**
`query` (with `match_index`). Everything else has sensible defaults (mirrors the
CLI).

| Field | Default | Notes |
|---|---|---|
| `path` | — | **Required.** Media path, `<epub>#seg=N` for an audiobook chapter, or `yt:<video-id>` for a YouTube source. |
| `start`, `end` | — | Explicit time range (seconds). `start` with no `end` = to EOF. |
| `query`, `match_index` | —, `0` | Search-based range: cut the Nth match of `query`. |
| `cue_text` | — | Matched dialogue for the `{cue}` naming token when clipping by `start`/`end` (search-based clips derive it). |
| `kind` | `"video"` | `audio` \| `video` \| `gif`. |
| `lossless` | `true` | `false` re-encodes (frame-exact video / MP3 audio). |
| `before`, `after` | `2.0` | Padding (s) around the line, 0–60. |
| `track` | auto | Subtitle track (s:N) for the search. |
| `audio_tracks` | all | Which audio streams to keep (a:N indices). |
| `audio_format` | — | `wav`\|`flac` — full-mix lossless audio (audio kind). |
| `split_channels` | `false` | Split surround into per-group files (audio). |
| `split_format` | `"wav"` | `wav`\|`flac`\|`original` with `split_channels`. |
| `split_groups` | all | Subset of `center,front,surround,lfe`. |
| `include_lfe` | `true` | Emit the LFE channel as its own file with `split_channels`. |
| `backend` | `"auto"` | `auto`\|`ffmpeg`\|`mkvmerge`. |
| `remux_first` | `false` | Remux a non-MKV source to a temp MKV first (mkvmerge backend). |
| `embed_subs` | `true` | Mux the sidecar subtitle into a lossless video clip. |
| `default_sub_track` | — | Subtitle track (s:N) to mark as the default in the saved video clip. |
| `chapters` | `true` | Keep chapters (mkvmerge). |
| `template` | default | Output-name template; default `{source}/{timestamp}_{cue}_{folder}`. Tokens, and which come from Jellyfin: [docs/CLI.md](CLI.md#settings). |

## Worked example: find a line and cut an audio clip

Assume a token is set (`QC_API_TOKEN=…`) and the site is gated, so we use
`-u api:$TOKEN`. Drop `-u` if the site is open.

```bash
BASE=http://mediabox:8896
FILE="/media/movies/The Terminator (1984)/Terminator.mkv"

# 1. Find the line
curl -s -u "api:$TOKEN" --get "$BASE/api/search" \
     --data-urlencode "path=$FILE" \
     --data-urlencode "query=I'll be back" | jq '.matches[0]'

# 2. Start an audio clip of the best match
JOB=$(curl -s -u "api:$TOKEN" -X POST "$BASE/api/clip" \
     -H 'Content-Type: application/json' \
     -d "{\"path\":\"$FILE\",\"query\":\"I'll be back\",\"match_index\":0,\"kind\":\"audio\"}" \
     | jq -r .job_id)

# 3. Poll until done
until [ "$(curl -s -u "api:$TOKEN" "$BASE/api/jobs/$JOB" | jq -r .status)" = done ]; do sleep 1; done

# 4. Download the result
NAME=$(curl -s -u "api:$TOKEN" "$BASE/api/jobs/$JOB" | jq -r '.files[0].name')
curl -s -u "api:$TOKEN" "$BASE/api/jobs/$JOB/download/$NAME" -o "$NAME"
```

The same in Python (using the `X-API-Key` header instead of basic auth):

```python
import time, requests

BASE = "http://mediabox:8896"
FILE = "/media/movies/The Terminator (1984)/Terminator.mkv"
s = requests.Session()
s.headers["X-API-Key"] = "your-token"      # exempts writes from the CSRF header

hit = s.get(f"{BASE}/api/search", params={"path": FILE, "query": "I'll be back"}).json()
job = s.post(f"{BASE}/api/clip", json={
    "path": FILE, "query": "I'll be back", "match_index": 0, "kind": "audio",
}).json()["job_id"]

while (j := s.get(f"{BASE}/api/jobs/{job}").json())["status"] not in ("done", "failed"):
    time.sleep(1)

name = j["files"][0]["name"]
open(name, "wb").write(s.get(f"{BASE}/api/jobs/{job}/download/{name}").content)
```

> Without a token, a script must send `X-Quipclipper: 1` on every `POST/DELETE`
> (and its basic-auth user/password); the token path exists precisely to avoid
> that. See the deployment env vars in
> [`../web/backend/README.md`](../web/backend/README.md).
