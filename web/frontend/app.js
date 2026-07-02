// quipclipper-web frontend

const $ = (id) => document.getElementById(id);

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

const qp = (path) => `?path=${encodeURIComponent(path)}`;

// --- views ------------------------------------------------------------------

/** Stop playback and release the source so audio doesn't keep playing
 *  after navigating away from the item view. */
function stopPlayer() {
  const player = $("player");
  if (!player) return;
  player.pause();
  player.removeAttribute("src");
  // Drop any subtitle tracks too, then reset the media element.
  player.querySelectorAll("track").forEach((t) => t.remove());
  player.load();
}

function showBrowser() {
  stopPlayer();
  $("item").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("browser").hidden = false;
}

function showItem() {
  $("browser").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("item").hidden = false;
}

// --- library browser --------------------------------------------------------

let currentBrowsePath = null;
let librarySearchTimer = null;
let librarySearchId = 0;  // generation token so a slow search can't overwrite a newer one
// When a library search surfaces folders, holds their paths so dialogue
// search can span all of them. null = search the current folder only.
let dialogueSearchScope = null;

async function browse(path) {
  showBrowser();
  currentBrowsePath = path;
  dialogueSearchScope = null;
  const list = $("entries");
  list.innerHTML = "";
  $("search-batch").hidden = true;
  $("browser-empty").hidden = true;
  $("library-search").value = "";
  $("dialogue-search").value = "";
  $("dialogue-search").placeholder = "Search dialogue in this folder…";
  $("dialogue-search-status").hidden = true;
  $("index-banner").hidden = true;
  $("reindex-folder-status").hidden = true;
  // Show dialogue search bar only when inside a folder (not at root)
  $("dialogue-search-bar").hidden = !path;
  let data;
  try {
    data = await getJSON("/api/library/browse" + (path ? qp(path) : ""));
  } catch (err) {
    list.innerHTML = `<li class="error">Could not browse: ${escapeHtml(err.message)}</li>`;
    return;
  }

  renderBreadcrumb(path);
  renderEntries(data.entries);
  checkFolderIndex(path);
}

function renderEntries(entries) {
  const list = $("entries");
  list.innerHTML = "";
  $("browser-empty").textContent = "Nothing here.";
  $("browser-empty").hidden = true;

  if (!entries.length) {
    $("browser-empty").hidden = false;
    return;
  }
  for (const e of entries) {
    const li = document.createElement("li");
    // A book (EPUB audiobook) browses into its chapters like a folder.
    const intoFolder = e.is_dir || e.is_book;
    li.className = "entry " + (intoFolder ? "dir" : "video");
    const icon = e.is_dir ? "📁" : e.is_book ? "📚" : e.is_audio ? "🎵" : "🎬";
    const tag = (!intoFolder && e.has_sidecar) ? ' <span class="badge">sub</span>' : "";
    li.innerHTML = `<span class="icon">${icon}</span><span class="label">${escapeHtml(e.name)}</span>${tag}`;
    li.onclick = () => (intoFolder ? browse(e.path) : openItem(e.path, e.name));
    list.appendChild(li);
  }
}

async function librarySearch(query) {
  if (!query.trim()) {
    browse(currentBrowsePath);
    return;
  }
  const list = $("entries");
  list.innerHTML = "";
  $("browser-empty").hidden = true;
  $("index-banner").hidden = true;
  ++indexCheckId;
  const myId = ++librarySearchId;

  let url = `/api/library/search?query=${encodeURIComponent(query)}`;
  if (currentBrowsePath) url += `&path=${encodeURIComponent(currentBrowsePath)}`;

  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    if (myId !== librarySearchId) return;  // a newer search superseded us
    list.innerHTML = `<li class="error">Search failed: ${escapeHtml(err.message)}</li>`;
    return;
  }
  if (myId !== librarySearchId) return;  // a newer search's results are already shown
  // A prior dialogue search may have left the batch bar showing; a name search
  // has no selectable hits, so hide it.
  $("search-batch").hidden = true;
  renderEntries(data.entries);

  // Enable cross-folder dialogue search over the folders this search surfaced.
  const folderPaths = data.entries.filter((e) => e.is_dir).map((e) => e.path);
  if (folderPaths.length) {
    dialogueSearchScope = folderPaths;
    $("dialogue-search").placeholder =
      `Search dialogue across ${folderPaths.length} folder${folderPaths.length > 1 ? "s" : ""}…`;
    $("dialogue-search-bar").hidden = false;
  } else {
    dialogueSearchScope = null;
    $("dialogue-search").placeholder = "Search dialogue in this folder…";
    $("dialogue-search-bar").hidden = !currentBrowsePath;
  }
}

$("library-search").addEventListener("input", (e) => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => librarySearch(e.target.value), 300);
});

// --- folder dialogue search ------------------------------------------------

async function dialogueSearch() {
  const query = $("dialogue-search").value.trim();
  // Search either the explicit multi-folder scope (from a library search) or
  // the current folder we're browsing.
  const folders = dialogueSearchScope || (currentBrowsePath ? [currentBrowsePath] : []);
  if (!query || !folders.length) return;

  const list = $("entries");
  list.innerHTML = "";
  $("search-batch").hidden = true;
  $("browser-empty").hidden = true;
  const status = $("dialogue-search-status");
  status.textContent = "Searching subtitles…";
  status.hidden = false;
  $("dialogue-search-btn").disabled = true;

  const pathParams = folders.map((f) => `path=${encodeURIComponent(f)}`).join("&");
  const url = `/api/search/folder?${pathParams}&query=${encodeURIComponent(query)}`;
  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    list.innerHTML = `<li class="error">Dialogue search failed: ${escapeHtml(err.message)}</li>`;
    status.hidden = true;
    $("dialogue-search-btn").disabled = false;
    return;
  }

  $("dialogue-search-btn").disabled = false;
  status.textContent = `${data.count} match${data.count !== 1 ? "es" : ""} across ${data.files_scanned} file${data.files_scanned !== 1 ? "s" : ""}`;

  if (!data.matches.length) {
    $("search-batch").hidden = true;
    $("browser-empty").textContent = "No dialogue matches found.";
    $("browser-empty").hidden = false;
    return;
  }

  // Reveal the batch bar (clip/bookmark selected hits) and reset its state.
  $("search-batch").hidden = false;
  $("search-select-all").checked = false;
  $("search-batch-status").textContent = "";

  for (const hit of data.matches) {
    const li = document.createElement("li");
    li.className = "dialogue-hit";
    li.innerHTML =
      `<label class="hit-check"><input type="checkbox" class="search-select" /></label>` +
      `<div class="hit-main">` +
        `<span class="hit-file">${escapeHtml(hit.file)}</span>` +
        `<span class="hit-text">${hit.speaker ? `<span class="hit-speaker">${escapeHtml(hit.speaker)}</span>` : ""}"${escapeHtml(hit.text)}"</span>` +
        `<span class="hit-time">${hit.start_ts} – ${hit.end_ts}  <span class="hit-score">${hit.score}%</span></span>` +
      `</div>` +
      `<div class="hit-actions">` +
        `<button class="mark-icon-btn hit-bookmark" title="Save as bookmark"><span class="star">★</span></button>` +
        `<button class="mark-icon-btn make-clip-btn hit-clip" title="Clip this line"><span class="scissors">✂</span></button>` +
        `<span class="hit-status"></span>` +
      `</div>`;
    // Checkbox drives the batch bar (and carries the data the batch bodies need).
    const cb = li.querySelector(".search-select");
    cb.dataset.path = hit.path;
    cb.dataset.start = hit.start;
    cb.dataset.end = hit.end;
    cb.dataset.cue = hit.text;
    cb.dataset.file = hit.file;
    cb.onclick = (e) => e.stopPropagation();
    cb.onchange = () => updateBatchState("search");
    li.querySelector(".hit-check").onclick = (e) => e.stopPropagation();
    const statusEl = li.querySelector(".hit-status");
    li.querySelector(".hit-bookmark").onclick = (e) => { e.stopPropagation(); bookmarkSearchHit(hit, statusEl); };
    li.querySelector(".hit-clip").onclick = (e) => { e.stopPropagation(); clipSearchHit(hit, statusEl); };
    // Clicking the row still opens the file with the cue selected.
    li.querySelector(".hit-main").onclick = () => openItem(hit.path, hit.file, {
      searchQuery: query,
      seekTo: hit.start,
      markRange: { start: hit.start, end: hit.end },
    });
    list.appendChild(li);
  }
}

// Clip a single search hit in place; the row's status shows spinner → ⤓ download.
async function clipSearchHit(hit, statusEl) {
  statusEl.innerHTML = '<span class="clip-spinner" title="Cutting clip…"></span>';
  const body = {
    path: hit.path, start: hit.start, end: hit.end, before: 0, after: 0,
    cue_text: hit.text, ...batchOptions("search"),
  };
  try {
    const r = await postJSON("/api/clip", body);
    pollHitClip(r.job_id, statusEl);
  } catch (err) {
    statusEl.innerHTML = `<span class="job-failed" title="${escapeHtml(err.message)}">✗</span>`;
  }
}

function pollHitClip(jobId, statusEl) {
  if (!statusEl.isConnected) return;
  getJSON(`/api/jobs/${jobId}`)
    .then((job) => {
      if (!statusEl.isConnected) return;
      if (job.status === "queued" || job.status === "running") {
        setTimeout(() => pollHitClip(jobId, statusEl), 1000);
      } else if (job.status === "done") {
        const f = job.files && job.files[0];
        statusEl.innerHTML = f
          ? `<a class="clip-dl-mini" href="/api/jobs/${jobId}/download/${encodeURIComponent(f.name)}" download title="Download ${escapeHtml(f.name)}" aria-label="Download clip">⤓</a>`
          : '<span class="job-done">✓</span>';
      } else {
        statusEl.innerHTML = `<span class="job-failed" title="${escapeHtml(job.error || "failed")}">✗</span>`;
      }
    })
    .catch(() => { if (statusEl.isConnected) setTimeout(() => pollHitClip(jobId, statusEl), 2000); });
}

// Save a single search hit as a bookmark (keeping its dialogue for {cue} + label).
async function bookmarkSearchHit(hit, statusEl) {
  statusEl.innerHTML = '<span class="muted">…</span>';
  try {
    await postJSON("/api/bookmarks", {
      path: hit.path, label: hit.text, start: hit.start, end: hit.end, cue: hit.text,
    });
    statusEl.innerHTML = '<span class="job-done" title="Saved to bookmarks">★ saved</span>';
  } catch (err) {
    statusEl.innerHTML = `<span class="job-failed" title="${escapeHtml(err.message)}">✗</span>`;
  }
}

