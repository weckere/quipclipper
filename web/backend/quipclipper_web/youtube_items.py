"""Adapt YouTube videos to quipclipper's per-file item model, via yt-dlp.

Adding a URL fetches **subtitles + metadata only** (no video download); the
video becomes a persistent item in a "▶ YouTube" pseudo-folder, addressed by the
virtual ref ``yt:<video-id>`` (the pseudo-folder itself is ``yt:``). Transcript
browsing, dialogue search, and bookmarks then work exactly like library files.

Playback and clipping never download the file either: googlevideo stream URLs
are resolved on demand (yt-dlp -g, cached briefly — they expire after ~6h) and
fed straight to ffmpeg, both for the preview transcode and for the actual cut.

Everything that talks to the network goes through :func:`_run_ytdlp` — the
single seam tests stub out.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from quipclipper.models import Cue
from quipclipper.subtitles import find_sidecar, load_subtitles

# The pseudo-folder ref, and the prefix of every item ref ("yt:<video-id>").
# A colon never appears in an absolute POSIX path under a media root, so these
# can't collide with real paths, and they're query-string safe.
FOLDER_REF = "yt:"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Accepted watch-page shapes, https + YouTube hosts only. The video id is
# validated separately against _ID_RE, so nothing URL-shaped ever reaches an
# argv unchecked (yt-dlp additionally gets `--` before the URL).
_URL_RES = [
    re.compile(r"^https://(?:www\.|m\.|music\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})(?:[&#]|$)"),
    re.compile(r"^https://(?:www\.|m\.)?youtube\.com/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})(?:[?#]|$)"),
    re.compile(r"^https://youtu\.be/([A-Za-z0-9_-]{11})(?:[?#]|$)"),
]

# Prefer an h264/mp4 video + m4a audio pair over plain-HTTP(S) (ffmpeg can read
# those directly; DASH/SABR-only formats can't be fetched by ffmpeg), then a
# progressive mp4, then any http format as a last resort.
FORMAT_EXPR = (
    "bv*[vcodec^=avc1][ext=mp4][protocol^=http]+ba[acodec^=mp4a][protocol^=http]"
    "/b[ext=mp4][vcodec^=avc1][protocol^=http]"
    "/b[protocol^=http]"
)

_YTDLP_TIMEOUT = 120       # seconds; metadata/sub fetches finish in a few
_DOWNLOAD_TIMEOUT = 3600   # a full video download can legitimately take a while


def parse_ref(path: str) -> str | None:
    """``yt:<video-id>`` → the video id; None for anything else (incl. ``yt:``)."""
    if not path.startswith(FOLDER_REF):
        return None
    vid = path[len(FOLDER_REF):]
    return vid if _ID_RE.match(vid) else None


def make_ref(video_id: str) -> str:
    return f"{FOLDER_REF}{video_id}"


def is_folder_ref(path: str | None) -> bool:
    return path == FOLDER_REF


def extract_video_id(url: str) -> str | None:
    """The video id from a YouTube watch/shorts/embed/youtu.be URL; None if the
    string isn't a well-formed https YouTube URL (the caller rejects it)."""
    url = url.strip()
    for rx in _URL_RES:
        m = rx.match(url)
        if m and _ID_RE.match(m.group(1)):
            return m.group(1)
    return None


# Fail-fast args on every yt-dlp call. yt-dlp cycles through several YouTube
# "player clients"; when some hang (throttling / unresponsive endpoints) the
# default long socket timeout + retries can turn a trivial fetch into 80s+.
# Capping the socket timeout and extractor retries roughly halved that on a real
# server (82s → 34s) with no loss of success. Overridable via QC_YTDLP_ARGS
# (space-separated) for a host that needs different tuning; set it empty to
# restore yt-dlp's defaults. (Fragment --retries for the actual video download
# are left at the default, so a downloaded byte-stream stays robust.)
_DEFAULT_YTDLP_ARGS = ["--socket-timeout", "10", "--extractor-retries", "1"]
_EXTRA_ARGS = shlex.split(os.environ.get("QC_YTDLP_ARGS", " ".join(_DEFAULT_YTDLP_ARGS)))


