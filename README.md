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
- **Fuzzy, ranked search** — case-insensitive, typo-tolerant, with surrounding
  context so you pick the right hit. Phrases that span two or three captions
  still match.
- **Flexible subtitle source** — explicit `--subs`, a sidecar file next to the
  video, or an embedded subtitle track pulled out via ffmpeg.
- **Audio / video / gif output** — pick with `--type`.
- **Configurable padding** — clip the line's own span plus `--before` / `--after`
  seconds (e.g. start 5s early and run a few seconds long).

## Requirements

- Python ≥ 3.10
- [`ffmpeg`](https://ffmpeg.org/) and `ffprobe` on your `PATH` (used to cut clips
  and to read/extract embedded subtitle tracks)

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
and ignores the flag.

### Inspect embedded subtitle tracks

```bash
clipper tracks movie.mkv
# #2 subrip eng 'English'
# #3 subrip spa 'Spanish'
clipper clip "i'll be back" -v movie.mkv --track 2
```

## How it works

| Module | Responsibility |
|---|---|
| `models.py` | `Cue` (a timed subtitle line) and `Match` (a ranked hit). |
| `subtitles.py` | Parse `.srt`/`.vtt`/`.ass`/`.sub`; find sidecars; list/extract embedded tracks. |
| `search.py` | Fuzzy ranking (`rapidfuzz`) over single cues and sliding windows of consecutive cues. |
| `clip.py` | Turn a match into a padded time range and cut it with ffmpeg (lossless `-c copy` or re-encode). |
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