$("dialogue-search-btn").addEventListener("click", dialogueSearch);
$("dialogue-search").addEventListener("keydown", (e) => {
  // Ignore Enter while a search is in flight (button disabled) to avoid double-submit.
  if (e.key === "Enter" && !$("dialogue-search-btn").disabled) dialogueSearch();
});

// --- folder subtitle index --------------------------------------------------

let indexCheckId = 0;

async function checkFolderIndex(path) {
  const banner = $("index-banner");
  banner.hidden = true;
  if (!path) return;
  const myId = ++indexCheckId;
  try {
    const data = await getJSON(`/api/search/folder/index-status?path=${encodeURIComponent(path)}`);
    if (myId !== indexCheckId) return;
    if (data.total === 0 || data.skipped) return;
    if (data.indexed >= data.total) return;
    $("index-banner-text").textContent = data.indexed === 0
      ? `Subtitles in this folder haven't been indexed yet (${data.total} files). Indexing speeds up dialogue search.`
      : `${data.indexed} of ${data.total} files in this folder indexed. Index the rest to speed up dialogue search.`;
    banner.hidden = false;
  } catch {
    // silent — non-critical
  }
}

$("index-btn").addEventListener("click", async () => {
  if (!currentBrowsePath) return;
  const btn = $("index-btn");
  const text = $("index-banner-text");
  btn.disabled = true;
  text.textContent = "Indexing…";

  try {
    const resp = await fetch(`/api/search/folder/index?path=${encodeURIComponent(currentBrowsePath)}`, { method: "POST" });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let last = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      last = decoder.decode(value, { stream: true }).trim().split("\n").pop();
      const m = last.match(/^(\d+)\/(\d+) (.+)/);
      if (m) text.textContent = `Indexing ${m[1]}/${m[2]}: ${m[3]}`;
    }
    $("index-banner").hidden = true;
  } catch {
    text.textContent = "Indexing failed.";
  }
  btn.disabled = false;
});

/** Stream a (re)index of one folder, reporting progress via a callback. */
async function streamFolderIndex(folder, force, onProgress) {
  let url = `/api/search/folder/index?path=${encodeURIComponent(folder)}`;
  if (force) url += "&force=true";
  const resp = await fetch(url, { method: "POST" });
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const last = decoder.decode(value, { stream: true }).trim().split("\n").pop();
    const m = last.match(/^(\d+)\/(\d+) (.+)/);
    if (m && onProgress) onProgress(m[1], m[2], m[3]);
  }
}

// Force-reindex the current folder(s) — clears cache then re-extracts.
$("reindex-folder-btn").addEventListener("click", async () => {
  const folders = dialogueSearchScope || (currentBrowsePath ? [currentBrowsePath] : []);
  if (!folders.length) return;
  const btn = $("reindex-folder-btn");
  const status = $("reindex-folder-status");
  btn.classList.add("disabled");
  status.hidden = false;
  try {
    for (let i = 0; i < folders.length; i++) {
      const prefix = folders.length > 1 ? `Folder ${i + 1}/${folders.length} — ` : "";
      await streamFolderIndex(folders[i], true, (d, t, name) => {
        status.textContent = `${prefix}Reindexing ${d}/${t}: ${name}`;
      });
    }
    status.textContent = "Reindexed.";
    if (currentBrowsePath) checkFolderIndex(currentBrowsePath);
  } catch {
    status.textContent = "Reindex failed.";
  }
  btn.classList.remove("disabled");
});

// Force-reindex the current file's subtitles, then reload the script.
$("ss-reindex").addEventListener("click", async () => {
  if (!currentItem) return;
  const btn = $("ss-reindex");
  const status = $("ss-reindex-status");
  btn.classList.add("disabled");
  status.hidden = false;
  status.textContent = "Reindexing…";
  try {
    const data = await postJSON(
      `/api/items/subtitles/reindex?path=${encodeURIComponent(currentItem.path)}`, {},
    );
    status.textContent = `Reindexed (${data.cues} cues).`;
    // Reload the script and subtitle track so the new subs show immediately.
    const track = getSelectedTrack();
    loadScript(currentItem.path, track);
    loadSubtitleTrack(currentItem.path, track, transcodeOffset || 0);
  } catch (err) {
    status.textContent = `Reindex failed: ${err.message}`;
  }
  btn.classList.remove("disabled");
});

function renderBreadcrumb(path) {
  const bc = $("breadcrumb");
  bc.innerHTML = "";

  const addLink = (label, target) => {
    const a = document.createElement("a");
    a.textContent = label;
    a.onclick = () => browse(target);
    bc.appendChild(a);
  };
  const addSep = () => {
    const sep = document.createElement("span");
    sep.className = "muted";
    sep.textContent = " / ";
    bc.appendChild(sep);
  };
  const addCurrent = (label) => {
    const span = document.createElement("span");
    span.className = "muted";
    span.textContent = label;
    bc.appendChild(span);
  };

  // Root "Library" link is always present.
  if (!path) {
    addCurrent("Library");
    return;
  }
  addLink("Library", null);

  // Find the media root that contains this path so we don't make segments
  // above a root clickable (they'd be outside the allowed roots).
  const root = mediaRoots.find((r) => path === r || path.startsWith(r + "/"));
  if (!root) {
    // Fallback: show the whole path as a single non-clickable segment.
    addSep();
    addCurrent(path);
    return;
  }

  const rootName = root.split("/").pop() || root;
  addSep();
  if (path === root) {
    addCurrent(rootName);
    return;
  }
  addLink(rootName, root);

  const rest = path.slice(root.length).replace(/^\/+/, "");
  const parts = rest.split("/");
  let cum = root;
  parts.forEach((part, i) => {
    cum += "/" + part;
    const target = cum;  // capture per-iteration value for the closure
    addSep();
    if (i === parts.length - 1) {
      addCurrent(part);  // current folder — not a link
    } else {
      addLink(part, target);
    }
  });
}

// --- item / inspection view -------------------------------------------------

// iOS/iPadOS detection. iPadOS 13+ reports as "MacIntel" but is touch-capable,
// so check maxTouchPoints too. iOS Safari can't play Matroska/Opus or raw .mkv,
// so these devices use the fragmented-MP4 transcode path (see openItem).
const IS_IOS = /iP(hone|ad|od)/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

// Preferred subtitle language(s) override (B14), remembered per browser. Empty
// = use the server default (QC_SUBTITLE_LANGS). Passed to /api/items so the
// auto-selected track honours it.
const SUB_LANGS_KEY = "qc_sub_langs";
function subtitleLangs() {
  return (localStorage.getItem(SUB_LANGS_KEY) || "").trim();
}

// Video codecs that play fine via stream-copy in our transcode/raw paths.
// H.264 always works; HEVC is browser-dependent (Firefox can't decode it).
const PLAYABLE_VIDEO = new Set(["h264", "avc1", "vp8", "vp9", "av1"]);
// Video codecs iOS Safari decodes natively over HLS (it adds HEVC vs desktop);
// anything else must be re-encoded to H.264 on the HLS path (B22 / iOS).
const IOS_PLAYABLE_VIDEO = new Set(["h264", "avc1", "hevc", "h265", "hev1", "hvc1"]);

/** Whether the source video codec must be re-encoded to H.264 for THIS browser
 *  (HEVC on Firefox, MPEG-4 ASP/XviD, MPEG-2, VC1, …) — B20/B22. */
function needsVideoReencode(codec) {
  const c = (codec || "").toLowerCase();
  if (PLAYABLE_VIDEO.has(c)) return false;
  if (c === "hevc" || c === "h265" || c === "hev1" || c === "hvc1") {
    const v = document.createElement("video");
    const ok = v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
               v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"');
    return !ok;  // re-encode only when this browser can't decode HEVC
  }
  return true;  // mpeg4 (ASP/XviD), mpeg2video, vc1, wmv, … → re-encode
}

/** Show/hide the "video is being re-encoded" indicator. */
function setVideoReencodeNote(on) {
  const el = $("video-reencode-note");
  if (!el) return;
  el.hidden = !on;
  if (on) {
    el.textContent = (appConfig && appConfig.hw_encode)
      ? "⚙ Re-encoding video to H.264 for your browser (hardware-accelerated) — loading and seeking may be a little slower."
      : "⚙ Re-encoding video to H.264 for your browser — loading and seeking may be slow.";
  }
}

let currentItem = null;
let transcodeOffset = 0;  // ffmpeg -ss offset for current transcode segment
// Per-item listeners on the persistent #player / seek slider are scoped to this
// controller; openItem aborts the previous one so they're removed, not just
// neutralized — a previous item's loop can't drive the new player.
let itemListeners = null;
// Range-aware playback controls, rebuilt per item by openItem (see there).
let pb = null;

/** Reflect the player's play/pause state on the custom control + range note. */
function updatePlaybackUI() {
  const player = $("player");
  $("pb-playpause").textContent = player.paused ? "▶" : "⏸";
  const note = $("pb-range-note");
  if (hasClipRange()) {
    note.hidden = false;
    note.textContent = `▶ ${formatTime(bufStart())} – ${formatTime(bufEnd())}`;
  } else {
    note.hidden = true;
  }
}
$("player").addEventListener("play", updatePlaybackUI);
$("player").addEventListener("pause", updatePlaybackUI);
$("pb-restart").onclick = () => pb && pb.restart();
$("pb-playpause").onclick = () => pb && pb.toggle();
// Lock the player box to the real video aspect once known, so subsequent
// transcode-segment reloads don't resize it and reflow the page.
$("player").addEventListener("loadedmetadata", () => {
  const p = $("player");
  if (p.videoWidth && p.videoHeight) p.style.aspectRatio = `${p.videoWidth} / ${p.videoHeight}`;
});

/** Absolute playback position in the source file. */
function playerTime() {
  return $("player").currentTime + transcodeOffset;
}

/** Seek to an absolute position in the source file. */
function seekTo(seconds) {
  const player = $("player");
  if (transcodeOffset > 0 || player.src.includes("/api/media/transcode")) {
    // For transcoded streams, request a new segment from the server
    player.dispatchEvent(new CustomEvent("transcode-seek", { detail: seconds }));
  } else {
    player.currentTime = seconds;
  }
  player.play().catch(() => {});
}

/** A clean display title for an item: the containing folder's name (movies
 *  live in a per-title folder), stepping up past a "Season N"/Specials folder
 *  to the series name. Falls back to the filename. */
function cleanItemName(path, fileName) {
  const parts = path.split("/").filter(Boolean);
  let parent = parts.length >= 2 ? parts[parts.length - 2] : "";
  const grand = parts.length >= 3 ? parts[parts.length - 3] : "";
  if (/^(season\s*\d+|specials|extras)$/i.test(parent) && grand) parent = grand;
  return parent || fileName;
}

