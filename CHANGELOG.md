# Changelog

All notable changes to quipclipper are written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (still 0.x: a minor version for features, a patch
for fixes).

Every commit on `dev` that changes behaviour adds a line under **Unreleased**;
`scripts/release.sh X.Y.Z` turns that section into the release's, dated.

## [Unreleased]

### Machine transcription (M21)

- **Find what the subtitles left out:** `item.transcribe` runs whisper.cpp over a range, the
  gaps between subtitle lines, or a whole file, and keeps what it hears as machine lines,
  which search mixes in (a subtitle line wins a tie) and prints marked `[auto]`. It only ever
  runs when asked, at idle priority with at most 3 threads, one run at a time. Models
  `small.en` (default) or `medium.en`, English only; `stt_model` sets your default.
  Transcripts are stored apart from the media (`item.transcript` shows or exports one,
  `item.transcript-clear` deletes it); `item.transcript-copy` writes one next to the file as
  `<name>.<lang>.whisper.srt`, which quipclipper reads as machine lines, never as the file's
  subtitles. The CLI runs whisper-cli itself when no server is set (`QC_WHISPER_MODEL`).
  `search` gains `--machine include|exclude|only`.
- **The server transcribes on request:** `POST /api/items/transcribe` runs as a job in its own
  one-at-a-time pool (progress, and Cancel keeps the chunks done). The script
  (`/api/items/subtitles?fmt=json`) and both searches mix machine lines in with `source:
  "machine"` / `machine: true` (`machine=include|exclude|only`), so a file with no subtitles
  but a transcript is searchable. The player's captions show machine lines only once they are
  copied to the library (`POST /api/items/transcript/library`), marked `≈`. `/api/health`
  and `/api/config` report whisper.cpp and its models. Configure with `QC_WHISPER_MODEL`,
  `QC_WHISPER_MODELS`, `QC_WHISPER_BIN` and `QC_WHISPER_THREADS` (capped at 3).
- **In the workspace:** **Transcribe selection** on the waveform, and a Machine transcript bar
  above the script with **Fill gaps** (or **Transcribe file** on a file with no subtitles), a
  Model choice, progress and Cancel, **Copy to library** / **Remove from library**, **Export
  .srt** and **Delete transcript**. Machine lines show in violet italics with an "auto" chip
  and hover text in the script, both searches, the hit preview and the waveform's line lane,
  and a screen reader hears "machine transcript" (AA contrast in both themes and with more
  contrast). Settings gains **Transcription model**.
- **NixOS module:** `services.quipclipper-web.transcribe` (`enable`, `models`, `modelFiles`,
  `defaultModel`, `threads` asserted at most 3, `vad`, `package`) installs whisper.cpp and the
  ggml models pinned by hash, and sets `QC_WHISPER_*`. Nothing in it starts a run.
- The idle launcher passes the app's import path to its child, so it finds the engine under a
  Nix-wrapped app.
- Transcribed lines land at the right time: the audio for whisper is resampled to 16 kHz inside
  the decode's filter chain. Resampling after it started an anchored decode of AC3 in Matroska
  seconds late, so every line came out early (4 s on Darkplace S01E06, found live).
- Removing a file's last machine lines (Remove from library, Delete transcript) clears its
  script and captions at once; one file's transcription status no longer shows on the next;
  filler-only lines ("♪ Mmm ♪") are dropped; a copy in the library takes the folder's umask
  (group-writable in a shared folder).
- On a read-only folder the transcript bar says in words that the transcript can't be copied to
  the library (it was only the disabled button's hover text).
- **Folders:** `index.transcribe` / `POST /api/search/folder/transcribe` transcribes a folder's
  files without subtitles (audio with no transcript included) or the gaps in every subtitled
  file, file after file in one cancellable job; the Dialogue index card offers both, after a
  confirm, and counts files with a machine transcript and audio without one. An audio file
  with a machine transcript is listed, searched and marked **auto** in the library.

### UI follow-ups (M20)

- **Send to LeadStinger** in the workspace is a button again, not a toggle on Make clip: the ⚡
  icon beside Make clip cuts the selection with its Before/After padding and sends it to
  LeadStinger in one click (a set file when the server has no `QC_PUBLIC_URL`). Its tooltip
  says so on hover and on keyboard focus, and Esc hides it. The batch bars keep their toggles.
- **Select on the waveform:** drag across it to select from the drag's start to its end (on
  touch, long-press then drag), for a song or a sound with no line of its own. The range
  replaces any line selection with Before/After at 0, snaps both ends, and the handles,
  audition, Make clip, Send to LeadStinger and Bookmark all work on it. A mouse drag no
  longer pans: scroll, Shift-drag or middle-drag does, and a touch drag still pans. **I** and
  **O** (and **Set in** / **Set out** by the play button) put the in or out point at the
  playhead, and start a 2 s range when nothing is selected. ★ Bookmark now saves a free
  range (a waveform range, a loaded bookmark, a whole clip) instead of staying disabled.
