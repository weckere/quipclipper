# quipclipper-web — Project Plan

A self-hosted **web application** that reproduces everything the quipclipper CLI
does, served by **nginx** and shipped as a **Docker container**. It is meant to
run on the same host as your Jellyfin server so it can mount the *same media
directories* and work directly on the files — no transcoding round-trips, no
re-uploading, local-speed access to your whole library.

This is **not** a Jellyfin plugin. quipclipper stays Python; the existing engine is
wrapped in a thin HTTP API. Nothing about it depends on Jellyfin being installed
(Jellyfin metadata is an optional enrichment).

---

## 1. Decisions locked in

| Decision | Choice |
|---|---|
| Form factor | Docker container(s), nginx front, Python API behind it |
| Core logic | **Reuse the existing `quipclipper` package as-is** (no rewrite) |
| Video discovery | **Both** — browse mounted folders (baseline) + optional Jellyfin metadata enrichment (posters/titles) |
| Clip output | **Both** — offer a download *and* optionally file the clip into a "Clips" folder that Jellyfin can show as a library |
| Clip methods | Full CLI parity (dialogue search, audio/video/gif, audio-track selection, surround split, backend choice, padding, multi-match) **plus** browser-native in/out marks & saved bookmarks |
| Access | LAN-only, no auth by default; **optional** shared password gate |
| Subtitle backends | Keep both ffmpeg **and** mkvmerge (we control the image, so MKVToolNix is installed) |

---

## 2. Why this is mostly assembly, not invention

The engine is already cleanly layered. Every module except `cli.py` is
UI-agnostic and called directly by the web backend:

| Existing module | Web reuse |
|---|---|
| `subtitles.py` — `resolve_subtitles`, `list_streams`, `list_embedded_tracks`, `extract_embedded`, `find_sidecar` | stream/subtitle listing, cue resolution, WebVTT generation |
| `search.py` — `search()` | the dialogue-search endpoint (verbatim) |
| `clip.py` — `compute_range`, `cut_clip`, `split_audio_channels` | the clip job (verbatim) |
| `mkv.py` — `cut_with_mkvmerge`, `estimate_remux_bytes`, … | the mkvmerge clip path (verbatim) |
| `models.py` — `Cue`, `Match`, `format_timestamp` | JSON serialization |

`cli.py` is **not** reused — its orchestration (resolve → search → pick → cut,
prompts, confirmations) is re-expressed as HTTP endpoints + a UI. The branching
logic there (backend auto-select, remux-first rules, mode labels) is the
reference for the API's behaviour.

---

## 3. Architecture

```
                  ┌─────────────────────────────────────────────┐
   browser  ──►   │  nginx                                       │
  (LAN/TV)        │   • serves static frontend (SPA)             │
                  │   • /api/*  → reverse-proxy to backend       │
                  │   • /media/* → byte-range stream of source   │
                  │   • /clips/* → finished clip downloads       │
                  │   • optional basic-auth gate (htpasswd)      │
                  └───────────────┬─────────────────────────────┘
                                  │
                  ┌───────────────▼─────────────────────────────┐
                  │  backend  (FastAPI + uvicorn)                │
                  │   imports the quipclipper engine             │
                  │   library.py  – safe browse of media roots   │
                  │   jellyfin.py – optional metadata client     │
                  │   jobs.py     – async clip jobs + progress   │
                  │   bookmarks.py– per-video saved marks (store)│
                  │   auth.py     – optional password            │
                  │   shells out to ffmpeg / ffprobe / mkvmerge  │
                  └───────────────┬─────────────────────────────┘
                                  │ reads (ro)            │ writes
                  ┌───────────────▼──────────┐   ┌────────▼─────────┐
                  │  /media  (your library,  │   │ /clips  (output, │
                  │   mounted read-only)     │   │  rw; can be a    │
                  │   == Jellyfin's media    │   │  Jellyfin lib)   │
                  └──────────────────────────┘   └──────────────────┘
```