async function openItem(path, name, opts) {
  const { searchQuery, seekTo: seekTarget, selectWholeClip, markRange } = opts || {};
  showItem();
  // A dialogue-search match to auto-select in Marks once the script loads.
  pendingCueSelect = markRange || null;
  // Consume any pending bookmark clip immediately so an early return (e.g.
  // probe failure) can't leak it into the next item opened.
  const bookmarkClip = pendingBookmarkClip;
  pendingBookmarkClip = null;
  currentItem = { path, name };
  // Remove the previous item's player/slider listeners before wiring this one's.
  if (itemListeners) itemListeners.abort();
  itemListeners = new AbortController();
  const signal = itemListeners.signal;
  $("item-name").textContent = cleanItemName(path, name);
  $("item-filename").textContent = name;
  $("stream-selector").hidden = true;
  $("preview-note").textContent = "";
  $("search-input").value = searchQuery || "";
  $("search-results").innerHTML = "";
  $("search-empty").hidden = true;
  showClipTarget(false);
  // Reset the per-page Clips list (in-flight polls stop when their row is gone).
  $("clips-made-panel").hidden = true;
  $("clips-made-list").innerHTML = "";
  setClipIndicator("hidden");
  activeJobId = null;
  clipRangeStart = null;
  clipRangeEnd = null;
  loadedBookmarkCue = null;
  clipFirst = -1;
  clipLast = -1;
  updatePlaybackUI();  // clear any stale range note / reset the play icon
  $("mark-range-display").innerHTML = "";
  $("mark-save").disabled = true;
  scriptCues = [];
  scriptActiveIdx = -1;
  $("script-wrap").hidden = true;
  $("marks-hint").hidden = true;
  $("script-list").innerHTML = "";

  let info;
  try {
    info = await getJSON("/api/items" + qp(path) +
      (subtitleLangs() ? `&langs=${encodeURIComponent(subtitleLangs())}` : ""));
  } catch (err) {
    if (signal.aborted) return;  // superseded while the request was in flight
    $("preview-note").innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    return;
  }
  // A newer openItem may have started (aborting our signal) while /api/items was
  // in flight. Bail before touching the shared player/pb/onerror/stream state —
  // otherwise this stale response wires the previous item's media into the new
  // item's view (and a clip would be cut from the wrong file). The per-item
  // listeners below are already signal-scoped; these direct assignments are not.
  if (signal.aborted) return;

  const player = $("player");
  player.style.aspectRatio = "";  // back to the default box until this item's dims load
  const mediaUrl = "/api/media" + qp(path);
  player.querySelectorAll("track").forEach((t) => t.remove());

  // Check if the primary audio codec is browser-playable; if not, use
  // the transcode endpoint from the start (browsers silently ignore
  // unsupported audio codecs like AC3/DTS/FLAC without firing onerror).
  // iOS never uses the desktop transcode path — it goes through HLS below.
  const BROWSER_AUDIO = new Set(["aac", "mp3", "opus", "vorbis"]);
  const primaryAudio = info.streams.find((s) => s.kind === "audio");
  // Ignore attached_pic "video" streams — that's embedded cover art (common in
  // podcast mp3/m4a), a still image, not real video to play, re-encode, or clip.
  const primaryVideo = info.streams.find((s) => s.kind === "video" && !s.attached_pic);
  // Audio-only sources (podcasts, audiobooks) have no video stream: collapse the
  // player to a control bar, force audio clips, and skip the iOS HLS path (iOS
  // plays mp3/aac/wav natively from the raw endpoint).
  const isAudioOnly = !primaryVideo;
  currentItem.audioOnly = isAudioOnly;
  player.classList.toggle("audio-only", isAudioOnly);
  // An EPUB segment reports its book title; show it as the heading (the segment
  // name stays as the filename subtitle) so a chapter reads like an episode.
  if (info.book_title) $("item-name").textContent = info.book_title;
  // "Audio only" / "Exact (re-encode)" are video-source concepts — hide them when
  // the source is already audio (the clip is always an audio passthrough/format).
  document.querySelectorAll(".video-only-opt").forEach((el) => { el.hidden = isAudioOnly; });
  // iOS decodes H.264 + HEVC natively (over HLS); other video codecs
  // (XviD/MPEG-4 ASP, MPEG-2, VC1, …) must be re-encoded — the iOS side of B22.
  const iosVideoReencode = IS_IOS && primaryVideo && !IOS_PLAYABLE_VIDEO.has((primaryVideo.codec || "").toLowerCase());
  // iOS Safari can't play Matroska/Opus, raw .mkv, or a chunked progressive MP4
  // (it needs HTTP range support). It plays HLS natively, so iOS uses the HLS
  // endpoint with the browser's own controls/seeking instead of our transcode.
  const hlsUrl = "/api/media/hls" + qp(path) + (iosVideoReencode ? "&venc=1" : "");
  // Does the primary video codec need re-encoding to H.264 for this (desktop)
  // browser? (HEVC on Firefox, MPEG-4 ASP/XviD, MPEG-2, VC1, …) — B20/B22.
  const videoReencode = !IS_IOS && primaryVideo && needsVideoReencode(primaryVideo.codec);
  const transcodeUrl = "/api/media/transcode" + qp(path) + (videoReencode ? "&venc=1" : "");
  const needsTranscode = !IS_IOS &&
    ((primaryAudio && !BROWSER_AUDIO.has(primaryAudio.codec)) || videoReencode);
  // Show/refresh the video-re-encode indicator (loading/seeking may be slower).
  setVideoReencodeNote(videoReencode || iosVideoReencode);

  // Transcode seek state
  let isTranscoding = false;
  let settingSrc = false;
  transcodeOffset = 0;
  const seekBar = $("transcode-seek-bar");
  const seekSlider = $("transcode-slider");
  const seekTimeLabel = $("transcode-time");
  const seekDurLabel = $("transcode-duration");
  let probedDuration = info.duration || 0;
  currentDuration = probedDuration;  // module-level, for the buffered preview range

  let firstLoad = true;
  // `autoplay`: undefined → play unless this is the very first load; or an
  // explicit boolean (used by the range loop to seek without auto-playing).
  function loadTranscode(startTime, autoplay) {
    isTranscoding = true;
    settingSrc = true;
    transcodeOffset = startTime;
    if (autoplay === undefined) autoplay = !firstLoad;
    firstLoad = false;
    let url = transcodeUrl;
    if (startTime > 0) url += `&start=${startTime}`;
    url += streamParams();  // selected audio stream / channel subset (B17)
    player.src = url;
    player.load();
    player.addEventListener("loadedmetadata", function onMeta() {
      player.removeEventListener("loadedmetadata", onMeta);
      settingSrc = false;
      if (autoplay) player.play().catch(() => {});
      updatePlaybackUI();  // sync the play/pause icon once the reload settles
    }, { once: true, signal });
    $("preview-note").textContent = "";
    // With -c:v copy, ffmpeg starts from the nearest keyframe before
    // the requested time.  Query the actual keyframe position so we can
    // shift subtitles accurately and keep the seek bar in sync.
    if (startTime > 0) {
      getJSON(`/api/media/keyframe?path=${encodeURIComponent(path)}&time=${startTime}`)
        .then((kf) => {
          transcodeOffset = kf.actual;
          loadSubtitleTrack(path, getSelectedTrack(), kf.actual);
        })
        .catch(() => {
          // Fall back to requested time if probe fails
          loadSubtitleTrack(path, getSelectedTrack(), startTime);
        });
    } else {
      loadSubtitleTrack(path, getSelectedTrack(), 0);
    }
  }

  // Stream-selector query params (selected audio stream / channel subset, B17).
  function streamParams() {
    let p = "";
    const a = getSelectedAudio();
    if (a != null) p += `&audio=${a}`;
    const c = getSelectedChan();
    if (c && c !== "whole") p += `&chan=${encodeURIComponent(c)}`;
    return p;
  }

  // Switching audio stream / channel subset requires the transcode path; reload
  // the current segment at the same position with the new params.
  function reloadStreams() {
    const pos = isTranscoding ? (player.currentTime + transcodeOffset) : player.currentTime;
    const wasPlaying = !player.paused;
    isTranscoding = true;
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    loadTranscode(pos || 0, wasPlaying);
  }

  // The seekable window: the selected clip range (expanded by the before/after
  // buffer) when one exists, else the whole file. The transcode seek bar and the
  // range loop both use this.
  function seekBounds() {
    return hasClipRange() ? [bufStart(), bufEnd()] : [0, probedDuration];
  }

  // Seek to an absolute time, optionally playing. Works for both the native
  // (seekable) source and the streaming transcode (which reloads a segment).
  function seekAbs(t, autoplay) {
    t = Math.max(0, t);
    if (isTranscoding) {
      loadTranscode(t, autoplay);
    } else {
      player.currentTime = t;
      if (autoplay) player.play().catch(() => {}); else player.pause();
    }
  }

  // Custom seek bar for transcoded streams (native scrubber can't seek
  // on a non-seekable streaming source). Restricted to the selected range.
  function updateSeekBar() {
    if (!isTranscoding || !probedDuration) return;
    const [lo, hi] = seekBounds();
    const span = Math.max(0.001, hi - lo);
    const absTime = player.currentTime + transcodeOffset;
    seekSlider.value = Math.min(100, Math.max(0, ((absTime - lo) / span) * 100));
    seekTimeLabel.textContent = formatTime(absTime);
  }

  // Re-render the seek bar bounds (called when the selected range changes).
  function refreshSeekBar() {
    if (!isTranscoding) return;
    const [, hi] = seekBounds();
    seekDurLabel.textContent = formatTime(hi);
    updateSeekBar();
  }

  player.addEventListener("timeupdate", updateSeekBar, { signal });

  // Range loop: when playback reaches the end of the selected range, pause and
  // return to the start so the clip can be previewed again.
  player.addEventListener("timeupdate", () => {
    if (settingSrc || !hasClipRange()) return;
    const absTime = player.currentTime + transcodeOffset;
    if (absTime >= bufEnd() - 0.04) {
      if ($("pb-loop").checked) {
        seekAbs(bufStart(), true);   // keep playing — continuous loop
      } else {
        player.pause();              // paused attribute flips synchronously
        seekAbs(bufStart(), false);  // return to start, paused, ready to replay
        updatePlaybackUI();          // reflect the paused state on the icon now
      }
    }
  }, { signal });

  seekSlider.addEventListener("input", () => {
    const [lo, hi] = seekBounds();
    seekTimeLabel.textContent = formatTime(lo + (seekSlider.value / 100) * (hi - lo));
  }, { signal });

  seekSlider.addEventListener("change", () => {
    if (!isTranscoding) return;
    const [lo, hi] = seekBounds();
    loadTranscode(lo + (seekSlider.value / 100) * (hi - lo));
  }, { signal });

  // Custom event for programmatic seeks (search results, bookmarks, etc.)
  player.addEventListener("transcode-seek", (e) => {
    loadTranscode(e.detail);
  }, { signal });

  // Expose range-aware playback controls to the module-level buttons. The
  // wiring (below, near boot) calls these; they close over this item's state.
  pb = {
    restart() { seekAbs(hasClipRange() ? bufStart() : 0, true); },
    toggle() {
      if (!player.paused) { player.pause(); return; }
      if (hasClipRange()) {
        const t = player.currentTime + transcodeOffset;
        if (t < bufStart() - 0.1 || t >= bufEnd() - 0.05) {
          seekAbs(bufStart(), true);
          return;
        }
      }
      player.play().catch(() => {});
    },
    pause() { player.pause(); },
    refreshSeekBar,
  };

  const seekHint = $("transcode-hint");

  if (needsTranscode) {
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    seekSlider.max = 100;
    loadTranscode(seekTarget || 0);
  } else {
    // Native playback: raw source on desktop, HLS on iOS (Safari seeks both
    // natively, so the custom transcode seek bar stays hidden).
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    player.src = (IS_IOS && !isAudioOnly) ? hlsUrl : mediaUrl;
  }

  player.onerror = () => {
    // On iOS (HLS) there's no further fallback; the desktop transcode path is
    // unplayable there, so just surface the message.
    if (IS_IOS || isTranscoding || player.src.includes("/api/media/transcode")) {
      $("preview-note").textContent =
        "Preview can't play this file in the browser. " +
        "Dialogue search still works — try the search box below.";
      return;
    }
    // Fall back to on-the-fly transcode
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    loadTranscode(0);
  };

  renderStreamSelector(info, path, reloadStreams);
  loadBookmarks();

  if (searchQuery) doSearch();
  if (seekTarget != null && !needsTranscode) {
    player.addEventListener("loadedmetadata", () => {
      player.currentTime = seekTarget;
    }, { once: true, signal });
  }

  // When opening a clip from the clips library, pre-select the whole file
  // so the clip options are immediately ready for re-export.
  if (selectWholeClip && info.duration) {
    clipRangeStart = 0;
    clipRangeEnd = info.duration;
    showClipTarget(true);
    $("clip-range-display").textContent =
      `Entire file — 0:00.0 – ${formatTime(info.duration)} (${info.duration.toFixed(1)}s)`;
  }

  // Apply pending bookmark clip (when navigating from bookmarks browser)
  if (bookmarkClip) useBookmarkForClip(bookmarkClip);
}

