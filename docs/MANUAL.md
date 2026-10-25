# quipclipper — User Manual

`quipclipper` finds and cuts audio/video clips from movies and TV shows by **searching
the subtitle dialogue**. Subtitles already carry precise timestamps, so the whole
job is: parse the subtitles → fuzzy-search the line you remember → map the match
back to its time span → cut it out. The result is a lossless clip, produced
in a fraction of a second.

quipclipper comes as a **self-hosted web app** and a **command-line tool** over the same
engine. This manual covers both: the web app's screens, the CLI on local files, and the
topics they share. For a shorter tour see [`../README.md`](../README.md); to install the
web app see [`DEPLOY.md`](DEPLOY.md); every CLI command (including the ones that drive a
server) is in [`CLI.md`](CLI.md); the HTTP API is in [`API.md`](API.md); for *why* the
code is built the way it is, see [`DESIGN_NOTES.md`](DESIGN_NOTES.md).

---

## Table of contents

- [Installation](#installation)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [The web app](#the-web-app)
  - [The top bar](#the-top-bar)
  - [The library](#the-library)
  - [Searching dialogue](#searching-dialogue)
  - [The workspace](#the-workspace)
  - [Bookmarks and clips](#bookmarks-and-clips)
  - [Settings](#settings)
  - [Keyboard, screen readers and low vision](#keyboard-screen-readers-and-low-vision)
  - [YouTube sources (experimental)](#youtube-sources-experimental)
- [Command reference](#command-reference)
  - [`quipclipper search`](#quipclipper-search)
  - [`quipclipper clip`](#quipclipper-clip)
  - [`quipclipper tracks`](#quipclipper-tracks)
- [Topics](#topics)
  - [Lossless cutting](#lossless-cutting)
  - [Backends: ffmpeg and mkvmerge](#backends-ffmpeg-and-mkvmerge)
  - [remux-first](#remux-first)
  - [Audio tracks and multichannel audio](#audio-tracks-and-multichannel-audio)
  - [Full-mix lossless WAV/FLAC](#full-mix-lossless-wavflac)
  - [Splitting surround sound](#splitting-surround-sound)
  - [Subtitles in clips](#subtitles-in-clips)
  - [Searching and the picker](#searching-and-the-picker)
  - [Output names and containers](#output-names-and-containers)
  - [EPUB3 audiobooks (experimental)](#epub3-audiobooks-experimental)
  - [Sending clips to LeadStinger](#sending-clips-to-leadstinger)
  - [Now playing (Jellyfin)](#now-playing-jellyfin)
  - [Titles, posters and the clips library (Jellyfin)](#titles-posters-and-the-clips-library-jellyfin)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Installation

```bash
pip install -e .            # or: pip install -e ".[dev]" for the test dependencies
```

This installs the `quipclipper` command and the Python dependencies (`pysubs2`,
`rapidfuzz`, `typer`). The web app is installed separately, with Docker or the
NixOS module: see [`DEPLOY.md`](DEPLOY.md).

### Nix

quipclipper provides a Nix flake. `ffmpeg` and `mkvmerge` are wrapped onto `PATH`
automatically — no separate install needed:

```nix
# flake input:
inputs.quipclipper.url = "github:weckere/quipclipper";
# add to packages:
inputs.quipclipper.packages.${system}.default
```

A dev shell is also available: `nix develop github:weckere/quipclipper`.

## Requirements

| Tool | Required | Used for |
|---|---|---|
| Python ≥ 3.10 | yes | the program itself |
| [`ffmpeg`](https://ffmpeg.org/) + `ffprobe` | yes | cutting clips, probing streams, reading/extracting embedded subtitles |
| [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge`) | optional | the preferred backend for lossless cuts (see [Backends](#backends-ffmpeg-and-mkvmerge)) |

`ffmpeg`/`ffprobe` and `mkvmerge` must be on your `PATH`. If `mkvmerge` is absent,
quipclipper falls back to ffmpeg automatically.

## Quick start

```bash
# 1. Find a line
quipclipper search "i'll be back" --subs movie.srt

# 2. Cut an audio clip of it (lossless stream copy)
quipclipper clip "i'll be back" --video movie.mkv --type audio

# 3. Cut a video clip with extra padding (frame-exact re-encode by default)
quipclipper clip "get to the chopper" --video movie.mkv --type video --before 5 --after 3

# 3b. ...or a near-instant lossless video copy (keyframe-aligned start)
quipclipper clip "get to the chopper" --video movie.mkv --type video --lossless
```

---

## How it works

```
subtitles ──parse──► cues ──fuzzy search──► ranked matches ──pick──► time range ──cut──► clip
 (.srt/.vtt/.ass/.ssa/.sub/.json) (timed lines) (non-overlapping)       (+padding)    (ffmpeg/mkvmerge)
```

1. **Parse** the subtitle source into `Cue`s (a timed line of dialogue). The
   source can be an explicit file, a sidecar file next to the video, or an
   embedded subtitle track extracted from the container. Sidecars may be
   `.srt`/`.vtt`/`.ass`/`.ssa`/`.sub` or a `.json` transcript — Podcast Namespace
   (`segments[].startTime/endTime/body`), Whisper (`segments[].start/end/text`),
   or whisper.cpp (`transcription[].offsets` in ms) — which makes podcasts and
   audiobooks first-class sources. (yt-dlp's `*.info.json` metadata is ignored.)
   Speaker attribution is read from WebVTT `<v Name>` voice tags, a transcript
   `speaker` field, or a recurring `Name:` prefix (one-off `Word:` in dialogue is
   not mistaken for a speaker).

   > **Note:** audio-only support (podcasts, audiobooks — audio files with a
   > sidecar transcript) is **experimental**. The video workflow is the stable
   > path; the audio-only path is newer and still being shaken out.
2. **Search** the cues for your dialogue text with fuzzy, ranked matching.
   Overlapping span-variants of the same line are collapsed so results don't
   duplicate one another.
3. **Choose** a match — the best one, an explicit `--index`, or interactively
   with `--pick` (which lets you select several at once).
4. **Compute the range** — the matched line's own start/end, padded by
   `--before` / `--after`.
5. **Cut** the clip losslessly with mkvmerge (preferred) or ffmpeg.

---

## The web app

The web app does everything in the browser: browse your media folders, find a line by
typing what you remember, select it on the script, and cut it. The screenshots in the
[README](../README.md#screenshots) show each screen.

### The top bar

- **quipclipper** (the logo) goes back to the top of the library.
- **The search box** searches dialogue. Its chip says where: **This video**, **This
  folder**, **Whole library**, or **N folders** when the library filter has narrowed the
  list. It follows the page you are on until you pick one yourself. With Jellyfin set up,
  the chip's menu also has **Now playing**. `/` jumps to the box from anywhere.
- **Library**, **Bookmarks** and **Clips** switch pages.
- **Jobs** counts the clips, downloads and subtitle fetches that are running, and opens a
  list of recent jobs: cancel or dismiss one, download a finished clip, or clear the
  finished ones.
- **The sliders button** opens [Settings](#settings).

### The library

A rail on the left lists the media roots and **YouTube**. The main column lists the
current folder: folders, video files, audio files that have a transcript beside them
(🎵) and EPUB3 audiobooks (📚). A **sub** chip marks a file with a
sidecar subtitle file. An **indexed** chip marks a file whose
dialogue is indexed (its subtitles are cached, so a search reads them at once), and a
**no subs** chip one that indexing found has no text subtitles, and a **changed** chip one
that was indexed but whose subtitles changed since (Bazarr upgraded them, or the file was
replaced); a file with none isn't indexed yet. The chips update when **Index N files**,
**Re-index all** or **Re-index them** finishes; `quipclipper library.browse` (or `ls`) prints
them as `[indexed]` / `[changed]` / `[no subs]`.
**Search library** filters the folder by name; the dialogue search then covers exactly
the folders the filter found (the chip says **N folders**), and clearing it goes back to
the current folder, searched recursively.

The **Dialogue index** card says how many files in the folder have their subtitles
cached. **Index N files** extracts and caches the rest, so dialogue search is fast;
**Re-index all** starts over (use it when subtitles changed). Files with no text
subtitles at all are counted separately and left out. A folder of more than 500 files is
indexed in the background instead (**Index N files in the background**, as the library
index below). **Fetch missing YouTube
subtitles** backfills transcripts for videos that came from YouTube (see
[below](#backfilling-subtitles-for-existing-videos)).

<a id="library-index"></a>On the library's home page the card is the library's own:
**Index the whole library** indexes every file in the library that isn't indexed yet, so a
whole-library dialogue search covers all of it. It never starts on its own: only this button
(after a confirm) or `quipclipper index.library [FOLDER]` start it. It runs in the
background, one file after another at idle priority, so it never slows playback. A file with
a subtitle file beside it takes a moment; one with embedded subtitles is read in full, at
about the disk's speed (some 15 TB of video a day from one hard disk), so a large library takes
a day or more. The card, every folder's Dialogue index card and **Jobs** show its progress (files
done, the file in hand, a rough time left; first, while it looks for files, the folders and
files found so far); **Cancel** stops it at any point, and the files done so far stay
indexed. Starting it again picks up where it stopped, after a cancel or a server
restart alike: files already indexed, or known to have no subtitles, are skipped. The home
card also says how the last run ended. In the CLI, `quipclipper index.library-status`
shows the same.

<a id="changed-subtitles"></a>**Subtitles that changed since indexing.** Search reads a
file's cached subtitles only while they match its subtitle file, so a file whose subtitles
changed (a **changed** chip) is left out of search until it is indexed again. Opening a folder
or searching checks the files it shows (file dates only, nothing is read), and when some
changed, a banner says so: "3 files have new or changed subtitles since indexing — **Re-index
them**". The button re-indexes exactly those files, in the background one after another at
idle priority, and the chips and the folder's card update when it is done. Once an hour the
server also checks the whole library the same way (listing folders and reading file dates
only, at the idle disk priority; the first check a couple of minutes after the server starts,
and none while a library run or a re-index is going), and the library's home card shows what
it found: **Index out of date:
N files**, with the same **Re-index them** button. Nothing is re-indexed on its own: only these
buttons, the CLI (`quipclipper index.changes`, `index.reindex-changed`, `index.file PATH…`) and
the [Bazarr, Sonarr and Radarr hooks](HOOKS.md) do it. The check's interval is
`QC_INDEX_CHECK_INTERVAL` (seconds, `0` = off; NixOS: `indexCheckInterval`).

With Jellyfin set up, a **Now playing** card at the top of the library lists what each
Jellyfin client is playing; see [Now playing (Jellyfin)](#now-playing-jellyfin).

### Searching dialogue

Type a line in the search box and press Enter. Search is fuzzy (typos and paraphrase are
fine) and phrases that span two or three subtitles still match.

<a id="search-scores"></a>Each match has a score, best first:

- **100%** only when the line holds your words verbatim: in order and next to each other,
  ignoring case, punctuation, apostrophes and subtitle markup (`<i>`, `♪`, speaker dashes).
  "make it so" scores 100 against "Make it so, Number One." Among these, the line that is
  just your words comes first.
- **Up to 99%** for your words in order with a difference: a typo ("mak it so"), a word
  missing, or an extra word ("Make it all so.").
- **75% and under** for your words in any order ("It was so hard to make."), less the more
  other words lie between them. A word you type twice ("no no") has to be there twice.

Matches under 60 are left out (`--min-score` in the CLI, `min_score` in the API).

- **In a video**, the matches appear in the workspace's **Matches** tab.
- **In a folder or the library**, only **indexed** files are searched (their subtitles are
  cached, so even the whole library takes seconds); the status line says how many files
  weren't, and **Index** on a folder (or [the whole library](#library-index)) includes them. Read-along books in a folder aren't
  searched either: open a book and search it on its own.
- The results are grouped by file, with the words of the
  query highlighted and a score on each line. On a wide screen (1250 px and up) clicking
  a line previews the lines around it, with **Open in workspace**; on a narrower one it
  opens the file with that line selected.
- Each result line has **★** (bookmark it) and **✂** (clip it) on hover or focus.
- Tick lines (or **Select all**) to act on several: **Bookmark selected**, **Clip
  selected**, and the **Send to LeadStinger** toggle beside it. **Options** opens the
  batch options: Video / Audio only, Split channels (pick the channel groups), Exact,
  Format and the Name template.

### The workspace

Opening a file opens its workspace.

- **The player** plays the file. When the browser can't play its audio (AC3, DTS, FLAC…)
  or video (HEVC in Firefox, XviD, MPEG-2…), the server converts it on the fly (with
  Intel Quick Sync when there is one) and a note says seeking may be slower; iOS plays an
  HLS stream instead. The header has the **Subtitles**, audio and channel pickers: they
  change playback and what the clip contains (the subtitle you pick becomes the clip's
  default track).
- **The dialogue map** under the player shows where the talking is over the whole file:
  the selection in amber (the range ▶ plays: selected lines, or a waveform range, I / O, a
  bookmark or a link's span, with Before / After), the playhead in blue, and the waveform's
  window outlined. Click it to seek.
- **The waveform** under it sets the exact cut. It shows the sound of a window of the file,
  from the whole runtime down to 20 ms (single samples): zoom with **−** / **+** (or the
  keys), Ctrl/⌘-scroll or a pinch; scroll, **Shift**-drag or middle-drag to pan; click to
  play from there. On a touch screen a drag pans and a tap plays. The subtitle lines run
  along the top as labelled spans, and on video the keyframes show as ◆.
  After a zoom or a pan the wave **turns grey until the sound for the new view arrives**,
  then the real wave appears: a close zoom on a long film can take a few seconds to
  decode. It never stretches a coarser view over the new one (older sound that is just as
  sharp, after a pan or zooming out, stays visible under the grey). The handles, playhead
  and selection keep working meanwhile, and only the last zoom of a quick run is fetched.
  **Drag across the waveform** (away from the handles) to select from where the drag
  starts to where it ends; on a touch screen, **long-press, then drag**. That is how you
  select something with no line of its own, such as a song: it replaces any line selection,
  with Before and After at 0, snaps both ends (when Snap is on) and is then fine-tuned with
  the handles like any other. **I** and **O**, or **Set in** and **Set out** by the play
  button, put the in or out point at the playhead; with nothing selected they select 2 s
  from it (I) or up to it (O), and pressing them again moves that end. A clip of a range
  with no line has an empty `{cue}`, which the default name drops:
  `{source}/{timestamp}_{folder}`.
  **Click a line's label** in the subtitle lane along the top (tap it on a touch screen) to
  select exactly that line, as if you had picked it in the script: Before / After, the
  dialogue map's amber band, play, Loop selection and Make clip follow, and the view stays
  where it is. **Shift-click** another label to widen the selection to take it in (a range
  dragged on the waveform grows to cover the line). Only a click selects the line: a drag
  that starts on a label still drags out a range, and Shift-drag still pans.
  The selection's **in and out handles** are the Before / After padding: drag one (or
  focus it and use the arrow keys) and the fields follow; a negative value trims into the
  line. **Snap** pulls a dropped handle to the nearest **zero crossing** (on by default,
  once zoomed in to 10 s or closer, so the cut starts without a click), a **silence**, or a
  **cue edge**. The zero crossing is the waveform's, which mixes every channel into one:
  on a stereo or 5.1 track each channel can still be mid-swing there, so for a
  multichannel FLAC/WAV cut, **silence** is the snap that avoids a click. **▶ Selection**, **▶ 1 s from in** and **▶ 1 s to out** audition the cut.
  A note says how the in point will be cut: on the sample with **Exact** on (or FLAC/WAV
  audio); on the keyframe before it for a lossless video copy; on a codec frame (20–30 ms)
  for passthrough audio.
- **The transport**: back to the start, play/pause, **Set in** / **Set out** at the
  playhead, **Loop selection**, and the selection's range. Play and **Loop selection**
  play the selection with its Before / After padding: the selected lines, else a range
  dragged on the waveform, set with I / O, or loaded from a bookmark or a link (with nothing
  selected, the whole file). Play from outside the selection starts at its in point; at the
  out point playback stops and returns to the in point, or starts it again with Loop on. A
  line selection always holds playback to it. A dragged or bookmarked range does once play
  (or back to the start, or Loop turned on inside it) has started it, and lets go when you
  seek outside it, so after **I** playback runs on and **O** can catch the out point.
- **The script** on the right lists every line. Click a line to seek; hover (or focus)
  a line for **Start** and **End**, which set the selection by whole lines. The first
  line, *Start of video*, lets a clip begin at 0:00. **Follow playback** keeps the
  playing line in view. The **?** beside it says all this, and the keys, as a tooltip on
  hover or focus; click it (or press Enter) to keep it open, Esc to close it. The tabs
  beside **Script** are **Matches** (after a search), **Bookmarks** for this file and
  **Clips** made from it this visit. In **Matches**, click a match (or press Enter on it)
  to select its line, or all its lines when it spans several, and play it: the selection
  then holds playback, so **▶** and **Loop selection** play just that line, with Before /
  After, and Make clip cuts it. A hit from a folder or library search opens its file with
  the line selected the same way.
- **The Selection panel** shows the selected range and acts on it: **★ Bookmark** (with
  a label you can edit; a range dragged on the waveform can be bookmarked too), **✂ Make clip**, and **Send to LeadStinger** (an icon button with a shopping cart:
  hover or focus it for what it does; one click cuts and sends). Under
  it: **Before** / **After** padding in seconds (it changes the preview, the bookmark and
  the clip), **Video | Audio only**, **Exact (re-encode)** for a frame-exact video clip
  (off: a lossless copy that starts on a keyframe), **Format** for audio (Passthrough,
  FLAC, WAV), and **Name**, the clip's path template (**Tokens** lists what it can use).
- **On a phone** the player comes first, then the script, with the Selection panel
  pinned to the bottom of the screen and its options behind **Options**.

Clips are cut as jobs; the Jobs list shows them, and finished clips land in the clips
library, filed by the Name template (by default `{source}/{timestamp}_{cue}_{folder}`: a
folder per source file). With Jellyfin set up, `{title}`, `{show}`, `{season}`,
`{episode}` and `{year}` are what Jellyfin knows (the episode's real title, the series
name), so `{show}/S{season}E{episode}_{title}` files a TNG clip as
`Star_Trek_The_Next_Generation/S03E26_The_Best_of_Both_Worlds.mkv`; every token is in
[docs/CLI.md](CLI.md#settings).

### Bookmarks and clips

**Bookmarks** lists every saved bookmark, grouped by file, with its padding and stream
choice. Open one in its workspace, change its padding, or tick several and **Clip
selected** / **Export selected** with the shared batch options (and the Send to
LeadStinger toggle). **Clear all** deletes them all, after asking.

**Clips** is the clips library: folders and finished clips with their sizes. Download a
clip, send one to LeadStinger, open one in the workspace (a clip is a file like any other:
search it, bookmark it, cut a clip from it), or tick several and re-cut them.

### Settings

Saved per browser:

- **Appearance**: System, Dark or Light.
- **Subtitle size** (Normal, Large, Extra large) and **Solid background behind
  subtitles**, for the subtitles on the video.
- **Subtitle languages**: the order to prefer when a file has several subtitle tracks
  (`eng,spa`); empty uses the server's `QC_SUBTITLE_LANGS`.

The clip name template is the Name field in the Selection panel, and the YouTube player
choice is on each YouTube video.

**Connected apps** is for the whole server, not just this browser: the apps you let in by
[pairing](#connecting-leadstinger-pairing), each with its name, host, when it connected and
when it was last used, and **Revoke** to disconnect one (its key stops working; it can pair
again). **Connect LeadStinger** opens LeadStinger on this computer to connect it to this
server. CLI: `clients.list`, `clients.revoke`, `leadstinger.connect`.

### Keyboard, screen readers and low vision

The web app aims at WCAG 2.2 AA.

- Every control has hover text saying what it does (and its shortcut) and a name a
  screen reader can say. A **skip link** jumps past the top bar.
- `/` focuses the search box. In the workspace, **Space** or **K** play and pause. The
  script is one tab stop: **↑ ↓** (and Page Up/Down, Home/End) move between lines,
  **Enter** plays from a line, **S** and **E** set the selection's start and end. The
  dialogue map and the seek bar are sliders: the arrow keys step 1 s (Shift: 10 s).
  The waveform's in and out handles are sliders too: **← →** move one 10 ms (Shift 100 ms,
  Alt one frame, Page Up/Down 1 s), **[** and **]** jump between them, **+** and **−**
  zoom. **I** and **O** set the in and out points at the playhead (and say where: "In at
  5:53.2"); the **Set in** and **Set out** buttons do the same.
  The waveform's subtitle labels are one tab stop: **← →** move between lines (bringing
  the line into view, and saying it), Home/End go to the first and last, **Enter** or
  **Space** selects the line, **Shift+Enter** adds it to the selection, and **S** and **E**
  set the selection's start and end on it, as in the script.
- Jobs and Settings are dialogs: focus moves in, **Esc** closes them and returns focus.
  When a folder, a file or a page opens, focus moves to it.
- One polite announcement says when a job finishes or fails, what the selection now is,
  that a bookmark was saved, and how many matches a search found.
- Both themes meet AA contrast, with a stronger palette under the system's *increase
  contrast* setting (`prefers-contrast: more`). The pages reflow down to 320 px wide, and
  small targets are at least 24 px tall.

### YouTube sources (experimental)

Open **YouTube** in the library's rail and paste a watch URL
(`youtube.com/watch`, `youtu.be/…`, shorts/embed links work too). quipclipper
runs **yt-dlp** to fetch the video's **subtitles and metadata only** — nothing
is downloaded — and the video joins the list permanently (stored under the
state dir; the ✕ on its row removes it).

From there it works like any library item:

- **Transcript & search** — the scrolling script view, per-item dialogue
  search, and cross-video dialogue search over the whole YouTube section all
  run on the fetched captions. Manual subtitles are preferred; YouTube's
  auto-captions are used as a fallback (with their rolling-duplicate lines
  cleaned up). **Reindex subtitles** re-fetches the transcript.
- **Preview** — a per-browser **Player** setting on each YouTube item picks
  between two modes (both support selecting, bookmarking and clipping):
  - **Official YouTube player** (the default on iOS) — the standard embed via
    the IFrame API: instant, reliable, plays anywhere, uses YouTube's own
    captions. Your browser talks to YouTube directly in this mode.
  - **Server stream** (the default on desktop) — yt-dlp resolves the direct
    stream URLs and ffmpeg remuxes them live to the player (same transcode
    path as unplayable library codecs); nothing in your browser ever loads
    YouTube scripts. Seeking works via the custom seek bar.
- **Bookmarks** — saved against the video like any file, and visible in the
  Bookmarks view.
- **Clipping** — cuts run ffmpeg **directly against the stream URLs**, so
  nothing is downloaded even then: **Exact** re-encodes a frame-tight H.264
  clip; unticking it stream-copies losslessly (keyframe-aligned start, no
  quality loss). Clips land in the clips library under
  `<Video Title>/<timestamp>_<cue>_<Channel>…` and are named by the same
  template tokens as everything else. (Channel-split export and the mkvmerge
  backend don't apply to stream sources.)
- **Optional full download** — a **Download video** button on each item
  fetches the whole video to the server once (browser-native mp4). Everything
  then transparently prefers the local copy: playback seeks natively (no
  transcode proxy), keyframe probes are exact, and clips cut from the file with
  the **full** pipeline (channel split and mkvmerge included) — no YouTube
  round-trips at all. An **on server** chip marks downloaded videos; **Remove download**
  deletes the file and keeps the transcript. By default the file is private to
  the app; set **`QC_YOUTUBE_DIR`** (Docker) / **`youtubeDir`** (NixOS) to a
  real folder and each download lands as `<Title> [<id>].mp4` with a `.vtt`
  sidecar — a browsable, dialogue-searchable library item, visible to Jellyfin
  and Samba too.

#### Backfilling subtitles for existing videos

If your library already holds YouTube-sourced videos with **no subtitles** — a
Pinchflat/yt-dlp archive that only saved `.info.json`, say — quipclipper can
fetch their transcripts on demand so they become dialogue-searchable and
clippable like anything else. It recognises a video's YouTube id from the
`[<id>]` in its filename or a sibling `.info.json`, then downloads a `.vtt`
sidecar next to it. This is **opt-in per video or per folder** — nothing is
fetched automatically across your library:

- **One video** — open it; if it's YouTube-sourced and has no subtitles, a
  **⬇ Fetch subtitles from YouTube** button appears beside the Subtitles picker.
- **A whole folder** — **Fetch missing YouTube subtitles** on the folder's
  Dialogue index card backfills every id'd, subtitle-less video in that folder as a
  background job (with progress and a Cancel button), skipping any that already
  have subtitles, so it's safe to re-run.

Backfill writes sidecars *next to the videos*, so the folder must be
writable by the app — a `writableMediaRoots` entry (NixOS) or a read-write
mount (Docker). Read-only roots show a clear "add it to writableMediaRoots"
message instead.

Requires `yt-dlp` on the backend's PATH — the Docker image installs it via pip
(rebuild the image to pick up a current release when YouTube changes something),
and the Nix package wraps it in. `/api/health` reports whether it's present.
Stream URLs expire after a few hours; quipclipper re-resolves them on demand,
so long sessions just keep working. Heads-up: some videos only expose formats
ffmpeg can't fetch directly — those fail with a clear error rather than
silently degrading.

---

## Command reference

This section covers the three commands that work on **local files**: `search`, `clip`
and `tracks`. Every action in the web app is also a command that drives a quipclipper
server over its HTTP API (`quipclipper ls`, `search.folder`, `bookmark.add`, `clip.batch`,
`leadstinger.quip`, `clip.nowplaying`…); those are all in [`CLI.md`](CLI.md), generated
from the command registry. Run `quipclipper --help` or `quipclipper <command> --help`
for the built-in summary.

### `quipclipper search`

Search subtitles and print ranked matches with timestamps. Useful to find the
exact phrasing/timestamp before cutting.

```
quipclipper search QUERY [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `QUERY` | — | Dialogue text to search for. |
| `--subs`, `-s` | — | Subtitle/transcript file (`.srt`/`.vtt`/`.ass`/`.sub`/`.json`). |
| `--video`, `-v` | — | Video file (used to find a sidecar or extract an embedded track). |
| `--track` | — | Subtitle track by s:N index (from `tracks`); auto-selected when several exist (English full dialogue > SDH > forced). |
| `--langs` | `en` | Preferred subtitle language(s) for auto-selection among sidecars/embedded tracks, comma-separated (e.g. `en,es`). |
| `--limit`, `-n` | 10 | Maximum number of matches to show. |
| `--min-score` | 60 | Drop matches scoring below this (0–100; see [Scores](#search-scores)). |
| `--max-span` | 3 | Max consecutive captions a single match may join. |

You must provide `--subs` or `--video` (or both).

```bash
quipclipper search "i'll be back" --subs movie.srt
# [0]     1.00s  00:00:01.000–00:00:03.000  (score 100)
#       I'll be back.
```

### `quipclipper clip`

Find the dialogue and cut a clip around it. This is the main command.

```
quipclipper clip QUERY --video FILE [OPTIONS]
```

**Source & subtitles**

| Option | Default | Description |
|---|---|---|
| `QUERY` | — | Dialogue text to locate and clip. |
| `--video`, `-v` | *required* | Source to cut from. A video file; an audio-only file (podcast/audiobook) — `--type video` is treated as `audio`; or an **EPUB3 media-overlay audiobook** (`.epub`, see [below](#epub3-audiobooks-experimental)) — always an audio clip from the embedded narration. |
| `--subs`, `-s` | — | Subtitle file; otherwise a sidecar or embedded track is used. |
| `--track` | — | Subtitle track by `s:N` index (from `tracks`); auto-selected when several exist (preferred language > others; full dialogue > SDH > forced; commentary tracks are deprioritised so a plain dialogue track wins). |
| `--langs` | `en` | Preferred subtitle language(s), comma-separated (e.g. `en,es`). Drives auto-selection among **multiple sidecars** (`movie.en.srt` vs `movie.de.srt` vs `movie.hi.en.srt`) and embedded tracks: the preferred language wins and HI/forced/commentary are deprioritised. |

**What to produce**

| Option | Default | Description |
|---|---|---|
| `--type`, `-t` | `video` | `audio`, `video`, or `gif`. |
| `--lossless / --no-lossless` | per `--type` | Default matches the web app: **video re-encodes** for a frame-exact clip; **audio** is a lossless stream copy (already exact). `--lossless` forces a stream copy (keyframe-aligned start), `--no-lossless` forces a re-encode. |
| `--hwaccel / --no-hwaccel` | auto | Hardware-encode a **video re-encode** on an Intel iGPU (Quick Sync / VAAPI, `h264_vaapi`). Auto-detects by default; `--no-hwaccel` forces software `libx264`. Only affects video re-encodes (not lossless copies, audio, or gif). |
| `--vaapi-device` | `/dev/dri/renderD128` | VAAPI render node for `--hwaccel`. |
| `--before`, `-b` | 2.0 | Seconds of padding before the line. |
| `--after`, `-a` | 2.0 | Seconds of padding after the line. |
| `--out`, `-o` | auto | Output path (auto-named if omitted; not allowed with multiple picked matches). |

**Backend & accuracy** (see [Backends](#backends-ffmpeg-and-mkvmerge))

| Option | Default | Description |
|---|---|---|
| `--backend` | `auto` | `auto`, `ffmpeg`, or `mkvmerge`. |
| `--remux-first / --no-remux-first` | off | Remux non-MKV sources to a temp MKV first for maximum accuracy (copies the full source; MKV sources are always cut directly). |
| `--chapters / --no-chapters` | keep | Keep chapters in mkvmerge output. |
| `--yes`, `-y` | off | Skip all confirmation prompts (including the remux disk-space confirmation). |

**Audio tracks & surround** (see [Audio tracks](#audio-tracks-and-multichannel-audio), [Splitting surround sound](#splitting-surround-sound))

| Option | Default | Description |
|---|---|---|
| `--audio-track`, `-A` | all | Audio stream(s) to keep, by `a:N` index, comma-separated (e.g. `0,2`). With `--split-channels` (or `--audio-format`), the **first** index selects the single stream to split/encode (default `a:0`). |
| `--audio-format` | — | `wav` / `flac`: full-mix lossless re-encode of one audio stream keeping **all** channels (5.1 → 5.1 WAV). Audio only; not with `--split-channels`. |
| `--split-channels` | off | Split a surround track into per-group files (audio only). |
| `--split-groups` | all | With `--split-channels`, which channel groups to export: comma-separated subset of `front,center,surround,lfe` (e.g. `center` for just the centre channel). |
| `--split-format` | `wav` | `wav` / `flac` (lossless, no re-encode) or `original` (re-encode to source codec). |
| `--include-lfe / --no-lfe` | include | Whether to emit the LFE channel as its own file when splitting. |

**Subtitles** (see [Subtitles in clips](#subtitles-in-clips))

| Option | Default | Description |
|---|---|---|
| `--embed-subs / --no-embed-subs` | embed | Mux the search subtitle into video clips; embedded video subtitle tracks are always kept. |

**Choosing matches** (see [Searching and the picker](#searching-and-the-picker))

| Option | Default | Description |
|---|---|---|
| `--index`, `-i` | 0 | Which ranked match to cut (0 = best). Ignored with `--pick`. |
| `--pick`, `-p` | off | Interactively choose one or more matches to clip. |
| `--limit`, `-n` | 10 | How many candidate matches to offer with `--pick`. |
| `--min-score` | 60 | Drop matches scoring below this (0–100; see [Scores](#search-scores)). |
| `--max-span` | 3 | Max consecutive captions a single match may join. |

Before cutting, quipclipper prints a preview (the selected match, the clip range and
mode, and any relevant notes) and asks you to confirm — unless `--yes` is given.

### `quipclipper tracks`

List the video, audio and subtitle streams in a container, with the per-type
indices you pass to `--audio-track` (`a:N`) and `--track` (`s:N`).

```bash
quipclipper tracks movie.mkv
# Video:
#   v:0  h264
# Audio:
#   a:0  ac3  5.1(side)  eng
#   a:1  aac  stereo  eng  'Commentary'
# Subtitle:
#   s:0  subrip  eng
```

---

## Topics

### Lossless cutting

quipclipper can cut **losslessly**: it copies the original encoded packets into a
new container with **no re-encoding at all**. A lossy AC3/AAC/EAC3 track stays
that exact lossy bitstream, byte-for-byte, and the cut is near-instant. The
inspiration and model is [LosslessCut](https://github.com/mifi/lossless-cut).

This is the **default for audio**. For **video** the default is a frame-exact
re-encode instead (matching the web app), because a lossless copy can only begin
at a **keyframe** — ffmpeg seeks to the nearest keyframe at or before your start,
so the clip begins a little earlier than requested (with ffmpeg the **end is exact**;
mkvmerge can only end on a keyframe too, so `auto` never uses it for video). On a
long-GOP source (sparse keyframes, e.g. some BluRay encodes 8–10 s apart) that
lead-in can be several seconds, hence the re-encode default. Pass `--lossless` for
a near-instant byte-for-byte video copy (accepting the keyframe lead-in).

What `--lossless` preserves:

- **No transcoding** — encoded audio/video bytes are copied, not re-rendered.
- **All audio tracks** — every audio stream (e.g. a 5.1 main track plus a stereo
  commentary), with language/title metadata.
- **Multichannel layouts** — 5.1 / 7.1 channel layouts are part of the copied
  bitstream and come through untouched.
- **Subtitle and (for mkvmerge) chapter/attachment tracks**.

A `--no-lossless` (re-encode) video clip is encoded to H.264 — on an Intel iGPU
(Quick Sync / VAAPI) when one is detected, else software libx264; see `--hwaccel`.
GIF output is inherently a re-encode and ignores `--lossless`.

### Backends: ffmpeg and mkvmerge

quipclipper has two cutting backends:

- **ffmpeg** — universal; used for gif, re-encodes (`--no-lossless`), channel
  splitting, and as a fallback when mkvmerge is unavailable.
- **mkvmerge** ([MKVToolNix](https://mkvtoolnix.download/)) — used for lossless
  audio cuts when installed (and video with `--backend mkvmerge`). It splits
  losslessly: keeps every track, chapter and attachment, never re-encodes, and
  trims/time-shifts subtitles natively. Output is always Matroska (`.mkv` / `.mka`).
  It can only end a video cut on a keyframe, so a clip of a long-GOP source runs on
  past the line (a 3 s line became 22 s); `auto` cuts video with ffmpeg for that reason.

`--backend` selects the strategy:

| Value | Behaviour |
|---|---|
| `auto` (default) | Use mkvmerge for a lossless audio cut when it's installed (MKV and non-MKV sources); ffmpeg for everything else, lossless video included. The web app does the same. |
| `mkvmerge` | Force mkvmerge. Lossless audio/video only — errors on gif, `--no-lossless`, or `--split-channels`. |
| `ffmpeg` | Always use ffmpeg. |

### remux-first

By default, non-MKV sources are cut **directly** from the source with mkvmerge —
no temporary copy needed. **MKV sources are always cut directly** (already a clean
container).

For maximum accuracy on non-MKV sources, pass `--remux-first`: quipclipper will mux
the source (and any sidecar subtitle) into a temporary MKV with mkvmerge first,
then cut from that. This bypasses ffmpeg entirely but copies the full source to
a temp file, so it needs local disk space:

```
remux-first: muxing the source to a temporary MKV for best accuracy.
estimated scratch space: ~4.7 GB (temp file, deleted afterward).
Proceed? [Y/n]
```

The temp file is written next to the output and deleted afterward. `--yes` skips
all prompts.

### Audio tracks and multichannel audio

By default every audio stream is preserved. Multichannel layouts (5.1, 7.1) are
inside the stream and are preserved automatically by the copy. To keep only some
streams, use `--audio-track` with the `a:N` indices shown by `quipclipper tracks`:

```bash
quipclipper clip "i'll be back" -v movie.mkv --audio-track 0      # first audio track only
quipclipper clip "i'll be back" -v movie.mkv --audio-track 0,2    # tracks 0 and 2
```

### Full-mix lossless WAV/FLAC

A lossless audio clip is a **passthrough** stream copy by default (the original
codec). `--audio-format wav|flac` instead decodes one audio stream and re-encodes
**every channel** into a single lossless file — a 5.1 source becomes a 5.1 WAV
(`pcm_s24le`) or FLAC:

```bash
quipclipper clip "get to the chopper" -v movie.mkv -t audio --audio-format wav
quipclipper clip "get to the chopper" -v movie.mkv -t audio --audio-format flac
```

WAV stores 5.1 via `WAVE_FORMAT_EXTENSIBLE`; no downmix is applied. It is lossless
relative to the decode (PCM verbatim / FLAC bit-exact). Because WAV/FLAC hold a
single stream, this maps one audio stream (the first `--audio-track`, or `a:0`);
to keep *all* tracks use the default passthrough (`.mka`). For separate
per-channel files, use `--split-channels`.

### Splitting surround sound

`--split-channels` writes one file per channel group — a stereo file for the front
pair and the surround pair(s), plus a mono file for the centre and LFE channels.
A 5.1 source has a single surround pair, written as `surround`; a 7.1 source has
two, kept distinct as `side` and `back`:

```bash
quipclipper clip "get to the chopper" -v movie.mkv --split-channels                  # WAV (pcm_s24le)
quipclipper clip "get to the chopper" -v movie.mkv --split-channels --split-format flac
quipclipper clip "get to the chopper" -v movie.mkv --split-channels --no-lfe         # drop LFE
quipclipper clip "get to the chopper" -v movie.mkv --split-channels --split-groups center  # only the centre channel
quipclipper clip "get to the chopper" -v movie.mkv --split-channels -A 1             # split the a:1 stream
```

By default every group is written. **`--split-groups`** narrows that to a subset
— a comma-separated list of `front`, `center`, `surround`, `lfe` (e.g. just
`center` to isolate dialogue from a 5.1 mix). When the file has several audio
streams, **`--audio-track N`** picks which one to split (the first index given;
default `a:0`) — run `quipclipper tracks <file>` to see the `a:N` indices.

**Channel splitting writes lossless WAV or FLAC and never does a lossy
re-encode.** Pulling channels apart does require *decoding* the surround mix —
that is unavoidable, you cannot route channels out of a compressed stream without
decoding it — but the decoded audio is written verbatim: `wav` is raw PCM
(`pcm_s24le`) and `flac` is lossless compression (bit-exact). The opt-in
`--split-format original` is the only option that re-encodes (back to the source
codec, e.g. AC3); use it only if you specifically need the original format.

The channel groups are derived from the stream's ffmpeg channel layout, so 5.1,
5.1(side), 7.1 and the other common layouts all split correctly.

### Subtitles in clips

Video clips keep all embedded subtitle tracks (they ride along in the stream
copy). When you search with an external subtitle file (`--subs` or a sidecar),
quipclipper also muxes those lines into the clip, trimmed and time-shifted to line up
with the cut. Disable with `--no-embed-subs`.

- With the **mkvmerge** backend, the sidecar is added as an extra input and
  mkvmerge trims and shifts it natively.
- With the **ffmpeg** backend, quipclipper renders a clip-aligned SRT from the parsed
  cues and muxes it (ffmpeg's own text-subtitle seeking is unreliable, so quipclipper
  does the trimming itself).

### Searching and the picker

Search is fuzzy and case-insensitive (powered by
[RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)). It scans single cues and
sliding windows of consecutive cues, so phrases split across two or three captions
still match. Results are **non-overlapping**: the many overlapping span-variants of
a line (the cue alone, plus windows that join it with neighbours) are collapsed to
the single best representative of each region, so a recurring phrase yields one
candidate per place it occurs.

When a phrase matches in several places, use `--pick` to choose interactively:

```bash
quipclipper clip "i'll be back" -v movie.mkv -s movie.srt --pick
```

```
3 match(es):
[0]    83.00s  00:01:23.000–00:01:24.500  (score 100)
      I'll be back.
[1]   742.00s  00:12:22.000–00:12:23.500  (score 100)
      I'll be back!
[2]  1290.00s  00:21:30.000–00:21:33.000  (score 90)
      I'll be right back.
Select matches to clip (comma-separated indices, or 'all') [0]: 0,1
```

Selection is **non-exclusive** — enter comma-separated indices (`0,2,3`), `all`,
or press Enter for the top match. Each selected match is clipped in one run and
auto-named by its timestamp so the files don't collide. `--out` cannot be combined
with multiple selections.

### Subtitle track selection

When a video has multiple embedded subtitle tracks and no `--track` is given,
quipclipper auto-selects the best dialogue track. Tracks are scored so that
**text beats image, then English full dialogue beats SDH, which beats forced**
(forced tracks carry only foreign-language portions, so they have minimal
dialogue); a track with no language tag counts as English so single-language
releases without metadata still resolve. SDH and forced are detected from the
container's `hearing_impaired`/`forced` dispositions, with a title-text fallback
(`SDH`, `forced`, …). A **commentary** track (title contains "comment") is ranked
below every normal text track, so a plain dialogue track always wins over a
commentary transcript — it's only chosen when it's the only option. Ties keep
container order. The preferred language is English by default; set `--langs`
(e.g. `--langs es,en`) to change it.

**Multiple sidecar files** are selected the same way. When several sit next to a
video — `movie.en.srt`, `movie.de.srt`, `movie.hi.en.srt`, `movie.commentary.en.srt`,
`movie.en.vtt` — the language and HI/forced/commentary flags are parsed from each
filename (`.en`, `.hi.en` = hearing-impaired English, `.forced`, `.sdh`/`.cc`) and
scored with the same logic, so the preferred-language non-HI file wins. (`.hi`
*alone* is treated as the Hindi language code, not a flag.) A single sidecar is
always used as-is; ties break by extension priority (`.srt` > `.vtt` > … > `.json`).

**Image subtitles (PGS/VOBSUB) are not usable.** Blu-ray `.sup` (PGS) and DVD
VOBSUB tracks are bitmaps, not text — they can't be extracted to SRT, searched,
or displayed. quipclipper only handles text subtitles, so image tracks rank
below every text track in auto-selection (chosen only if nothing else exists,
then erroring with "supply an .srt"), and the web app hides them from its picker.
If a file has *only* image subtitles, supply a text sidecar with `--subs`.

Run `quipclipper tracks <video>` to see all tracks, and pass `--track N` to
override the automatic choice. The same scoring drives the web app's subtitle
picker and its search/index cache, so all paths agree on the default track.

### Output names and containers

When `--out` is omitted, clips are auto-named next to the source as
`<stem>_<HH-MM-SS>.<ext>` (whole seconds; `_2`, `_3` … when that name is taken). With
`--template` (or the `clip_template` setting) the name comes from the template instead,
under `--out-dir` (default: the current folder); see [CLI.md](CLI.md). The extension is chosen to hold the source streams
without transcoding:

| Mode | Container |
|---|---|
| Lossless audio, single stream (ffmpeg) | codec-matched: `.m4a` / `.ac3` / `.eac3` / `.opus` / `.flac` / … |
| Lossless audio, multiple streams (ffmpeg) | `.mka` (Matroska holds any number of streams/codecs) |
| Lossless audio (mkvmerge) | `.mka` |
| Lossless video | `.mkv` |
| `--no-lossless` audio | `.mp3` |
| `--no-lossless` video | `.mp4` (H.264 / AAC) |
| `--split-channels` | `.wav` / `.flac` / source-codec extension, one per channel group |
| gif | `.gif` |

### EPUB3 audiobooks (experimental)

> **Experimental.** Supported in both the CLI (below) and the web app, where a
> book browses like a folder (📚) into chapters — each a normal audio item you can
> play, search, and clip.

A synced **EPUB3 Media Overlay** book bundles the text *and* the narration audio
in one `.epub`, with a SMIL map tying each sentence to an audio time span. The
biggest producer is [Storyteller](https://gitlab.com/storyteller-platform/storyteller),
which aligns an ebook to its audiobook; its output is named `<Title> (readaloud).epub`
(newer) or `aligned/<Title>.epub` (older). quipclipper detects these by their
manifest (a media-overlay declaration), regardless of filename.

Point `search`/`clip` at the `.epub` and quipclipper reads its overlay cues
directly — no sidecar, no extraction step for you:

```
quipclipper search "you will become one with the borg" --video "Book (readaloud).epub"
quipclipper clip   "you will become one with the borg" --video "Book (readaloud).epub"
```

- The result is **always an audio clip** cut straight from the embedded narration
  (the matched line's own audio file is pulled from the zip and trimmed).
- Granularity is **sentence-level** (tighter than typical subtitles).
- Defaults to a lossless stream copy (`.mka`); `--no-lossless` re-encodes to MP3,
  and `--audio-format wav|flac` writes a lossless WAV/FLAC. Video/`--split-channels`
  options don't apply (narration is mono/stereo, no video).
- All the search options (`--index`/`--pick`/`--min-score`/`--before`/`--after`/…)
  work as usual.

### Sending clips to LeadStinger

[LeadStinger](https://github.com/weckere/leadstinger) is a soundboard for live shows.
quipclipper can hand it a line in one click. In the web app, click the cart-icon **Send to
LeadStinger** button beside Make clip (it uses the Before/After padding), tick the small
Send to LeadStinger toggle beside Clip / Export selected with one line or clip ticked, or
use a clip's Send to LeadStinger button in the Clips library; on
the command line, `quipclipper leadstinger.quip` / `leadstinger.link`. quipclipper cuts
the line, then opens a `leadstinger://quip` link; LeadStinger downloads the clip from the
quipclipper server into a new tile whose trim is exactly the line. (With several lines
ticked, the toggle makes a LeadStinger set file instead; so does the button when the server
can't tell its own address, which is rare: see step 1 below.)

The line is cut with **handles**: its padding, kept as extra audio either side (the clip
bar's Before/After, a bookmark's saved padding, none for a search hit; `--before`/`--after`
on the CLI, default 2 s), so a cue that starts a beat early or ends a word late can be
fixed by moving the trim in LeadStinger instead of cutting again. The handles stop at
the file's edges: a line 0.5 s into an episode gets a 0.5 s lead, and one in the last
second gets whatever is left (a line that runs past the end is cut at the end).

The clip is a **full-mix FLAC**, the same choice as LeadStinger set files:

- **Lossless.** Re-encoding a lossy source (AC-3, AAC, DTS) to FLAC adds no second
  generation of loss, and the cut starts on the exact sample instead of the nearest
  packet or keyframe, as a stream copy would.
- **Every channel kept.** 5.1 stays 5.1; LeadStinger downmixes it or routes the centre
  (dialogue) channel itself.
- **LeadStinger reads it natively.** FLAC needs no ffmpeg on the LeadStinger machine,
  where AC-3, DTS and video containers do.

Clips land in `LeadStinger/` in the clips library, named
`<source>_<HH-MM-SS>_<line>.flac` (the time is where the clip, handles included, starts).
A clip that is already in the library is sent as it is, untrimmed (with ffmpeg,
LeadStinger converts video or AC-3/DTS clips on import).

**Setup, once:**

1. **quipclipper's own address** goes in each link. It is `QC_PUBLIC_URL` when set
   (`https://quipclipper.example.net` on the `app` service in Docker, or
   `services.quipclipper-web.publicUrl` on NixOS); else the address the
   [LAN advertiser](#advertising-on-the-local-network) announces; else the address your
   browser is using for quipclipper, so a fresh install works without setting anything.
   Set `QC_PUBLIC_URL` when the machine with LeadStinger reaches quipclipper at a different
   address than your browser does, and for the CLI's and agents' links.
2. **Connect LeadStinger**: the easy way is [pairing](#connecting-leadstinger-pairing), below,
   with nothing to copy. Or, by hand, with an API token:
3. **Have an API token.** LeadStinger downloads with quipclipper's `QC_API_TOKEN`. If
   there is none yet, make one (`openssl rand -hex 32`) and set it:
   - **Docker:** `QC_API_TOKEN: "<token>"` on **both** the `app` and `web` services in
     `docker-compose.yml` (the `web` one lets `api:<token>` through the password gate).
   - **NixOS:** `services.quipclipper-web.apiTokenFile`, a file (outside the Nix store)
     holding `QC_API_TOKEN=<token>`. If the site has a `passwordFile`, add an `api`
     user to it with the token as its password.

   To find an existing token, look in that same place: the compose file's environment,
   or the file `apiTokenFile` points at (readable by root).
4. **Connect LeadStinger to the server** on the machine where it runs, pasting the token
   when asked (`--token -` reads it from stdin, keeping it out of `ps` and your history):

   ```bash
   leadstinger-cli quip.connect https://quipclipper.example.net --token -
   ```

   LeadStinger keeps the token in its own data folder and checks it against the server.
   It refuses links naming any server it isn't connected to. On Linux with the AppImage
   or a tarball, run `leadstinger --register-links` once so the browser hands it
   `leadstinger://` links.

**The link** ([LeadStinger's reference](https://github.com/weckere/leadstinger/blob/main/docs/QUIPCLIPPER.md)):

```text
leadstinger://quip?server=<QC_PUBLIC_URL>&clip=<clip path>&title=<text>&start=<s>&end=<s>&set=<name>
```

| Parameter | |
|---|---|
| `server` | quipclipper's address (step 1 above), without a trailing `/`. |
| `clip` | The clip's path inside the clips library, as `/api/clips/download/{path}` takes it (`LeadStinger/S03E26_00-33-20_Resistance_is_futile.flac`). |
| `title` | The tile's title: the line (one line, at most 200 characters), or the clip's name. |
| `start`, `end` | Where the line sits *inside the clip*, in seconds: the tile's trim. With 2 s before, a line from 33:22.4 to 33:24.0 gives `start=2&end=3.6`. Left out for a whole library clip. |
| `set` | The LeadStinger set for the tile (made if missing). Left out: LeadStinger's current set. |

Every value is percent-encoded the way a browser's `encodeURIComponent` does, so a `+`
in a title goes out as `%2B`. The link holds no token.

#### Connecting LeadStinger (pairing)

LeadStinger can connect without anyone copying a token: it asks, and you let it in.

1. Start it from either side: **Connect LeadStinger** in quipclipper's Settings (on the
   computer with LeadStinger), **Send to LeadStinger** on a line, which offers to connect
   when LeadStinger doesn't know this server yet, or LeadStinger's own Connect or "Find on
   network".
2. LeadStinger asks whether to connect, then shows a **code** such as `7F3K`.
3. A banner appears at the top of every quipclipper page: **"LeadStinger on laptop wants
   access, code 7F3K."** Check that the code is the one LeadStinger shows, then **Allow**
   (or **Deny**). LeadStinger may open quipclipper for you with that request highlighted.
4. LeadStinger now has its own key to this server, and links and Browse just work.
   Settings ▸ Connected apps lists it; **Revoke** disconnects it.

A request waits 10 minutes. Behind a password (`QC_PASSWORD`), LeadStinger can ask and wait
without the password, but only someone signed in to quipclipper can allow it. From the
command line: `quipclipper pair.list`, `pair.approve ID`, `pair.deny ID`, `clients.list`,
`clients.revoke ID` (behind a password, with `QC_API_TOKEN`). `QC_PAIRING=0` (NixOS:
`pairing.enable = false`) turns pairing off.

#### Advertising on the local network

On a home network, quipclipper can announce itself so LeadStinger finds it ("QuipClipper on
nas") without anyone typing an address. It is off by default:

- **Docker:** uncomment the `advertise` service in `docker-compose.yml` (it needs host
  networking, because announcements don't cross Docker's own network).
- **NixOS:** `services.quipclipper-web.advertise.enable = true` (Avahi).

**Leave it off when quipclipper is only reached over a tailnet or VPN**: it tells every
device on the LAN that quipclipper runs there. See [DEPLOY.md](DEPLOY.md#advertising-on-the-local-network).

From the CLI (it needs a server, `--server`/`QC_URL`):

```bash
quipclipper leadstinger.quip "/shows/TNG/S03E26.mkv" --start 2002.4 --end 2004 \
    --title "Resistance is futile." --set Borg
quipclipper leadstinger.link "Heat/Heat - attached.mkv" --set "Movie Lines"
quipclipper --json leadstinger.quip "/shows/TNG/S03E26.mkv" --start 2002.4 --end 2004 --no-open
```

Both open the link on the machine you run them on (`open` on macOS, `xdg-open` on
Linux) and print it; `--no-open` only prints it, for a script or an agent to pass on.
For several lines at once, `leadstinger.set` and `leadstinger.send` make a LeadStinger
set file instead (see [`CLI.md`](CLI.md)).

### Now playing (Jellyfin)

Watching something on a Jellyfin client and hear a line worth keeping? quipclipper can
ask Jellyfin what is playing and open that file at the playhead, so there is nothing to
search for. quipclipper still reads the media from its own media roots and cuts it
itself; Jellyfin only says what is playing and, below, what each file is called.

**In the web app**, a **Now playing** card at the top of the library lists each Jellyfin
client that is playing or paused: the title, the device and user, and the position. It
refreshes every 5 seconds while it is on screen. **Open at 12:34** opens the file in the
workspace with the line at or before the playhead selected, so **✂ Make clip** cuts what
was just heard (nudge the selection a line up or down first if needed). The search box's
scope menu has **Now playing** too: it opens the one session that is playing a library
file, or shows the card when there are several.

**From the CLI** (needs a server):

```sh
quipclipper nowplaying                               # jellyfin.sessions: what is playing
quipclipper clip.nowplaying -t audio --out-dir .     # cut the line just heard
quipclipper clip.nowplaying --session bedroom --back 5 --lines 2
```

`clip.nowplaying` takes the line at or before the playhead (minus `--back` seconds),
`--lines N` to end a run of N lines there, and every `clip.create` option (`-t audio`,
`--before`/`--after`, `--template`…). With several sessions playing it stops and lists
them; pick one with `--session` (its id, or part of the device, user or client name).

**Accuracy.** A playing client reports its position about every 10 seconds; quipclipper
adds the time since the last report (at most 30 s), so the playhead is close, and a
paused client is exact. The line *at or before* the playhead is the one that has
started, which is the one you just heard.

**Setup**, on the server:

1. **An API key:** Jellyfin → Dashboard → API Keys → +.
2. **Where Jellyfin is and the key:** `QC_JELLYFIN_URL=http://jellyfin:8096` plus
   `QC_JELLYFIN_API_KEY` (or `QC_JELLYFIN_API_KEY_FILE`, a file holding it) on the `app`
   service (Docker), or `services.quipclipper-web.jellyfin.url` and `.apiKeyFile`
   (NixOS; the file can stay root-only, systemd hands the service a copy).
3. **Path mapping**, when Jellyfin sees the media under other paths (it runs in a
   container, or on another machine): `QC_JELLYFIN_PATH_MAP=/movies=/media/movies:/shows=/media/shows`
   (Jellyfin's path `=` quipclipper's, pairs separated by `:`; for a path holding `:`, `;`
   or `=`, JSON: `{"/movies": "/media/movies"}`), or
   `jellyfin.pathMap = { "/movies" = "/srv/movies"; }` (NixOS). A file that maps
   nowhere, or outside the media roots, shows on the card as not in the library.

`GET /api/health` and `/api/config` report `jellyfin: true` once the URL and key are set.

### Titles, posters and the clips library (Jellyfin)

With Jellyfin set up, quipclipper also learns what Jellyfin knows about each file:

- **Posters.** Library rows for the shows, seasons, movies and episodes Jellyfin knows
  show its poster (an episode shows its still), and so do the workspace header (with the
  episode's real title under it) and the Now playing card. A folder or file Jellyfin
  doesn't know keeps its icon. The images come through quipclipper's server, so the API
  key never reaches the browser.
- **Clip names.** In the Name template, `{title}` is the episode's or movie's real title,
  and `{show}`, `{season}`, `{episode}` and `{year}` come from Jellyfin too:
  `{show}/S{season}E{episode}_{title}` files a clip as
  `Star_Trek_The_Next_Generation/S03E26_The_Best_of_Both_Worlds.mkv`. A file Jellyfin
  doesn't know gets the names parsed from its path. The default name (`…_{folder}`) is
  the same as before.
- **New clips on the TV.** When a clip or a LeadStinger set lands in the clips folder,
  quipclipper asks Jellyfin to scan the library that holds it, ten seconds after the last
  one, so the clip is there on the TV without waiting for Jellyfin's own scan.

What Jellyfin knows comes from an index of its whole library, kept in the state dir and
rebuilt every hour (a 25,000-item library takes about 15 seconds; quipclipper keeps
working from the old index meanwhile). After adding media, Jellyfin's own scan picks it
up, and the next rebuild brings it here; `quipclipper jellyfin.reindex` does it now.
`quipclipper jellyfin.lookup PATH` shows what Jellyfin knows about a file or folder and
the values its clip names get.

**For the clips library**, add the clips folder to Jellyfin as a library (a *Home videos*
library suits clips: no metadata lookups), mount it into Jellyfin's container if it runs
in one, and add it to the path map like the media folders
(`QC_JELLYFIN_PATH_MAP=…:/clips=/srv/clips`, or `jellyfin.pathMap."/clips"`). quipclipper
finds the library by its folder. Without one, clips still work; they just wait for
Jellyfin's own scan.

---

## Architecture

```
src/quipclipper/
  models.py      Cue (a timed subtitle line) and Match (a ranked hit); timestamp formatting.
  subtitles.py   Parse .srt/.vtt/.ass/.ssa/.sub/.json; find sidecars; list/extract embedded tracks; list all streams.
  search.py      Fuzzy ranking over single cues and sliding windows; collapses overlapping span-variants.
  clip.py        Compute the padded time range and cut with ffmpeg (lossless copy, re-encode, or channel split).
                 Sources may be local files or http(s) URLs (resolved YouTube streams — used by the web app).
  mkv.py         MKVToolNix (mkvmerge) backend for lossless cuts; remux-first; disk-space estimate.
  epub.py        EPUB3 media-overlay audiobooks: detect, OPF/NCX/SMIL → chapters + cues, extract embedded audio.
  naming.py      Clip-name templates (tokens, clean titles), shared by the CLI and the web app.
  commands.py    The command registry: every web UI action as a CLI command (endpoint, parameters, UI place).
  client.py      The HTTP client the CLI uses to drive a quipclipper server.
  cli.py         The CLI, built from the registry; search, clip and tracks also run on local files.
web/backend/quipclipper_web/   FastAPI app: library, search, jobs, bookmarks, YouTube, LeadStinger, Jellyfin.
web/frontend/                  The web UI: index.html, app.js, styles.css, tokens.css (no build step).
```

The cutting backends share the same `ClipRange` (start/end in seconds) and
`Cue`/`Match` data types. The CLI decides which backend to use, builds the
preview/confirmation, and dispatches to `clip.cut_clip`,
`clip.split_audio_channels`, or `mkv.cut_with_mkvmerge`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite covers subtitle parsing (markup stripping, multi-line joining),
search ranking and span-variant collapse, output/container selection, ffmpeg and
mkvmerge command construction, channel grouping, the SRT renderer, the CLI's
selection parsing, EPUB media-overlay parsing, and JSON transcript formats; the web
app's tests (`cd web/backend && pytest`) cover the API, CLI parity, and guards on the
frontend (every action keeps a control, hover text and names, token contrast). The
command-building tests are pure functions and need no
media; the integration behaviour was verified against real `ffmpeg` and
`mkvmerge` during development.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ffmpeg not found on PATH` | Install ffmpeg (and ffprobe). |
| `mkvmerge not found` (note) | Install MKVToolNix for the most accurate cuts; quipclipper falls back to ffmpeg. |
| `Could not determine the channel layout` | The audio stream's layout is unknown to ffprobe; channel splitting needs a recognised layout. |
| `None of the requested --audio-track indices exist` | Check indices with `quipclipper tracks`. |
| Clip starts slightly early | Expected for lossless cuts (keyframe lead-in); use `--no-lossless` for frame-exact boundaries. |
| `--out can't be used when clipping multiple matches` | Drop `--out`; clips are auto-named per match. |
| `Send to LeadStinger can't tell this server's address` (503), or the toggle only makes set files | Set `QC_PUBLIC_URL` (NixOS: `publicUrl`) to the server's base URL; see [Sending clips to LeadStinger](#sending-clips-to-leadstinger). |
| LeadStinger refuses the link as not connected | Connect it: Settings ▸ Connected apps ▸ **Connect LeadStinger** ([pairing](#connecting-leadstinger-pairing)), or `leadstinger-cli quip.connect <the link's server> --token -` on that machine. |
| No banner appears when LeadStinger asks to connect | The banner checks every 20 s (every 3 s while a request waits, or right after Connect LeadStinger), only while the page is visible; reload the page. `QC_PAIRING=0` turns pairing off. |
| No Now playing card | Jellyfin isn't configured: set `QC_JELLYFIN_URL` and the API key (`/api/health` says `jellyfin: true` when it is). |
| `Jellyfin answered 401 (check the API key)` / `Jellyfin is unreachable` (502) | The key is wrong or revoked, or `QC_JELLYFIN_URL` isn't reachable from the server. |
| Now playing says "Not in quipclipper's library" | Jellyfin's path for that file has no `QC_JELLYFIN_PATH_MAP` entry, or it maps outside the media roots. |
| Nothing happens on Send to LeadStinger | The system doesn't know the `leadstinger://` scheme: open LeadStinger once (macOS) or run `leadstinger --register-links` (Linux AppImage/tarball). |

## License

MIT.
