# quipclipper

[![CI](https://github.com/weckere/quipclipper/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/weckere/quipclipper/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/weckere/quipclipper)](https://github.com/weckere/quipclipper/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Find a moment in your movies and shows by its dialogue, and cut it.** Type the line you
remember; quipclipper finds it in the subtitles, shows it on the script, and cuts the clip
(losslessly when it can) from any browser or the command line.

Subtitles already carry exact timestamps, so the trick is simple: parse the subtitles,
fuzzy-search the line, map the match back to its time span, and cut it with ffmpeg or
mkvmerge. quipclipper is a **self-hosted web app** over your own media folders, plus a
**CLI** that can do everything the web app does.

![The workspace: a video with its script, one line selected, and the waveform strip under the player with the selection's in and out handles](docs/screenshots/workspace-dark.png)

## Features

- **Dialogue search** in a video, a folder or your whole library: fuzzy and typo-tolerant,
  phrases that span several subtitles still match, lines holding your exact words rank
  first, and results come grouped by file with the words highlighted and a preview of the
  lines around each hit.
- **A dialogue index that stays current:** index a folder or the whole library once (in
  the background, at low priority, picking up where it stopped), and search reads every
  indexed file in seconds. Each file shows an **indexed** chip; when its subtitles change
  (Bazarr upgraded them, say), a **changed** chip and a banner offer **Re-index them**, and
  [Bazarr, Sonarr and Radarr hooks](#bazarr-sonarr-and-radarr) can do it for you.
- **A workspace for each file:** the player, a dialogue map of where the talking is, and the
  scrolling script. Set the selection by whole lines (Start / End), add padding, and see it
  loop before you cut.
- **Exact cue points:** a zoomable waveform under the player (down to 20 ms across) with the
  subtitle lines on it. Drag the in and out handles, snapped to a zero crossing, a quiet
  spot or a line's edge, and audition the first and last second before you cut. Where
  there's no line to pick (a song, a sound), drag a range out on the waveform, or press
  I and O (Set in / Set out) at the playhead.
- **[Find what the subtitles left out](#machine-transcription):** where whisper.cpp is
  installed, transcribe a song the subtitles skip, a file's gaps or a file with no subtitles
  at all, on request and at idle priority. The machine lines show in violet with an
  **auto** chip, search mixes them in, and they stay out of the captions until you copy them
  to the library.
- **Clips that keep the source:** a lossless stream copy (every audio track, 5.1/7.1
  intact) or a frame-exact H.264 re-encode (Intel Quick Sync when present); full-mix
  WAV/FLAC; surround split into channel groups (just the centre channel, say); GIFs from
  the CLI. Clips are filed by a name template you choose.
- **Bookmarks and batches:** save lines as bookmarks, then clip or export many at once
  from search results, bookmarks or the clips library.
- **Links to every view:** each folder, search and moment has its own URL, so Back and
  Forward work (a phone's back gesture too) and **Copy link** shares a line, e.g.
  `…/#/item?path=/media/Movies/T2.mkv&t=3621.2&sel=3621.2-3624.8`. The format is in
  [docs/API.md](docs/API.md#deep-links); `item.link` prints one from the CLI.
- **Plays anything:** files the browser can't play are converted on the fly (audio, or the
  video too), and iOS gets an HLS stream.
- **[Send to LeadStinger](#leadstinger):** hand a line straight to the LeadStinger
  soundboard as a trimmed tile, or many lines as a set. LeadStinger connects by pairing:
  you click Allow on a banner, and no token gets copied.
- **[Jellyfin Now playing](#jellyfin-now-playing):** open whatever a Jellyfin client is
  playing at its playhead, with the line just heard selected.
- **[Accessible](#accessibility):** hover text and screen-reader names on every control,
  full keyboard use, subtitle size options, AA contrast in dark and light, a phone layout.
- **A CLI and an HTTP API** for scripts and agents: every web action is a CLI command
  ([docs/CLI.md](docs/CLI.md)) and an API call ([docs/API.md](docs/API.md)).
- **More sources (experimental):** YouTube videos by URL (via yt-dlp), podcasts and
  audiobooks with a transcript, and EPUB3 read-along audiobooks.

## Screenshots

**The library:** the media roots on the left, a Dialogue index card that caches
subtitles so dialogue search is instant, and Jellyfin's stills on the episodes. Each file
says whether it is indexed; here one episode's subtitles changed since, so a banner offers
to re-index it.

![A season folder in the library: the Dialogue index card, episode stills from Jellyfin, indexed chips, and one file marked changed with a Re-index them banner](docs/screenshots/library.png)

**Dialogue search** over the whole library: grouped by file, the words highlighted, a
preview of the lines around a hit, and a batch bar for the ticked lines. The status line
counts the files not indexed yet, and **Index the whole library** brings them in.

![Search results across the library, grouped by file, with a preview](docs/screenshots/search-results.png)

**The workspace** in the light theme (the dark one is at the top). Under the player, the
dialogue map outlines the stretch the waveform shows, and the waveform's amber handles
mark the selected line's in and out points.

![The workspace in the light theme, with the waveform strip and its handles under the player](docs/screenshots/workspace-light.png)

**Now playing** from Jellyfin on the library's home: open what a client is playing, at its
playhead. Below it, the home's Dialogue index card indexes the whole library.

![The library home: Now playing from Jellyfin with each item's poster, and the Dialogue index card with Index the whole library](docs/screenshots/now-playing.png)

**Send to LeadStinger:** the cart button beside Make clip cuts the selection with its
Before/After padding and sends it to LeadStinger as a new tile, the padding becoming the
tile's handles.

<img src="docs/screenshots/send-to-leadstinger.png" width="880" alt="The Selection panel with the pointer on the Send to LeadStinger button and its tooltip showing">

**On a phone**, the selection stays pinned to the bottom of the screen, and the waveform's
handles have grips big enough for a finger.

<p align="center"><img src="docs/screenshots/phone.png" width="300" alt="The workspace on a phone: the player, the waveform strip with its handles, and the selection pinned to the bottom"></p>

**Pairing LeadStinger:** when LeadStinger asks to connect, a banner on every page shows
its name, computer and code; Allow it if LeadStinger shows the same code. Settings ▸
Connected apps lists the apps let in, each with its own key and a Revoke button.

<img src="docs/screenshots/pairing.png" width="880" alt="The pairing banner: LeadStinger on studio-macbook wants access, with its code and the Allow and Deny buttons">

<p align="center"><img src="docs/screenshots/connected-apps.png" width="420" alt="Settings, with Connected apps listing LeadStinger on booth-mini, a Revoke button and Connect LeadStinger"></p>

## Install

quipclipper needs `ffmpeg` and `ffprobe`; `mkvmerge` (MKVToolNix) is used when present.
The Docker images and the Nix packages include them. Releases are listed in
[CHANGELOG.md](CHANGELOG.md) and on the [releases page](https://github.com/weckere/quipclipper/releases).

### Docker (GHCR images)

Each release publishes two images, `ghcr.io/weckere/quipclipper-app` (the backend) and
`ghcr.io/weckere/quipclipper-web` (nginx and the UI), tagged with the version (e.g.
`0.3.1`), the series (`0.3`) and `latest`. Copy
[`docker-compose.example.yml`](docker-compose.example.yml), set your media paths, and:

```bash
docker compose up -d
```

Then browse to `http://localhost:8896`. [docs/DEPLOY.md](docs/DEPLOY.md) has a minimal
compose file, every setting (`QC_MEDIA_ROOTS`, the password gate, the API token, hardware
transcoding, LeadStinger and Jellyfin), and building from source.

### NixOS module

```nix
# flake.nix
inputs.quipclipper.url = "github:weckere/quipclipper";   # main = the latest release

# a NixOS module, with quipclipper.nixosModules.default imported
services.quipclipper-web = {
  enable = true;
  mediaRoots = [ "/srv/media/movies" "/srv/media/tv" ];
  openFirewall = true;   # then browse to http://<host>/
};
```

It runs the backend as a hardened systemd service behind the host's nginx, and a NixOS VM
test in CI covers it. The options (password, API token, a shared clips folder, Quick
Sync, your own front proxy, Jellyfin, LeadStinger, machine transcription) are in
[docs/DEPLOY.md](docs/DEPLOY.md#nixos-module).

### The CLI on its own

```bash
pip install "git+https://github.com/weckere/quipclipper@main"   # main: the latest release
# or with Nix: nix run github:weckere/quipclipper -- --help
```

## The CLI

On local files, `search`, `clip` and `tracks` need no server:

```bash
quipclipper search "resistance is futile" --video "Large Locutus Leporid (2016).mp4"
quipclipper clip "you will become one with the borg" -v "Large Locutus Leporid (2016).mp4" -t audio
quipclipper clip "we are the borg" -v movie.mkv --split-channels --split-groups center
```

Point it at a server (`QC_URL` and `QC_API_TOKEN`, or `--server`) and every web action is
a command: browse, search folders, bookmark, batch, follow jobs, download.

```bash
quipclipper ls /movies
quipclipper search.folder "resistance is futile" --path /shows
quipclipper --json bookmark.list | quipclipper clip.batch --kind audio --out-dir ~/clips
```

`--json` gives machine output and `--safe` refuses anything destructive (for agents).
[docs/CLI.md](docs/CLI.md) lists all the commands; [docs/MANUAL.md](docs/MANUAL.md)
covers local cutting in depth (lossless copies, mkvmerge, audio tracks, surround split,
subtitles in clips).

## The HTTP API

The web UI is a thin layer over a plain HTTP+JSON API, and the API is meant to be used:
browse, search dialogue, cut clips, poll the job and download the result from a script,
a cron job or an AI agent. One `QC_API_TOKEN` gets a client through the password gate
(`curl -u api:<token>`), and your instance serves its own OpenAPI docs at `/docs`.
[docs/API.md](docs/API.md) documents every endpoint, with a worked find-a-line-and-cut-it
example.

## Integrations

### LeadStinger

[LeadStinger](https://github.com/weckere/leadstinger) is a soundboard for live shows.
Click the **Send to LeadStinger** button (the cart icon) beside Make clip, or tick the toggle beside
Clip / Export selected, and the line goes straight into LeadStinger as a new tile: quipclipper cuts it
as a full-mix FLAC with its padding kept as handles, and the tile's trim sits exactly on
the line, so it can still be nudged there. Several lines at once make a LeadStinger set
file. LeadStinger connects once by **pairing**: it asks, you click Allow on the banner in
quipclipper (after checking the code), and it gets its own key, revocable under Settings ▸
Connected apps; no token to copy. On a LAN, quipclipper can also announce itself so
LeadStinger finds it (opt-in). From the CLI it is `leadstinger.quip`, `leadstinger.link`,
`leadstinger.set`, `leadstinger.send` and `leadstinger.connect`. Setup and the link format:
[docs/MANUAL.md](docs/MANUAL.md#sending-clips-to-leadstinger).

### Jellyfin Now playing

Watching something on Jellyfin and hear a line worth keeping? With `QC_JELLYFIN_URL` and
an API key set, a **Now playing** card on the library lists what each client is playing,
and **Open at 12:34** opens the file in the workspace with the line just heard selected:
nothing to search for. Jellyfin's posters show on the library and in the workspace, clip
names can use the episode's real title (`{show}/S{season}E{episode}_{title}`), and new
clips are scanned into a Jellyfin clips library so they show up on the TV. quipclipper
still reads and cuts the media from its own folders. From the CLI: `quipclipper
nowplaying`, `quipclipper clip.nowplaying -t audio` and `quipclipper jellyfin.lookup PATH`.
Setup and path mapping: [docs/MANUAL.md](docs/MANUAL.md#now-playing-jellyfin).

### Bazarr, Sonarr and Radarr

When Bazarr downloads or upgrades a subtitle, a one-line post-processing command tells
quipclipper, and it re-indexes just that file in the background at idle priority, so
search always has the current lines. Sonarr and Radarr can do the same on import,
upgrade and rename with a plain webhook. A hook token can only queue re-indexes, and
`QC_PATH_MAP` maps their container paths onto yours. The script
([contrib/hooks/quipclipper-reindex](contrib/hooks/quipclipper-reindex), `sh` and `curl`)
and copy-paste setup: [docs/HOOKS.md](docs/HOOKS.md).

## Machine transcription

Subtitles skip things: most often a song's lyrics, sometimes a whole file has none. Where
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) is installed (`whisper-cli` and a
model; on NixOS, `services.quipclipper-web.transcribe.enable = true`), quipclipper can
transcribe what you point it at: a waveform selection, the gaps between a file's subtitle
lines, a whole file, or a folder. It only runs when you ask, never on its own, at idle
priority with at most 3 threads and one run at a time, so playback never notices it.

What whisper hears is kept apart from the real subtitles: in the script, search and the
waveform its lines are violet italics with an **auto** chip, a subtitle line wins wherever
both cover a stretch, and the player's captions leave them out until **Copy to library**
writes them next to the file as `<name>.en.whisper.srt` (writable folders only). Models are
`small.en` (default) and `medium.en`, English only. From the CLI: `item.transcribe`,
`item.transcript`, `index.transcribe` and `search --machine only`. Where whisper isn't
installed, none of this shows; `QC_TRANSCRIBE=0` turns it off and `=1` forces it on. The
Docker images don't include whisper.cpp yet.

Everything else, the settings and the API: [docs/TRANSCRIPTION.md](docs/TRANSCRIPTION.md).

## Accessibility

The web app aims at WCAG 2.2 AA, and tests keep it there:

- every control has hover text (what it does, and its shortcut) and a name a screen reader
  can say; a skip link jumps past the top bar;
- the keyboard does everything: `/` to search, Space or K to play, the script as one tab
  stop (↑ ↓ to move, Enter to play, S and E to set the selection), I and O to set the in
  and out points at the playhead, arrow keys on the dialogue map, the seek bar and the
  waveform's handles;
- finished and failed jobs, the selection and match counts are announced;
- Settings has subtitle size (Normal, Large, Extra large) and a solid subtitle background;
  both themes meet AA contrast (a test checks every colour pair), with a stronger palette
  under `prefers-contrast: more`; pages reflow down to 320 px wide.

Details: [docs/MANUAL.md](docs/MANUAL.md#keyboard-screen-readers-and-low-vision).

## Experimental sources

- **YouTube:** paste a URL and quipclipper fetches the transcript and details with yt-dlp
  (nothing is downloaded unless you ask); search, bookmark and clip it like a file. It can
  also backfill transcripts for YouTube videos already in your library.
- **Podcasts and audiobooks:** an audio file with a sidecar transcript (`.srt`, `.vtt`,
  `.ass`, or Podcast Namespace / Whisper `.json`) is a source like any other, with speaker
  names picked up from the transcript.
- **EPUB3 read-along audiobooks** (such as [Storyteller](https://gitlab.com/storyteller-platform/storyteller)
  output): a book browses like a folder of chapters, and a line is cut straight from the
  embedded narration.

See [docs/MANUAL.md](docs/MANUAL.md#youtube-sources-experimental) and
[EPUB3 audiobooks](docs/MANUAL.md#epub3-audiobooks-experimental).

## Documentation

| | |
|---|---|
| [docs/MANUAL.md](docs/MANUAL.md) | The user manual: the web app's screens, the CLI on local files, and the topics they share. |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Docker Compose, every setting, the NixOS module, versions and upgrades. |
| [docs/CLI.md](docs/CLI.md) | Every CLI command (generated from the command registry). |
| [docs/API.md](docs/API.md) | The HTTP API. |
| [docs/HOOKS.md](docs/HOOKS.md) | Re-index hooks for Bazarr, Sonarr and Radarr. |
| [docs/TRANSCRIPTION.md](docs/TRANSCRIPTION.md) | Machine transcription with whisper.cpp: turning it on, the web app, the CLI and the API. |
| [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) | Why it is built the way it is. |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release. |

## Development

```bash
pip install -e ".[dev]" -e "web/backend[dev]"
pytest -q                              # the engine and the CLI
(cd web/backend && pytest -q)          # the web app, CLI parity and the frontend guards
nix develop                            # or a shell with everything, including ffmpeg
```

Work happens on the `dev` branch; `main` is always the latest release. Each change that
users would notice adds a line to the Unreleased section of [CHANGELOG.md](CHANGELOG.md).
`scripts/release.sh X.Y.Z` cuts a release: it bumps the version (only the two
`pyproject.toml` files hold it; the backend's is a checked copy), dates the changelog, tags `vX.Y.Z` and moves `main`; the tag's workflow runs the
tests, publishes the images and creates the GitHub Release. Every new web UI action needs a
CLI command in `src/quipclipper/commands.py`: a parity test fails until it has one.

## Licence and credits

quipclipper is released under the [MIT licence](LICENSE). The web app's optional LAN
advertiser uses [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf)
(LGPL-2.1) as an installed library; none of its code is copied into quipclipper.

The screenshots use **"Large Locutus Leporid (2016)"**, a fixture cut from
[*Big Buck Bunny*](https://peach.blender.org/) (© 2008 Blender Foundation,
www.bigbuckbunny.org), licensed [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
Its subtitles quote a few Borg lines from *Star Trek: The Next Generation* for search to
find; Star Trek is a trademark of CBS Studios, and quipclipper has nothing to do with it.
How the fixture and the screenshots are made:
[docs/screenshots/fixture](docs/screenshots/fixture/README.md).