// Bitmap subtitle codecs the player can't display and we can't search.
// Mirrors IMAGE_SUBTITLE_CODECS in the engine's subtitles.py.
const IMAGE_SUBTITLE_CODECS = new Set([
  "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
  "dvb_subtitle", "dvbsub", "xsub",
]);
const isTextSubtitle = (t) => !IMAGE_SUBTITLE_CODECS.has((t.codec || "").toLowerCase());

const CHAN_LABELS = {
  front: "Front (L/R)", center: "Center", lfe: "LFE",
  side: "Surround (side)", back: "Surround (back)", surround: "Surround",
};
const chanLabel = (g) => CHAN_LABELS[g] || g;

// The unified stream selector under the seek bar (B17): pick the subtitle
// track, the audio stream, and — for multichannel audio — a channel subset.
// `onStreamChange` (an openItem closure) reloads the transcode when the audio
// stream or channel subset changes. Subtitle changes are handled inline.
function renderStreamSelector(info, path, onStreamChange) {
  const wrap = $("stream-selector");
  const subSel = $("ss-subs"), audSel = $("ss-audio"), chanSel = $("ss-chan");

  // --- Subtitles ---
  const allTracks = info.subtitle_tracks || [];
  const tracks = allTracks.filter(isTextSubtitle);  // image tracks can't be shown
  const hasSubs = tracks.length > 0 || info.has_sidecar;
  subSel.innerHTML = "";
  // Always show the Subtitles slot: the dropdown when there are subtitles, or a
  // "No subtitle stream available" note otherwise (B19).
  $("ss-subs-field").hidden = false;
  subSel.hidden = !hasSubs;
  $("ss-no-subs").hidden = hasSubs;
  $("ss-reindex").hidden = !hasSubs;
  $("ss-reindex-status").hidden = true;

  let initialTrack;  // undefined => nothing to load
  if (hasSubs) {
    if (info.has_sidecar) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "Sidecar file"; subSel.appendChild(o);
    }
    // Backend is the source of truth for auto-selection; fall back to first text.
    const bestIsText = tracks.some((t) => t.index === info.best_track);
    const bestIdx = bestIsText ? info.best_track : (tracks[0] && tracks[0].index);
    tracks.forEach((t) => {
      const o = document.createElement("option");
      o.value = t.index;
      o.textContent = `${t.codec}${t.language ? " " + t.language : ""}${t.title ? " — " + t.title : ""}`;
      subSel.appendChild(o);
    });
    if (info.has_sidecar) { subSel.value = ""; initialTrack = null; }
    else { subSel.value = String(bestIdx); initialTrack = bestIdx; }
  }
  subSel.onchange = () => {
    const t = getSelectedTrack();
    loadSubtitleTrack(path, t, transcodeOffset || 0);
    loadScript(path, t);
  };
  if (hasSubs) { loadSubtitleTrack(path, initialTrack); loadScript(path, initialTrack); }

  // --- Audio streams ---
  const audios = (info.streams || []).filter((s) => s.kind === "audio");
  audSel.innerHTML = "";
  audios.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.index;
    o.textContent =
      `${s.codec}${s.channel_layout ? " " + s.channel_layout : ""}` +
      `${s.language ? " " + s.language : ""}${s.title ? " — " + s.title : ""}`;
    audSel.appendChild(o);
  });
  if (audios.length) audSel.value = String(audios[0].index);
  // Audio-stream switching needs the transcode reload path (not iOS HLS).
  $("ss-audio-field").hidden = audios.length <= 1 || IS_IOS;

  // --- Channel subset (depends on the selected audio stream's groups) ---
  function buildChan() {
    const aidx = audSel.value;
    const aud = audios.find((s) => String(s.index) === String(aidx)) || audios[0];
    const groups = (aud && aud.groups) || [];
    $("ss-chan-field").hidden = groups.length <= 1 || IS_IOS;  // multichannel only
    chanSel.innerHTML = "";
    const all = document.createElement("option");
    all.value = "whole"; all.textContent = "All channels"; chanSel.appendChild(all);
    groups.forEach((g) => {
      const o = document.createElement("option");
      o.value = g; o.textContent = chanLabel(g); chanSel.appendChild(o);
    });
    chanSel.value = "whole";
  }
  buildChan();
  audSel.onchange = () => { buildChan(); onStreamChange(); };
  chanSel.onchange = () => onStreamChange();

  // Show the bar if there's anything to pick.
  // The Subtitles slot is always shown (dropdown or "no subtitle" note), so the
  // selector bar is always visible.
  wrap.hidden = false;
}

function loadSubtitleTrack(path, track, offset) {
  const player = $("player");
  player.querySelectorAll("track").forEach((t) => t.remove());
  let url = "/api/items/subtitles" + qp(path);
  if (track !== null && track !== undefined) url += `&track=${track}`;
  if (offset) url += `&offset=${offset}`;
  const track_el = document.createElement("track");
  track_el.kind = "subtitles";
  track_el.label = "Dialogue";
  track_el.src = url;
  track_el.default = true;
  player.appendChild(track_el);
  // Browsers often ignore .default on dynamically added tracks; force showing.
  track_el.track.mode = "showing";
}

// --- dialogue search --------------------------------------------------------

// The selected subtitle track index from the stream selector; null = sidecar/
// auto (empty option) or no subtitles, matching the backend's auto-resolve.
function getSelectedTrack() {
  const sel = $("ss-subs");
  if (!sel || $("ss-subs-field").hidden || sel.value === "") return null;
  return sel.value;
}

// Selected audio stream index (null = default/single) and channel subset.
function getSelectedAudio() {
  const sel = $("ss-audio");
  if (!sel || $("ss-audio-field").hidden || sel.value === "") return null;
  return parseInt(sel.value);
}
function getSelectedChan() {
  const sel = $("ss-chan");
  if (!sel || $("ss-chan-field").hidden) return "whole";
  return sel.value;
}

async function doSearch() {
  if (!currentItem) return;
  const query = $("search-input").value.trim();
  if (!query) return;

  const results = $("search-results");
  const empty = $("search-empty");
  results.innerHTML = "";
  empty.hidden = true;
  $("search-btn").disabled = true;

  let url = `/api/search${qp(currentItem.path)}&query=${encodeURIComponent(query)}`;
  const track = getSelectedTrack();
  if (track !== null) url += `&track=${track}`;

  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    results.innerHTML = `<li class="error">Search failed: ${escapeHtml(err.message)}</li>`;
    $("search-btn").disabled = false;
    return;
  }
  $("search-btn").disabled = false;

  if (!data.matches.length) {
    empty.hidden = false;
    return;
  }

  for (const m of data.matches) {
    const li = document.createElement("li");
    li.className = "search-result";
    li.innerHTML =
      `<span class="result-text">${escapeHtml(m.text)}</span>` +
      `<span class="result-meta">` +
        `<span class="result-score">${m.score}</span>` +
        `<span>${m.start_ts} – ${m.end_ts}</span>` +
      `</span>`;
    li.onclick = () => {
      seekTo(m.start);
      // Select the matching cue(s) in Marks (highlighted yellow) — identical to
      // clicking a library dialogue-search hit, so both search paths behave the
      // same way.
      selectCueRange(m.start, m.end);
    };
    results.appendChild(li);
  }
}

// seekTo is defined above (module-level, handles transcode offset)

