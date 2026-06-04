# clipper

Find and cut audio/video clips from movies & TV shows by **searching the subtitle dialogue**.

Subtitles already carry precise timestamps, so the whole trick is: parse the
subtitles → fuzzy-search the line you remember → map the match back to its time
span → cut it out with ffmpeg. Point it at a video plus a subtitle file (or let
it find a sidecar `.srt` / extract an embedded track), type the dialogue, and get
a clip.

## Features

- **Local, offline** — works on your own video + subtitle files, no API keys.
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
# audio (default)
clipper clip "i'll be back" --video movie.mkv --type audio

# video segment, with extra padding: start 5s before the line, end 3s after
clipper clip "get to the chopper" --video movie.mkv --type video --before 5 --after 3

# gif
clipper clip "hasta la vista" --video movie.mkv --type gif

# pick a different ranked match, name the output, skip the confirm prompt
clipper clip "come with me" -v movie.mkv -i 1 -o out.mp3 --yes
```

By default the clip covers the matched line's own start/end plus `--before` /
`--after` padding (0.5s each). The output is auto-named next to the source unless
you pass `--out`.

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
| `clip.py` | Turn a match into a padded time range and cut it with ffmpeg. |
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
