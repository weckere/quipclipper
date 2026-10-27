# Changelog

All notable changes to quipclipper are written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (still 0.x: a minor version for features, a patch
for fixes).

Every commit on `dev` that changes behaviour adds a line under **Unreleased**;
`scripts/release.sh X.Y.Z` turns that section into the release's, dated. Entries between
`<!-- held back -->` and `<!-- /held back -->` lines stay in Unreleased for a later release.

## [Unreleased]

### Check for updates (M24)

- **Check for updates** in Settings ▸ Updates, and a quiet daily check (on unless
  `QC_UPDATE_CHECK=0`; the **Check once a day** switch wins once used): when the latest GitHub release is newer than the running one, the footer says
  "quipclipper X.Y.Z is available" by the version, with its release notes and how to update
  this install (Docker, NixOS, Nix or pip). It never updates itself, sends nothing but the
  request (`User-Agent: quipclipper/<version>`), and GitHub unreachable or rate-limited only
  reads "Couldn't check" with the last good answer. `GET /api/update`,
  `POST /api/update/check`, `PUT /api/update/settings`; CLI `update.status`, `update.check`
  (also with no server, for the CLI's own install) and `update.settings`.

## [0.5.1] - 2026-10-26

### Pairing (M23)

- **The banner says who is really asking:** behind Tailscale serve it also names the tailnet
  user ("From wiz@example.com, 100.101.102.103"), and Settings ▸ Connected apps keeps who
  paired each app, scoped or not (`address`, `user`, `user_name` in `/api/pair` and
  `/api/clients`; `pair list` and `clients list` print them). nginx passes Tailscale serve's
  `Tailscale-User-Login`/`-Name` on only from a trusted proxy (`QC_TRUSTED_PROXIES`,
  `nginx.trustedProxies`) and drops them from anyone else, and the backend believes nginx's
  `X-Real-IP` and those headers only from a peer in the new `QC_FORWARDED_ALLOW_IPS`
  (default loopback, where the NixOS module's nginx connects from; NixOS
  `forwardedAllowIps`, which adds a non-loopback `listenAddress`; the Docker app image sets
  the private ranges) or with the proxy secret. uvicorn no longer rewrites the client
  address from `X-Forwarded-For` itself. A caller who sends those headers itself names
  nobody and can't dodge the per-address rate limit.

### Fixed

- A seek into a file whose audio the browser can't play (DTS, TrueHD) while its transcode
  is still running lands where it was asked: the transcode segments the file from the start,
  and a line, a waveform click or a link's `t=` past what it had reached played from that
  point instead (a line at 36:17 from 22:05, `t=1800` from 0:10). Such a seek now waits,
  paused, with a note saying how far the transcode has got, and jumps there once it arrives;
  pressing play meanwhile plays from where the player is.
- While such a transcode runs, the player's own seek bar spans the whole file from the start
  instead of only the part transcoded so far (it read 30:00 on a 2:07 film and stretched as
  the transcode went on, so nothing past that could be picked there). A pick past the
  transcode's edge waits like any other seek, and plays on if the player was playing;
  dragging back inside before letting go drops it.

## [0.5.0] - 2026-10-26

### Play at the line on Jellyfin (M22)

- **Play on…** starts a line found in quipclipper on a Jellyfin client: on a search hit, a
  match in the workspace's Matches tab, or the workspace selection, it lists the clients
  that can be controlled (Jellyfin in a browser, the Jellyfin Android TV app) and plays the
  file there from a second before the line; a client already on that file seeks. Infuse
  (no remote control) and Wholphin (ignores the command) are listed as "can't be
  controlled". API `POST
  /api/jellyfin/play` and `GET /api/jellyfin/sessions?all=1` (every client, with
  `can_play`); CLI `jellyfin.play` and `nowplaying --all`.

### Subtitle re-index from Jellyfin (M22)

- **Scoped client tokens:** an app that pairs may ask for only part of the API
  (`"scopes": ["index.file", "transcripts.read"]` in `POST /api/pair`). The pairing banner
  says what it asks for ("wants to queue subtitle re-indexes and read transcripts"), Settings
  ▸ Connected apps and `clients.list` / `pair.list` show each app's scopes, and its token
  answers 403 on every other route. An app that names no scopes gets a full token, as
  before (LeadStinger is unchanged).
- `POST /api/index/file` takes `"source": "jellyfin"`: the paths are Jellyfin's and go
  through Jellyfin's path map. This is what the Jellyfin plugin's new subtitle watcher sends
  when Jellyfin downloads a subtitle or notices one dropped into a library.
- **The Jellyfin path map is derived** when `QC_JELLYFIN_PATH_MAP` is unset: quipclipper finds
  a few files of each Jellyfin library folder under its media roots and the clips folder, and
  maps that folder there. A library of several folders is read a page at a time until each
  folder has its samples (Jellyfin doesn't list a library by folder, so the first few files
  can all come from one). An explicit map still wins. `/api/jellyfin/lookup`'s `index`
  says which map is in use (`path_map`).

### Machine transcripts in Jellyfin's subtitle search (M22)

- `GET /api/items/transcript` takes `lines=machine` (the stored lines no subtitle has, what
  Copy to library writes) or `lines=all` (the whole stored transcript), and
  `source=jellyfin` (the path is Jellyfin's, mapped as for `POST /api/index/file`). With
  `lines`, an SRT or VTT export with nothing to serve is a 404. CLI `item.transcript
  --lines --source`. The Jellyfin plugin's new subtitle provider uses it, so Jellyfin's
  **Search subtitles** dialog offers a transcribed movie's or episode's machine lines and
  saves them as `<name>.en.whisper.srt`, which quipclipper reads as machine lines.
- A machine sidecar Jellyfin numbered because the name was taken
  (`<name>.en.whisper.0.srt`) is read as machine lines too, never as the file's subtitles.

### Waveform Playhead button (M20)

- **Playhead** (or **P**) on the waveform brings the playhead into view, in the middle, at
  the current zoom, for when you have panned or zoomed away from it.

### Fixed

- Pairing request and client ids never start with "-": about 1 in 64 did, and the CLI then read
  the id as an option (`pair.approve`, `pair.deny`, `clients.revoke` failed with a usage error).

## [0.4.1] - 2026-10-25

### LeadStinger broadcast-cart icon (M20)

- **Send to LeadStinger draws a broadcast cart** (the NAB tape cartridge stations play
  stingers from: label on top, one reel hub, the tape opening in the front edge) instead of
  0.3.1's shopping cart, on every LeadStinger control. Tooltips, names and behaviour are
  unchanged.

## [0.4.0] - 2026-10-25

### Machine transcription (M21)

Sung lines and stretches the subtitles skip, and files with no subtitles at all, become
searchable: whisper.cpp transcribes what you ask it to, and its lines show apart from the real
subtitles everywhere. See [docs/TRANSCRIPTION.md](docs/TRANSCRIPTION.md).

- **On where whisper.cpp is installed (D-053):** transcription turns itself on where
  `whisper-cli` and a whisper model file are installed (`QC_WHISPER_MODEL`; the NixOS
  module's `transcribe.enable` installs both), and stays off elsewhere. `QC_TRANSCRIBE=0`
  turns it off and `QC_TRANSCRIBE=1` forces it on. The Docker images carry no whisper, so it
  is off there for now. Off, its endpoints answer 404 with the reason, `/api/health` and
  `/api/config` leave it out, stored transcripts and `….whisper.srt` copies stay out of
  search, the script and the library, the web UI shows none of it, and the CLI keeps its
  commands and `--machine` out of its help and refuses them locally. Nothing ever
  transcribes on its own, on or off.
- **Find what the subtitles left out:** `item.transcribe` runs whisper.cpp over a range, the
  gaps between subtitle lines, or a whole file, and keeps what it hears as machine lines,
  which search mixes in (a subtitle line wins a tie) and prints marked `[auto]`. It only ever
  runs when asked, at idle priority with at most 3 threads, one run at a time. Models
  `small.en` (default) or `medium.en`, English only; `stt_model` sets your default.
  Transcripts are stored apart from the media (`item.transcript` shows or exports one,
  `item.transcript-clear` deletes it); `item.transcript-copy` writes one next to the file as
  `<name>.<lang>.whisper.srt`, which quipclipper reads as machine lines, never as the file's
  subtitles. The CLI runs whisper-cli itself when no server is set (`QC_WHISPER_MODEL`).
  `search`, `search.item`, `search.folder` and `item.script` gain
  `--machine include|exclude|only`.
- **The server transcribes on request:** `POST /api/items/transcribe` runs as a job in its own
  one-at-a-time pool (progress, and Cancel keeps the chunks done). The script
  (`/api/items/subtitles?fmt=json`) and both searches mix machine lines in with `source:
  "machine"` / `machine: true` (`machine=include|exclude|only`), so a file with no subtitles
  but a transcript is searchable. The player's captions show machine lines only once they are
  copied to the library (`POST /api/items/transcript/library`), marked `≈`. `/api/health`
  and `/api/config` report whisper.cpp and its models. Configure with `QC_WHISPER_MODEL`,
  `QC_WHISPER_MODELS`, `QC_WHISPER_BIN`, `QC_WHISPER_THREADS` (capped at 3),
  `QC_WHISPER_VAD_MODEL` and `QC_WHISPER_LANGUAGE`.
- **In the workspace:** **Transcribe selection** on the waveform, and a Machine transcript bar
  above the script with **Fill gaps** (or **Transcribe file** on a file with no subtitles), a
  Model choice, progress and Cancel, **Copy to library** / **Remove from library**, **Export
  .srt** and **Delete transcript**. Machine lines show in violet italics with an "auto" chip
  and hover text in the script, both searches, the hit preview and the waveform's line lane,
  and a screen reader hears "machine transcript" (AA contrast in both themes and with more
  contrast). Settings gains **Transcription model**.
- **NixOS module:** `services.quipclipper-web.transcribe` (`enable`, `models`, `modelFiles`,
  `defaultModel`, `threads` 1 to 3, `vad`, `package`) installs whisper.cpp and the ggml models
  pinned by hash, and sets `QC_WHISPER_*`. Nothing in it starts a run. `modelFiles` overrides
  one name and keeps the pinned download for the rest, and a local model file outside the
  store is bound into the sandbox (one under /home was hidden). The VM test now runs a whole
  transcription inside the hardened unit and checks it ran at `SCHED_IDLE`, nice 19.
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
- Ctrl-C on a CLI transcription stops whisper-cli too (it kept running, in its own session,
  for up to an hour), and the idle launcher no longer imports from the folder you ran the CLI
  in. It refuses to start whisper at all if it couldn't lower the CPU priority.
- A file's machine transcript and its library copy are only its own: `Show.mkv` no longer
  reads, or removes, `Show.Part2.en.whisper.srt`. A job waiting behind another transcription
  of the same store can be cancelled while it waits, and a range can't ask for more than a
  week (an unknown length let a huge `end` through).
- Writing a library copy no longer briefly sets the server's umask to 0 for every thread.
- Outro lines are dropped only when the line is one ("Thanks for watching!"): "It was
  translated by my mother" stays.
- CLI: `item.transcribe` / `index.transcribe` against a server print the number of new lines
  instead of crashing, `item.transcript` shows a server's stored transcript, a local
  `item.transcript-copy` copies only what the subtitles don't have (as the server does) and
  prints the sidecar's name, and `search --subs … --machine only` is a usage error rather than
  no matches.
- Server: stopping it stops a running transcription and drops the queued ones (it waited for
  every one, hours for a folder). Two identical requests at once start one job. A folder job
  checks for subtitles with a quick probe instead of extracting them at normal priority,
  skips (and lists) a file it can't check instead of transcribing it whole, counts audio
  files against its 500-file cap, and keys a file reached through a link by the file itself.
  A request may name any model the server has (`QC_WHISPER_MODELS`), not only `small.en` and
  `medium.en`.
- The transcript bar's messages ("Copied … to the library", "Transcription done", "That didn't
  work: this folder is read-only") stay on screen instead of vanishing at once, and errors are
  read out too. Going back to a file whose transcription still runs shows its progress and
  Cancel again (the buttons stayed greyed out with no word why).
- A transcript action or transcription started on one file no longer writes its result into,
  or re-enables buttons of, a file opened meanwhile; a folder transcription started before
  opening another folder no longer hides that folder's buttons. The page stops checking on a
  transcription when you leave the file, or after a few failed tries, instead of every 5 s
  for ever.
- Machine lines in the waveform's line lane keep AA contrast inside the selection in the
  light theme; a machine hit's highlighted words keep the violet; a script row's spoken name
  starts with the visible "auto".

- Folder and library search also search a file's stored machine transcript when the file
  isn't indexed.
- A transcript belongs to the file it was made of: a media file replaced since (a new
  release under the same name) loses its **auto** chip in the library and its count in the
  Dialogue index card, as search and the script already ignored it. Transcripts of files gone
  from the library are never swept on their own: `index.transcript-orphans` (`GET
  /api/transcripts/orphans`) lists them, and `index.transcript-clean` (`POST
  /api/transcripts/clean`, **Remove orphaned transcripts** on the library's home) deletes
  them, with `--stale` also those of replaced files. A disk that isn't mounted, or a file
  outside every library root, never counts as gone.
- **Fill gaps** (one file or a folder) extracts the subtitles of a file not indexed yet at idle
  priority, as the folder index does, and Cancel stops that extraction too (it ran at normal
  priority).
- **Songs get a second look (D-047):** whisper hears audio 30 s at a time and sometimes drops
  a sung line at the end of one of those windows, depending only on where the decode starts.
  A range or gaps run now hears every stretch of 4 s or more where it heard nothing (not
  quiet, no subtitle line there) again on its own, starting at a breath just before it.
  Measured on wizrock, the worst start of three songs found 88 % instead of 83 % of
  Darkplace's lines and 73 % instead of 64 % of a Glee song's, with no line made up; dialogue
  is unchanged and costs nothing more. medium.en stays an option but isn't the fix: on the
  same songs it dropped more lines than small.en and took three to five times as long.
- A line spoken across the edge of two 3-minute pieces of a long transcription is kept once
  (it could come out twice, with slightly different words). Measured on wizrock at 8 edges
  of a podcast and an episode: 3 lines were doubled and none was lost.
- The workspace's machine transcript bar is one slim row above the script, about a line tall,
  instead of three or four: **auto Machine transcript**, the state in a few words ("14 lines",
  "none yet"), and a running job's progress or the last message with Cancel. Hover or keyboard
  focus explains what machine transcripts are; a click or Enter opens Fill gaps, the model,
  Copy to library and the rest below it, and Esc or a click closes them. Each browser
  remembers which, and messages are read out as before.
- **Fill gaps** on a file whose subtitles couldn't be read (a probe that timed out on a busy
  machine) fails with the reason instead of transcribing the whole file, hours of whisper;
  the CLI does the same, and reads a file's subtitles at idle priority too. A second look
  that fails keeps what the first pass heard, and a gaps run's second looks share one
  budget of about half what it transcribes (each short gap could hear more than itself
  again). A line repeated across the far edge of a very short range is kept once.
- Orphaned transcripts: a disk mounted inside a library root that isn't mounted, or the
  CLI's empty mount point, no longer counts as files gone; a folder in the store, or a media
  folder it can't look into, no longer breaks the listing or stops a clean half-way; and the
  library home's check reuses what the store already read instead of parsing every
  transcript on each visit. Its line says what it counts ("aren't needed any more: media
  file gone, unreadable or unfinished"), is no longer read out on every visit, and shows
  the new count after a clean, also after one that failed half-way.
- The slim transcript bar: Cancel is always visible beside a running job's message (it was
  cut off, yet still took the keyboard focus), a long message can be read whole in its
  hover text and in the opened bar, the toggle no longer overlaps the summary at phone
  width, and its hover keeps AA contrast in the dark theme. "auto" chips on buttons are
  part of their spoken names, the toggle's hover text says a click opens the bar, and
  **Export .srt** lines up with the buttons beside it.

### Fixed

- `QC_PATH_MAP` and `QC_JELLYFIN_PATH_MAP` take a path holding `:`, `;` or `=`: give the map
  as JSON (`{"/tv": "/srv/shows"}`, or a list of pairs), or put a backslash before the
  character in the plain form (`/a\:b=/srv/x`). The plain form works as before, and the
  NixOS module now writes JSON.
- Cancel and progress work while a library index run is still looking for files: the
  folder walk shows "Scanning: N folders, M files so far…" and stops on Cancel (it used to
  list the whole library first, minutes on a cold pool).
- A file only ever opened with a subtitle track numbered above 15 no longer reads as
  "changed" (out of date). Cache entries now record their track.
- A folder's **Index** and opening a file clear its "changed" mark once its subtitles are
  read again; before, it stayed on the Index out of date list until the next check.
- A cancelled re-index job says "Stopped" instead of "Done: nothing to do".
- A file found to have no subtitles any more drops its old cache entries.
- **Re-index them** on the Index out of date badge (`index.reindex-changed`,
  `POST /api/index/changes/reindex`) answers at once, however many files are listed: the
  job, not the request, finds the ones gone since the check.
- `index.file` and the hook script send their paths in a JSON body, so a long list no longer
  hits HTTP 414 behind a proxy. `POST /api/index/file` also takes `{"path": [...]}`; the
  query form still works.
- A clip by match number (`clip.create --index`, `POST /api/clip` with `match_index`) counts
  in a search tuned like the one that showed it: `--min-score` and `--max-span` now reach the
  server (`min_score`, `max_span`). They were ignored, so the number could point at another
  line.
- ffmpeg never reads from standard input (`-nostdin` on every run).

## [0.3.1] - 2026-10-24

### Connecting LeadStinger without a token (pairing)

- **Pairing (`quipclipper.pair/1`):** an app such as LeadStinger asks for access
  (`POST /api/pair` with its name and host, no token) and shows a 4-character code; a banner
  on every page of the web UI says "LeadStinger on laptop wants access, code 7F3K" with
  **Allow** and **Deny**, and the app's next poll (`GET /api/pair/{id}` with its poll secret)
  collects a client token of its own, once. At most 5 requests wait at once, 10 a minute from
  one address (429), each for 10 minutes. LeadStinger can open the page as `?pair=<id>` to
  highlight its request. The banner polls only while the page is visible. CLI: `pair.list`,
  `pair.approve`, `pair.deny`. `QC_PAIRING=0` turns every `/api/pair` route into 404.
- **Client tokens:** `qcc_…` tokens work wherever `QC_API_TOKEN` does and mark a machine
  client (no CSRF header needed), also when no `QC_API_TOKEN` is set; an unknown or revoked
  one named outright is a 401. They are stored hashed in `state_dir/clients.json` (0600). A
  paired app can't approve, deny or list requests or list or revoke apps (403);
  `DELETE /api/clients/self` revokes its own token. **Settings ▸ Connected apps** lists the
  apps with Revoke, and **Connect LeadStinger** opens `leadstinger://pair?server=…`. CLI:
  `clients.list`, `clients.revoke`, `leadstinger.connect`.
- `/api/config` gains `pairing`, `server_id` (made once, kept in `state_dir/server-id`),
  `server_name` (`QC_SERVER_NAME`, else the host name) and `link_server`.
- **Send to LeadStinger without `QC_PUBLIC_URL`:** links name the advertised URL
  (`state_dir/advertise.json`), else the address the browser (or CLI) reached the server at,
  so Send to LeadStinger works on a fresh install. `QC_PUBLIC_URL` still wins, and deep links
  still need it.
- **LAN advertising (opt-in, `QC_ADVERTISE=1`):** `quipclipper-web advertise` announces the
  server over mDNS as `_quipclipper._tcp` (instance = server name, port `QC_ADVERTISE_PORT`,
  TXT `v=1`, `url`, `id`) on the LAN interfaces only, and writes `state_dir/advertise.json`
  for the link fallback and the server name. The compose example has an optional, commented
  `advertise` service on the host's network. Uses python-zeroconf (LGPL-2.1, a new
  dependency). Tailnet-only installs should leave it off.
