# Machine transcription

> **On where whisper.cpp is installed.** Machine transcription turns itself on where
> `whisper-cli` and a whisper model file are installed, and stays out of sight everywhere
> else (see [Turning it on](#turning-it-on)). The NixOS module's `transcribe.enable` installs
> both; the Docker images carry neither, so it is off there for now. Even when it's on,
> nothing transcribes until you ask.

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

**Running it on your own machine.** With no server set, the CLI runs `whisper-cli` itself:
install whisper.cpp (`nix shell nixpkgs#whisper-cpp`, `brew install whisper-cpp`), fetch a
model from [Hugging Face](https://huggingface.co/ggerganov/whisper.cpp/tree/main) and set
`QC_WHISPER_MODEL=/path/to/ggml-small.en.bin` (or several, as
`QC_WHISPER_MODELS=small.en=/…/ggml-small.en.bin,medium.en=/…/ggml-medium.en.bin`).
That's all it takes: the transcription commands appear in the CLI's help from then on.
`QC_WHISPER_BIN` names another binary, `QC_WHISPER_THREADS` the threads (capped at 3).

## Turning it on

Transcription is **on exactly where it can run**: `whisper-cli` is found (on `PATH`, or at
`QC_WHISPER_BIN`) and at least one model file named by `QC_WHISPER_MODEL` /
`QC_WHISPER_MODELS` exists. Anywhere else it is off, and off means out of sight: nothing
transcribes, the web app shows none of it, the API's transcription endpoints answer 404 with
the reason, the CLI keeps its commands out of its help and refuses them locally, and
transcripts stored earlier stay out of search and the script. `QC_TRANSCRIBE` overrides the
default either way.

| Variable | Default | Purpose |
|---|---|---|
| `QC_TRANSCRIBE` | *(automatic)* | `0` (or `false`, `no`, `off`) turns machine transcription off even where whisper.cpp is installed; `1` (or `true`, `yes`, `on`) forces it on, so a server without whisper shows it as not set up. Unset, or any other value: on where whisper-cli and a model are installed. |
| `QC_WHISPER_MODEL` / `QC_WHISPER_MODELS` | *(none)* | A ggml model file (`/models/ggml-small.en.bin`), or `small.en=/models/ggml-small.en.bin,medium.en=/models/ggml-medium.en.bin` for a choice with `QC_WHISPER_MODEL` naming the default. A bare name needs `QC_WHISPER_MODELS` to say where its file is. No model file, no transcription. It runs only when asked, at idle priority, one run at a time. |
| `QC_WHISPER_BIN` / `QC_WHISPER_THREADS` / `QC_WHISPER_VAD_MODEL` / `QC_WHISPER_LANGUAGE` | `whisper-cli` / `2` / *(none)* / `en` | The whisper.cpp binary, its threads per run (capped at 3, whatever you set), a Silero VAD model, used for whole-file runs only, and the language (English models only for now). |

**Docker:** not supported in the images yet. They carry no whisper.cpp, so transcription
stays off there. If you want to try it anyway (untested): build a self-contained
`whisper-cli` for the image's Debian (x86-64 or arm64, `cmake -DBUILD_SHARED_LIBS=OFF`),
mount it and a ggml model into the `app` container, and set `QC_WHISPER_BIN` and
`QC_WHISPER_MODEL` to their paths; it then turns on by itself. The idle-priority rule still
holds there: quipclipper refuses to start whisper if it can't lower its priority.

On NixOS, `services.quipclipper-web.transcribe`:

| Option | Purpose |
|---|---|
| `transcribe.enable` | Machine transcription with whisper.cpp, on request only (no timer, no hook): `transcribe.models` (`[ "small.en" ]`; add `"medium.en"`, 1.5 GB) installs ggml models pinned by hash from Hugging Face (`transcribe.modelFiles` points a name at your own file; the others keep their pinned download, and a file outside the store is bound into the sandbox), `transcribe.defaultModel`, `transcribe.threads` (2; **1 to 3 only**: whisper shares the host with playback), `transcribe.vad` (Silero, for whole-file runs) and `transcribe.package` (`pkgs.whisper-cpp`; a CUDA override for a GPU). Every run is idle priority. Enabling it sets `QC_TRANSCRIBE=1`; left off, the service has no whisper, so transcription stays off. |

With `writableMediaRoots`, **Copy to library** can write a transcript next to a file; on a
read-only root it says it can't.

## In the web app

- **The workspace** has a slim **Machine transcript** row above the script, about a line
  tall: the state in a few words ("14 lines", "none yet") and a running job's progress with
  **Cancel**. A click or Enter opens the rest below it, Esc closes it, and each browser
  remembers which. **Fill gaps** transcribes every stretch between subtitle lines (a song the subtitles
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

The CLI's help and `quipclipper commands` list these only where transcription is on for
the CLI (whisper-cli and a model there, or `QC_TRANSCRIBE=1`); `search`, `search.item`,
`search.folder` and `item.script` take `--machine`. Against a server, the server decides
(404 when it's off there). [CLI.md](CLI.md) lists them too, marked ².

| Command | What it does | Arguments | API | Web UI |
|---|---|---|---|---|
| `item.transcribe` | Transcribe a file's audio with whisper.cpp, for lines the subtitles leave out: a range (--start/--end), every gap between subtitle lines, or the whole file. Runs as a background job at idle priority; lines it hears are searchable and marked as machine-made. | `PATH` `--mode` `--start` `--end` `--model` `--audio` `--redo` `--wait` `--out-dir` | `POST /api/items/transcribe` · local | Transcribe selection (waveform); Fill gaps / Transcribe file (Machine transcript bar) |
| `item.transcript` | Show a file's machine transcript: the lines whisper heard, with what was transcribed. | `PATH` `--fmt` | `GET /api/items/transcript` · local | Machine transcript bar (workspace) |
| `item.transcript-clear` ⚠ | Delete a file's machine transcript (not a copy in the library). | `PATH` | `DELETE /api/items/transcript` · local | Machine transcript bar: Delete transcript |
| `item.transcript-copy` ⚠ | Copy a file's machine transcript into the library as a subtitle sidecar, <name>.<lang>.whisper.srt, next to the file (writable folders only). From then on it also shows in the player's captions and in Jellyfin. | `PATH` | `POST /api/items/transcript/library` · local | Machine transcript bar: Copy to library |
| `item.transcript-uncopy` ⚠ | Remove the machine transcript sidecar (…whisper.srt) from the library; the transcript itself stays. | `PATH` | `DELETE /api/items/transcript/library` · local | Machine transcript bar: Remove from library |
| `index.transcribe` | Transcribe a folder's files with whisper.cpp, one after another at idle priority: whole files that have no subtitles (audio without a transcript included), or the gaps between every file's subtitle lines. Only ever on request; cancel it like any job. | `PATH` `--mode` `--model` `--wait` `--out-dir` | `POST /api/search/folder/transcribe` | Transcribe files without subtitles; Fill gaps in every file (Dialogue index card); ✕ Cancel |
| `index.transcript-orphans` | List the stored machine transcripts no file needs any more: their media file was deleted (with --stale, also replaced since), or they can't be read. Deletes nothing. | `--stale` | `GET /api/transcripts/orphans` · local | Library home: the orphaned transcripts count |
| `index.transcript-clean` ⚠ | Delete the stored machine transcripts `index.transcript-orphans` lists. Never runs on its own; copies in the library stay. A disk that isn't mounted counts as nothing deleted. | `--stale` | `POST /api/transcripts/clean` · local | Library home: Remove orphaned transcripts |

## API

The endpoints (transcribe, show, export, delete, copy to the library, folder runs, orphans)
and what transcription adds to search, the script, `/api/health` and `/api/config` are in
[API.md](API.md#machine-transcription). Off, every transcription endpoint answers 404 with
the reason, and `machine=only` too.
