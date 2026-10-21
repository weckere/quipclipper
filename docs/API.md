# quipclipper Web API

The quipclipper web app is backed by a plain HTTP+JSON API — the same one the
browser UI runs on. It's designed to be driven by agents and scripts, not just
the frontend: browse the library, search dialogue, cut clips, and download them,
all over HTTP.

- **Base URL:** wherever the app is served, e.g. `http://mediabox:8896`. All
  endpoints are under `/api/`.
- **Format:** requests and responses are JSON (file downloads stream bytes).
  Errors are `{"detail": "..."}` with a 4xx/5xx status.
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
- **A normal basic-auth user** (`-u quip:<password>`) plus the token in a header
  (below), if you'd rather not use the `api` user.

If `QC_PASSWORD` is unset the site is open and no basic auth is needed.

**2. Token identification (backend).** Present `QC_API_TOKEN` as **any** of:

| Method | Header / form |
|---|---|
| API key header | `X-API-Key: <token>` |
| Bearer token | `Authorization: Bearer <token>` |
| Basic password | `Authorization: Basic base64(api:<token>)` (i.e. `-u api:<token>`) |

A recognised token marks the request as a **machine client**, which **exempts it
from the CSRF header** below. An *explicit* but wrong `X-API-Key`/`Bearer` is
rejected with `401`. Set several comma-separated tokens for rotation
(`QC_API_TOKEN=old,new`); all are accepted (the `api` basic-auth user uses the
first one).

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
| GET | `/api/items/subtitles?path=&track=&offset=&fmt=` | Subtitles as WebVTT (`fmt=vtt`, default) or JSON cues `{start, end, text, speaker}` (`fmt=json` — what the script view uses). |
| POST | `/api/items/subtitles/reindex?path=` | Rebuild the subtitle cache for one file. |
| POST | `/api/media/fetch-subs?path=` | Backfill one YouTube-sourced library video's subtitles (async job — poll `job_id`). The video's id is read from its `[<id>]` filename or a sibling `.info.json`; a `.vtt` sidecar is written next to it. 400 = no id found or the folder is read-only (needs `writableMediaRoots`). Job `progress` ends `ok`/`skipped`/`none`. |
| POST | `/api/search/folder/fetch-subs?path=` | Backfill every id'd, subtitle-less YouTube video in a folder (async job with `progress`; cancellable via `DELETE /api/jobs/{id}`). Skips videos that already have subtitles. `{job_id: null, candidates: 0}` = nothing to do. 400 = read-only folder. |

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
  minutes, and wider spans once built. Until then such a request answers **202** with
  `kind: "pending"` and `overview: {state, progress}` (`queued`, `building`, `error`).
- `source: "detail"`: a decode of just the span, for spans up to 2 minutes, at the stream's
  own rate (capped at 48 kHz) up to 15 s and 16 kHz beyond.
- `kind: "peaks"`: `data` is base64 little-endian int16, `bins` interleaved (min, max) pairs
  (full scale ±32768). `kind: "samples"`: zoomed past one sample per bin, `data` is the raw
  mono samples, `count` of them, the first at `t0`, `rate` apart. Detail decodes and snaps
  are *anchored*: they seek to a whole 10 s mark less 1 s before the span and count samples
  from there, as an exact (re-encoded) clip cut does, so the drawing, `snapped` and the
  cut agree on the sample even where `-ss` alone wouldn't (DTS in Matroska carries
  timestamps too coarse for it). `t0` is the sample nearest `start`.
- `peak`: the largest sample in the file (from the overview), else in the span.
- `keyframes=true` (video, spans up to 60 s) adds `keyframes`: the times a lossless video cut
  can start at.