- **Behind the password gate** (Docker image and NixOS module), nginx lets exactly
  `POST /api/pair` and `GET /api/pair/<id>` through without the password, and lets a request
  with a client token (`X-API-Key` or `Bearer`) through once the backend says it is current
  (`satisfy any` with an `auth_request` to `/api/clients/check`); everything else still needs
  the password. The API locations pass the browser's Host, port included, as `X-Quip-Host`,
  so links without `QC_PUBLIC_URL` name the right port, and a proxy in front keeps its own
  `X-Forwarded-*` headers. The VM test pairs through the gate.
- **NixOS module:** `pairing.enable` (default true; false sets `QC_PAIRING=0`),
  `serverName` (`QC_SERVER_NAME`), and `advertise.enable` / `advertise.port` (default off):
  an Avahi service file with TXT `v=1` and `url` (`publicUrl`, else
  `http://<hostName>.local:<port>`; no `id`, which is made at run time) and address
  publishing, with a warning when every `nginx.listen` address looks unreachable from the
  LAN (loopback, a Docker or private bridge, the tailnet).

- **Behind another proxy** (a Tailscale serve sidecar, Caddy, Traefik): `QC_TRUSTED_PROXIES`
  (Docker) or `nginx.trustedProxies` (NixOS) names it, and nginx takes the caller's address
  from its `X-Forwarded-For` (or `QC_REAL_IP_HEADER` / `nginx.realIpHeader`), so the pairing
  banner's "From" and the pairing rate limit see the real caller, not the proxy.