- **Waveform zoom waits in grey:** after a zoom or a pan the wave turns grey (the strip is
  `aria-busy`) until the peaks for the new view arrive, instead of drawing the previous,
  coarser view stretched into blocks. Only sound at least as sharp as the view needs is
  drawn; a request for a view already left is cancelled, so a quick run of zooms shows only
  the last. The handles, playhead and selection stay usable, and the fade is skipped under
  reduced motion.
- **Play and Loop selection follow any selection:** the transport's play/pause, back to the
  start and Loop selection play a range dragged on the waveform, set with I / O, or loaded
  from a bookmark or a link, as well as selected lines (lines first), with the range shown
  by the play button. A line selection holds playback to it as before; a free range does
  once play starts it, and lets go on a seek outside it, so playback after **I** runs on to
  the out point.
- **The dialogue map shows any selection:** its amber band is the range the transport plays,
  a waveform range, I / O, a bookmark or a link's span as well as selected lines (lines
  first), with Before / After, where it showed only selected lines before.
- **Library rows show the dialogue index:** an **indexed** chip marks a file whose subtitles
  are cached (a search reads them at once), a **no subs** chip one indexing found has none,
  each with hover text and a name a screen reader reads; they update when indexing finishes.
  `/api/library/browse` entries gain `index_state` (`indexed`, `no_subs`, `not_indexed`;
  null for folders), worked out from one listing of the folder, and `library.browse` in the
  CLI prints `[indexed]` / `[no subs]`. Listing a folder no longer lists it again for every
  file's sidecar lookup.

### Fixed

- The waveform, snaps and exact cuts land on the sample again when the audio is resampled on
  the way: detail views over 15 s (decoded at 16 kHz), any view of a 88.2/96/192 kHz file
  (drawn at 48 kHz), silence snaps, and an mp3 cut of a 96 kHz file. The anchored decode
  skipped its lead-in counting samples at the new rate, so the window started up to seconds
  late and a click drew that much early.
- Switches sit on their row's centre line instead of a few pixels high: Exact (re-encode) in
  the clip options, the waveform's Snap switches, Loop selection and Follow playback. The
  "Exact (re-encode)" text is the same size as Before, After and Format.
- The workspace header no longer shows a movie's title twice: the Jellyfin line under the
  title is hidden when it would repeat it (a "Title (Year)" folder), and shows just the year
  under a bare "Title" folder. Episodes keep their "S01E02 · Title" line.