**Containerization:** a `docker-compose.yml` with two services — `app`
(Python/uvicorn) and `web` (nginx) — sharing the clips volume. The image
installs `ffmpeg`, `ffprobe`, and `mkvtoolnix`. Media is bind-mounted read-only;
clips and the bookmark store are writable volumes. (A single combined image with
a process supervisor is a viable alternative; compose is cleaner and recommended.)

---

## 4. Notable design points & risks

- **Path safety (critical).** The API shells out to ffmpeg with file paths, so
  it must never act on arbitrary host paths. All requested paths are resolved
  (`realpath`) and must fall inside a configured whitelist of media roots;
  traversal is rejected. This is the one piece that needs to be bullet-proof.
- **Long cuts are async jobs.** A re-encode, gif, channel split, or remux-first
  cut can take real time. `POST /api/clip` enqueues a job and returns an id; the
  frontend polls `GET /api/jobs/{id}` for progress, then gets a download link or
  the saved library path. Job concurrency is capped to avoid hammering the host.
- **In-browser preview is best-effort.** An HTML5 `<video>` element streams the
  raw source via nginx range requests for scrubbing and setting in/out points.
  Browsers don't decode every codec/container (HEVC, AC3, some MKV). For
  unsupported files the *search-by-dialogue* workflow still works fully (it needs
  no playback); preview just won't render. An optional on-the-fly transcoded
  preview (ffmpeg → HLS/fragmented-mp4) is a later enhancement, explicitly out of
  v1 scope.
- **Subtitles in the player.** Resolved cues are served as **WebVTT** so they
  display in the preview player, and clicking a search hit seeks the player to
  that timestamp.
- **mkvmerge stays.** Because we own the image, MKVToolNix is installed and the
  `auto` backend behaves exactly like the CLI (mkvmerge for lossless MKV cuts,
  ffmpeg otherwise). No feature is dropped relative to the CLI.
- **Bookmarks.** Jellyfin has no arbitrary-timestamp bookmark API, so we keep our
  own small store (SQLite or a JSON file in the config volume), keyed by file
  path: named timestamps and in/out pairs, created from the player position, and
  reusable as a clip range.
- **Jellyfin enrichment is optional & degrades.** With a configured Jellyfin URL
  + API key, the browser shows real titles/posters and can resolve an item to its
  on-disk path. Without it, you get folder/filename browsing. The app never
  *requires* Jellyfin to be up.

---

## 5. Configuration (env / mounted config)

| Setting | Purpose |
|---|---|
| `QC_MEDIA_ROOTS` | colon-separated whitelist of mounted media dirs |
| `QC_CLIPS_DIR` | where finished clips are written |
| `QC_SAVE_TO_LIBRARY` | also file clips into the clips library (on/off) |
| `QC_PASSWORD` | if set, enables the basic-auth gate (else open) |
| `QC_BIND` | bind address (default LAN) |
| `QC_JELLYFIN_URL`, `QC_JELLYFIN_API_KEY` | optional metadata enrichment |
| `QC_MAX_CONCURRENT_JOBS` | ffmpeg job cap |

---

## 6. API surface (draft)

| Method & path | Purpose |
|---|---|
| `GET /api/library/roots` | configured media roots |
| `GET /api/library/browse?path=` | sub-folders + video files (path-checked) |
| `GET /api/items?path=` | streams (`list_streams`), subtitle tracks, sidecar info, + optional Jellyfin metadata |
| `GET /api/items/subtitles?path=&track=` | resolved cues as **WebVTT** |
| `GET /api/search?path=&query=&track=&limit=&min_score=&max_span=` | ranked matches (JSON) |
| `POST /api/clip` | enqueue a clip: `path`, `mode`, range **or** match/query+index/pick, `before/after`, `audio_tracks`, `lossless`, `backend`, `chapters`, `remux_first`, `embed_subs`, split opts, `destination` → returns job id |
| `GET /api/jobs/{id}` | status / progress / result path |
| `GET /api/bookmarks?path=` · `POST /api/bookmarks` · `DELETE …` | saved marks store |
| `/clips/{file}` · `/media/…` | downloads (served by nginx) · range-streamed source for the player |