### Send to LeadStinger icon (M20)

- **Send to LeadStinger draws a shopping cart** instead of the placeholder lightning bolt:
  the workspace button, the batch-bar toggles and the Clips rows share one stroke icon in
  the app's style. Tooltips, names and behaviour are unchanged.

## [0.3.0] - 2026-10-24

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
- **Click a subtitle label on the waveform to select its line:** a click (or a tap) on a line's
  label along the top of the waveform selects exactly that line, as picking it in the script
  does, and Shift-click widens the selection to take it in (a dragged range grows to cover it).
  Labels show a pointer and an outline on hover. A drag that starts on a label still
  drag-selects. From the keyboard the labels are one tab stop: ← → move between lines,
  Enter or Space selects, Shift adds, S and E set the start and end.
- **A click on a match selects it for playback:** in the workspace's Matches tab, a click (or
  Enter) on a match selects its line, or all of a multi-line match's lines, picking the
  match's own cues even where lines overlap, then plays from it and says the selection. ▶ and
  Loop selection then play just that line with Before/After, as with any line selection. A
  click while a big file is still loading (its script not there yet, an HLS stream still
  attaching) is no longer lost: the selection waits for the script and the seek for the player.
- **Script help is a "?":** the four lines of help under the Script tab are now a small
  **?** button (named "Script help") beside Follow playback. Hover or focus shows the text,
  a click or Enter keeps it open, and Esc closes it and returns focus to the button. It now
  also covers the waveform label click and the match click.
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
- **Dialogue search ranks the exact line first (D-041):** a score of 100 now means the line
  holds the query's words verbatim, in order and next to each other, ignoring case,
  punctuation, apostrophes and subtitle markup; whole-line matches come first. The words in
  order with a typo, a word missing or an extra word score up to 99, and the words in any
  order at most 75 (less when other words lie between them; a word typed twice must be there
  twice). Before, any line holding the query's words in any order scored 100 ("make it so"
  matched "You make it sound…" and "it… so… make" at 100), and a short line inside the query
  did too ("HOT" for "tea earl grey hot"). Equal scores keep one order across files (the
  closer line first, then file and time), the score shows as a % with hover text in both
  searches, and a library-wide search is about four times faster.
