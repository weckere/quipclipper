# Machine transcription (experimental)

> **Experimental, and off by default.** quipclipper 0.3.0 ships this code switched off: it
> runs and shows only where a deployment turns it on with **`QC_TRANSCRIBE=1`** (the NixOS
> module's `transcribe.enable` sets it). whisper.cpp and a model alone don't turn it on, and
> the Docker images leave it off. Switched off, nothing transcribes, the web app shows none of
> it, the API's transcription endpoints answer 404, the CLI keeps its commands out of its help
> and refuses them locally, and transcripts stored earlier stay out of search and the script.
> It is held back from the release notes until it is released (D-040).

Search reads subtitle lines, so a line the subtitles leave out (most often a song's
lyrics), or a file with no subtitles at all, can't be found. **Machine transcription**
runs [whisper.cpp](https://github.com/ggml-org/whisper.cpp) over a stretch of a file's
audio and keeps what it hears as *machine* lines. They are a search aid, clearly marked
as machine-made wherever they appear, and never replace the real subtitles: where a
subtitle line covers a stretch, the subtitle line wins.

**It only runs when you ask.** Nothing transcribes on its own: not indexing, not opening
a file, not a timer. Each request transcribes one of:

- **a range**: `--start` and `--end`, in seconds (in the web app, a waveform selection);
- **the gaps**: every stretch of 4 s or more between subtitle lines (the default);
- **the whole file**: `--mode file`, for a file with no subtitles.

A whole **folder** (`quipclipper index.transcribe FOLDER`, `--mode nosubs` for the files with
no subtitles, audio without a transcript included, or `--mode gaps`) runs file after file
in one job, on a server (the CLI on its own transcribes one file at a time). A file whose
subtitles can't be checked is skipped and listed with the reason, rather than transcribed
whole. Parts transcribed before are skipped, so asking again only does what's new (`--redo`
does it again, say with a bigger model). English only. **Models:** `small.en` (the
default: quick, a few word errors) or `medium.en` (better punctuation and names in
dialogue, three to five times slower, and no better on songs: measured on three songs it
dropped more sung lines than `small.en`, D-047). Set your default with
`quipclipper settings.set stt_model medium.en`, or pass `--model` per run.

**Songs get a second look.** whisper hears audio 30 s at a time and sometimes drops a sung
line at the end of one of those windows. In a range or the gaps, every stretch of 4 s or
more where it heard nothing (and that isn't quiet, and has no subtitle line) is heard again
on its own. That finds most of what the first pass dropped, for an extra short whisper run
or two per minute of music; dialogue rarely leaves such a stretch, so it costs nothing
there. A whole-file run uses voice detection instead. A long stretch is transcribed in 3-minute pieces, each heard with 2 s of the next; a line heard by both pieces where they meet is kept once.

**It runs at idle priority.** whisper gets the CPU only when nothing else wants it (Linux
`SCHED_IDLE` and nice 19, idle I/O), with 2 threads (3 at most), one transcription at a
time. So it never slows down playback or a clip, and it is slower when the machine is busy.

```bash
quipclipper item.transcribe "Darkplace S01E06.mkv"                  # the gaps
quipclipper item.transcribe "Darkplace S01E06.mkv" --start 353 --end 395
quipclipper item.transcribe "podcast.mp3" --mode file --model medium.en
quipclipper search "way too fast" --video "Darkplace S01E06.mkv"
# [0]   383.10s  00:06:23.100–00:06:27.000  (score 100)
#       [auto] ♪ Driving way too fast ♪
quipclipper item.transcript "Darkplace S01E06.mkv" --fmt srt > lyrics.srt
```

Machine lines print with `[auto]`; `--machine exclude` searches the subtitles alone and
`--machine only` the machine lines alone (of a media file: not with `--subs`).

**Where transcripts live.** Not next to the media: in quipclipper's state (the server's
state dir, or `~/.cache/quipclipper/transcripts` when the CLI runs whisper itself), keyed
on the media file, so new subtitles don't throw a transcript away. `item.transcript-clear`
deletes one. A transcript belongs to the file it was made of: once the media file is
replaced (a new release under the same name), it is no longer read or marked **auto**.
Nothing is ever swept on its own: `index.transcript-orphans` lists the transcripts of media
files that are gone (and, with `--stale`, of files replaced since), and
`index.transcript-clean` deletes them. A media file on a disk that isn't mounted, or outside
every library root, never counts as gone.

**Copying to the library.** `item.transcript-copy` writes a file's transcript next to it
as a subtitle file named **`<name>.<lang>.whisper.srt`** (for example
`S01E06.en.whisper.srt`), in a writable folder only. The `whisper` token marks it as
machine-made: quipclipper reads it as machine lines (and from then on shows them in the
player's captions), never as the file's subtitles, and Jellyfin offers it as its own
subtitle track. Only lines the subtitles don't already have are copied. `item.transcript-uncopy` removes it
again (only this file's copy: `Show.Part2.en.whisper.srt` belongs to `Show.Part2.mkv`, not
to `Show.mkv`).

**Running it on your own machine.** With no server set, the CLI runs `whisper-cli` itself
once you set `QC_TRANSCRIBE=1`: install whisper.cpp (`nix shell nixpkgs#whisper-cpp`, `brew install whisper-cpp`), fetch a
model from [Hugging Face](https://huggingface.co/ggerganov/whisper.cpp/tree/main) and set
`QC_WHISPER_MODEL=/path/to/ggml-small.en.bin` (or several, as
`QC_WHISPER_MODELS=small.en=/…/ggml-small.en.bin,medium.en=/…/ggml-medium.en.bin`).
`QC_WHISPER_BIN` names another binary, `QC_WHISPER_THREADS` the threads (capped at 3).

## Turning it on

| Variable | Default | Purpose |
|---|---|---|
| `QC_TRANSCRIBE` | *(off)* | `1` (or `true`, `yes`, `on`) switches machine transcription on. Anything else, or unset, leaves it off. |
| `QC_WHISPER_MODEL` / `QC_WHISPER_MODELS` | *(none)* | Machine transcription with whisper.cpp: a ggml file (`/models/ggml-small.en.bin`), or `small.en=/models/ggml-small.en.bin,medium.en=/models/ggml-medium.en.bin` for a choice with `QC_WHISPER_MODEL` naming the default. A bare name needs `QC_WHISPER_MODELS` to say where its file is. No model file = off, even when switched on. It runs only when asked, at idle priority, one run at a time. |
| `QC_WHISPER_BIN` / `QC_WHISPER_THREADS` / `QC_WHISPER_VAD_MODEL` / `QC_WHISPER_LANGUAGE` | `whisper-cli` / `2` / *(none)* / `en` | The whisper.cpp binary, its threads per run (capped at 3, whatever you set), a Silero VAD model, used for whole-file runs only, and the language (English models only for now). The Docker image doesn't include whisper.cpp; mount a static build and point `QC_WHISPER_BIN` at it. |

On NixOS, `services.quipclipper-web.transcribe`:

| Option | Purpose |
|---|---|
| `transcribe.enable` | Machine transcription with whisper.cpp, on request only (no timer, no hook): `transcribe.models` (`[ "small.en" ]`; add `"medium.en"`, 1.5 GB) installs ggml models pinned by hash from Hugging Face (`transcribe.modelFiles` points a name at your own file; the others keep their pinned download, and a file outside the store is bound into the sandbox), `transcribe.defaultModel`, `transcribe.threads` (2; **1 to 3 only**: whisper shares the host with playback), `transcribe.vad` (Silero, for whole-file runs) and `transcribe.package` (`pkgs.whisper-cpp`; a CUDA override for a GPU). Every run is idle priority. Enabling it sets `QC_TRANSCRIBE=1`. |

With `writableMediaRoots`, **Copy to library** can write a transcript next to a file; on a
read-only root it says it can't.

## In the web app

- **The workspace** has a **Machine transcript** bar above the script.
  **Fill gaps** transcribes every stretch between subtitle lines (a song the subtitles
  skip); on a file with no subtitles it's **Transcribe file**. **Transcribe selection**
  on the waveform transcribes the stretch you selected, again if it was done before, for
  a second go with **Model** medium.en. Each runs in the background at low priority, so it
  can take a while: the bar shows its progress and **Cancel** (it keeps what's done). The
  lines whisper hears appear in the script and in search **in violet italics with an
  "auto" chip**, never mistaken for the subtitles (a screen reader hears "machine
  transcript"), and on the waveform's line lane in violet. They stay out of the video's
  captions until **Copy to library** writes them next to the file as
  `<name>.en.whisper.srt` (writable folders only); then the captions show them too, marked
  ≈, and Jellyfin offers that file as a subtitle track. **Export .srt** saves them,
  **Remove from library** and **Delete transcript** undo the two.
- **The library's Dialogue index card** also counts the files with a machine transcript
  and the audio files with no transcript yet, and offers **Transcribe files without
  subtitles** (whole files, audio included) and **Fill gaps in every file**: after you
  confirm, they run file after file in the background at low priority, which can take
  hours, with progress and **Cancel** (what's done is kept). Nothing transcribes a folder
  unless you press one of them. An audio file with a machine transcript is listed (🎵),
  and an **auto** chip marks any file that has one.
- **The library's home** says when stored transcripts belong to files that are gone from
  the library, and offers **Remove orphaned transcripts** (after a confirm).
- **Settings** gains **Transcription model** (when the server has both): small.en, quicker,
  or medium.en, which hears more and is about three times slower. The transcript bar's
  **Model** picks another for one run.

## CLI commands

The CLI's help and `quipclipper commands` list these only with `QC_TRANSCRIBE=1` set where
the CLI runs; `search`, `search.item`, `search.folder` and `item.script` take `--machine`.
Against a server, the server's switch decides (404 when it's off there).

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `item.transcribe` | Transcribe a file's audio with whisper.cpp, for lines the subtitles leave out: a range (--start/--end), every gap between subtitle lines, or the whole file. Runs as a background job at idle priority; lines it hears are searchable and marked as machine-made. | `PATH` `--mode` `--start` `--end` `--model` `--audio` `--redo` `--wait` `--out-dir` | `POST /api/items/transcribe` · local | Transcribe selection (waveform); Fill gaps / Transcribe file (Transcript card) |
| `item.transcript` | Show a file's machine transcript: the lines whisper heard, with what was transcribed. | `PATH` `--fmt` | `GET /api/items/transcript` · local | Transcript card (workspace) |
| `item.transcript-clear` ⚠ | Delete a file's machine transcript (not a copy in the library). | `PATH` | `DELETE /api/items/transcript` · local | Transcript card: Delete transcript |
| `item.transcript-copy` ⚠ | Copy a file's machine transcript into the library as a subtitle sidecar, <name>.<lang>.whisper.srt, next to the file (writable folders only). From then on it also shows in the player's captions and in Jellyfin. | `PATH` | `POST /api/items/transcript/library` · local | Transcript card: Copy to library |
| `item.transcript-uncopy` ⚠ | Remove the machine transcript sidecar (…whisper.srt) from the library; the transcript itself stays. | `PATH` | `DELETE /api/items/transcript/library` · local | Transcript card: Remove from library |
| `index.transcribe` | Transcribe a folder's files with whisper.cpp, one after another at idle priority: whole files that have no subtitles (audio without a transcript included), or the gaps between every file's subtitle lines. Only ever on request; cancel it like any job. | `PATH` `--mode` `--model` `--wait` `--out-dir` | `POST /api/search/folder/transcribe` | Transcribe files without subtitles; Fill gaps in every file (Dialogue index card); ✕ Cancel |
| `index.transcript-orphans` | List the stored machine transcripts no file needs any more: their media file was deleted (with --stale, also replaced since), or they can't be read. Deletes nothing. | `--stale` | `GET /api/transcripts/orphans` · local | Library home: the orphaned transcripts count |
| `index.transcript-clean` ⚠ | Delete the stored machine transcripts `index.transcript-orphans` lists. Never runs on its own; copies in the library stay. A disk that isn't mounted counts as nothing deleted. | `--stale` | `POST /api/transcripts/clean` · local | Library home: Remove orphaned transcripts |

## API

whisper.cpp hears what the subtitles leave out (M21). It only runs when asked: nothing
transcribes on its own. Each run is a job in its own one-at-a-time pool, at idle CPU and
I/O priority with at most 3 threads, and stores its lines in the state dir, never next to
the media. Needs `QC_TRANSCRIBE=1`, `whisper-cli` and a model (`QC_WHISPER_MODEL`, the
NixOS module's `transcribe.*`). Switched off, every endpoint here answers **404** with the
reason; on but without whisper or a model, the ones that run whisper answer 503 and
`/api/health`'s `transcribe.available` is false.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/items/transcribe` | Start transcribing (job): `{path, mode, start, end, model, audio, redo}`. `mode` is `range` (`start`..`end`, seconds), `gaps` (every stretch of 4 s or more between subtitle lines) or `file`; omitted, it is `range` when `start` is given, else `gaps`. `model` names one the server has (`/api/health`'s `transcribe.models`: `small.en`, the default, and `medium.en` when installed); never a path. Parts done before are skipped unless `redo`. Returns `{job_id}` (`existing: true` when the same request is already queued or running). The job's `progress` reads `"1:10 of 4:00 transcribed"`; `DELETE /api/jobs/{id}` stops it, keeping finished chunks. Its `result`: `{path, mode, model, transcribed: [[start, end]…], new_lines, transcript, note?}`. 422: a bad range (end before start, or past a week), a model the server lacks, or a YouTube/EPUB ref; 404: not a file; 503: transcription isn't set up. |
| GET | `/api/items/transcript?path=&fmt=` | A file's machine transcript (json): `{path, stored, library, writable, sidecar_name, lines: [{start, end, text, source}]}`. `stored` is `{ranges, covered, lines, models, language, updated, engine}` or null; `library` names the copies next to the file. `fmt=srt` or `vtt` downloads the lines. |
| DELETE | `/api/items/transcript?path=` | Delete the stored transcript → `{deleted}` (a library copy stays). |
| POST | `/api/items/transcript/library?path=` | Copy the machine lines (those no subtitle line has) next to the file as `<name>.<lang>.whisper.srt` → `{sidecar, lines}`. quipclipper reads that file as machine lines, never as the file's subtitles; the captions then show them, and Jellyfin offers it as a subtitle track. 403 = a read-only folder (needs `writableMediaRoots`), 404 = no transcript, 409 = no line left that the subtitles don't already have. |
| DELETE | `/api/items/transcript/library?path=` | Remove this file's copies next to it (`<name>.<lang>.whisper.srt`/`.vtt`; never another file's, such as `<name>.Part2.en.whisper.srt`) → `{removed}`. 403 = a read-only folder. |
| POST | `/api/search/folder/transcribe` | Transcribe a folder's files one after another (job): `{path, mode, model}`. `mode=nosubs` (default) does whole files that have no subtitles, audio files with no transcript included; `gaps` does the gaps in every subtitled file. Files already done are skipped, and so is a file whose subtitles can't be checked (an `error` in its `files` entry: a whole-file run of a subtitled film would cost hours). In `gaps` mode, a file not indexed yet has its subtitles extracted first, at idle priority as the folder index does (Cancel stops that too). Returns `{job_id, candidates}`, or `{job_id: null, candidates: 0}` when there is nothing to do. Progress reads `"3/41 files, <name>: 1:10 of 4:00 transcribed"`; the `result` is `{path, mode, model, files: [{name, path, new_lines, error?}], new_lines, note}`. 400 = over 500 files (audio counted too) or not a folder; 422 = a model the server lacks. A server shutdown stops a running transcription (its finished chunks are kept) and drops queued ones. |
| GET | `/api/transcripts/orphans?stale=` | The stored transcripts a clean would delete, deleting nothing → `{count, orphans: [{file, path, reason}]}` (at most 200 listed). `reason` is `gone` (the media file was deleted), `stale` (only with `stale=true`: the media file was replaced since), `unreadable` (an older store format or a broken file) or `partial` (an interrupted save's temp file, over an hour old). A media file counts as gone only under a library root that is there and not empty, so a disk that isn't mounted loses nothing; a transcript of a file outside every root is left alone. |
| POST | `/api/transcripts/clean?stale=` | Delete what the GET lists → `{count, removed}`. Only ever on request: nothing sweeps the store on a timer or a scan. Copies in the library are never touched. |

`/api/items` adds `transcript: {stored, library, writable, sidecar_name}` for a library
file. `/api/library/browse` lists an audio file with a machine transcript (stored or in the
library) like one with a transcript beside it, and marks entries `has_transcript` (not for a
transcript of the file a replaced one used to be).
`/api/search/folder/index-status` adds `transcribed` (files with a machine transcript) and
`audio_untranscribed` (audio files with no transcript of any kind, outside `total`).

Switched on, the rest of the API changes too:

- `/api/health` gains `tools["whisper-cli"]` and `transcribe`: `{enabled, available, engine,
  binary, models: [{name, size, present}], default_model, threads, max_threads, vad,
  language}`; `/api/config` gains `transcribe` (`{available, models, default_model, threads,
  language}`). Off, neither appears.
- `/api/items/subtitles` (`fmt=json`) cues carry `source`: `"machine"` for a machine line,
  else null. `machine=include` (the default) mixes them in, `exclude` leaves them out, `only`
  gives them alone. The captions carry only machine lines copied into the library, as
  `<c.machine>≈ …</c>`.
- `/api/search` and `/api/search/folder` take the same `machine=` and mark a hit on a machine
  line `machine: true`; at an equal score a subtitle line ranks first. A folder search also
  searches a file that isn't indexed on its stored machine transcript.
- Off, machine lines are left out everywhere and `machine=only` answers 404.