function escapeHtml(s) {
  // Escape &<> plus quotes so the output is safe in both text and (double- or
  // single-quoted) attribute contexts. `&` must be replaced first.
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

$("search-btn").onclick = doSearch;
$("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

// --- marks (in/out) ---------------------------------------------------------

// --- clip selection (dialogue-based) ----------------------------------------
//
// A clip is defined as a contiguous range of dialogue cues.  Timestamps
// are *derived* from the selected cues, not the other way around.
//   clipFirst / clipLast  — indices into scriptCues[]
//   clipStart()           — scriptCues[clipFirst].start
//   clipEnd()             — scriptCues[clipLast].end

let clipFirst = -1;  // index of first selected cue (-1 = none)
let clipLast = -1;   // index of last selected cue (-1 = none)

/** Derived start time of the clip (dialogue boundary, no padding). */
function clipStart() {
  return clipFirst >= 0 ? scriptCues[clipFirst].start : null;
}
/** Derived end time of the clip (dialogue boundary, no padding). */
function clipEnd() {
  return clipLast >= 0 ? scriptCues[clipLast].end : null;
}
/** Whether we have a valid clip selection. */
function hasClipRange() {
  return clipFirst >= 0 && clipLast >= 0 && clipLast >= clipFirst;
}

// Buffer (before/after padding) — part of the selection (B16). The previewed /
// looped range is the cue range expanded by the buffer (clamped to the file).
let currentDuration = 0;  // set per item in openItem (probed duration)
function clipPadBefore() { const v = parseFloat($("clip-before").value); return v > 0 ? v : 0; }
function clipPadAfter() { const v = parseFloat($("clip-after").value); return v > 0 ? v : 0; }
/** Buffered start of the previewed clip (cue start − before, clamped ≥ 0). */
function bufStart() { const s = clipStart(); return s === null ? null : Math.max(0, s - clipPadBefore()); }
/** Buffered end of the previewed clip (cue end + after, clamped ≤ duration). */
function bufEnd() {
  const e = clipEnd();
  if (e === null) return null;
  const padded = e + clipPadAfter();
  return currentDuration ? Math.min(currentDuration, padded) : padded;
}

function setClipInAt(idx) {
  clipFirst = idx;
  if (clipLast < clipFirst) clipLast = clipFirst;
  updateClipUI();
  if (pb) pb.pause();  // stop playback when a clip boundary is set
}

function setClipOutAt(idx) {
  clipLast = idx;
  if (clipFirst < 0 || clipFirst > clipLast) clipFirst = clipLast;
  updateClipUI();
  if (pb) pb.pause();
}

function clearClip() {
  clipFirst = -1;
  clipLast = -1;
  updateClipUI();
}

function updateClipUI() {
  // Range display
  const el = $("mark-range-display");
  if (!hasClipRange()) {
    el.innerHTML = "";
    $("mark-save").disabled = true;
    showClipTarget(false);
  } else {
    // Display the buffered range (cue range ± before/after) — what actually
    // plays and gets clipped. The raw cue range still drives clipRangeStart/End
    // (the server applies the padding once at cut time).
    const s = clipStart(), e = clipEnd();
    const bs = bufStart(), be = bufEnd();
    const dur = be - bs;
    const nLines = clipLast - clipFirst + 1;
    el.innerHTML =
      `<span class="range-times">${formatTime(bs)} – ${formatTime(be)}</span> ` +
      `<span>(${dur.toFixed(1)}s, ${nLines} line${nLines > 1 ? "s" : ""})</span>` +
      ` <button class="mark-clear-inline" title="Clear selection">×</button>`;
    el.querySelector(".mark-clear-inline").onclick = clearClip;
    $("mark-save").disabled = false;
    // Auto-show clip panel and populate range (raw cue range; server pads).
    clipRangeStart = s;
    clipRangeEnd = e;
    showClipTarget(true);
    $("clip-range-display").textContent =
      `${formatTime(bs)} – ${formatTime(be)} (${dur.toFixed(1)}s)`;
  }
  // Highlight script lines
  updateScriptHighlight();
  // Reflect the new range on the seek bar and playback controls.
  if (pb) pb.refreshSeekBar();
  updatePlaybackUI();
}

function formatTime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(1);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${sec.padStart(4, "0")}`
    : `${m}:${sec.padStart(4, "0")}`;
}

/** Like formatTime but rounded to the whole second (for bookmark/clip names). */
function formatTimeWhole(s) {
  const t = Math.round(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

async function saveBookmark() {
  if (!currentItem || !hasClipRange()) return;
  const s = clipStart(), e = clipEnd();
  // Build default label: time range (whole seconds) + first dialogue line
  let defaultLabel = `${formatTimeWhole(s)} – ${formatTimeWhole(e)}`;
  if (clipFirst >= 0 && scriptCues[clipFirst]) {
    const firstLine = scriptCues[clipFirst].text.slice(0, 60);
    defaultLabel += ` — ${firstLine}`;
  }
  const label = prompt("Bookmark label:", defaultLabel);
  if (label === null) return;
  try {
    const t = getSelectedTrack();
    await postJSON("/api/bookmarks", {
      path: currentItem.path,
      label: label || defaultLabel,
      start: s,
      end: e,
      before: clipPadBefore(),
      after: clipPadAfter(),
      sub_track: t != null ? parseInt(t) : null,
      audio_track: getSelectedAudio(),
      channel_subset: getSelectedChan(),
      // Keep the matched dialogue so exporting the bookmark fills the {cue} token.
      cue: (clipFirst >= 0 && scriptCues[clipFirst]) ? scriptCues[clipFirst].text : "",
    });
    loadBookmarks();
  } catch (err) {
    alert("Failed to save bookmark: " + err.message);
  }
}

$("mark-save").onclick = saveBookmark;

// Before/After buffer inputs (B16): re-render the previewed range live.
["clip-before", "clip-after"].forEach((id) =>
  $(id).addEventListener("input", () => { if (hasClipRange()) updateClipUI(); })
);

// --- subtitle script view ---------------------------------------------------

let scriptCues = [];       // full cue list [{start, end, text}, ...]
let scriptAutoScroll = true;
let scriptScrollTimer = null;
let scriptActiveIdx = -1;  // index of the currently highlighted cue
let scriptProgScroll = false; // true when we're doing a programmatic scroll
let scriptLoadId = 0;      // generation token to ignore stale script loads
// A dialogue-search match to auto-select in Marks once the script has loaded
// (set in openItem from the library/folder search, consumed in loadScript).
let pendingCueSelect = null;

/** Load cues as JSON and populate the script panel. */
async function loadScript(path, track) {
  const wrap = $("script-wrap");
  const list = $("script-list");
  scriptCues = [];
  scriptActiveIdx = -1;

  // Guard against overlapping/stale loads (e.g. fast track switches): only
  // the most recent call may commit its results.
  const myId = ++scriptLoadId;

  // Show a loading placeholder — extraction from large files can take
  // several seconds the first time, and a blank panel looks broken.
  wrap.hidden = false;
  $("marks-hint").hidden = false;
  list.innerHTML = '<div class="script-loading muted">Extracting subtitles… (first load can take a few seconds for large files)</div>';

  let url = `/api/items/subtitles?fmt=json&path=${encodeURIComponent(path)}`;
  if (track !== null && track !== undefined) url += `&track=${track}`;
  let cues;
  try {
    cues = await getJSON(url);
  } catch {
    if (myId === scriptLoadId) {
      list.innerHTML = '<div class="script-loading muted">Could not load subtitles.</div>';
    }
    return;
  }
  if (myId !== scriptLoadId) return;  // a newer load superseded us
  scriptCues = cues;
  if (!scriptCues.length) {
    list.innerHTML = '<div class="script-loading muted">No subtitle cues found.</div>';
    return;
  }

  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  scriptCues.forEach((cue, i) => {
    const row = document.createElement("div");
    row.className = "script-line";
    row.dataset.idx = i;
    const speaker = cue.speaker
      ? `<span class="script-speaker">${escapeHtml(cue.speaker)}</span>` : "";
    row.innerHTML =
      `<span class="script-ts">${formatTime(cue.start)}</span>` +
      `<span class="script-text">${speaker}${escapeHtml(cue.text)}</span>` +
      `<span class="script-actions">` +
        `<button class="script-mark-btn script-start-btn" title="Set clip start">Start</button>` +
        `<button class="script-mark-btn script-end-btn" title="Set clip end">End</button>` +
      `</span>`;
    row.onclick = () => scriptLineClick(i);
    row.querySelector(".script-start-btn").onclick = (e) => { e.stopPropagation(); setClipInAt(i); };
    row.querySelector(".script-end-btn").onclick = (e) => { e.stopPropagation(); setClipOutAt(i); };
    frag.appendChild(row);
  });
  list.appendChild(frag);

  // Pause auto-scroll when the user manually scrolls, resume after 5 s idle.
  list.addEventListener("scroll", onScriptManualScroll, { passive: true });

  // Auto-select the matching cue range when this item was opened from a
  // dialogue search (consumed once — track switches afterward don't re-select).
  if (pendingCueSelect) {
    const { start, end } = pendingCueSelect;
    pendingCueSelect = null;
    selectCueRange(start, end);
  }
}

/** Index of the cue whose `edge` ("start"|"end") time is nearest to `t`. */
function nearestCueIndex(t, edge) {
  let best = -1, bestDelta = Infinity;
  for (let i = 0; i < scriptCues.length; i++) {
    const delta = Math.abs((edge === "end" ? scriptCues[i].end : scriptCues[i].start) - t);
    if (delta < bestDelta) { bestDelta = delta; best = i; }
  }
  return best;
}

/** Select the cue range covering [start, end] in Marks and scroll it in view. */
function selectCueRange(start, end) {
  if (!scriptCues.length) return;
  const first = nearestCueIndex(start, "start");
  let last = nearestCueIndex(end, "end");
  if (first < 0) return;
  if (last < first) last = first;
  clipFirst = first;
  clipLast = last;
  updateClipUI();  // sets the range, clears any match, highlights cues yellow
  // Center the selection in the script panel.
  const list = $("script-list");
  const row = list.children[first];
  if (row) {
    scriptProgScroll = true;
    const listRect = list.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    list.scrollTo({
      top: list.scrollTop + (rowRect.top - listRect.top) - listRect.height / 2,
      behavior: "smooth",
    });
    setTimeout(() => { scriptProgScroll = false; }, 600);
  }
}

/** Handle manual scroll — pause auto-scroll, resume after idle timeout. */
function onScriptManualScroll() {
  if (scriptProgScroll) return; // ignore our own programmatic scrolls
  scriptAutoScroll = false;
  clearTimeout(scriptScrollTimer);
  scriptScrollTimer = setTimeout(() => { scriptAutoScroll = true; }, 5000);
}

/** Click handler for a script line — seek to that cue's start time. */
function scriptLineClick(idx) {
  const cue = scriptCues[idx];
  if (!cue) return;
  seekTo(cue.start);
  // Re-enable auto-scroll since user interacted with the script
  scriptAutoScroll = true;
  clearTimeout(scriptScrollTimer);
}

/** Called on timeupdate — highlight the active cue and auto-scroll. */
function updateScript() {
  if (!scriptCues.length) return;
  const t = playerTime();
  let idx = -1;
  for (let i = scriptCues.length - 1; i >= 0; i--) {
    if (t >= scriptCues[i].start - 0.15) {
      idx = i;
      break;
    }
  }
  if (idx === scriptActiveIdx) return;
  scriptActiveIdx = idx;

  const list = $("script-list");
  const prev = list.querySelector(".script-line.active");
  if (prev) prev.classList.remove("active");

  if (idx >= 0) {
    const row = list.children[idx];
    if (row) {
      row.classList.add("active");
      if (scriptAutoScroll) {
        scriptProgScroll = true;
        const listRect = list.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        const targetTop = list.scrollTop + (rowRect.top - listRect.top) - listRect.height / 3;
        list.scrollTo({ top: targetTop, behavior: "smooth" });
        setTimeout(() => { scriptProgScroll = false; }, 600);
      }
    }
  }
}

/** Highlight the selected clip range on the script. */
function updateScriptHighlight() {
  const list = $("script-list");
  if (!list || !scriptCues.length) return;
  for (let i = 0; i < scriptCues.length; i++) {
    const row = list.children[i];
    if (!row) continue;
    const inRange = hasClipRange() && i >= clipFirst && i <= clipLast;
    row.classList.toggle("in-range", inRange);
    row.classList.toggle("range-first", i === clipFirst && hasClipRange());
    row.classList.toggle("range-last", i === clipLast && hasClipRange());
  }
}

$("player").addEventListener("timeupdate", updateScript);

// --- bookmarks --------------------------------------------------------------

async function loadBookmarks() {
  if (!currentItem) return;
  const item = currentItem;  // guard against an item switch during the fetch
  const list = $("bookmarks-list");
  const empty = $("bookmarks-empty");
  list.innerHTML = "";
  empty.hidden = true;

  let data;
  try {
    data = await getJSON(`/api/bookmarks${qp(item.path)}`);
  } catch { return; }

  // A newer item was opened while this request was in flight — its own
  // loadBookmarks owns the list now; don't overwrite it with stale bookmarks.
  if (currentItem !== item) return;

  if (!data.bookmarks.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  for (const bm of data.bookmarks) {
    const li = document.createElement("li");
    li.className = "bookmark-item";
    const [bs, be] = bmBuffered(bm);
    li.innerHTML =
      `<span class="bookmark-label">${escapeHtml(bm.label)}</span>` +
      `<span class="bookmark-range">${formatTime(bs)} – ${formatTime(be)}</span>` +
      bmBufferControls(bm) +
      `<button class="bookmark-use" title="Load as clip range">Clip</button>` +
      `<button class="bookmark-seek" title="Seek to start">▶</button>` +
      `<button class="bookmark-del" title="Delete">✕</button>`;
    wireBmBufferControls(li, bm, () => loadBookmarks());
    li.querySelector(".bookmark-use").onclick = (e) => {
      e.stopPropagation();
      useBookmarkForClip(bm);
    };
    li.querySelector(".bookmark-seek").onclick = (e) => {
      e.stopPropagation();
      seekTo(bs);
    };
    li.querySelector(".bookmark-del").onclick = (e) => {
      e.stopPropagation();
      deleteBookmark(bm.id);
    };
    list.appendChild(li);
  }
}

/** Buffered [start, end] of a bookmark (cue range ± its saved before/after). */
function bmBuffered(bm) {
  return [Math.max(0, bm.start - (bm.before || 0)), bm.end + (bm.after || 0)];
}

/** Inline before/after editors for a bookmark row (PATCH on change). */
function bmBufferControls(bm) {
  return (
    `<span class="bm-buffer" title="Padding around the bookmark (seconds)">` +
    `<label>−<input class="bm-before" type="number" min="0" max="60" step="0.5" value="${bm.before || 0}" /></label>` +
    `<label>+<input class="bm-after" type="number" min="0" max="60" step="0.5" value="${bm.after || 0}" /></label>` +
    `</span>`
  );
}

function wireBmBufferControls(li, bm, onSaved) {
  const before = li.querySelector(".bm-before");
  const after = li.querySelector(".bm-after");
  const save = async (e) => {
    e.stopPropagation();
    const b = parseFloat(before.value) || 0;
    const a = parseFloat(after.value) || 0;
    try {
      await fetch(`/api/bookmarks/${bm.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ before: b, after: a }),
      });
      bm.before = b; bm.after = a;
      if (onSaved) onSaved();
    } catch {}
  };
  before.onchange = save;
  after.onchange = save;
  before.onclick = (e) => e.stopPropagation();
  after.onclick = (e) => e.stopPropagation();
}

