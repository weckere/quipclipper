# clipper

Find and cut audio/video clips from movies & TV shows by **searching the subtitle dialogue**.

Subtitles already carry precise timestamps, so the whole trick is: parse the
subtitles → fuzzy-search the line you remember → map the match back to its time
span → cut it out with ffmpeg. Point it at a video plus a subtitle file (or let
it find a sidecar `.srt` / extract an embedded track), type the dialogue, and get
a clip.

## Features

- **Local, offline** — works on your own video + subtitle files, no API keys.
- **Lossless by default** — audio/video are stream-copied (`-c copy`) with no
  re-encoding, so the original format is preserved exactly (lossy stays lossy),
  including all audio tracks and 5.1/7.1 channel layouts. Near-instant cuts.
- **MKVToolNix backend** — for MKV sources clipper cuts with `mkvmerge` (tighter
  cuts, all tracks/chapters, native subtitle handling); `--remux-first` runs a
  full mkvmerge pipeline that bypasses ffmpeg entirely.
- **Fuzzy, ranked search** — case-insensitive, typo-tolerant, with surrounding
  context so you pick the right hit. Phrases that span two or three captions
  still match.
- **Flexible subtitle source** — explicit `--subs`, a sidecar file next to the
  video, or an embedded subtitle track pulled out via ffmpeg.
- **Audio / video / gif output** — pick with `--type`.
- **Track selection** — keep all audio tracks or pick specific ones with
  `--audio-track`; video clips also retain embedded subtitle tracks.
- **Surround channel split** — `--split-channels` exports a 5.1/7.1 track as
  stereo pairs + centre/LFE files, in lossless WAV/FLAC or the source codec.
- **Subtitles in clips** — embedded subtitle streams are preserved, and external
  search subtitles are muxed in, aligned to the cut (`--no-embed-subs` to skip).
- **Configurable padding** — clip the line's own span plus `--before` / `--after`
  seconds (e.g. start 5s early and run a few seconds long).

## Requirements

- Python ≥ 3.10
- [`ffmpeg`](https://ffmpeg.org/) and `ffprobe` on your `PATH` (used to cut clips
  and to read/extract embedded subtitle tracks)
