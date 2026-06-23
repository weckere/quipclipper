# quipclipper — User Manual

`quipclipper` finds and cuts audio/video clips from movies and TV shows by **searching
the subtitle dialogue**. Subtitles already carry precise timestamps, so the whole
job is: parse the subtitles → fuzzy-search the line you remember → map the match
back to its time span → cut it out. The result is a lossless clip, produced
in a fraction of a second.

This manual is the complete reference. For a shorter tour see [`../README.md`](../README.md);
for *why* the code is built the way it is, see [`DESIGN_NOTES.md`](DESIGN_NOTES.md).

---

## Table of contents

- [Installation](#installation)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
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
`rapidfuzz`, `typer`).

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
 (.srt/.vtt/.ass/.json) (timed lines)          (non-overlapping)        (+padding)    (ffmpeg/mkvmerge)
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

## Command reference

quipclipper has three commands: `search`, `clip`, and `tracks`. Run `quipclipper --help`
or `quipclipper <command> --help` for the built-in summary.

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
| `--limit`, `-n` | 10 | Maximum number of matches to show. |
| `--min-score` | 60 | Drop matches scoring below this (0–100). |
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
| `--video`, `-v` | *required* | Source to cut from. A video file, or an audio-only file (podcast/audiobook) — for audio sources `--type video` is treated as `audio` (there's no video stream). |
| `--subs`, `-s` | — | Subtitle file; otherwise a sidecar or embedded track is used. |
| `--track` | — | Subtitle track by `s:N` index (from `tracks`); auto-selected when several exist (English full dialogue > SDH > forced; commentary tracks are deprioritised so a plain dialogue track wins). |

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
| `--min-score` | 60 | Drop matches scoring below this (0–100). |
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
so the clip begins a little earlier than requested (the **end is exact**). On a
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
mkvmerge produces tighter lossless cuts than ffmpeg. GIF output is inherently a
re-encode and ignores `--lossless`.

### Backends: ffmpeg and mkvmerge

quipclipper has two cutting backends:

- **ffmpeg** — universal; used for gif, re-encodes (`--no-lossless`), channel
  splitting, and as a fallback when mkvmerge is unavailable.
- **mkvmerge** ([MKVToolNix](https://mkvtoolnix.download/)) — used for lossless
  audio/video cuts when installed. It splits losslessly and superbly: keeps every
  track, chapter and attachment, never re-encodes, produces tighter cuts, and
  trims/time-shifts subtitles natively. Output is always Matroska (`.mkv` / `.mka`).

`--backend` selects the strategy:

| Value | Behaviour |
|---|---|
| `auto` (default) | Use mkvmerge for any lossless audio/video cut when it's installed (MKV and non-MKV sources); otherwise ffmpeg. |
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
[2]  1290.00s  00:21:30.000–00:21:33.000  (score 88)
      You said you'll be back.
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
container order.

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
`<stem>_<HH-MM-SS_mmm>.<ext>`. The extension is chosen to hold the source streams
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

---

## Architecture

```
src/quipclipper/
  models.py      Cue (a timed subtitle line) and Match (a ranked hit); timestamp formatting.
  subtitles.py   Parse .srt/.vtt/.ass/.sub; find sidecars; list/extract embedded tracks; list all streams.
  search.py      Fuzzy ranking over single cues and sliding windows; collapses overlapping span-variants.
  clip.py        Compute the padded time range and cut with ffmpeg (lossless copy, re-encode, or channel split).
  mkv.py         MKVToolNix (mkvmerge) backend for lossless cuts; remux-first; disk-space estimate.
  cli.py         typer CLI: search, clip, tracks.
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
mkvmerge command construction, channel grouping, the SRT renderer, and the CLI's
selection parsing. The command-building tests are pure functions and need no
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

## License

MIT.