function useBookmarkForClip(bm) {
  clipRangeStart = bm.start;
  clipRangeEnd = bm.end;
  // No cue is selected in the script for a bookmark-loaded range, so remember the
  // bookmark's dialogue for the {cue} naming token (makeClip falls back to it).
  loadedBookmarkCue = bm.cue || null;
  // Restore the bookmark's saved buffer into the inputs (B16).
  $("clip-before").value = bm.before ?? 0;
  $("clip-after").value = bm.after ?? 0;
  // Restore the saved stream selection (B17), best-effort (only if the option
  // exists for this file).
  restoreSelect("ss-subs", bm.sub_track);
  restoreSelect("ss-audio", bm.audio_track);
  restoreSelect("ss-chan", bm.channel_subset);
  showClipTarget(true);
  const bs = Math.max(0, bm.start - (bm.before || 0));
  const be = bm.end + (bm.after || 0);
  $("clip-range-display").textContent =
    `"${bm.label}" — ${formatTime(bs)} – ${formatTime(be)}`;
}

/** Set a <select> to a saved value if that option exists, firing change. */
function restoreSelect(id, value) {
  if (value == null) return;
  const sel = $(id);
  const v = String(value);
  if (sel && [...sel.options].some((o) => o.value === v) && sel.value !== v) {
    sel.value = v;
    sel.dispatchEvent(new Event("change"));
  }
}

async function deleteBookmark(id) {
  try {
    await fetch(`/api/bookmarks/${id}`, { method: "DELETE" });
    loadBookmarks();
  } catch {}
}

// --- clipping ---------------------------------------------------------------

let activeJobId = null;
let clipRangeStart = null;
let clipRangeEnd = null;
let loadedBookmarkCue = null;  // {cue} fallback when a bookmark range is loaded

// Enable the Make-clip button + reveal the name/template row when there's a
// clip target (a cue selection or a loaded bookmark); hide otherwise.
function showClipTarget(on) {
  $("clip-btn").disabled = !on;
  $("clip-name-row").hidden = !on;
}

// Map a channel-group label from the stream selector to a clip split category.
const CHAN_CATEGORY = {
  front: "front", center: "center", lfe: "lfe",
  side: "surround", back: "surround", surround: "surround",
};
const chanCategory = (g) => CHAN_CATEGORY[g] || g;