---

## 7. Frontend (single-page app, served static by nginx)

- **Library browser** — folder tree / grid; Jellyfin posters when enabled.
- **Item page** —
  - HTML5 player with WebVTT subtitles (best-effort preview).
  - Stream list (video/audio/subtitle, the CLI's `tracks` view).
  - **Dialogue search** box → ranked results; click a hit to seek the player and
    pre-fill the clip range.
  - **In/out markers** — set from the current player position (or type timecodes);
    save as a bookmark.
  - **Clip options panel** — type (audio/video/gif), padding, audio-track pick,
    lossless toggle, backend, chapters, embed-subs, surround split + format/LFE.
  - **Make clip** → job progress → download button and/or "saved to library".
- **Jobs / Clips** — recent jobs, statuses, and a list of produced clips.

Plain vanilla JS + a small amount of structure is enough; no heavy framework
required for v1.

---

## 8. Proposed repo layout (additions)

```
web/
  backend/
    quipclipper_web/
      app.py          # FastAPI app + routes
      config.py       # env-driven settings
      library.py      # safe browse + path resolution
      jellyfin.py     # optional metadata client
      jobs.py         # async clip jobs + registry
      bookmarks.py    # persisted marks
      auth.py         # optional password gate
      serializers.py  # Cue/Match/StreamInfo -> JSON / WebVTT
    pyproject.toml    # depends on quipclipper (path), fastapi, uvicorn
    tests/
  frontend/
    index.html  app.js  styles.css
  nginx/
    nginx.conf
  Dockerfile
  docker-compose.yml
  README.md
```

The existing `src/quipclipper` package is the engine and stays untouched (the CLI
keeps working); the backend installs it and imports it.

---

## 9. Phased delivery

- **Phase 0 — Scaffold.** compose + Dockerfile (python + ffmpeg + mkvtoolnix),
  nginx config, FastAPI skeleton, health check. Container boots and serves a page.
- **Phase 1 — Library & inspection.** `library.py` with strict path safety;
  browse endpoint; `GET /api/items` (streams + subtitle tracks); WebVTT endpoint.
  Frontend folder browser + item page (no clipping yet).
- **Phase 2 — Dialogue search.** `GET /api/search` over `search()`; results UI;
  click-to-seek in the player.
- **Phase 3 — Clipping.** `POST /api/clip` + job system wrapping `cut_clip` /
  `cut_with_mkvmerge` / `split_audio_channels` with full CLI option parity;
  download delivery. The clip options panel.
- **Phase 4 — Player marks & bookmarks.** In/out marking from the player; the
  bookmark store; clip-from-range and clip-from-bookmark.
- **Phase 5 — Library save + Jellyfin enrichment.** "Save to Clips library"
  destination; optional Jellyfin metadata/posters and path resolution.
- **Phase 6 — Hardening.** Optional password gate, job concurrency cap, docs,
  and API tests.

---

## 10. Testing

- Engine unit tests (existing) keep running unchanged.
- New **API tests** with FastAPI's `TestClient`, kept media-free: path-safety
  (traversal rejected, roots enforced), search JSON shape, job lifecycle with the
  actual cut mocked, WebVTT rendering, bookmark CRUD.
- Integration smoke test in the built container against a tiny sample file with a
  sidecar `.srt` (the repo already ships `tests/fixtures/sample.srt`).

---

## 11. Open questions for later (not blockers)

1. Transcoded preview for non-browser-friendly codecs — worth adding after v1?
2. Bookmark store: SQLite vs. a JSON file (SQLite if multi-user/concurrent).
3. Multi-user (per-user bookmarks/clips) or single shared instance for v1?
   (Plan assumes single shared instance.)
```