def _run_ytdlp(args: list[str], timeout: int = _YTDLP_TIMEOUT) -> str:
    """Run yt-dlp and return stdout. THE test seam — every network call routes
    through here. Raises RuntimeError with a stderr excerpt on failure."""
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not found on PATH. Install yt-dlp for YouTube support.")
    try:
        proc = subprocess.run(
            ["yt-dlp", *_EXTRA_ARGS, *args], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out after {timeout}s.") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("yt-dlp failed: " + (" / ".join(tail) or f"exit {proc.returncode}"))
    return proc.stdout


# QC_SUBTITLE_LANGS is documented with ISO-639-2 codes ("eng,spa") because the
# embedded-track selector matches those; YouTube caption tags are ISO-639-1
# ("en", "es"). Map the common 3-letter codes so a docs-per-the-README config
# doesn't silently fetch no captions; unknown values pass through unchanged.
_ISO639_2_TO_1 = {
    "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh",
    "rus": "ru", "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no", "dan": "da",
    "fin": "fi", "pol": "pl", "ara": "ar", "hin": "hi", "tur": "tr", "ces": "cs",
    "cze": "cs", "ell": "el", "gre": "el", "heb": "he", "hun": "hu", "ind": "id",
    "tha": "th", "ukr": "uk", "vie": "vi",
}


def _yt_lang_tags(langs: list[str]) -> list[str]:
    """Language tags for yt-dlp --sub-langs: normalize 3-letter codes to
    YouTube's 2-letter tags, keeping the originals too (harmless, and covers
    the odd channel that labels captions with a 3-letter tag)."""
    out: list[str] = []
    for lang in langs:
        norm = _ISO639_2_TO_1.get(lang.lower())
        if norm and norm not in out:
            out.append(norm)
        if lang not in out:
            out.append(lang)
    return out


def dedupe_auto_cues(cues: list[Cue]) -> list[Cue]:
    """Collapse YouTube auto-caption "rolling window" duplicates.

    Auto-caption VTTs repeat the previous line at the top of each cue as new
    words roll in, so after the engine joins a cue's lines the text reads
    "<previous text> <new words>". Strip the repeated prefix, merge cues whose
    text is an exact repeat (extending the previous cue's end), and drop
    empties. Manual subtitles are never passed through here — intentional
    repeats (song lyrics) must survive."""
    out: list[Cue] = []
    for c in cues:
        text = c.text.strip()
        if not text:
            continue
        if out:
            prev = out[-1]
            if text == prev.text:
                out[-1] = Cue(index=prev.index, start=prev.start,
                              end=max(prev.end, c.end), text=prev.text,
                              speaker=prev.speaker)
                continue
            if text.startswith(prev.text) and len(text) > len(prev.text):
                text = text[len(prev.text):].strip()
                if not text:
                    continue
        out.append(Cue(index=len(out), start=c.start, end=c.end,
                       text=text, speaker=c.speaker))
    return out


def entry_dict(meta: dict) -> dict:
    """A browse-listing entry for one stored video (shape of library.Entry)."""
    return {
        "name": meta.get("title") or meta["id"],
        "path": make_ref(meta["id"]),
        "is_dir": False,
        "is_video": True,
        "has_sidecar": True,  # the stored transcript plays the sidecar's role
        "is_audio": False,
        "is_book": False,
        "is_youtube": True,
        "downloaded": bool(meta.get("downloaded")),
    }


def folder_entry() -> dict:
    """The "▶ YouTube" pseudo-folder shown at the library root."""
    return {
        "name": "▶ YouTube",
        "path": FOLDER_REF,
        "is_dir": True,
        "is_video": False,
        "has_sidecar": False,
        "is_audio": False,
        "is_book": False,
        "is_youtube": True,
    }


def item_info(meta: dict) -> dict:
    """`item_info`-shaped dict, *synthesized* — no ffprobe, nothing downloaded.

    The preview always goes through the transcode endpoint, which delivers
    h264 + opus regardless of the source formats, so fabricated h264/aac stereo
    descriptors are an honest description of what the player will receive."""
    video_stream = {
        "kind": "video", "index": 0, "selector": "v:0", "codec": "h264",
        "language": None, "title": None, "channels": None, "channel_layout": None,
        "forced": False, "hearing_impaired": False, "attached_pic": False,
        "groups": [], "label": "v:0  h264",
    }
    audio_stream = {
        "kind": "audio", "index": 0, "selector": "a:0", "codec": "aac",
        "language": None, "title": None, "channels": 2, "channel_layout": "stereo",
        "forced": False, "hearing_impaired": False, "attached_pic": False,
        "groups": [], "label": "a:0  aac stereo",
    }
    return {
        "name": meta.get("title") or meta["id"],
        "path": make_ref(meta["id"]),
        "size": meta.get("download_size"),
        "duration": float(meta.get("duration") or 0.0),
        "streams": [video_stream, audio_stream],
        "subtitle_tracks": [],
        "best_track": None,
        "has_sidecar": True,
        "is_youtube": True,
        "downloaded": bool(meta.get("downloaded")),
        "download_size": meta.get("download_size"),
        "channel": meta.get("channel"),
        "webpage_url": meta.get("webpage_url"),
    }


def _fetch_vtt(work_dir: Path, video_id: str, url: str,
               langs: list[str]) -> tuple[Path | None, str | None, str | None]:
    """yt-dlp-fetch a subtitle .vtt into *work_dir* — manual subs first, auto
    captions as fallback. Returns ``(chosen_vtt_path, lang, source)`` (the file
    is still named ``<id>.<lang>.vtt`` in work_dir — the caller moves it to its
    final location), or ``(None, None, None)`` if the video has no captions."""
    lang_spec = ",".join(_yt_lang_tags(langs)) if langs else "en"
    prefs = [t.lower() for t in _yt_lang_tags(langs)]
    for flag, source in (("--write-subs", "manual"), ("--write-auto-subs", "auto")):
        _run_ytdlp([
            "--no-playlist", "--skip-download", flag,
            "--sub-langs", lang_spec, "--sub-format", "vtt/best",
            "-o", str(work_dir / "%(id)s"), "--", url,
        ])
        vtts = sorted(work_dir.glob(f"{video_id}.*.vtt"))
        if vtts:
            def rank(p: Path) -> int:
                tag = p.name[len(video_id) + 1:-len(".vtt")].lower()
                for i, lang in enumerate(prefs):
                    if tag == lang or tag.startswith(lang + "-"):
                        return i
                return len(prefs)
            vtts.sort(key=rank)
            chosen = vtts[0]
            lang_tag = chosen.name[len(video_id) + 1:-len(".vtt")]
            for extra in vtts[1:]:
                extra.unlink(missing_ok=True)
            return chosen, lang_tag, source
    return None, None, None


def youtube_id_for_file(video_path: Path) -> str | None:
    """The YouTube video id for a *library* file, if detectable — from a
    ``[<id>]`` in the name (the Pinchflat/yt-dlp convention) or a sibling
    ``<stem>.info.json``. None when it isn't a recognisable YouTube download."""
    brackets = re.findall(r"\[([A-Za-z0-9_-]{11})\]", video_path.stem)
    if brackets:
        return brackets[-1]  # the id is the last bracketed token before the ext
    info = video_path.with_name(video_path.stem + ".info.json")
    with contextlib.suppress(OSError, json.JSONDecodeError):
        vid = json.loads(info.read_text(encoding="utf-8")).get("id")
        if isinstance(vid, str) and _ID_RE.match(vid):
            return vid
    return None


def fetch_subs_for_file(video_path: Path, langs: list[str], force: bool = False) -> dict:
    """Backfill subtitles for a *library* video from YouTube: detect its id,
    fetch a transcript, and write a ``.vtt`` sidecar next to it so it becomes
    dialogue-searchable. Returns ``{status: skipped|none|ok, ...}``.

    - ``skipped`` — it already has a sidecar (pass ``force`` to refetch).
    - ``none`` — the video has no captions on YouTube.
    Raises ValueError (no id) / PermissionError (read-only folder) / RuntimeError
    (yt-dlp failure)."""
    vid = youtube_id_for_file(video_path)
    if vid is None:
        raise ValueError("No YouTube id in the file name or a sibling .info.json.")
    if not force and find_sidecar(video_path) is not None:
        return {"status": "skipped", "id": vid}
    if not os.access(video_path.parent, os.W_OK):
        raise PermissionError(
            "That folder is read-only — add it to writableMediaRoots to backfill subtitles.")
    url = f"https://www.youtube.com/watch?v={vid}"
    with tempfile.TemporaryDirectory() as td:
        chosen, lang, source = _fetch_vtt(Path(td), vid, url, langs)
        if chosen is None:
            return {"status": "none", "id": vid}
        sidecar = video_path.with_suffix(".vtt")  # <stem>.vtt — find_sidecar matches
        shutil.move(str(chosen), str(sidecar))
    return {"status": "ok", "id": vid, "lang": lang, "source": source, "sidecar": sidecar.name}


def _fs_safe(name: str) -> str:
    """A path-component-safe rendition of a title/channel for the naming template."""
    return re.sub(r"[/\\\0]", "_", name).strip() or "YouTube"


def _fs_safe_filename(name: str) -> str:
    """A portable file NAME from a video title: drop characters illegal on
    Linux/Windows/SMB (``/ \\ < > : " | ? *`` + control chars) and collapse
    whitespace, so a downloaded ``<Title> [<id>].mp4`` is safe on a ZFS/SMB share."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name or "video"


def clip_name_source(meta: dict) -> Path:
    """A synthetic path whose stem is the video title and whose parent is the
    channel, so the default clip template yields
    ``<Title>/<timestamp>_<cue>_<Channel>.<ext>`` (mirrors the EPUB scheme)."""
    channel = _fs_safe(meta.get("channel") or "YouTube")
    title = _fs_safe(meta.get("title") or meta["id"])
    return Path("/yt") / channel / f"{title}.mp4"


# --- stream-URL resolution (preview + cutting) --------------------------------

# googlevideo URLs expire (~6h) and resolving costs a yt-dlp run (~1-3s), so
# cache briefly. Keyed by webpage URL; guarded for concurrent requests.
_URL_TTL = 15 * 60
_url_cache: dict[str, tuple[float, tuple[str, str | None]]] = {}
_url_lock = threading.Lock()


def resolve_stream_urls(webpage_url: str) -> tuple[str, str | None]:
    """Resolve direct stream URL(s) for a watch page: ``(video, audio|None)``.

    Two lines from ``yt-dlp -g`` mean a DASH pair (separate video + audio);
    one line is a progressive (muxed) format → audio is None."""
    now = time.monotonic()
    with _url_lock:
        hit = _url_cache.get(webpage_url)
        if hit and now - hit[0] < _URL_TTL:
            return hit[1]
    out = _run_ytdlp(["--no-playlist", "-f", FORMAT_EXPR, "-g", "--", webpage_url])
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("yt-dlp returned no stream URLs.")
    urls = (lines[0], lines[1] if len(lines) > 1 else None)
    with _url_lock:
        # Keep the cache bounded: drop expired entries, then the oldest if a
        # (pathological) number of distinct videos is still live.
        for k in [k for k, (ts, _) in _url_cache.items() if now - ts >= _URL_TTL]:
            del _url_cache[k]
        while len(_url_cache) >= 256:
            del _url_cache[min(_url_cache, key=lambda k: _url_cache[k][0])]
        _url_cache[webpage_url] = (now, urls)
    return urls


def clear_url_cache() -> None:  # test helper
    with _url_lock:
        _url_cache.clear()


# --- persistent store ----------------------------------------------------------

class YouTubeStore:
    """File-backed store of added videos: ``state_dir/youtube/<id>/``, holding
    ``info.json`` (trimmed metadata) + ``subs.vtt`` (the fetched transcript).
    No index file — the listing is a directory scan, so adds/removes are atomic
    per-video and can't race each other."""

    def __init__(self, state_dir: Path, download_dir: Path | None = None) -> None:
        self._dir = state_dir / "youtube"
        self._dir.mkdir(parents=True, exist_ok=True)
        # Where a full-video download is written. None → keep it private in the
        # item dir (``<item>/video.mp4``). A real folder (ideally under a media
        # root) → a browsable ``<Title> [<id>].mp4`` + ``.vtt`` sidecar.
        self._download_dir = download_dir
        # Serialize downloads per video: two concurrent download() calls would
        # otherwise run two yt-dlp processes into the same output template.
        self._dl_locks: dict[str, threading.Lock] = {}
        self._dl_guard = threading.Lock()

    def _dl_lock(self, video_id: str) -> threading.Lock:
        with self._dl_guard:
            return self._dl_locks.setdefault(video_id, threading.Lock())

    def _item_dir(self, video_id: str) -> Path:
        if not _ID_RE.match(video_id):
            raise KeyError(f"Invalid YouTube video id: {video_id!r}")
        return self._dir / video_id

    # -- fetch helpers ----------------------------------------------------------

    def _fetch_subs(self, item_dir: Path, video_id: str, url: str,
                    langs: list[str]) -> tuple[str | None, str | None]:
        """Fetch a .vtt into *item_dir* as ``subs.vtt``. Manual subs first, auto
        captions as fallback. Returns (lang, source) — (None, None) if the video
        has no captions at all. An existing ``subs.vtt`` is only replaced when a
        fresh one actually lands, so a failed or caption-less refetch can't
        destroy a stored transcript."""
        chosen, lang, source = _fetch_vtt(item_dir, video_id, url, langs)
        if chosen is not None:
            chosen.replace(item_dir / "subs.vtt")  # only replace on a fresh landing
        return lang, source

    # -- public API -------------------------------------------------------------

    def add(self, url: str, langs: list[str]) -> dict:
        """Fetch metadata + subtitles for *url* and persist them. Idempotent:
        re-adding an existing video returns its stored metadata unchanged."""
        video_id = extract_video_id(url)
        if video_id is None:
            raise ValueError("Not a recognised YouTube video URL.")
        item_dir = self._item_dir(video_id)
        if (item_dir / "info.json").is_file():
            return self.meta(video_id)

        item_dir.mkdir(parents=True, exist_ok=True)
        try:
            _run_ytdlp([
                "--no-playlist", "--skip-download", "--write-info-json",
                "-o", str(item_dir / "%(id)s"), "--", url,
            ])
            raw_path = item_dir / f"{video_id}.info.json"
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"yt-dlp produced no readable metadata: {exc}") from exc
            raw_path.unlink(missing_ok=True)

            webpage_url = raw.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
            sub_lang, sub_source = self._fetch_subs(item_dir, video_id, webpage_url, langs)

            meta = {
                "id": video_id,
                "title": raw.get("title") or video_id,
                "channel": raw.get("channel") or raw.get("uploader"),
                "duration": raw.get("duration") or 0,
                "upload_date": raw.get("upload_date"),
                "webpage_url": webpage_url,
                "added_at": time.time(),
                "sub_lang": sub_lang,
                "sub_source": sub_source,
            }
            (item_dir / "info.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
            return meta
        except Exception:
            # Leave nothing half-added — a retry starts clean.
            shutil.rmtree(item_dir, ignore_errors=True)
            raise

    def remove(self, video_id: str) -> bool:
        try:
            item_dir = self._item_dir(video_id)
        except KeyError:
            return False
        if not item_dir.is_dir():
            return False
        # Drop the video's stream-URL cache entry along with its state.
        with contextlib.suppress(KeyError, RuntimeError):
            url = self.meta(video_id).get("webpage_url")
            with _url_lock:
                _url_cache.pop(url, None)
        # Also delete an external download (video + sidecar) so removing a video
        # doesn't orphan it in the media folder — must run before the state dir
        # (which holds the recorded download_path) is gone.
        self.remove_download(video_id)
        shutil.rmtree(item_dir, ignore_errors=True)
        return True

    def list_all(self) -> list[dict]:
        """Stored videos, newest-added first."""
        out: list[dict] = []
        for d in self._dir.iterdir() if self._dir.is_dir() else []:
            info = d / "info.json"
            if not (d.is_dir() and info.is_file()):
                continue
            with contextlib.suppress(OSError, json.JSONDecodeError):
                out.append(self._with_download(json.loads(info.read_text(encoding="utf-8"))))
        out.sort(key=lambda m: m.get("added_at") or 0, reverse=True)
        return out

    def meta(self, video_id: str) -> dict:
        info = self._item_dir(video_id) / "info.json"
        try:
            meta = json.loads(info.read_text(encoding="utf-8"))
        except OSError as exc:
            raise KeyError(f"Unknown YouTube video: {video_id}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Corrupt metadata for {video_id}: {exc}") from exc
        return self._with_download(meta)

    def _raw(self, video_id: str) -> dict:
        """The persisted info.json as a plain dict ({} on any error)."""
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
            return json.loads((self._item_dir(video_id) / "info.json").read_text(encoding="utf-8"))
        return {}

    def _download_loc(self, video_id: str, recorded: str | None) -> Path | None:
        """The existing download file for a video: the externally-recorded path
        (info.json ``download_path``) if it still exists, else the private
        state-dir ``video.mp4``; None if neither is present."""
        if recorded:
            p = Path(recorded)
            if p.is_file():
                return p
        vp = self._dir / video_id / "video.mp4"
        return vp if vp.is_file() else None

    def _with_download(self, meta: dict) -> dict:
        """Annotate a meta dict with the download's presence/size (from disk —
        the file is the source of truth, so a hand-deleted file reads as gone)."""
        vp = self._download_loc(meta["id"], meta.get("download_path"))
        if vp is not None:
            with contextlib.suppress(OSError):
                meta["download_size"] = vp.stat().st_size
                meta["downloaded"] = True
                return meta
        meta["downloaded"] = False
        meta["download_size"] = None
        return meta

    def is_added(self, video_id: str) -> bool:
        """Whether this video has been added (its metadata is stored)."""
        try:
            return (self._item_dir(video_id) / "info.json").is_file()
        except KeyError:
            return False

    def video_path(self, video_id: str) -> Path | None:
        """The locally downloaded video file, if present (external or state-dir)."""
        return self._download_loc(video_id, self._raw(video_id).get("download_path"))

    def _clean_dl_temps(self, item_dir: Path, video_id: str) -> None:
        """Drop yt-dlp working files for this video (``video-dl.*`` in the state
        dir, and the hidden ``.qc-dl-<id>.*`` temps in the download dir) so a
        retry can never pick up a stale partial."""
        for leftover in item_dir.glob("video-dl.*"):
            leftover.unlink(missing_ok=True)
        if self._download_dir is not None and self._download_dir.is_dir():
            for leftover in self._download_dir.glob(f".qc-dl-{video_id}.*"):
                leftover.unlink(missing_ok=True)

    def download(self, video_id: str) -> Path:
        """Download the full video (browser-native h264/aac mp4). Idempotent: an
        existing download is returned as-is, and concurrent calls for the same
        video serialize on a per-video lock.

        With no configured download dir the file stays private in the item dir
        (``video.mp4``). With one, it lands as ``<Title> [<id>].mp4`` there,
        with a ``.vtt`` transcript sidecar, and its path is recorded in
        info.json — so it's a browsable, dialogue-searchable library item."""
        meta = self.meta(video_id)  # KeyError for unknown ids
        with self._dl_lock(video_id):
            existing = self.video_path(video_id)
            if existing is not None:
                return existing
            item_dir = self._item_dir(video_id)
            external = self._download_dir is not None
            if external:
                self._download_dir.mkdir(parents=True, exist_ok=True)
                tmp_tpl = self._download_dir / f".qc-dl-{video_id}.%(ext)s"
                merged = self._download_dir / f".qc-dl-{video_id}.mp4"
                final = (self._download_dir /
                         f"{_fs_safe_filename(meta.get('title') or video_id)} [{video_id}].mp4")
            else:
                tmp_tpl = item_dir / "video-dl.%(ext)s"
                merged = item_dir / "video-dl.mp4"
                final = item_dir / "video.mp4"
            # A previous failed/timed-out run may have left fragments behind;
            # start clean so nothing stale can be mistaken for the result.
            self._clean_dl_temps(item_dir, video_id)
            try:
                _run_ytdlp([
                    "--no-playlist", "-f", FORMAT_EXPR, "--merge-output-format", "mp4",
                    "-o", str(tmp_tpl), "--", meta["webpage_url"],
                ], timeout=_DOWNLOAD_TIMEOUT)
                # --merge-output-format mp4 pins the merged output's name; the
                # f*-fragment intermediates and .part files never match it.
                if not merged.is_file():
                    raise RuntimeError("yt-dlp reported success but produced no merged video file.")
                # Atomic move to the final name; readers only ever see it complete.
                merged.replace(final)
                if external:
                    # A transcript sidecar next to the video makes it a
                    # dialogue-searchable library item (and Jellyfin-visible).
                    subs = item_dir / "subs.vtt"
                    if subs.is_file():
                        with contextlib.suppress(OSError):
                            shutil.copyfile(subs, final.with_suffix(".vtt"))
                    raw = self._raw(video_id)
                    raw["download_path"] = str(final)
                    (item_dir / "info.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
            finally:
                self._clean_dl_temps(item_dir, video_id)
            return final

    def remove_download(self, video_id: str) -> bool:
        """Delete the local video file + its sidecar (transcript/metadata stay)."""
        vp = self.video_path(video_id)
        if vp is None:
            return False
        vp.unlink(missing_ok=True)
        vp.with_suffix(".vtt").unlink(missing_ok=True)  # the sidecar we wrote (external only)
        raw = self._raw(video_id)
        if raw.pop("download_path", None) is not None:
            with contextlib.suppress(OSError):
                (self._item_dir(video_id) / "info.json").write_text(
                    json.dumps(raw, indent=2), encoding="utf-8")
        return True

    def cues(self, video_id: str) -> list[Cue]:
        """The stored transcript as engine Cues; [] when the video has none.
        Auto captions get the rolling-duplicate cleanup; manual subs pass
        through untouched (intentional repeats must survive)."""
        meta = self.meta(video_id)  # KeyError for unknown ids
        vtt = self._item_dir(video_id) / "subs.vtt"
        if not vtt.is_file():
            return []
        cues = load_subtitles(vtt)
        if meta.get("sub_source") == "auto":
            cues = dedupe_auto_cues(cues)
        return cues

    def refresh_subs(self, video_id: str, langs: list[str]) -> int:
        """Re-fetch the transcript (the reindex button). Returns the cue count.

        The stored ``subs.vtt`` is never deleted up front: _fetch_subs replaces
        it only when a fresh transcript actually lands, so a failed refetch (or
        a video whose captions were since removed) keeps the old transcript —
        for a deleted video it would otherwise be unrecoverable."""
        meta = self.meta(video_id)
        item_dir = self._item_dir(video_id)
        sub_lang, sub_source = self._fetch_subs(
            item_dir, video_id, meta["webpage_url"], langs)
        if sub_source is not None:  # keep the old lang/source when nothing new came
            meta["sub_lang"], meta["sub_source"] = sub_lang, sub_source
        # downloaded/download_size are derived from disk (_with_download), not
        # stored state — strip them before persisting.
        stored = {k: v for k, v in meta.items() if k not in ("downloaded", "download_size")}
        (item_dir / "info.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")
        return len(self.cues(video_id))