// Compact clip status next to the ★/✂ buttons: spinner while cutting, then a
// download button (or ✕ on failure). The detailed bottom panel still shows too.
function setClipIndicator(state, opts = {}) {
  const el = $("mark-clip-indicator");
  if (!el) return;
  if (state === "hidden") { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  if (state === "processing") {
    el.innerHTML = '<span class="clip-spinner" title="Cutting clip…"></span>';
  } else if (state === "done") {
    el.innerHTML = `<a class="clip-dl-mini" href="${opts.href}" download ` +
      `title="Download ${escapeHtml(opts.name || "clip")}" aria-label="Download clip">⤓</a>`;
  } else if (state === "failed") {
    el.innerHTML = `<span class="clip-fail-mini" title="${escapeHtml(opts.error || "Clip failed")}">✕</span>`;
  }
}

async function makeClip() {
  if (!currentItem) return;
  // Need a selected clip range (cue selection or loaded bookmark).
  if (clipRangeStart === null || clipRangeEnd === null) return;
  setClipIndicator("processing");

  const fmt = $("clip-format").value;
  // The stream selector drives the output (B17): a channel subset extracts one
  // group (split → audio-only re-encode); the audio stream picks the track.
  const chan = getSelectedChan();
  const chanSubset = chan && chan !== "whole";
  const splitCh = chanSubset;
  // An audio-only source can only yield an audio clip (there's no video stream).
  const audioOnly = currentItem.audioOnly || $("clip-audio-only").checked || chanSubset;
  const kind = audioOnly ? "audio" : "video";
  const aud = getSelectedAudio();
  // "Exact (re-encode)" (default on) re-encodes video so the clip is frame-tight:
  // a lossless copy can only start on a keyframe, so it runs long on long-GOP
  // sources. Audio passthrough is already frame-exact, so this only affects video.
  const reencodeVideo = $("clip-exact").checked && kind === "video";

  const body = {
    path: currentItem.path,
    kind,
    lossless: !reencodeVideo && fmt === "lossless",
    before: parseFloat($("clip-before").value) || 0,
    after: parseFloat($("clip-after").value) || 0,
    backend: "auto",
    embed_subs: kind === "video",  // implicit (B17): always embed for video
    split_channels: splitCh,
    split_format: fmt === "lossless" ? "wav" : fmt,
    split_groups: splitCh ? [chanCategory(chan)] : undefined,
    // Audio + WAV/FLAC without splitting = full-mix lossless (keeps 5.1, etc.).
    audio_format: (audioOnly && !splitCh && (fmt === "wav" || fmt === "flac")) ? fmt : undefined,
    // Selected audio stream from the stream selector.
    audio_tracks: aud != null ? [aud] : undefined,
    // For a video clip, make the selected embedded subtitle the default track.
    default_sub_track: (kind === "video" && getSelectedTrack() != null)
      ? parseInt(getSelectedTrack()) : undefined,
    template: clipTemplate(),
  };

  body.start = clipRangeStart;
  body.end = clipRangeEnd;
  // The first selected cue's dialogue drives the {cue} naming token.
  // The {cue} naming token: the selected cue's dialogue, or — when a bookmark
  // range is loaded (no cue selected in the script) — the bookmark's saved cue.
  if (clipFirst >= 0 && scriptCues[clipFirst]) body.cue_text = scriptCues[clipFirst].text;
  else if (loadedBookmarkCue) body.cue_text = loadedBookmarkCue;

  // A label for the Clips list: the matched dialogue, else the time range.
  const label = body.cue_text
    ? `"${body.cue_text.length > 70 ? body.cue_text.slice(0, 67) + "…" : body.cue_text}"`
    : `${formatTime(clipRangeStart)} – ${formatTime(clipRangeEnd)}`;
  try {
    const data = await postJSON("/api/clip", body);
    activeJobId = data.job_id;  // the inline indicator tracks the latest clip
    pollClipJob(data.job_id, addClipRow(data.job_id, label));
  } catch (err) {
    const row = addClipRow(null, label);
    row.querySelector(".clip-made-status").innerHTML =
      `<span class="job-failed">Failed: ${escapeHtml(err.message)}</span>`;
    setClipIndicator("failed", { error: err.message });
  }
}

// Append a row to the per-page Clips list (newest first) and return it.
function addClipRow(jobId, label) {
  $("clips-made-panel").hidden = false;
  const li = document.createElement("li");
  li.className = "clip-made-row";
  if (jobId) li.dataset.jobId = jobId;
  li.innerHTML =
    `<span class="clip-made-title">${escapeHtml(label)}</span>` +
    `<span class="clip-made-status"><span class="job-running">Queued…</span></span>`;
  const list = $("clips-made-list");
  list.insertBefore(li, list.firstChild);
  return li;
}

// Poll one clip job and update its row. Stops if the row is cleared (new item).
// Updates the inline ★/✂ indicator only for the most recent clip (activeJobId).
function pollClipJob(jobId, row) {
  if (!row.isConnected) return;
  getJSON(`/api/jobs/${jobId}`)
    .then((job) => {
      if (!row.isConnected) return;
      const statusEl = row.querySelector(".clip-made-status");
      const isLatest = jobId === activeJobId;
      if (job.status === "queued" || job.status === "running") {
        const elapsed = job.started ? ` (${Math.round(Date.now() / 1000 - job.started)}s)` : "";
        statusEl.innerHTML = `<span class="job-running">Processing${elapsed}…</span>`;
        setTimeout(() => pollClipJob(jobId, row), 1000);
      } else if (job.status === "done") {
        let html = `<span class="job-done">✓</span>`;
        for (const f of job.files || []) {
          const size = f.size > 1048576
            ? (f.size / 1048576).toFixed(1) + " MB"
            : (f.size / 1024).toFixed(0) + " KB";
          html += ` <a class="download-link" href="/api/jobs/${jobId}/download/${encodeURIComponent(f.name)}" download>${escapeHtml(f.name)} (${size})</a>`;
        }
        statusEl.innerHTML = html;
        const first = job.files && job.files[0];
        if (isLatest) {
          if (first) setClipIndicator("done", {
            href: `/api/jobs/${jobId}/download/${encodeURIComponent(first.name)}`, name: first.name });
          else setClipIndicator("hidden");
        }
      } else {
        statusEl.innerHTML = `<span class="job-failed">Failed: ${escapeHtml(job.error || "unknown error")}</span>`;
        if (isLatest) setClipIndicator("failed", { error: job.error });
      }
    })
    .catch(() => { if (row.isConnected) setTimeout(() => pollClipJob(jobId, row), 2000); });
}

$("clip-btn").onclick = makeClip;

// --- clip name template (B9) ------------------------------------------------
const DEFAULT_CLIP_TEMPLATE = "{source}/{timestamp}_{cue}_{title}";
const CLIP_TEMPLATE_KEY = "qc_clip_template";

/** The effective name template for a given input field: its value, else the
 *  remembered one, then the default. Used by both single and batch clips. */
function clipTemplateFrom(el) {
  const fromField = el ? el.value.trim() : "";
  if (fromField) return fromField;
  const saved = (localStorage.getItem(CLIP_TEMPLATE_KEY) || "").trim();
  return saved || DEFAULT_CLIP_TEMPLATE;
}
function clipTemplate() { return clipTemplateFrom($("clip-template")); }

// All template fields (the in-item one + each batch bar's) share one remembered
// value: prefill them from it, and on edit save + mirror to the others so the
// template stays consistent wherever you set it.
(() => {
  const fields = () => [...document.querySelectorAll(".clip-template-input")];
  const init = (localStorage.getItem(CLIP_TEMPLATE_KEY) || "").trim() || DEFAULT_CLIP_TEMPLATE;
  for (const el of fields()) {
    el.value = init;
    el.addEventListener("input", () => {
      const v = el.value.trim();
      if (v) localStorage.setItem(CLIP_TEMPLATE_KEY, v);
      else localStorage.removeItem(CLIP_TEMPLATE_KEY);
      for (const other of fields()) if (other !== el) other.value = el.value;
    });
  }
})();

// Preferred subtitle language(s) override (B14): remember edits and re-open the
// current item so the new preference re-drives auto-selection.
(() => {
  const el = $("ss-langs");
  if (!el) return;
  el.value = subtitleLangs();
  el.addEventListener("change", () => {
    const v = el.value.trim();
    if (v) localStorage.setItem(SUB_LANGS_KEY, v);
    else localStorage.removeItem(SUB_LANGS_KEY);
    if (currentItem) openItem(currentItem.path, currentItem.name, {});
  });
})();

// Clip option interactions

/** Enable/disable the Passthrough option on a format select.
 *  Splitting channels always decodes the audio, so passthrough (no re-encode)
 *  is impossible; grey it out and fall back to WAV. */
function setPassthroughAllowed(sel, allowed) {
  const opt = sel.querySelector('option[value="lossless"]');
  if (!opt) return;
  opt.disabled = !allowed;
  opt.title = allowed ? "" : "Splitting channels requires decoding — passthrough unavailable";
  if (!allowed && sel.value === "lossless") sel.value = "wav";
}


// --- clips library ----------------------------------------------------------

function showClips() {
  stopPlayer();
  $("browser").hidden = true;
  $("item").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("clips-browser").hidden = false;
}

let currentClipsFolder = null;

async function browseClips(folder) {
  showClips();
  currentClipsFolder = folder;
  const list = $("clips-entries");
  const empty = $("clips-empty");
  list.innerHTML = "";
  empty.hidden = true;
  $("clips-folder-crumb").textContent = folder ? ` / ${folder}` : "";
  $("clips-batch").hidden = true;
  $("clips-select-all").checked = false;
  $("clips-batch-status").textContent = "";

  let data;
  try {
    const url = folder ? `/api/clips?folder=${encodeURIComponent(folder)}` : "/api/clips";
    data = await getJSON(url);
  } catch (err) {
    list.innerHTML = `<li class="error">Could not load clips: ${escapeHtml(err.message)}</li>`;
    return;
  }

  if (!data.folders.length && !data.clips.length) {
    empty.hidden = false;
    return;
  }

  for (const f of data.folders) {
    const li = document.createElement("li");
    li.className = "entry dir";
    li.innerHTML = `<span class="icon">📁</span><span class="label">${escapeHtml(f)}</span>`;
    li.onclick = () => browseClips(f);
    list.appendChild(li);
  }

  for (const c of data.clips) {
    const li = document.createElement("li");
    li.className = "entry video";
    const size = c.size > 1048576
      ? (c.size / 1048576).toFixed(1) + " MB"
      : (c.size / 1024).toFixed(0) + " KB";
    li.innerHTML =
      `<input type="checkbox" class="entry-select clip-select" title="Select for batch export" />` +
      `<span class="icon">🎬</span>` +
      `<span class="label">${escapeHtml(c.name)}</span>` +
      `<span class="badge">${size}</span>` +
      `<a class="download-link clip-dl" href="${escapeHtml(c.download_url)}" download>Download</a>`;
    const cb = li.querySelector(".clip-select");
    cb.dataset.path = c.path;
    cb.onclick = (e) => { e.stopPropagation(); updateBatchState("clips"); };
    li.onclick = (e) => {
      if (e.target.closest("a")) return; // let download link work normally
      openItem(c.path, c.name, { selectWholeClip: true });
    };
    list.appendChild(li);
  }

  // Batch export bar only when there are clips to select.
  if (data.clips.length) $("clips-batch").hidden = false;
}

$("clips-link").onclick = () => browseClips(null);
$("clips-home").onclick = () => browseClips(null);
$("clips-back-lib").onclick = () => browse(null);

// --- bookmarks browser ------------------------------------------------------

function showBookmarksBrowser() {
  stopPlayer();
  $("browser").hidden = true;
  $("item").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = false;
}

async function browseBookmarks() {
  showBookmarksBrowser();
  const list = $("all-bookmarks-list");
  const empty = $("all-bookmarks-empty");
  list.innerHTML = "";
  empty.hidden = true;
  $("bm-batch").hidden = true;
  $("bm-select-all").checked = false;
  $("bm-batch-status").textContent = "";

  let data;
  try {
    data = await getJSON("/api/bookmarks");
  } catch (err) {
    list.innerHTML = `<li class="error">Could not load bookmarks: ${escapeHtml(err.message)}</li>`;
    return;
  }

  if (!data.bookmarks.length) {
    empty.hidden = false;
    return;
  }
  $("bm-batch").hidden = false;

  // Group by source path
  const groups = new Map();
  for (const bm of data.bookmarks) {
    if (!groups.has(bm.path)) groups.set(bm.path, []);
    groups.get(bm.path).push(bm);
  }

  for (const [path, bookmarks] of groups) {
    const fileName = path.split("/").pop() || path;
    const header = document.createElement("li");
    header.className = "bm-browser-group";
    header.innerHTML =
      `<span class="bm-browser-file clickable" title="Open this video">${escapeHtml(fileName)}</span>`;
    // Clicking the filename opens the source video.
    header.querySelector(".bm-browser-file").onclick = () => openItem(path, fileName);
    list.appendChild(header);

    for (const bm of bookmarks) {
      const li = document.createElement("li");
      li.className = "bm-browser-item";
      const [bs, be] = bmBuffered(bm);
      li.innerHTML =
        `<input type="checkbox" class="entry-select bm-select" title="Select for batch export" />` +
        `<span class="bookmark-label clickable" title="Open the video with this bookmark loaded for clipping">${escapeHtml(bm.label)}</span>` +
        `<span class="bookmark-range">${formatTime(bs)} – ${formatTime(be)}</span>` +
        bmBufferControls(bm) +
        `<button class="bookmark-seek" title="Open & seek">▶</button>` +
        `<button class="bookmark-del" title="Delete">✕</button>`;
      const cb = li.querySelector(".bm-select");
      cb.dataset.path = bm.path;
      cb.dataset.start = bm.start;
      cb.dataset.end = bm.end;
      cb.dataset.before = bm.before || 0;
      cb.dataset.after = bm.after || 0;
      cb.dataset.cue = bm.cue || "";
      cb.onclick = (e) => { e.stopPropagation(); updateBatchState("bm"); };
      wireBmBufferControls(li, bm, () => browseBookmarks());
      // Clicking the bookmark name opens the video with this bookmark preloaded
      // for clipping.
      li.querySelector(".bookmark-label").onclick = (e) => {
        e.stopPropagation();
        pendingBookmarkClip = bm;  // set before openItem — consumed in its prologue
        openItem(path, fileName, { seekTo: bm.start });
      };
      li.querySelector(".bookmark-seek").onclick = (e) => {
        e.stopPropagation();
        openItem(path, fileName, { seekTo: bm.start });
      };
      li.querySelector(".bookmark-del").onclick = async (e) => {
        e.stopPropagation();
        try {
          await fetch(`/api/bookmarks/${bm.id}`, { method: "DELETE" });
          browseBookmarks();
        } catch {}
      };
      list.appendChild(li);
    }
  }
}

// When navigating from bookmarks browser to an item with "Clip", we need to
// apply the bookmark range after the item finishes loading.
let pendingBookmarkClip = null;

$("bookmarks-link").onclick = () => browseBookmarks();
$("all-bm-back-lib").onclick = () => browse(null);

// --- batch export (clips & bookmarks browsers) -------------------------------
//
// Both browsers share the same machinery, parameterized by an id prefix:
//   "clips" → #clips-batch bar + .clip-select checkboxes in #clips-entries
//   "bm"    → #bm-batch bar + .bm-select checkboxes in #all-bookmarks-list

const BATCH = {
  clips: { listId: "clips-entries", boxClass: "clip-select" },
  bm: { listId: "all-bookmarks-list", boxClass: "bm-select" },
  // Dialogue-search hits in #entries — clip or bookmark straight from results.
  search: { listId: "entries", boxClass: "search-select", verb: "Clip" },
};

function batchBoxes(prefix, checkedOnly) {
  const sel = `#${BATCH[prefix].listId} .${BATCH[prefix].boxClass}` + (checkedOnly ? ":checked" : "");
  return [...document.querySelectorAll(sel)];
}

function updateBatchState(prefix) {
  const n = batchBoxes(prefix, true).length;
  const verb = BATCH[prefix].verb || "Export";
  const btn = $(`${prefix}-batch-export`);
  btn.disabled = n === 0;
  btn.textContent = n ? `${verb} selected (${n})` : `${verb} selected`;
  // Optional second action (the search bar also offers "Bookmark selected").
  const bm = $(`${prefix}-batch-bookmark`);
  if (bm) { bm.disabled = n === 0; bm.textContent = n ? `Bookmark selected (${n})` : "Bookmark selected"; }
}

// Channel groups a split can export (categories the backend understands), in
// display order. A 5.1/7.1 source maps onto these; missing ones are skipped.
const CHAN_GROUPS = [
  ["front", "Front"], ["center", "Center"], ["surround", "Surround"], ["lfe", "LFE"],
];

function wireBatchBar(prefix) {
  // Select all
  $(`${prefix}-select-all`).onchange = (e) => {
    batchBoxes(prefix).forEach((cb) => { cb.checked = e.target.checked; });
    updateBatchState(prefix);
  };
  // Channel-group picker (which channels to export when splitting). Populated
  // here so each bar shares one definition; all checked = every group (default).
  const chanWrap = $(`${prefix}-chan-groups`);
  if (chanWrap) {
    // Default to Center only — the common case (e.g. isolating dialogue from a
    // 5.1 mix). Tick the others to also export Front / Surround / LFE.
    chanWrap.innerHTML = `<span class="chan-groups-label">Channels:</span>` +
      CHAN_GROUPS.map(([cat, label]) =>
        `<label class="checkbox-label"><input type="checkbox" id="${prefix}-chan-${cat}"${cat === "center" ? " checked" : ""} /> ${label}</label>`
      ).join("");
  }
  // Split channels is a sub-option of Audio only (same as the clip panel):
  // enabled only when Audio only is checked, forced off otherwise. The channel
  // picker shows only while splitting.
  const applyGuard = () => {
    const audioOnly = $(`${prefix}-batch-audio`).checked;
    const split = $(`${prefix}-batch-split`);
    split.disabled = !audioOnly;
    if (!audioOnly) split.checked = false;
    if (chanWrap) chanWrap.hidden = !split.checked;
    setPassthroughAllowed($(`${prefix}-batch-format`), !split.checked);
  };
  $(`${prefix}-batch-audio`).onchange = applyGuard;
  $(`${prefix}-batch-split`).onchange = applyGuard;
  applyGuard();  // set initial disabled state
}
wireBatchBar("clips");
wireBatchBar("bm");
wireBatchBar("search");

/** Read the shared export options from a batch bar. */
function batchOptions(prefix) {
  const audioOnly = $(`${prefix}-batch-audio`).checked;
  const split = $(`${prefix}-batch-split`).checked;
  const fmt = $(`${prefix}-batch-format`).value;
  const kind = audioOnly || split ? "audio" : "video";
  // Exact (re-encode) — video only; audio passthrough is already frame-exact.
  // The control is optional (only the bookmarks bar has it).
  const exactEl = $(`${prefix}-batch-exact`);
  const reencodeVideo = exactEl != null && exactEl.checked && kind === "video";
  // Which channel groups to export when splitting (e.g. just Center). All boxes
  // checked = every group (same as omitting it); none checked falls back to all.
  let splitGroups;
  if (split) {
    const cats = CHAN_GROUPS.map(([c]) => c).filter((c) => {
      const el = $(`${prefix}-chan-${c}`);
      return el && el.checked;
    });
    if (cats.length && cats.length < CHAN_GROUPS.length) splitGroups = cats;
  }
  return {
    kind,
    lossless: !reencodeVideo && fmt === "lossless",
    backend: "auto",
    embed_subs: !(audioOnly || split),
    split_channels: split,
    split_groups: splitGroups,
    split_format: fmt === "lossless" ? "wav" : fmt,
    // Full-mix lossless WAV/FLAC (keeps 5.1) when audio + not splitting.
    audio_format: (!split && (fmt === "wav" || fmt === "flac")) ? fmt : undefined,
    // This bar's name template (e.g. a leading folder to collect the batch).
    template: clipTemplateFrom($(`${prefix}-batch-template`)),
  };
}

/** Poll a set of job ids until all finish, updating a status element. */
function pollBatch(jobIds, statusEl, onDone) {
  const total = jobIds.length;
  if (!total) { statusEl.textContent = ""; if (onDone) onDone(); return; }
  const tick = async () => {
    let done = 0, failed = 0;
    for (const id of jobIds) {
      try {
        const j = await getJSON(`/api/jobs/${id}`);
        if (j.status === "done") done++;
        else if (j.status === "failed") failed++;
      } catch {}
    }
    if (done + failed >= total) {
      statusEl.textContent = failed
        ? `${done}/${total} exported, ${failed} failed`
        : `${done}/${total} exported ✓ — see the Clips library`;
      if (onDone) onDone();
    } else {
      statusEl.textContent = `Exporting… ${done + failed}/${total}`;
      setTimeout(tick, 1000);
    }
  };
  tick();
}

/** Submit one /api/clip job per selected row; bodyFor maps a checkbox to a body. */
async function runBatchExport(prefix, bodyFor, onDone) {
  const boxes = batchBoxes(prefix, true);
  if (!boxes.length) return;
  const btn = $(`${prefix}-batch-export`);
  const status = $(`${prefix}-batch-status`);
  btn.disabled = true;
  status.textContent = "Submitting…";
  const jobIds = [];
  const failures = [];
  for (const cb of boxes) {
    try {
      const r = await postJSON("/api/clip", bodyFor(cb));
      jobIds.push(r.job_id);
    } catch (err) {
      failures.push(err.message);
    }
  }
  if (failures.length) {
    status.textContent = `${failures.length} failed to submit: ${failures[0]}`;
  }
  pollBatch(jobIds, status, () => {
    btn.disabled = false;
    if (onDone) onDone();
  });
}

$("clips-batch-export").onclick = () => {
  const opts = batchOptions("clips");
  runBatchExport(
    "clips",
    // Whole file: send start=0 with no end — the backend fills in the duration.
    (cb) => ({ path: cb.dataset.path, start: 0, before: 0, after: 0, ...opts }),
    // Refresh so newly exported files appear (exports land in the clips root);
    // restore the status text the refresh would otherwise wipe.
    () => {
      const msg = $("clips-batch-status").textContent;
      browseClips(currentClipsFolder).then(() => {
        $("clips-batch-status").textContent = msg;
      });
    },
  );
};

$("bm-batch-export").onclick = () => {
  const opts = batchOptions("bm");
  // Each bookmark carries its own saved buffer (B16).
  runBatchExport("bm", (cb) => ({
    path: cb.dataset.path,
    start: parseFloat(cb.dataset.start),
    end: parseFloat(cb.dataset.end),
    before: parseFloat(cb.dataset.before) || 0,
    after: parseFloat(cb.dataset.after) || 0,
    // The bookmark's saved dialogue drives the {cue} naming token.
    cue_text: cb.dataset.cue || undefined,
    ...opts,
  }));
};

$("bm-clear-all").onclick = async () => {
  if (!confirm("Delete ALL bookmarks? This cannot be undone.")) return;
  try {
    await fetch("/api/bookmarks", { method: "DELETE" });
    browseBookmarks();
  } catch (err) {
    alert("Failed to clear bookmarks: " + err.message);
  }
};

// Clip / bookmark the selected dialogue-search hits straight from the results.
$("search-batch-export").onclick = () => {
  const opts = batchOptions("search");
  runBatchExport("search", (cb) => ({
    path: cb.dataset.path,
    start: parseFloat(cb.dataset.start),
    end: parseFloat(cb.dataset.end),
    before: 0, after: 0,
    cue_text: cb.dataset.cue || undefined,
    ...opts,
  }));
};

$("search-batch-bookmark").onclick = async () => {
  const boxes = batchBoxes("search", true);
  if (!boxes.length) return;
  const btn = $("search-batch-bookmark");
  const status = $("search-batch-status");
  btn.disabled = true;
  status.textContent = "Saving bookmarks…";
  let ok = 0, fail = 0;
  for (const cb of boxes) {
    try {
      await postJSON("/api/bookmarks", {
        path: cb.dataset.path, label: cb.dataset.cue, cue: cb.dataset.cue,
        start: parseFloat(cb.dataset.start), end: parseFloat(cb.dataset.end),
      });
      ok++;
    } catch { fail++; }
  }
  status.textContent = fail ? `${ok} bookmarked, ${fail} failed` : `${ok} bookmarked ✓`;
  btn.disabled = false;
};

// --- boot -------------------------------------------------------------------

let appConfig = {};
let mediaRoots = [];  // absolute media-root paths, for breadcrumb segmentation

async function loadStatus() {
  try {
    const h = await getJSON("/api/health");
    const tools = Object.entries(h.tools)
      .map(([k, v]) => `${v ? "✓" : "✗"} ${k}`)
      .join("  ");
    $("status").textContent = `${h.service} v${h.version} — ${tools}`;
  } catch (err) {
    $("status").textContent = `backend unreachable: ${err.message}`;
  }
  // Load config (subtitle_langs, hw_encode, etc.)
  try {
    appConfig = await getJSON("/api/config");
    // Show the server default subtitle-language preference as the placeholder.
    if (appConfig.subtitle_langs && $("ss-langs")) {
      $("ss-langs").placeholder = appConfig.subtitle_langs.join(",");
    }
  } catch {}
  // Load media roots so the breadcrumb can offer clickable path segments.
  try {
    const r = await getJSON("/api/library/roots");
    mediaRoots = r.roots || [];
  } catch {}
}

$("back").onclick = () => browse(currentParent());
$("home-link").onclick = () => browse(null);

// Derive the parent directory of the current item for the Back button.
function currentParent() {
  if (!currentItem) return null;
  const p = currentItem.path;
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i > 0 ? p.slice(0, i) : null;
}

loadStatus();
browse(null);
