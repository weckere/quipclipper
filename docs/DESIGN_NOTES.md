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
13. [Automatic subtitle track selection (`best_track`)](#13-automatic-subtitle-track-selection-best_track)
14. [CLI and UX conventions](#14-cli-and-ux-conventions)
15. [Nix flake](#15-nix-flake)
16. [Testing strategy](#16-testing-strategy)
17. [Experimental: audio-only sources and EPUB3 media overlays](#17-experimental-audio-only-sources-and-epub3-media-overlays)
18. [The web app: one engine, CLI parity, the redesign](#18-the-web-app-one-engine-cli-parity-the-redesign)
19. [Releases and the version](#19-releases-and-the-version)
20. [References](#references)

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
(A web app was later added on top of the same engine — see
[`WEBAPP_PLAN.md`](WEBAPP_PLAN.md) and [section 18](#18-the-web-app-one-engine-cli-parity-the-redesign);
the CLI-first scoping here describes the original engine.)

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
  `ratio` scorer rates misspelled words and whole-line fit in dialogue search (see §11).
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

*Update:* the video default later changed to a frame-exact re-encode (matching
the web app's Exact toggle); audio remains a lossless copy by default.
`--lossless` restores the stream-copy video cut.

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
runs to (requested start + duration). With ffmpeg the **end is exact**; the **start
may include a small keyframe lead-in**. This matches LosslessCut's behaviour and is documented
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

**Output labels:** groups are named `front`, `center`, `lfe`, and the surround
pair(s). A 5.1 mix has a single surround pair (whether the layout calls it `side`
or `back`), so that one file is labelled `surround`; a 7.1 mix has both, kept
distinct as `side` and `back`. The web app's channel-group filter buckets all of
these into `center | front | surround | lfe` (`group_category`), so a user can
export, say, only centre + surround.

**Three distinct audio output modes.** It is worth being precise about how the
surround handling relates to the other audio paths, since they are easy to
conflate:

| Mode | What it does | Channels | When |
|---|---|---|---|
| **Passthrough** (`lossless=True`) | stream-copy (`-c copy`), every audio track kept | source layout, untouched | default; keeps the exact bitstream |
| **Full-mix WAV/FLAC** (`cut_clip(audio_codec=…)`) | decode **one** stream → `pcm_s24le`/`flac`, one file | **all channels kept** (5.1 → 5.1) | want an editable lossless file without the original codec |
| **Split channels** (`split_audio_channels`) | decode → one file per channel group | regrouped (stereo pairs + mono) | want the stems separately |

The full-mix path was added because the "WAV"/"FLAC" format options previously
fell through to the generic re-encode, which produces stereo MP3 — so there was no
way to get, say, a single 5.1 WAV. It is exposed as `--audio-format wav|flac` on
the CLI and the `audio_format` field of the web clip request (both feed
`cut_clip(audio_codec=…)`). WAV holds 5.1 fine: ffmpeg's `wav` muxer writes
`WAVE_FORMAT_EXTENSIBLE` with the channel mask, so `-c:a pcm_s24le` with **no**
`-ac` preserves the layout. The path maps exactly one audio stream (the first
selected, or `a:0`) because the WAV/FLAC containers hold a single stream; keeping
*all* tracks is the passthrough/`.mka` case. Like splitting, it is lossless only
*relative to the decode* — the source is decoded, then written verbatim (PCM) or
losslessly compressed (FLAC).

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
writes the output to the exact filename, and trims/shifts subtitles natively. For
**video** it can only end a kept range on a keyframe, so on a long-GOP source the clip
runs well past the cut (207d7c8: a 3 s line became 22 s): the `auto` backend (server and
CLI alike) uses it for lossless audio only and cuts lossless video with ffmpeg. During development it consistently
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

**Decision:** Cut sources **directly** by default. Offer `--remux-first` as an
opt-in for non-MKV sources that remuxes to a temporary MKV before cutting.
**MKV sources are always cut directly** regardless of the flag. Estimate the
scratch space and confirm before remuxing; `--yes` skips prompts.

**Rationale for changing the default (from on to off):**
- **Network sources** — remux-first copies the entire source to a temp file,
  which is prohibitively slow over network mounts (NAS, SMB, NFS). Since the
  mkvmerge backend now uses ffprobe to find the correct keyframe before the
  start time (see below), direct cuts are accurate enough for the vast
  majority of files. `--remux-first` remains available when maximum accuracy
  matters and the source is on local storage.
- **Accuracy** — normalising an arbitrary container into a clean MKV means the
  whole cut is done by mkvmerge, avoiding container-specific quirks. This is
  still the most accurate path, but it is now opt-in.
- **Don't waste work on MKV** — an `.mkv` is already a clean Matroska container, so
  remuxing it to another MKV gains nothing. Cutting MKV directly with mkvmerge
  is equally accurate. Hence the automatic skip
  (`do_remux = use_mkvmerge and remux_first and not is_matroska(source)`).
- **Disk safety** — because the temp file is roughly the size of the source, quipclipper
  estimates it (`~source size + sidecars`, since a remux re-wraps without
  recompressing) and asks for confirmation. The temp file is created next to the
  output and deleted in a `finally` block so it is cleaned up even on error.

**Keyframe alignment fix:** mkvmerge's `--split parts:` can snap the start to the
*next* keyframe after the requested time, which may land after the dialogue. To
prevent this, `cut_with_mkvmerge` now uses ffprobe to find the last video keyframe
at or before the requested start and passes that to `--split parts:` instead. This
gives the same "keyframe lead-in" behaviour as ffmpeg's `-ss` before `-i`, ensuring
the clip always includes the requested content.

---

## 11. Search ranking and span-variant collapse

**Decision (D-041, 2026-10-23):** Score each candidate on words, so that **100 means
verbatim**; rank by score with a `ratio` tiebreaker; and **collapse overlapping
span-variants** so results never overlap.

**How a candidate is scored.** Query and line are normalised alike: markup (`<i>`,
`{\an8}`) dropped, case folded, apostrophes removed ("I'll" → "ill"), every other
non-word character (punctuation, `♪`, speaker dashes) a word break. Then:
- **100** when the query's words appear in the line as one contiguous run of whole
  words. "make it so" is verbatim in "Make it so, Number One." but not in "You make it
  sound…".
- **Up to 99** for the query's words in order with differences. Each line word is
  matched to query words exactly, or, for words of 3+ letters, at `fuzz.ratio` ≥ 80
  (one misheard or misspelled word), credited by that similarity. The best in-order
  run is rated with an F-measure weighted by word length: recall is the query weight
  matched, precision the matched weight over the line's words from the run's first
  match to its last. Beta 2 makes a missing query word cost more than an extra word
  in between ("Make it all so." 92, "Make it." 76).
- **Up to 75** for the query's words present in any order: 75 × the same F-measure, of
  the weight found over the window's words from the first word found to the last, so
  words scattered through a three-caption window of other talk score well under 60. A
  query word given twice is found once per time the window has it ("no no no" no longer
  scores 75 on "No."). Before splitting into words, text beyond ASCII is NFKC-normalised
  and combining marks (category M) count as part of their word, not as breaks: `\w`
  leaves them out, which split Devanagari or pointed Arabic words at every vowel sign.
  A folder search's query is capped at 300 characters (the in-order alignment is
  quadratic in its words) and returns the best 500 matches (review 2, SR-1/4/5/6).

**Why not the earlier scorers.** It used `max(partial_ratio, token_set_ratio)`.
`token_set_ratio` is 100 whenever one side's words are a subset of the other's, so
any line holding the query's words in any order scored 100 alongside the real line;
`partial_ratio` aligns the *shorter* string inside the longer, so a one-word caption
("HOT") scored 100 for a longer query ("tea earl grey hot"), and it matched inside
words ("make it so" in "make it sound"). On wizrock's library, "make it so" had 315
hits at 100 across 1,278 files, most of them not the line.

**Cost.** Each cue is normalised once per search (not once per window), words are
matched against the query through a per-search memo, and a window is aligned only if
an upper bound from the query words it holds can reach `min_score`. A library-wide
search over 641k cues takes about 2 s on one core, down from about 8 s.

**Unspaced scripts.** A query in a script written without spaces (Chinese, Japanese,
Korean, Thai) is matched on characters: 100 when it is a substring, else
`partial_ratio` capped at 99.

**Multi-caption windows:** A spoken line is often split across two or three
captions, so the search scans not just single cues but sliding windows of
consecutive cues (up to `--max-span`), joining their text. This lets a sentence
split across captions match as one hit.

**The span-variant problem and its fix:** The window scan emits many overlapping
variants of every line (the cue alone, plus windows joining it with its
neighbours), which cluttered results and the picker. The fix collapses them: in
ranked order, a candidate is accepted only if its cue-index span does not overlap an
already-accepted one, yielding **one candidate per distinct region**.

Getting the *representative* right required a second insight. Several windows can
tie, e.g. the verbatim line alone and the line joined with its neighbour both score
100, so naively preferring the "tightest" span could pick the wrong one. The ranking
therefore adds a **`fuzz.ratio` tiebreaker** (`Match.fit`) among equal-scoring variants. `ratio` penalises both missing and extra
words, so it prefers the window whose *whole text* best matches the *whole query*:
a one-line query keeps its tight single cue, while a sentence split across captions
keeps the joined window. A `collapse_overlapping=False` switch preserves the old
behaviour for callers that want every variant.

**Folder and library search read indexed files only (D-042, 2026-10-23).** A folder
search used to stop after 500 media files (R3: one request must finish inside nginx's
600 s) and extracted the subtitles of any it reached that weren't cached. On wizrock
(~33k media files, ~1k indexed) the walk never left `shows/`, so a library search
missed most indexed files and still ran ffmpeg mid-search. Now it searches every file
whose subtitles are cached and no others: extraction (1-9 s a file) belongs to the
folder's Index, and the response counts what it left out (`not_indexed`,
`books_not_searched`). Read-along books are left out of folder sweeps too: a book has
no index and parsing wizrock's 31 took 30-40 s a search. To keep a sweep of ~70k files
quick on a FUSE mount, each folder is listed once (`scandir` file types, no stat per
file), each file gets only its own sidecars from that listing, the cache folder and
transcript store are listed once instead of stat-ed per file, the index check's stats
run in 8 threads, and the EPUB overlay check is cached per path and mtime. A warm
library search there takes ~10 s (the first after a restart ~17 s).

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

## 13. Automatic subtitle track selection (`best_track`)

**Decision:** When multiple embedded subtitle tracks exist and no `--track` is
given, auto-select the best dialogue track with a single scoring function,
`best_track()` in `subtitles.py`. The ranking is **English full dialogue >
English SDH > English forced**; an untagged language counts as English; ties
keep container order. SDH/forced are detected from ffprobe's
`hearing_impaired`/`forced` dispositions with a title-regex fallback.

**Evolution (three iterations):**
1. *Error out* — the original behaviour raised "choose one with `--track`",
   forcing a round-trip.
2. *Interactive prompt* — the CLI caught that error and presented a picker
   (`_pick_track`). Better for humans, but the web app couldn't use it, so the
   web frontend grew its own JS scoring — and the two **drifted**: the backend
   required an explicit `eng` language tag (erroring otherwise) while the
   frontend treated untagged tracks as English, so they disagreed on untagged
   releases.
3. *Score-based single source of truth (current)* — the engine's `best_track()`
   always returns a choice, `resolve_subtitles` reports which track it landed on
   (`ResolvedSubtitles.track`), and the web `/api/items` endpoint exposes it as
   `best_track` so the frontend preselects rather than recomputing. The CLI no
   longer prompts.

**Why image subtitles rank below every text track:** bitmap formats (PGS on
Blu-ray, VOBSUB on DVD) are *pictures* of text — ffmpeg can't turn them into
SRT/VTT without OCR, so they can't be searched, shown in the browser player, or
rendered in the script panel. `best_track` gives any image track a large
negative score (`is_text_subtitle` / `IMAGE_SUBTITLE_CODECS`), so a text track
always wins when one exists; an image track is chosen only when it is the *only*
option (and then extraction fails cleanly with "supply an .srt"). This was a real
bug: *The NeverEnding Story* ships one English `subrip` track plus eight PGS
tracks, and the old picker listed all nine — selecting a `.sup` track silently
showed nothing (HTTP 500). The web picker now filters image tracks out entirely
and notes how many were hidden.

**Why forced ranks below SDH:** forced tracks contain only the foreign-language
portions of a film — minimal dialogue — so for a dialogue-search tool they are
nearly useless. SDH adds sound descriptions to otherwise-complete dialogue,
making it the better fallback when no plain dialogue track exists.

**Why untagged counts as English:** single-language releases frequently ship
with no language metadata at all; treating "no tag" as "probably the release's
primary language" means those files resolve automatically instead of failing.

**Cache consequence:** because auto-selection is deterministic and shared, the
web subtitle cache can be **dual-keyed** — an extraction performed during folder
pre-indexing (auto-select) is also stored under the concrete track index the
item view will request, so indexing warms playback and the script view, not just
search.

**Trade-off accepted:** the CLI lost its interactive prompt for the no-English
multi-track case; it now picks the top-scoring track and **echoes which track it
auto-selected** (`_resolve` reads `ResolvedSubtitles.track` and prints its
label). `--track N` remains the explicit override, and `quipclipper tracks`
lists the candidates. The old `_pick_track` prompt and its now-unreachable
`ValueError` recovery were removed (review finding R4).

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

- The version comes from `pyproject.toml` (`builtins.fromTOML`), not a literal
  (see [section 19](#19-releases-and-the-version)).

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

## 17. Experimental: audio-only sources and EPUB3 media overlays

These are newer, still-being-shaken-out source classes (flagged experimental in
the README/manual). The common thread: quipclipper's core is *cues → search →
cut a time range*, and anything that can be reduced to "(audio file, timed text)"
fits with little new machinery.

**Audio-only sources (podcasts, audiobooks).** A plain audio file (mp3/m4a/m4b/
flac/opus/…) with a **sidecar transcript** is a first-class source.

- *Detection is by extension, transcript-gated.* `AUDIO_EXTS` lists the
  containers; an audio file is only listed/indexed when `find_sidecar` finds a
  transcript next to it (there's nothing to extract embedded subs from). Video
  files still appear unconditionally.
- *Clips are forced to audio.* The web backend coerces a stray `kind="video"`
  request to audio by extension, and the CLI does the same — so batch export and
  scripts can't ask ffmpeg to map a non-existent video stream.
- *Cover art is not video.* Podcast mp3s embed cover art as an `attached_pic`
  mjpeg "video" stream; `list_streams` flags it so the frontend's primary-video
  check ignores it and the file is still treated as audio-only. (Found only by
  testing a real episode — a synthetic file wouldn't have surfaced it.)

**JSON transcripts.** `load_subtitles` routes `.json` to a tolerant parser
covering the shapes that exist in the wild — Podcast Namespace
(`segments[].startTime/endTime/body`), Whisper (`segments[].start/end/text`),
whisper.cpp (`transcription[].offsets` in ms) — rather than one rigid schema.
`.json` is lowest priority in `find_sidecar`, and yt-dlp's `*.info.json` metadata
is explicitly skipped so it isn't mistaken for a transcript.

**Speaker attribution.** `Cue.speaker` is populated from a WebVTT `<v Name>`
voice tag, a transcript `speaker` field, or a recurring `Name:` prefix.

- *Voice tags are recovered from the raw file* — pysubs2 silently strips `<v>`,
  so a raw scan maps each cue start to its speaker. (Validated against real
  Linux Unplugged VTT.)
- *The `Name:` prefix is recurrence-guarded* — only trusted when a name recurs
  (≥2 cues) or is ALL-CAPS, so a one-off "Wait:" in ordinary movie dialogue isn't
  mistaken for a speaker. (Validated: zero false positives on real SRTs.)

**EPUB3 media overlays (Storyteller audiobooks).** A synced EPUB3 bundles text +
narration + a SMIL map (each `<par>` ties a sentence to an audio clip) in one zip.

- *Detect by manifest, not name.* `is_media_overlay_epub` checks the OPF for a
  media-overlay declaration, because the file is named inconsistently
  (`<Title> (readaloud).epub` vs older `aligned/<Title>.epub`).
- *Model per-cue, not per-chapter.* Consecutive chapters often share one audio
  file as sub-ranges, and a chapter can span two files — so every cue carries its
  own `(audio member, start, end)`. This makes the *clip* path trivial (extract
  that one member, trim) and reduces the per-chapter-vs-whole-book question to a
  browse/UX choice rather than a data-model constraint. Whole-book concatenation
  was rejected: multi-GB temp audio, offset bookkeeping, and drift risk, for a
  whole-book search that the existing *folder* search already covers across
  chapters.
- *Extract on demand.* The engine cuts from real paths, so a matched line's audio
  member is copied out of the zip to a temp file, then handed to the normal
  `cut_clip` path. No whole-book unpack.
- *Engine + CLI first, then web.* The reader and CLI `search`/`clip` landed
  first (isolated, testable without the web plumbing). The **web** integration
  presents a book as a *folder of audio segments* (grouped by audio member,
  `<epub>#seg=N`) and reuses the whole audio-only player/script/clip path, so a
  chapter behaves like any other audio item. The clip naming template is fed a
  synthetic source so clips land under `<Book>/…_<Chapter>`.
- *No persistent copy of the audio.* The embedded audio is never cached to disk.
  Playback **streams the zip member directly** with HTTP Range support
  (`ZipExtFile` is seekable; chunked, ~constant memory); `item_info` is
  **synthesized** from the manifest + cue timings (no ffprobe, no file), so
  opening a chapter writes nothing. Only *clipping* needs a real seekable file —
  ffmpeg/mkvmerge can't read a zip member — so the member is extracted to a
  **temp that's deleted the moment the cut finishes**. Nothing accumulates.

---

## 18. The web app: one engine, CLI parity, the redesign

**One engine.** The web app (`web/backend`, FastAPI; `web/frontend`, plain HTML/CSS/JS
with no build step) calls the same `quipclipper` package as the CLI for parsing,
search, track selection, naming and cutting. Anything learned about media (keyframes,
layouts, subtitle tracks) is fixed once and both get it.

**Every web UI action is a CLI command.** `src/quipclipper/commands.py` is a registry
of commands, each with its HTTP endpoint, parameters and place in the web UI; the CLI
is built from it, and `docs/CLI.md` is generated from it. A parity test fails when an
API route, a request field, an `/api/` call in `app.js` or a web preference has no
command, so a new UI feature can't skip the CLI (and agents, which drive quipclipper
through the CLI, get it too). Presentation-only preferences (theme, subtitle size) are
listed as exempt, with the reason.

*Alternative considered:* a separate, hand-written CLI for the server. Rejected: it
would drift from the UI, which is exactly what the parity test prevents.

**Guards against losing controls.** Every control that carries a command is marked
`data-action="<command id>"`; tests check that each command with a web UI entry keeps a
marked control and that every element id the script uses exists. A redesign can move
markup freely but can't silently drop an action.

**The redesign (M12).** The UI was rebuilt around a workspace (player, dialogue map,
transport and the Selection panel on the left, the script with its tabs on the right),
one search box whose scope chip replaces three separate search forms, and design tokens
(`tokens.css`, generated from a FigCraft collection) for dark and light themes. Amber is
the only accent and always means "the selection / the primary action"; blue marks the
playhead.

**Accessibility is tested, not just intended (M16).** `test_frontend_a11y.py` fails on
a control without hover text or an accessible name, and `test_frontend_contrast.py`
checks every token pair against WCAG 2.2 AA (4.5:1 for text, 3:1 for UI), in both themes
and the `prefers-contrast: more` palettes. axe-core runs were clean on every page.

---

## 19. Releases and the version

**Decision:** one version source (`pyproject.toml`), a `dev` branch for work, and
`main` moved only by `scripts/release.sh` at a tagged release.

- The Python packages read their installed metadata (`importlib.metadata`), falling
  back to the checkout's `pyproject.toml`; the Nix packages read it with
  `builtins.fromTOML`. `web/backend/pyproject.toml` stays a copy because the backend is
  built from its own directory (Docker, Nix); `tests/test_version.py` fails if any copy
  disagrees.
- `CHANGELOG.md` gets an Unreleased line with each behaviour change; the release script
  dates that section, and the tag's workflow publishes it as the GitHub Release notes
  next to the GHCR images (`X.Y.Z`, `X.Y`, `latest`).
- `main` is the repository's default branch, so `github:weckere/quipclipper` and a
  visitor's clone get the latest release; `dev` is for those who want work in progress.

*Alternative considered:* a version derived from git tags (setuptools-scm, hatch-vcs).
Rejected: the Nix builds see a source tree without `.git`, and a tag-derived version
can't be read with `builtins.fromTOML`.

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