**Snap** returns `{path, t, mode, radius, rate, found, snapped, delta}`. `mode=zero` (default
radius 0.01 s, at most 0.5) finds the nearest sign change of the mono mix and returns the
sample of the pair nearer zero (`value`); `mode=silence` (default radius 1 s, at most 10)
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
| GET | `/api/search/folder?path=&query=` | Search dialogue across a folder (repeat `path=` for several). |
| GET | `/api/search/folder/index-status?path=` | How much of a folder's subtitles are cached: `{total, indexed, no_subs, skipped}`. `no_subs` counts files indexing found to have no subtitles at all (no sidecar, no text track); they are never offered for indexing. |
| POST | `/api/search/folder/index?path=&force=` | Pre-extract subtitles for a folder (streams progress). |

`/api/search` returns `{query, count, matches: [{index, score, text, speaker,
start, end, start_ts, end_ts, cue_count, link}]}`. The `index` is what you pass back as
`match_index` when clipping. `link` (here, on `/api/search/folder` hits, bookmarks and
clips-library entries) is a [deep link](#deep-links) to that line, present only when
`QC_PUBLIC_URL` is set.

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
| DELETE | `/api/jobs/{id}` | Cancel a queued job / prune a finished one → `{cancelled}`; a running job that can stop (folder subtitle backfill) → `{cancelling}`; any other running job → 409. |
| GET | `/api/jobs/{id}/download/{filename}` | Download a finished clip by name. |

A job is `{id, status, label, created[, progress, started, finished, elapsed, files, error]}`
where `status` is `queued|running|done|failed|cancelled`, `files` is
`[{name, size}]` once done, and `error` is the failure message on a failed job.

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

Both need `QC_PUBLIC_URL` (this server's base URL as LeadStinger reaches it); without it
they answer 503. The link names this server and a clip path, never a token: LeadStinger
downloads `GET /api/clips/download/{clip}` itself with the token it was connected with
(`leadstinger-cli quip.connect <url> --token -`). The format is LeadStinger's own
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
| GET | `/api/health` | Liveness + tool presence (no auth-secret required), and `jellyfin` (Now playing, titles and artwork configured). |
| GET | `/api/config` | Non-secret config the UI needs, including `public_url` (`QC_PUBLIC_URL`, or `null` when Send to LeadStinger links are off) and `jellyfin` (true when Now playing is configured). |

## Clips library contract

**Version `quipclipper.clips/1`** (the listing's `contract` field). LeadStinger's
QuipClipper browser builds on it, so within a version fields are only ever added: a client
ignores fields it doesn't know, and a removal or a change of meaning bumps the version.
Authentication is the API token as everywhere ([above](#authentication)); nothing here
writes except the backfill.

### `GET /api/clips`

| Param | Default | Meaning |
|---|---|---|
| `folder` | the library's top | A folder of the clips library (relative; it can't leave the library: 403). |
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
| `cursor` | `v1.<n>`: the newest entry's time (the newest file of this listing's `folder`/`recursive`/`q`/`kind`, never older than `since`). Pass it as `since` to poll for new clips. It follows the file's modification time, so a re-cut clip counts as new; writing or backfilling a sidecar doesn't. |
| `etag` | Changes when any listed file or its sidecar changes (or the folders do). |

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
download is instant and a re-cut clip converts again (its old copy is deleted). A FLAC clip
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
| `#/item?path=…&t=…[&sel=A-B][&track=N]` | The workspace: seeks to `t` seconds, selects the lines covering `A`–`B`, shows subtitle track `N`. |
| `#/bookmarks` | Every bookmark. |
| `#/clips?folder=…` | The clips library (or one of its folders). |
| `#/nowplaying` | What Jellyfin is playing: the file at its playhead when one client is playing, else the Now playing card. |

Values are percent-encoded (`/` and `:` are left as they are) and times are seconds with up
to three decimals, e.g.
`https://quip.example/#/item?path=/media/Movies/T2%20(1991)/T2.mkv&t=3621.2&sel=3621.2-3624.8`.
A path outside the roots, or one that no longer exists, opens the library with a message
instead. **Copy link** in the workspace (the selection, else the playhead) and on a search hit
copies one; `GET /api/items/link` and the `link` fields build them from `QC_PUBLIC_URL`, and
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
