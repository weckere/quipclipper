# quipclipper — Design Notes & Rationale

This document records the significant design decisions made while building
quipclipper, the alternatives considered, and why each choice was made. It is
organised by topic rather than chronologically. For usage see
[`MANUAL.md`](MANUAL.md).

A recurring theme: quipclipper is built around the observation that **subtitles are a
free, accurate timecode index** for spoken dialogue, and that **lossless cutting**
of media is a solved problem if you respect codec constraints. Most decisions
follow from taking those two facts seriously.

---

## Contents

1. [Scope and overall approach](#1-scope-and-overall-approach)
2. [Language and libraries](#2-language-and-libraries)
3. [Lossless by default: stream copy](#3-lossless-by-default-stream-copy)
4. [The seek/keyframe model and ffmpeg flag ordering](#4-the-seekkeyframe-model-and-ffmpeg-flag-ordering)
5. [Container and extension selection](#5-container-and-extension-selection)
6. [Preserving all audio tracks and multichannel layouts](#6-preserving-all-audio-tracks-and-multichannel-layouts)
7. [Splitting surround sound](#7-splitting-surround-sound)
8. [Subtitles: preservation and alignment](#8-subtitles-preservation-and-alignment)
9. [The mkvmerge backend](#9-the-mkvmerge-backend)
10. [remux-first, the disk-space prompt, and MKV auto-skip](#10-remux-first-the-disk-space-prompt-and-mkv-auto-skip)
11. [Search ranking and span-variant collapse](#11-search-ranking-and-span-variant-collapse)
12. [The interactive picker](#12-the-interactive-picker)
13. [Interactive subtitle track selection](#13-interactive-subtitle-track-selection)
14. [CLI and UX conventions](#14-cli-and-ux-conventions)
15. [Nix flake](#15-nix-flake)
16. [Testing strategy](#16-testing-strategy)
17. [References](#references)

---

## 1. Scope and overall approach

**Decision:** Build a single-purpose CLI that turns a remembered line of dialogue
into a clip, doing the least possible work to each media stream.

**Rationale:** The core insight is that subtitle files already contain
frame-relevant timestamps for every line of dialogue. That removes the hardest
part of "find the clip" — no audio fingerprinting, speech-to-text, or scene
detection is needed. Everything else is a thin orchestration layer over existing,
battle-tested tools (ffmpeg, mkvmerge). The design goal throughout was **fidelity
first**: never degrade the source unless explicitly asked.

**Alternatives considered:** A GUI/web app, or speech-to-text search of the audio
track. Both were rejected as far more work for no benefit when subtitles exist;
the project deliberately targets "any video that ships with subtitles."

---

## 2. Language and libraries

**Decision:** Python ≥ 3.10, with `pysubs2`, `rapidfuzz`, and `typer`.

- **Python** — best ecosystem for subtitle parsing, fuzzy matching and shelling
  out to media tools; easy to iterate on.
- **[`pysubs2`](https://github.com/tkarabela/pysubs2)** — one library that parses
  SubRip (`.srt`), WebVTT (`.vtt`), SubStation Alpha (`.ass`/`.ssa`) and MicroDVD
  (`.sub`) with a uniform API. Rationale: handling all common subtitle formats
  without writing parsers, and pysubs2 stores times in milliseconds with a simple
  model. Alternative `srt`/`webvtt-py` were narrower (single-format).
- **[`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz)** — fast C++-backed fuzzy
  string matching (MIT-licensed, unlike `fuzzywuzzy` which is GPL and slower). Its
  `partial_ratio`, `token_set_ratio` and `ratio` scorers are exactly the building
  blocks needed for dialogue search (see §11).
- **[`typer`](https://typer.tiangolo.com/)** — declarative CLI with type hints,
  automatic `--help`, and boolean `--flag/--no-flag` pairs. Rationale: minimal
  boilerplate for a CLI with many options; the `--x/--no-x` convention maps
  cleanly onto quipclipper's many toggles.

**Layout:** `src/`-layout package with a `pyproject.toml` (hatchling backend) and a
`quipclipper` console-script entry point — standard, tooling-friendly Python packaging.

---

## 3. Lossless by default: stream copy

**Decision:** Default to lossless cutting via ffmpeg stream copy (`-c copy`); make
re-encoding opt-in (`--no-lossless`).

**Context / inspiration:** [LosslessCut](https://github.com/mifi/lossless-cut) is
the well-known GUI proof that the right way to cut media is to **copy the encoded
packets** rather than decode-and-re-encode. quipclipper adopts the same philosophy on
the command line.

**Rationale:**
- **Quality** — a lossy source (AC3, AAC, H.264) must never be silently
  re-encoded; that compounds generation loss. With `-c copy` the exact bitstream
  is preserved.
- **Speed** — copying is I/O-bound and essentially instant versus real-time
  re-encoding.

**Clarification baked into the code and docs:** "lossless" here means *no
re-encode*. A lossy format stays lossy (the identical bitstream); it is not
converted to a so-called lossless audio format. This distinction is called out
explicitly because it is easy to conflate "lossless cut" with "lossless codec."

**Exceptions:** GIF is inherently a re-encode. `--no-lossless` exists for the cases
where a re-encode is actually wanted (frame-exact boundaries, or a portable format
like MP3/MP4).

---

## 4. The seek/keyframe model and ffmpeg flag ordering

**Decision:** Seek with `-ss` **before** `-i`, specify duration with `-t`, and add
`-avoid_negative_ts make_zero`. Accept that lossless cuts snap the start to the
nearest keyframe.

**Context:** ffmpeg's seeking semantics are notoriously subtle
([FFmpeg wiki: Seeking](https://trac.ffmpeg.org/wiki/Seeking)). `-ss` before `-i`
performs a fast input seek; `-ss` after `-i` is an accurate but slower
decode-and-discard seek. For stream copy you must land on a keyframe, because a
copied video stream cannot start mid-GOP and remain decodable.

**What was learned during development:** With `-c copy`, a copy can only begin at a
keyframe, so the output starts at the keyframe at or before the requested time and
runs to (requested start + duration). The **end is exact**; the **start may include
a small keyframe lead-in**. This matches LosslessCut's behaviour and is documented
as a feature (a little extra lead-in before a line is usually welcome).

- `-t <duration>` is used rather than `-to <end>` because, combined with a
  pre-input `-ss`, duration is interpreted unambiguously relative to the seek.
- `-avoid_negative_ts make_zero` normalises the output so it starts cleanly at
  timestamp zero after the keyframe seek (otherwise a copied stream can carry a
  negative/again-offset start that confuses players).

**"Smart cut" (re-encoding only the lead-in GOP)** — LosslessCut offers this for
frame-exact lossless starts. It was deliberately **not** implemented: for dialogue
clips the lead-in is harmless, and `--no-lossless` already covers anyone who needs
exact boundaries. Adding partial re-encode/concat was judged not worth the
complexity.

---

## 5. Container and extension selection

**Decision:** When auto-naming a lossless audio clip, choose a container that can
hold the source codec without transcoding; fall back to Matroska when in doubt.

| Situation | Container | Why |
|---|---|---|
| Single audio stream, known codec | codec-matched (`.m4a`, `.ac3`, `.eac3`, `.opus`, `.flac`, …) | Portable, "natural" container for that codec. |
| Multiple audio streams | `.mka` | A single-codec container can't hold several streams; [Matroska](https://www.matroska.org/) holds any number of streams of any codec. |
| Video | `.mkv` | Holds video + all audio + subtitle tracks losslessly. |

**Rationale:** Stream copy only works if the destination container supports the
codec. Mapping codec → extension (e.g. AAC → `.m4a`, AC3 → `.ac3`) keeps single
files portable, while `.mka`/`.mkv` are the universal fallbacks. The audio codec is
discovered with `ffprobe`; if probing fails or the codec is unknown, `.mka` is the
safe default.

---

## 6. Preserving all audio tracks and multichannel layouts

**Decision:** Lossless audio maps **all** audio streams (`-map 0:a`) by default, not
just the first; lossless video maps all video, audio and subtitle tracks
(`-map 0:v? -map 0:a? -map 0:s?`).

**Context:** An early version mapped only `-map 0:a:0`, which silently dropped a
5.1 main track's companions (a stereo commentary, other-language dubs). This was a
real bug fixed after the requirement was clarified: *completely preserve the
original, including every track*.

**Rationale:**
- Channel layouts (5.1/7.1) live **inside** a stream, so `-c copy` preserves them
  automatically — no `-ac`/downmix is ever applied.
- Multiple audio *streams* must be mapped explicitly, or ffmpeg's default stream
  selection keeps only one.
- `--audio-track` lets the user narrow to specific streams by `a:N` index when they
  don't want all of them.

---

## 7. Splitting surround sound

**Decision:** Provide `--split-channels` that writes one file per channel group
(stereo pairs front/side/back, plus mono centre and LFE), in lossless WAV/FLAC by
default, with an opt-in `original` re-encode. `--include-lfe/--no-lfe` toggles LFE.

**The honest constraint:** Splitting **cannot be a stream copy**. Individual
channels can't be routed out of a compressed surround bitstream without decoding
it. This is a property of the codecs, not a quipclipper limitation, and it is stated
plainly in the CLI note and the docs.

**Rationale for the format choices:**
- **WAV (`pcm_s24le`)** and **FLAC** are lossless *relative to the decoded audio* —
  the decoded PCM is written verbatim (WAV) or losslessly compressed (FLAC). 24-bit
  PCM avoids quantising below the decoder's output. This is what the user wants when
  they say "lossless split."
- **`original`** re-encodes back to the source codec for those who specifically
  need the original format; it is clearly marked as the only re-encoding path.

**Implementation:** Channels are routed with ffmpeg's `pan` filter using named
channels (`FL`, `FR`, `FC`, `LFE`, `SL`, `SR`, …). The channel names are derived
from the stream's `channel_layout` (probed via ffprobe) using a table of the common
layouts (`5.1`, `5.1(side)`, `7.1`, etc.), with a fall back keyed on channel count.
A two-stage seek (fast seek to just before the start, then an accurate trim) makes
the split sample-accurate, which is affordable because splitting re-encodes anyway.

---

## 8. Subtitles: preservation and alignment

**Decision:** Always keep embedded subtitle tracks in lossless video clips, and
mux the *search* subtitle (sidecar/explicit) into the clip, aligned to the cut.
`--no-embed-subs` opts out.

**The hard part — alignment:** A first attempt added the sidecar `.srt` as a second
ffmpeg input with its own `-ss`. This produced **misaligned** subtitles: ffmpeg's
text-subtitle input seeking is unreliable (it kept lines from outside the range and
shifted times incorrectly), and the muxed stream picked up a multi-second offset.

**Resolution (ffmpeg backend):** Because quipclipper has already parsed the cues, it
**renders its own clip-aligned SRT** in memory (the cues overlapping the window,
shifted to start at zero) and muxes that with no `-ss`. The key realisation was
that `-avoid_negative_ts make_zero` applies the **same global timestamp shift** to
the muxed subtitle as to the copied video, so generating the SRT relative to the
requested start (not the keyframe) keeps the two perfectly in sync — the keyframe
lead-in cancels out. This was verified by extracting both the preserved embedded
track and the muxed sidecar from a test clip and confirming identical timing.

**With mkvmerge** the problem disappears: a sidecar added as an extra input is
trimmed and time-shifted natively by `--split`, so quipclipper just passes the file.

---

## 9. The mkvmerge backend

**Decision:** Use [MKVToolNix](https://mkvtoolnix.download/)'s `mkvmerge` as the
preferred backend for lossless audio/video cuts, falling back to ffmpeg when it is
absent.

**Rationale:** For Matroska (and, via mkvmerge's broad input support, many other
containers) `mkvmerge --split parts:START-END`
([mkvmerge docs](https://mkvtoolnix.download/doc/mkvmerge.html)) is an excellent
lossless splitter: it keeps every track, chapter and attachment, never re-encodes,
produces tighter cuts than ffmpeg's keyframe seek, writes the output to the exact
filename, and trims/shifts subtitles natively. During development it consistently
produced cleaner results than the ffmpeg copy path (correct start timestamps,
native subtitle handling, all tracks retained).

**Implementation details:**
- **Track-id mapping:** `mkvmerge -J` (JSON identification) lists tracks with global
  IDs and types. quipclipper maps the user's ffmpeg-style `a:N` audio indices to
  mkvmerge global IDs by enumerating the audio tracks in order.
- **Selection flags:** `-a` (audio tracks), `-D`/`-S`/`-M`/`-B` (drop video /
  subtitles / attachments / buttons), `--no-chapters`. Audio-only output drops
  video and subtitles and writes `.mka`.
- **Output naming:** a single retained part is written to the given name; older
  mkvmerge versions append `-001`, which quipclipper normalises by renaming.
- **Command construction is a pure function** (`build_mkvmerge_args`) so it can be
  unit-tested without invoking mkvmerge.

---

## 10. remux-first, the disk-space prompt, and MKV auto-skip

**Decision:** By default, remux non-MKV sources (plus any sidecar subtitle) into a
temporary MKV with mkvmerge and cut from that — a fully mkvmerge pipeline that
bypasses ffmpeg. **Skip the remux automatically for sources that are already MKV.**
Estimate the scratch space and confirm before remuxing; `--no-remux-first` opts out;
`--yes` skips prompts.

**Rationale:**
- **Accuracy** — normalising an arbitrary container into a clean MKV first means the
  whole cut is done by mkvmerge, avoiding ffmpeg's container-specific quirks and the
  keyframe-lead-in behaviour. mkvmerge muxes local files very fast, so on a fast
  disk this is often barely slower than reading the source directly.
- **Don't waste work on MKV** — an `.mkv` is already a clean Matroska container, so
  remuxing it to another MKV gains nothing while costing a full-size temporary copy.
  Cutting MKV directly with mkvmerge is equally accurate. Hence the automatic skip
  (`do_remux = use_mkvmerge and remux_first and not is_matroska(source)`).
- **Disk safety** — because the temp file is roughly the size of the source, quipclipper
  estimates it (`~source size + sidecars`, since a remux re-wraps without
  recompressing) and asks for confirmation. The temp file is created next to the
  output and deleted in a `finally` block so it is cleaned up even on error.

**Accuracy disclaimer:** When remux-first is skipped (or ffmpeg is used), quipclipper
prints the specific tradeoff (keyframe lead-in / less precise container timestamp
handling) so the choice is informed. The disclaimer is suppressed for the case
that loses no accuracy (a direct mkvmerge cut of a native MKV).

---

## 11. Search ranking and span-variant collapse

**Decision:** Score each candidate with `max(partial_ratio, token_set_ratio)`; rank
by score with a `ratio` tiebreaker; and **collapse overlapping span-variants** so
results never overlap.

**Why these scorers (RapidFuzz):**
- **`partial_ratio`** finds the best-matching substring, so a short query still
  scores high against a longer caption that contains it — essential for locating a
  line.
- **`token_set_ratio`** rewards matches where word order differs or extra words are
  present. The max of the two surfaces either kind of match.

**Multi-caption windows:** A spoken line is often split across two or three
captions, so the search scans not just single cues but sliding windows of
consecutive cues (up to `--max-span`), joining their text. This lets a sentence
split across captions match as one hit.

**The span-variant problem and its fix:** The window scan emits many overlapping
variants of every line (the cue alone, plus windows joining it with its
neighbours), which cluttered results and the picker. The fix collapses them: in
ranked order, a candidate is accepted only if its cue-index span does not overlap an
already-accepted one, yielding **one candidate per distinct region**.

Getting the *representative* right required a second insight. `partial_ratio` scores
a *fragment* (e.g. "Come with me if you") just as high (100) as the full match, so
naively preferring the "tightest" span would wrongly pick a fragment over the joined
window for a full-sentence query. The ranking therefore adds a **`fuzz.ratio`
tiebreaker** among equal-scoring variants. `ratio` penalises both missing and extra
words, so it prefers the window whose *whole text* best matches the *whole query*:
a one-line query keeps its tight single cue, while a sentence split across captions
keeps the joined window. A `collapse_overlapping=False` switch preserves the old
behaviour for callers that want every variant.

---

## 12. The interactive picker

**Decision:** `--pick` lists the candidate matches and lets the user select **one or
more** (non-exclusive) to clip in a single run.

**Rationale:** A memorable line is frequently a recurring catchphrase. Forcing the
user to guess `--index` for each occurrence is poor UX; a multi-select picker lets
them grab every occurrence (or a chosen subset) at once. This composes naturally
with the non-overlapping search results (§11) — each picker entry is a distinct
place the line occurs.

**Design details:**
- Selection input is parsed from comma-separated indices, `all`, or an empty Enter
  (top match). It reads from stdin via the prompt, so piping a selection works in
  scripts and tests; an earlier `isatty()` gate was removed because it skipped the
  prompt for piped input.
- Duplicate indices are de-duplicated preserving order; out-of-range indices are
  rejected with a clear error.
- A single candidate is auto-selected (no pointless prompt).
- Each selection is auto-named by its timestamp so the files don't collide; `--out`
  is rejected with multiple selections because one path can't name many files.
- The cut dispatch is factored into one helper looped over the selected ranges, so
  single- and multi-clip paths share exactly one code path.

---

## 13. Interactive subtitle track selection

**Decision:** When multiple embedded subtitle tracks exist and no `--track` is
given, prompt the user to choose interactively rather than erroring with a
"re-run with `--track`" message.

**Rationale:** The original behaviour forced a round-trip: the user runs a
command, gets an error listing tracks, then re-runs with `--track N`. The
interactive picker eliminates that friction — the same pattern that `--pick`
uses for match selection. Single-track files and explicit `--subs` still
auto-resolve without a prompt, so the common case is unchanged.

**Non-interactive use is unaffected:** `--track N` bypasses the prompt entirely,
so scripted and piped workflows keep working. The prompt only fires for the
specific case of multiple embedded text tracks with no explicit choice.

**Implementation:** The prompt lives in the CLI layer (`_pick_track` in
`cli.py`), not in `subtitles.py` — subtitle resolution stays non-interactive
and testable. The CLI intercepts the `ValueError` that `resolve_subtitles`
raises for the multi-track case, presents the picker, and re-calls with the
chosen index.

---

## 14. CLI and UX conventions

- **Preview then confirm.** Before cutting, quipclipper prints the selected match(es),
  the clip range, the mode (e.g. "lossless copy (mkvmerge)"), and any relevant
  notes, then asks "Proceed?". `--yes/-y` skips **all** prompts (including the remux
  disk-space confirmation) for scripting.
- **Boolean toggles** use typer's `--x/--no-x` style (`--lossless/--no-lossless`,
  `--remux-first/--no-remux-first`, `--chapters/--no-chapters`,
  `--embed-subs/--no-embed-subs`, `--include-lfe/--no-lfe`).
- **Clean errors, not tracebacks.** Missing tools (ffmpeg/ffprobe/mkvmerge), missing
  files, unknown layouts, bad track indices, image-subtitle extraction failures, and
  ffmpeg/mkvmerge failures are caught and printed as concise red messages with a
  non-zero exit code. Raw ffmpeg/mkvmerge stderr is suppressed when the error is
  already explained by quipclipper's own message.
- **`tracks` shows selectable indices.** It groups streams by type and prints the
  `a:N`/`s:N` indices that feed `--audio-track`/`--track`, so the user can discover
  what to select.

---

## 15. Nix flake

**Decision:** Provide a `flake.nix` so quipclipper can be installed declaratively
on NixOS or nix-darwin systems.

**Rationale:** A Nix flake makes quipclipper a first-class package that can be
added as a flake input to a system configuration. The wrapper automatically
puts `ffmpeg` and `mkvmerge` on `PATH`, so the user never has to manage runtime
dependencies manually.

**Implementation:**
- `buildPythonApplication` with `pyproject = true` and a `hatchling` build
  system, matching the existing `pyproject.toml`.
- `makeWrapperArgs` prefixes `PATH` with `ffmpeg` and `mkvtoolnix-cli` so both
  backends are available without the user installing them separately.
- `pytestCheckHook` runs the test suite during the Nix build.
- A `devShells.default` provides a development environment with all
  dependencies and pytest.

**Usage in a system config:**
```nix
inputs.quipclipper.url = "github:weckere/quipclipper";
# then add inputs.quipclipper.packages.${system}.default to packages
```

---

## 16. Testing strategy

**Decision:** Unit-test the pure logic exhaustively; verify the media integration
against real tools during development rather than mocking ffmpeg/mkvmerge.

**Rationale:** The risky, fiddly parts of quipclipper are *decisions about command
construction and ranking*, not the act of running a subprocess. So:

- **Command builders are pure functions** (`_ffmpeg_args`, `build_mkvmerge_args`)
  returning argument lists, asserted against expected flags. This catches mistakes
  like mapping the wrong streams or using `-to` instead of `-t` without touching
  the filesystem.
- **Ranking, parsing and selection logic** (search scoring, span-variant collapse,
  channel grouping, extension selection, the clip-aligned SRT renderer, the picker's
  selection parser, track-id mapping, disk-size formatting) are all unit-tested.
- **Integration behaviour** (real stream copies, multichannel preservation,
  subtitle alignment, mkvmerge splits, remux-first, the channel split) was verified
  end-to-end against real `ffmpeg` and `mkvmerge` builds — e.g. confirming an AC3
  5.1 track survives a cut at the identical codec/bitrate/layout, and that a muxed
  sidecar lands at the same timing as the preserved embedded track.

This keeps the test suite fast and media-free while still having confidence the
real pipeline works.

---

## References

- **LosslessCut** — lossless media cutting via stream copy; the model for quipclipper's
  default behaviour. <https://github.com/mifi/lossless-cut>
- **FFmpeg — Seeking** — `-ss` before vs. after `-i`, accurate vs. fast seeks.
  <https://trac.ffmpeg.org/wiki/Seeking>
- **FFmpeg documentation** — `-c copy`, `-map`, `-t`, `-avoid_negative_ts`, the
  `pan` and `channelsplit` filters. <https://ffmpeg.org/ffmpeg.html>,
  <https://ffmpeg.org/ffmpeg-filters.html>
- **MKVToolNix — `mkvmerge`** — `--split parts:`, track selection, `-J`
  identification. <https://mkvtoolnix.download/doc/mkvmerge.html>
- **Matroska** — the container used for multi-stream lossless output.
  <https://www.matroska.org/>
- **RapidFuzz** — `partial_ratio`, `token_set_ratio`, `ratio` scorers.
  <https://github.com/rapidfuzz/RapidFuzz>
- **pysubs2** — multi-format subtitle parsing. <https://github.com/tkarabela/pysubs2>
- **Typer** — the CLI framework. <https://typer.tiangolo.com/>