## [0.2.0] - 2026-10-21

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
- Re-encoded clips (Exact video, FLAC/WAV and MP3 audio) of a file on the server (not a
  YouTube video that hasn't been downloaded) start on the sample: the audio
  is decoded from a fixed anchor before the start and trimmed by counting samples, the
  same way the waveform's detail views and snaps decode, so a cut placed on a zero
  crossing starts on it (before, a DTS track in Matroska could start a few ms off).
  Seeks are to the microsecond (was the millisecond).

### Fixed

- A file played through the HLS transcode (AC3/DTS audio, HEVC in Firefox, …) now opens at
  the line when you open a search hit, a bookmark or a link, instead of at 0:00: the seek
  was made before the player could take it.
- The CLI works with typer 0.27, which bundles its own click and no longer installs one;
  `typer>=0.24` replaces the `<0.27` pin (and the direct `click` dependency).
- Exact cuts and the waveform stay in sync on files whose audio starts after the video
  (an audio delay in Matroska, TS audio behind its video). Before, a cut starting in the
  first 9 s of such a file had its audio early by the delay, and the waveform's overview
  was drawn that much early everywhere.
- The waveform overview no longer hangs on a damaged file (ffmpeg's error output filled
  a pipe nobody read, which blocked every later overview until a restart), stops a
  decode that stalls for 2 minutes, reports a build it couldn't save as failed instead
  of building forever, and draws the file you just opened before older requests.
- Zoomed-in waveform views draw audio where it is when it ends inside the window, instead
  of stretching it across the whole window. Lossless-cut keyframe markers are in the
  right place on files whose timestamps don't start at 0 (MPEG-TS).
- A snapped in point on a whole 10 s mark cuts from the same anchor the snap decoded from.
- The clips library lists every clip a cut writes: lossless audio cuts in their
  codec's own container (`.m4a`, `.ac3`, `.eac3`, `.opus`, `.dts`, `.thd`) were missing
  from `/api/clips`, the Clips view and LeadStinger's browser, and the clips backfill
  deleted their metadata.
- A clip shows up in `/api/clips` only once it and its metadata are both written, so a
  client polling with `since` never gets a half-written file or a clip whose `meta` it
  then never sees; the listing's ETag no longer changes with the order a share lists
  files in.
- Cancelling a running clip cut answers 409 again ("it will finish") instead of
  reporting the finished clip as cancelled; set exports and the clips backfill still
  stop when asked.
- `?as=flac` reports a conversion that times out (422) and keeps its FLAC copies under
  4 GB, two conversions at a time. A NUL byte in a path is a 400 and an infinite time a
  422 (both were 500s); a Jellyfin id with a trailing newline is refused.
- CLI: a server that redirects (http → https) is an error naming the new address,
  instead of a silent success; the token never follows a redirect. Text that isn't JSON
  on a batch command's stdin is a usage error (it recursed until Python gave up). An
  unreadable token file fails only commands that call the server, so `settings.set` can
  fix it. Ctrl-C stops a whole `clip.batch`. A download keeps the server's file name
  inside `--out-dir`.
- CLI: `--safe` also refuses `job.cancel`, `bookmark.update`, `clips.backfill` and
  `settings.set` (they change data). `clip.nowplaying` re-encodes video like `clip`
  (it stream-copied); a server-mode `clip` of an audio file copies it like local mode
  (it re-encoded to MP3), and refuses the local-only `--min-score`, `--max-span`,
  `--limit` and `--vaapi-device` instead of ignoring them. A server-mode search with no
  matches exits 1, as a local one does. Bad URLs, unreadable `run` scripts and
  unwritable `--out-dir`s are errors, not tracebacks.
- Clip names stay under the 255-byte file-name limit (a long non-Latin line plus a long
  folder name could exceed it).
- Web app: opening another file while a script was still loading could put the old
  file's lines (and its selection) into the new one, so a clip was cut at the wrong
  times. Slow folder listings, filters, dialogue searches, in-video searches, keyframe
  probes and waveform snaps no longer land in whatever you opened since.
- Web app: Clip on a bookmark (and a whole clip from the Clips library) shows its range
  where a line selection shows, replacing the selection instead of hiding behind it; a
  bookmark's line names the clip.
- Web app: batch exports, Send to LeadStinger and clip rows stop waiting on a job
  cleared from the Jobs list or cancelled; a YouTube add or a Clips export finishing in
  the background no longer drags you back to its page; a search started from the
  workspace leaves one history entry, and Back/Forward to a search isn't lost while one
  runs; a link opened cold has a clickable breadcrumb and Send to LeadStinger buttons.
- Web app: on a phone, the workspace's bookmark rows wrap instead of hiding their names;
  the search preview goes away when the window gets too narrow for it; a failed
  waveform overview stops polling; Shift+Tab can't leave the Settings dialog; clicking
  around no longer stalls the Jobs pill; screen readers hear the outcome of indexing
  and of the waveform overview, not every percentage; reduced motion slows every
  spinner and stops smooth script scrolling; row posters no longer repeat the title
  to a screen reader; Reindex and Fetch subtitles are really disabled while they run;
  a re-index that fails says why.
- CLI: a local `clip --lossless` of a video cuts with ffmpeg, as the web app does:
  mkvmerge (which `auto` picked when installed) can only end on a keyframe, so a clip of
  a long-GOP source ran on well past the line. mkvmerge still cuts lossless audio.
- The Docker `web` image ships hls.js again (it fetches the pinned npm tarball, checked by
  its hash): without it, Chrome and Firefox fell back to the old ffmpeg pipe, where long
  files never started playing.
- nginx (NixOS module and the Docker image) waits up to 15 minutes on
  `/api/clips/download` and streams it unbuffered, so the first `?as=flac` download of a
  long clip isn't cut off at 60 s.
- The NixOS module waits for `youtubeDir`'s mount, like the clips and media folders.
- `scripts/release.sh` refuses versions with leading zeros and a changelog whose Unreleased
  section holds only headings, puts the files back when a step fails (the dry run too),
  and when the push fails says how to retry it rather than leaving a half-published
  release. Release notes keep code blocks and tables intact.

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

[Unreleased]: https://github.com/weckere/quipclipper/compare/v0.2.0...dev
[0.2.0]: https://github.com/weckere/quipclipper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/weckere/quipclipper/releases/tag/v0.1.1