- **Folder and library search read every indexed file, and only those:** the 500-file cap is
  gone, so a whole-library search reaches every file whose subtitles are cached (it used to
  stop after 500 files, often before reaching the show you wanted), and it no longer runs
  ffmpeg to extract a file's subtitles mid-search. Files not indexed yet, and read-along
  books found in a folder, are left out and counted: `/api/search/folder` gains
  `not_indexed` and `books_not_searched` (`capped` now means any of `not_indexed`,
  `changed`, `books_not_searched` is above 0), and the web status line and the CLI say how
  many weren't searched (the CLI even when nothing matched). A search returns the best 500
  matches with the full count, takes queries of up to 300 characters, skips hidden folders
  such as `.snapshots`, and counts a linked file once. Listing the library is quicker too:
  one listing per folder serves every file's sidecar lookups, and the EPUB check is cached.
- **Index the whole library:** a button on the library's home page (and `quipclipper
  index.library [FOLDER]`, `POST /api/search/library/index`) indexes every file not indexed
  yet, or every file in one folder of any size, as a background job. It only ever starts when
  someone starts it, never on its own. One file at a time, and every ffprobe and ffmpeg it
  runs is at idle priority (`SCHED_IDLE`, nice 19, idle I/O). Jobs and the Dialogue index
  cards show its progress with a rough time left; Cancel stops it mid-file, and starting it
  again skips the files indexed or known to have no subtitles, so it resumes after a cancel
  or a restart (`index.library-status`, `GET /api/search/library/index`, show the last run).
  A folder of over 500 files offers **Index N files in the background**, and its index-status
  now counts it instead of answering `skipped`. `index.library --wait` on a run that is
  already going only watches it: Ctrl-C leaves it running.
- **Re-index hooks for Bazarr, Sonarr and Radarr:** when Bazarr saves a subtitle, or Sonarr
  or Radarr imports, upgrades or renames a file, it can tell quipclipper, which re-indexes
  just that file in the background, one at a time at idle priority, each file once, and only
  if its index is out of date (`POST /api/index/file`, `quipclipper index.file PATH…`;
  `index.file-status` shows the queue and the last results). It takes a media file or a
  subtitle file (whose video is found beside it), or a Sonarr/Radarr webhook body as it
  comes. `QC_PATH_MAP` maps their container paths onto yours, and `QC_HOOK_TOKEN` is a
  token that can only do this (the Docker nginx adds a `hook` login for it; NixOS:
  `hookTokenFile`, `pathMap`). The `hook` login is refused everywhere else, even when the
  server doesn't know its token. A library run takes the hooks' files between its own. The
  hook script `contrib/hooks/quipclipper-reindex` needs only `sh` and `curl`, and its Test
  fails when the server has no token set; setup for each app: docs/HOOKS.md.
- **Subtitles that changed since indexing are noticed, and re-indexed only when you ask:** a
  file indexed before its subtitles changed (Bazarr upgraded them, the file was replaced) now
  shows a **changed** chip instead of reading as never indexed (`index_state: "changed"`).
  Opening a folder or searching checks what it shows, and a banner says "N files have new or
  changed subtitles since indexing — **Re-index them**", which re-indexes exactly those files
  in the background at idle priority. Folder search counts them apart (`changed`,
  `changed_files`), as does index-status. Once an hour (`QC_INDEX_CHECK_INTERVAL`, `0` = off;
  NixOS `indexCheckInterval`) the server checks the whole library the same way, listing
  folders and reading file dates only, at the idle disk priority and never while the library
  is being indexed, and the library's home card shows
  **Index out of date: N files** with the same button (`quipclipper index.changes`,
  `index.reindex-changed`, `GET /api/index/changes`). A library run now indexes changed files
  too. The checks never re-index anything themselves.

### Fixed

- An embedded subtitle track with no lines (an empty "Forced" track) reads as no lines
  instead of an error, so indexing stops reading the whole file again on every run.
- Indexing no longer slows down as the subtitle cache grows: after each extraction the cache
  read every entry it held to find the file's stale ones (on a library-sized cache, gigabytes
  per file). It now knows each entry's file, read once.

- Seeking out of a range started with ▶ lets playback carry on for a transcoded file and in
  the YouTube player too (it jumped back to the range later).
- Dragging a range on the waveform, loading a bookmark, or I / O on a selected line no longer
  changes Before / After for the lines you select afterwards.
- The waveform no longer stays grey as if loading when the file is a little shorter than
  first measured.
- A middle-click on the waveform no longer plays; Set in / Set out say why they do nothing on a
  file without a waveform.
- Release: `scripts/release.sh` leaves a changelog block marked `<!-- held back -->` in
  Unreleased for a later release (`scripts/changelog-cut.py`, now tested).
- Release: `:latest` and `:X.Y` move only once both images are pushed. A failed image build
  used to leave `latest` pointing at the new app with the old web image (NIX-7, deferred in
  0.2.0's review).
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

[Unreleased]: https://github.com/weckere/quipclipper/compare/v0.5.1...dev
[0.5.1]: https://github.com/weckere/quipclipper/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/weckere/quipclipper/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/weckere/quipclipper/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/weckere/quipclipper/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/weckere/quipclipper/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/weckere/quipclipper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/weckere/quipclipper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/weckere/quipclipper/releases/tag/v0.1.1
