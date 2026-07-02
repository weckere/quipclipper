# quipclipper Web API

The quipclipper web app is backed by a plain HTTP+JSON API — the same one the
browser UI runs on. It's designed to be driven by agents and scripts, not just
the frontend: browse the library, search dialogue, cut clips, and download them,
all over HTTP.

- **Base URL:** wherever the app is served, e.g. `http://mediabox:8896`. All
  endpoints are under `/api/`.
- **Format:** requests and responses are JSON (file downloads stream bytes).
  Errors are `{"detail": "..."}` with a 4xx/5xx status.
- **Times** are seconds (floats); `*_ts` fields are `H:MM:SS.mmm` strings.
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
| GET | `/api/items/subtitles?path=&track=&offset=` | Subtitles as WebVTT + a script view. |
| POST | `/api/items/subtitles/reindex?path=` | Rebuild the subtitle cache for one file. |

### Dialogue search
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/search?path=&query=&track=&limit=&min_score=&max_span=` | Rank dialogue matches in one file. |
| GET | `/api/search/folder?path=&query=` | Search dialogue across a folder (repeat `path=` for several). |
| GET | `/api/search/folder/index-status?path=` | How much of a folder's subtitles are cached. |
| POST | `/api/search/folder/index?path=&force=` | Pre-extract subtitles for a folder (streams progress). |

`/api/search` returns `{query, count, matches: [{index, score, text, speaker,
start, end, start_ts, end_ts, cue_count}]}`. The `index` is what you pass back as
`match_index` when clipping.

### Media (preview/streaming)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/media?path=` | Stream the raw file. |
| GET | `/api/media/keyframe?path=&time=` | Nearest keyframe at/just before `time`. |
| GET | `/api/media/transcode?path=&...` | On-the-fly transcode stream (desktop preview). |
| GET | `/api/media/hls?path=&venc=` | HLS playlist (iOS preview). |

### Clipping & jobs
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/clip` | Start a clip job (JSON body, see below). Returns `{job_id, status}`. |
| GET | `/api/jobs` | Recent jobs. |
| GET | `/api/jobs/{id}` | One job's status/result. |
| DELETE | `/api/jobs/{id}` | Cancel a queued job / prune a finished one. |
| GET | `/api/jobs/{id}/download/{filename}` | Download a finished clip by name. |

A job is `{id, status, label, created[, started, finished, elapsed, files]}`
where `status` is `pending|running|done|failed|cancelled` and `files` is
`[{name, size}]` once done.

### Clips library
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/clips?folder=` | List finished clips (optionally within a subfolder). |
| GET | `/api/clips/download/{path}` | Download a finished clip. |
| GET | `/api/clips/stream/{path}` | Stream a finished clip. |

### Bookmarks & meta
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/bookmarks` | List / create bookmarks. |
| PATCH/DELETE | `/api/bookmarks/{id}` | Edit / delete one. `DELETE /api/bookmarks` clears all. |
| GET | `/api/health` | Liveness + tool presence (no auth-secret required). |
| GET | `/api/config` | Non-secret config the UI needs. |

## `POST /api/clip` body

Minimal: a `path` plus a range — either `start` (with optional `end`) **or**
`query` (with `match_index`). Everything else has sensible defaults (mirrors the
CLI).

| Field | Default | Notes |
|---|---|---|
| `path` | — | **Required.** Media path (or `<epub>#seg=N` for an audiobook chapter). |
| `start`, `end` | — | Explicit time range (seconds). `start` with no `end` = to EOF. |
| `query`, `match_index` | —, `0` | Search-based range: cut the Nth match of `query`. |
| `kind` | `"video"` | `audio` \| `video` \| `gif`. |
| `lossless` | `true` | `false` re-encodes (frame-exact video / MP3 audio). |
| `before`, `after` | `2.0` | Padding (s) around the line, 0–60. |
| `track` | auto | Subtitle track (s:N) for the search. |
| `audio_tracks` | all | Which audio streams to keep (a:N indices). |
| `audio_format` | — | `wav`\|`flac` — full-mix lossless audio (audio kind). |
| `split_channels` | `false` | Split surround into per-group files (audio). |
| `split_format` | `"wav"` | `wav`\|`flac`\|`original` with `split_channels`. |
| `split_groups` | all | Subset of `center,front,surround,lfe`. |
| `backend` | `"auto"` | `auto`\|`ffmpeg`\|`mkvmerge`. |
| `embed_subs` | `true` | Mux the sidecar subtitle into a lossless video clip. |
| `chapters` | `true` | Keep chapters (mkvmerge). |
| `template` | default | Output-name template, `{source}/{timestamp}_{cue}_{title}`. |

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