- *(optional)* [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge`) — used as
  the cutting backend for MKV sources; see "MKV sources" below

## Install

```bash
cd clipper
pip install -e .          # or: pip install -e ".[dev]" for the test deps
```

This exposes a `clipper` command.

## Usage

### Search for a line

```bash
clipper search "i'll be back" --subs movie.srt
```

```
[0]     1.00s  00:00:01.000–00:00:03.000  (score 100)
      I'll be back.
```

Use a video instead of a subtitle file and clipper will look for a sidecar
subtitle, or pull an embedded track:

```bash
clipper search "hasta la vista" --video movie.mkv
```

### Cut a clip

```bash
# audio (default, lossless stream copy -> codec-matched container e.g. .m4a)
clipper clip "i'll be back" --video movie.mkv --type audio

# video segment, with extra padding: start 5s before the line, end 3s after
clipper clip "get to the chopper" --video movie.mkv --type video --before 5 --after 3

# gif (always re-encoded)
clipper clip "hasta la vista" --video movie.mkv --type gif

# force a re-encode for frame-exact boundaries / a specific format (e.g. mp3)
clipper clip "i'll be back" --video movie.mkv --type audio --no-lossless

# pick a different ranked match, name the output, skip the confirm prompt
clipper clip "come with me" -v movie.mkv -i 1 -o out.m4a --yes
```

By default the clip covers the matched line's own start/end plus `--before` /
`--after` padding (0.5s each). The output is auto-named next to the source unless
you pass `--out`.

### Choosing audio tracks

By default every audio stream is kept. Use `--audio-track` (a `a:N` index, or a
comma-separated list) to keep only some:

```bash
clipper clip "i'll be back" -v movie.mkv --audio-track 0      # just the first track
clipper clip "i'll be back" -v movie.mkv --audio-track 0,2    # tracks 0 and 2
clipper tracks movie.mkv                                       # list all streams + indices
```

`clipper tracks` prints the video, audio and subtitle streams with the `a:N` /
`s:N` indices to feed back into `--audio-track` / `--track`:

```
Video:
  v:0  h264
Audio:
  a:0  ac3  5.1(side)  eng
  a:1  aac  stereo  eng  'Commentary'
Subtitle:
  s:0  subrip  eng
```

### Splitting surround sound into separate files

`--split-channels` writes one file per channel group — a stereo file for each
L/R pair (front, side, back) plus a mono file for the centre and LFE channels:

```bash
# 5.1 -> front.wav (stereo) + side/back.wav + center.wav + lfe.wav  (lossless PCM)
clipper clip "get to the chopper" -v movie.mkv --split-channels

# lossless FLAC instead of WAV
clipper clip "get to the chopper" -v movie.mkv --split-channels --split-format flac

# drop the LFE channel
clipper clip "get to the chopper" -v movie.mkv --split-channels --no-lfe
```

**Channel splitting writes lossless WAV or FLAC and never does a lossy
re-encode.** Pulling channels apart does require *decoding* the surround mix —
that is unavoidable, you cannot route channels out of a compressed stream without
decoding it — but the decoded audio is written verbatim: `wav` is raw PCM
(`pcm_s24le`) and `flac` is lossless compression (bit-exact). Neither loses any
quality relative to the source.

There is also an opt-in `--split-format original`, the **only** option that
re-encodes (back to the source codec, e.g. AC3). Use it only if you specifically
need the original format rather than lossless WAV/FLAC.

## MKV sources: the mkvmerge backend

For Matroska sources, clipper can cut with **[MKVToolNix](https://mkvtoolnix.download/)'s
`mkvmerge`** instead of ffmpeg. mkvmerge splits MKV losslessly and superbly: it
keeps every track, chapter and attachment, never re-encodes, produces tighter
cuts, and trims and time-shifts subtitles natively (including a sidecar file).

```bash
clipper clip "i'll be back" -v movie.mkv -t video                    # auto: uses mkvmerge
clipper clip "i'll be back" -v movie.mkv -t audio --backend mkvmerge  # force mkvmerge (-> .mka)
clipper clip "i'll be back" -v movie.mp4 -t video --backend ffmpeg    # force ffmpeg
clipper clip "i'll be back" -v movie.mkv -t video --no-chapters       # drop chapters
```

`--backend` is `auto` (default), `ffmpeg`, or `mkvmerge`:

- **auto** — uses mkvmerge for any lossless **audio or video** cut of an MKV
  source when mkvmerge is installed (audio → `.mka`, video → `.mkv`); otherwise
  ffmpeg. For non-MKV sources auto stays on ffmpeg unless you force `--backend
  mkvmerge` or `--remux-first`.
- **mkvmerge** — used for both audio and video (output is always Matroska:
  `.mkv` / `.mka`; works on non-MKV sources too). Only supports lossless cuts —
  not gif, `--no-lossless`, or `--split-channels`.
- **ffmpeg** — always use ffmpeg.

`--chapters` / `--no-chapters` (default keep) controls whether chapters are kept
in mkvmerge output; mkvmerge trims them to the clip range.

### `--remux-first` — a fully-mkvmerge pipeline

`--remux-first` first muxes the source **and any sidecar subtitle** into a
temporary MKV with mkvmerge, then cuts the clip from that, so ffmpeg is bypassed
entirely — useful for non-MKV sources (or sidecar subs) when you want mkvmerge's
accuracy for the whole pipeline:

```bash
clipper clip "i'll be back" -v movie.mp4 -s movie.srt -t video --remux-first
```

This writes a full-size temporary copy next to the output (extra disk space) and
deletes it afterward. mkvmerge muxes local sources very fast, so on a fast disk
this is often barely slower — and more accurate — than reading the source
directly. Requires a lossless audio/video cut.

mkvmerge requires MKVToolNix on your `PATH` (`mkvmerge`).

### Subtitles in video clips

Video clips keep all embedded subtitle tracks (they ride along in the `.mkv`
stream copy). When you search with an external subtitle file (`--subs` or a
sidecar), clipper also muxes those lines into the clip, trimmed and time-shifted
to line up with the cut. Disable with `--no-embed-subs`:

```bash
clipper clip "i'll be back" -v movie.mkv -t video                 # subtitles included
clipper clip "i'll be back" -v movie.mkv -t video --no-embed-subs # video subs only
```

## Lossless cutting

Inspired by [LosslessCut](https://github.com/mifi/lossless-cut), clips are cut
**losslessly by default**: ffmpeg stream-copies (`-c copy`) the original encoded
packets straight into a new container — there is **no re-encoding at all**. The
source format is preserved exactly: a lossy AC3/AAC/EAC3 track stays that same
lossy bitstream, byte-for-byte, and the cut is near-instant.

What "preserve everything" means here:

- **No transcoding** — the encoded audio/video bytes are copied, not re-rendered.
- **All audio tracks are kept** — clipper maps *every* audio stream (e.g. a 5.1
  EAC3 main track plus a stereo commentary), with their language/title metadata.
- **Multichannel layouts are intact** — 5.1 / 7.1 channel layouts are part of the
  copied bitstream, so they come through untouched.
- **Video mode keeps all video, audio and subtitle tracks** in one `.mkv`.

The one inherent tradeoff — true of every codec — is that a copy can only begin
at a **keyframe**. clipper seeks to the nearest keyframe at or before your start
time, so a lossless clip may start a little earlier than requested (the **end is
exact**). For dialogue clips that just adds a small lead-in, which is usually
welcome.

Containers are chosen to hold the source streams without transcoding:

| Mode | Output container |
|---|---|
| Lossless audio, single stream | codec-matched (`.m4a` / `.ac3` / `.eac3` / `.opus` / `.flac` / …) |
| Lossless audio, **multiple streams** | `.mka` (Matroska — holds any number of streams/codecs) |
| Lossless video | `.mkv` (all video + audio + subtitle tracks) |
| `--no-lossless` audio (re-encode) | `.mp3` |
| `--no-lossless` video (re-encode) | `.mp4` (H.264 / AAC) |

Use `--no-lossless` only when you deliberately want a re-encode (frame-exact
boundaries or a specific format like mp3). GIF output is inherently a re-encode
and ignores the flag. (For MKV sources the default backend is mkvmerge — see
"MKV sources" above.)

## How it works

| Module | Responsibility |
|---|---|
| `models.py` | `Cue` (a timed subtitle line) and `Match` (a ranked hit). |
| `subtitles.py` | Parse `.srt`/`.vtt`/`.ass`/`.sub`; find sidecars; list/extract embedded tracks; list all streams. |
| `search.py` | Fuzzy ranking (`rapidfuzz`) over single cues and sliding windows of consecutive cues. |
| `clip.py` | Turn a match into a padded time range and cut it with ffmpeg (lossless `-c copy`, re-encode, or surround channel split). |
| `mkv.py` | MKVToolNix (`mkvmerge`) backend for lossless cuts of Matroska sources. |
| `cli.py` | `typer` CLI: `search`, `clip`, `tracks`. |

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests cover subtitle parsing (markup stripping, multi-line joining) and the
search ranking, and don't require ffmpeg.

## License

MIT
