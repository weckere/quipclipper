# Changelog

All notable changes to quipclipper are written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (still 0.x: a minor version for features, a patch
for fixes).

Every commit on `dev` that changes behaviour adds a line under **Unreleased**;
`scripts/release.sh X.Y.Z` turns that section into the release's, dated.

## [Unreleased]

### Deep links (M18)

- Every view has a URL (`#/library`, `#/search`, `#/item`, `#/bookmarks`, `#/clips`,
  `#/nowplaying`): a reload keeps your place, Back and Forward move between views (a phone's
  back gesture too), and a link to a moment opens the file seeked there with the line
  selected. A bad or out-of-roots path opens the library with a message. Format:
  [docs/API.md](https://github.com/weckere/quipclipper/blob/main/docs/API.md#deep-links).
- **Copy link** in the workspace (the selection, else the playhead) and on each search hit.
- With `QC_PUBLIC_URL` set, search hits, bookmarks and clips carry a `link` field, and
  `GET /api/items/link` / `quipclipper item.link PATH --at T --sel A-B` build one.

### Clips library API for LeadStinger (M18)

- Every clip and LeadStinger set quipclipper writes into the clips library gets a hidden
  `.<name>.qc.json` metadata sidecar: source, line, padding, where the line sits in the
  clip, cue text, kind, codec, channels, duration, and Jellyfin's show, season, episode,
  title and year. `quipclipper clips.backfill` (`POST /api/clips/backfill`) gives older
  clips a best-effort one from ffprobe, the file name and Jellyfin, re-attaches sidecars of
  clips moved outside quipclipper and drops those of deleted clips.
- `GET /api/clips` / `clips.list` gains `recursive`, `q` (`--query`: every word in the
  line, title, show or file name), `kind` (audio, video, gif, set), `sort=newest`, and a
  `since` cursor with an ETag (`If-None-Match` answers 304) for cheap polling. Entries carry
  `clip`, `kind`, `title`, `modified`, `flac_url` and their metadata (`meta`); set files
  (`.lsset`) are listed as `kind: "set"`, and the Clips view shows them as download rows.
  The fields are a versioned contract, `quipclipper.clips/1`:
  [docs/API.md](https://github.com/weckere/quipclipper/blob/main/docs/API.md#clips-library-contract).
- `GET /api/clips/download/{path}?as=flac` / `clips.download --as flac`: the clip as a
  full-mix FLAC keeping every channel (cached until the clip changes), so LeadStinger needs
  no ffmpeg for quipclipper clips.

### Jellyfin titles and artwork (M18)

- With Jellyfin set up, clip names can use what Jellyfin knows about the file: `{title}` is
  the episode's or movie's real title, plus `{show}`, `{season}`, `{episode}` and `{year}`.
  A file Jellyfin doesn't know keeps the names parsed from its path. The default template is
  now `{source}/{timestamp}_{cue}_{folder}`, where `{folder}` is what `{title}` used to
  give, and the old default is read as it, so default-named clips land where they always did.
- The server keeps a path index of everything in Jellyfin (cached in the state dir, rebuilt
  hourly, or now with `quipclipper jellyfin.reindex`); `quipclipper jellyfin.lookup PATH`
  (`GET /api/jellyfin/lookup`) shows what Jellyfin knows about a file or folder.
- `GET /api/jellyfin/image/{item_id}`: posters and stills through the server, so the API
  key stays there; sizes capped, cached by the browser.
- After a clip or a LeadStinger set lands in the clips folder, Jellyfin scans the library
  that holds it (debounced), so new clips show up on the TV.
- Posters from Jellyfin on the library rows it knows (shows, seasons, movies, episode
  stills), in the workspace header (with the episode's real title under the show) and on
  the Now playing card; lazy-loaded, with alt text. The Name help lists the new tokens.

### Waveform for exact cue points (M18)

- A zoomable waveform under the player, from the whole file down to 20 ms across (single
  samples): **−**/**+** buttons and keys, Ctrl/⌘-scroll, a trackpad or two-finger pinch; drag
  or scroll to pan. The dialogue map above it is now a slim overview with the window
  outlined. Subtitle lines are drawn as labelled spans, with the playhead, and keyframes
  (◆) on video: a lossless video cut starts on the keyframe before the in point, and the
  strip says so.
- The selection's in and out points are draggable handles (sliders for the keyboard: ← →
  10 ms, Shift 100 ms, Alt one frame; **[** and **]** jump between them). They are the
  Before / After padding, which can now go negative to trim into a line.
- **Snap** to the nearest zero crossing (on by default, when zoomed in to 10 s or closer),
  to a quiet point, or to a line's start or end. **Audition**: play the selection, the
  first second after the in point, or the last second before the out point.
- `GET /api/items/peaks` (`quipclipper item.peaks`): min/max peaks of any span, from an
  overview built once per file in the background and cached in the state dir (about
  1.4 MB per 2-hour film), or a decode of just the window for spans under 2 minutes, down
  to raw samples. `GET /api/items/snap` (`quipclipper item.snap`): the nearest zero
  crossing or quiet point to a time.
- Clips seek to the microsecond (was the millisecond), and the waveform's detail views and
  snaps decode from the sample grid, so a cut placed on a zero crossing starts on it.

### Fixed

- A file played through the HLS transcode (AC3/DTS audio, HEVC in Firefox, …) now opens at
  the line when you open a search hit, a bookmark or a link, instead of at 0:00: the seek
  was made before the player could take it.
- The CLI works with typer 0.27, which bundles its own click and no longer installs one;
  `typer>=0.24` replaces the `<0.27` pin (and the direct `click` dependency).

## [0.1.1] - 2026-10-19

### CLI parity (M9)

- Every action in the web app is a CLI command: 36 commands named `<area>.<action>`
  (`library.browse`, `search.folder`, `bookmark.add`, `clip.batch`, …) built from one command
  registry, with shortcuts for the common ones (`ls`, `search`, `clip`, `tracks`, `jobs`,
  `status`). See [docs/CLI.md](https://github.com/weckere/quipclipper/blob/main/docs/CLI.md), generated from the registry.
- Commands drive a quipclipper server over the HTTP API (`--server` or `QC_URL`, token from
  `QC_API_TOKEN[_FILE]`); `search`, `clip`, `tracks` and `status` still work on local files
  without one. Jobs are followed to completion (`--no-wait`, `--out-dir`), `--json` gives
  machine output, `--safe` refuses destructive commands, batch commands read other commands'
  `--json` on stdin, and `run` executes a JSON-lines script.
- CLI settings (`~/.config/quipclipper/cli.json`) mirror the web UI's preferences (name
  template, subtitle languages); local clips can be cut by `--start`/`--end` and named with
  `--template`.
- A parity test fails when an API route, request field, `/api/` call in the web UI or web
  preference has no command, and guards fail if a UI action loses its control.

### Redesigned web app (M12)

- New look on design tokens, in dark and light (System / Dark / Light in Settings).
- A top bar with one search box whose scope chip picks this video, this folder, the whole
  library or Now playing; `/` focuses it. Library, Bookmarks and Clips in the nav, and a Jobs
  popover that lists recent jobs (cancel, dismiss, download, clear finished).
- Library: a rail of media roots and YouTube, icon rows, and a Dialogue index card with
  progress and real buttons. It knows which files have no subtitles, so they no longer keep
  a folder from counting as indexed.
- The item page is a workspace: player, a dialogue map (cue density over the whole runtime,
  click to seek), transport and the clip bar on the left; the script on the right with
  Script / Matches / Bookmarks / Clips tabs; a phone layout with the clip bar pinned to the
  bottom.
- Search results are grouped by file with the query highlighted, a preview of the lines
  around a hit on wide screens, and a compact batch bar at the bottom.
- Bookmarks and Clips pages in the new style; bookmarks get an inline label field instead of
  a browser prompt.
- A Video | Audio only segmented control and toggle switches for the clip options.
- A job's files say where they are in the clips library (`clip` in `GET /api/jobs`).

### Send to LeadStinger (M14)

- Hand clips to [LeadStinger](https://github.com/weckere/leadstinger) as a set file (`.lsset`):
  each line is cut as a full-mix FLAC with its padding as handles and the tile's trim on the
  line (`POST /api/leadstinger/set`, CLI `leadstinger.set` and `leadstinger.send`).
- Or send one line straight in by a `leadstinger://quip` link (`POST /api/leadstinger/quip`,
  `GET /api/leadstinger/link`, CLI `leadstinger.quip` and `leadstinger.link`), with the server
  address from the new `QC_PUBLIC_URL` setting (NixOS `publicUrl`).
- In the web UI it is a Send to LeadStinger toggle beside Make clip and the batch bars' Clip /
  Export selected, plus a button on each Clips-library row.

### Jellyfin now playing (M15)

- A Now playing card on the library home and a Now playing search scope: see what each
  Jellyfin client is playing and open it in the workspace at the playhead, with the line just
  heard selected. Jellyfin is used for live sessions only; media is still read from the media
  roots.
- Settings `QC_JELLYFIN_URL`, `QC_JELLYFIN_API_KEY` (or `_FILE`) and `QC_JELLYFIN_PATH_MAP`;
  NixOS `jellyfin.url` / `apiKeyFile` / `pathMap` (the key goes in by systemd
  `LoadCredential`).
- `GET /api/jellyfin/sessions`; CLI `jellyfin.sessions` (`nowplaying`) and `clip.nowplaying`.

### Accessibility (M16)

- Hover text on every control (what it does, and its shortcut), and an accessible name on
  every glyph-only or per-row button; a guard test keeps it that way.
- Screen readers: a skip link, one polite announcer for finished or failed jobs, the
  selection, bookmarks and match counts; rows without nested controls; the script as one tab
  stop with arrow keys, Enter, S and E; the dialogue map and seek bar as sliders; Jobs and
  Settings as modal dialogs that keep and return focus; focus kept when a folder, file or page
  opens.
- Low vision: subtitle size (Normal / Large / Extra large) and a solid background for the
  video's subtitles; tokens that meet WCAG 2.2 AA in both themes (checked by a test); stronger
  lines and focus under `prefers-contrast: more`; no sideways scroll at 320 px; targets at
  least 24 px tall.

### Releases (M17)

- One version source, `pyproject.toml`: the Python packages read their installed metadata and
  the Nix packages read it with `builtins.fromTOML`; a test fails if a copy disagrees.
- This changelog, a `dev` branch for work in progress (`main` is the latest release), and a
  release workflow: a `v*` tag runs the tests, publishes the GHCR images tagged `X.Y.Z`,
  `X.Y` and `latest`, and creates a GitHub Release from the changelog. Images are no longer
  pushed on every commit.
- A rewritten README with screenshots of the current UI, a web-app chapter in the manual,
  and [docs/DEPLOY.md](https://github.com/weckere/quipclipper/blob/main/docs/DEPLOY.md)
  for Docker and NixOS.

### Fixed

- Subtitles vanished when hls.js attached after the subtitle track had loaded, and when the
  audio stream was switched.
- macOS `._` AppleDouble files showed up as clips in the clips library.
- YouTube server streams came back empty (yt-dlp bumped to 2026.08.19).
- A fresh `pip install` had no `click` (typer 0.27 stopped installing it): typer is pinned
  below 0.27 and click is declared.
- The CLI reported a 403 with a real message (such as a path outside the media roots) as a
  token problem.
- On phones the Selection panel covered the script.

## 0.1.0 (untagged, 2026-06-04 to 2026-09-12)

The first version, before releases were tagged. Highlights:

- **Engine and CLI:** find a line by its dialogue (fuzzy search over `.srt`, `.vtt`, `.ass`
  and embedded text tracks) and cut it losslessly with ffmpeg stream copy or mkvmerge, or
  frame-exact with a re-encode (Intel Quick Sync when available); every audio track kept
  (5.1/7.1), track and channel selection, a centre-channel split, subtitles in the clip,
  full-mix WAV/FLAC export, and clip names from a template.
- **Audio-only sources (experimental):** podcasts and audiobooks with a transcript (SRT, VTT,
  Podcast Namespace / Whisper JSON) and EPUB3 media-overlay audiobooks.
- **Web app:** browse several media roots, search dialogue in a file, a folder or the whole
  library (with a subtitle cache and an index), set a selection on the script, bookmark it,
  clip it, and batch-export bookmarks and clips; in-browser playback with an audio or video
  transcode when the browser can't play the source (HLS on iOS and desktop).
- **YouTube sources:** add a video by URL, search and clip it, optional official player,
  full-video download and subtitle backfill (via yt-dlp).
- **HTTP API** with token auth for scripts and agents ([docs/API.md](https://github.com/weckere/quipclipper/blob/main/docs/API.md)), a CSRF
  guard, and an optional password gate.
- **Deployment:** a Nix flake and NixOS module (with a VM test), and Docker images on GHCR.

[Unreleased]: https://github.com/weckere/quipclipper/compare/v0.1.1...dev
[0.1.1]: https://github.com/weckere/quipclipper/releases/tag/v0.1.1
