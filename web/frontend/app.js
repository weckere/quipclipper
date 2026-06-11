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
// When a library search surfaces folders, holds their paths so dialogue
// search can span all of them. null = search the current folder only.
let dialogueSearchScope = null;

async function browse(path) {
  showBrowser();
  currentBrowsePath = path;
  dialogueSearchScope = null;
  const list = $("entries");
  list.innerHTML = "";
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
    li.className = "entry " + (e.is_dir ? "dir" : "video");
    const icon = e.is_dir ? "📁" : "🎬";
    const tag = !e.is_dir && e.has_sidecar ? ' <span class="badge">sub</span>' : "";
    li.innerHTML = `<span class="icon">${icon}</span><span class="label">${escapeHtml(e.name)}</span>${tag}`;
    li.onclick = () => (e.is_dir ? browse(e.path) : openItem(e.path, e.name));
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

  let url = `/api/library/search?query=${encodeURIComponent(query)}`;
  if (currentBrowsePath) url += `&path=${encodeURIComponent(currentBrowsePath)}`;

  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    list.innerHTML = `<li class="error">Search failed: ${escapeHtml(err.message)}</li>`;
    return;
  }
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
    $("browser-empty").textContent = "No dialogue matches found.";
    $("browser-empty").hidden = false;
    return;
  }

  for (const hit of data.matches) {
    const li = document.createElement("li");
    li.className = "dialogue-hit";
    li.innerHTML = `
      <span class="hit-file">${escapeHtml(hit.file)}</span>
      <span class="hit-text">"${escapeHtml(hit.text)}"</span>
      <span class="hit-time">${hit.start_ts} – ${hit.end_ts}  <span class="hit-score">${hit.score}%</span></span>
    `;
    li.onclick = () => openItem(hit.path, hit.file, { searchQuery: query, seekTo: hit.start });
    list.appendChild(li);
  }
}

$("dialogue-search-btn").addEventListener("click", dialogueSearch);
$("dialogue-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") dialogueSearch();
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
$("reindex-item-btn").addEventListener("click", async () => {
  if (!currentItem) return;
  const btn = $("reindex-item-btn");
  const status = $("reindex-item-status");
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

let currentItem = null;
let transcodeOffset = 0;  // ffmpeg -ss offset for current transcode segment

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

async function openItem(path, name, opts) {
  const { searchQuery, seekTo: seekTarget } = opts || {};
  showItem();
  // Consume any pending bookmark clip immediately so an early return (e.g.
  // probe failure) can't leak it into the next item opened.
  const bookmarkClip = pendingBookmarkClip;
  pendingBookmarkClip = null;
  currentItem = { path, name };
  $("item-name").textContent = name;
  $("subs-controls").textContent = "Loading…";
  $("streams").innerHTML = "";
  $("preview-note").textContent = "";
  $("search-input").value = searchQuery || "";
  $("search-results").innerHTML = "";
  $("search-empty").hidden = true;
  $("clip-panel").hidden = true;
  $("job-panel").hidden = true;
  selectedMatch = null;
  lastSearchQuery = null;
  lastSearchMatches = [];
  activeJobId = null;
  clipRangeStart = null;
  clipRangeEnd = null;
  clipFirst = -1;
  clipLast = -1;
  $("mark-range-display").innerHTML = "";
  $("mark-save").disabled = true;
  scriptCues = [];
  scriptActiveIdx = -1;
  $("script-wrap").hidden = true;
  $("script-list").innerHTML = "";
  if (jobPollTimer) { clearTimeout(jobPollTimer); jobPollTimer = null; }

  let info;
  try {
    info = await getJSON("/api/items" + qp(path));
  } catch (err) {
    $("subs-controls").innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    return;
  }

  const player = $("player");
  const mediaUrl = "/api/media" + qp(path);
  const transcodeUrl = "/api/media/transcode" + qp(path);
  player.querySelectorAll("track").forEach((t) => t.remove());

  // Check if the primary audio codec is browser-playable; if not, use
  // the transcode endpoint from the start (browsers silently ignore
  // unsupported audio codecs like AC3/DTS/FLAC without firing onerror).
  const BROWSER_AUDIO = new Set(["aac", "mp3", "opus", "vorbis"]);
  const primaryAudio = info.streams.find((s) => s.kind === "audio");
  const needsTranscode = primaryAudio && !BROWSER_AUDIO.has(primaryAudio.codec);

  // Transcode seek state
  let isTranscoding = false;
  let settingSrc = false;
  transcodeOffset = 0;
  const seekBar = $("transcode-seek-bar");
  const seekSlider = $("transcode-slider");
  const seekTimeLabel = $("transcode-time");
  const seekDurLabel = $("transcode-duration");
  let probedDuration = info.duration || 0;

  let firstLoad = true;
  function loadTranscode(startTime) {
    isTranscoding = true;
    settingSrc = true;
    transcodeOffset = startTime;
    const autoplay = !firstLoad;
    firstLoad = false;
    let url = transcodeUrl;
    if (startTime > 0) url += `&start=${startTime}`;
    player.src = url;
    player.load();
    player.addEventListener("loadedmetadata", function onMeta() {
      player.removeEventListener("loadedmetadata", onMeta);
      settingSrc = false;
      if (autoplay) player.play().catch(() => {});
    }, { once: true });
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

  // Custom seek bar for transcoded streams (native scrubber can't seek
  // on a non-seekable streaming source).
  function updateSeekBar() {
    if (!isTranscoding || !probedDuration) return;
    const absTime = player.currentTime + transcodeOffset;
    seekSlider.value = (absTime / probedDuration) * 100;
    seekTimeLabel.textContent = formatTime(absTime);
  }

  player.addEventListener("timeupdate", updateSeekBar);

  seekSlider.addEventListener("input", () => {
    const absTime = (seekSlider.value / 100) * probedDuration;
    seekTimeLabel.textContent = formatTime(absTime);
  });

  seekSlider.addEventListener("change", () => {
    if (!isTranscoding) return;
    const absTime = (seekSlider.value / 100) * probedDuration;
    loadTranscode(absTime);
  });

  // Custom event for programmatic seeks (search results, bookmarks, etc.)
  player.addEventListener("transcode-seek", (e) => {
    loadTranscode(e.detail);
  });

  const seekHint = $("transcode-hint");

  if (needsTranscode) {
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    seekSlider.max = 100;
    loadTranscode(seekTarget || 0);
  } else {
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    player.src = mediaUrl;
  }

  player.onerror = () => {
    if (isTranscoding || player.src.includes("/api/media/transcode")) {
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

  renderStreams(info.streams);
  renderSubs(info, path);
  loadBookmarks();

  if (searchQuery) doSearch();
  if (seekTarget != null && !needsTranscode) {
    player.addEventListener("loadedmetadata", () => {
      player.currentTime = seekTarget;
    }, { once: true });
  }

  // Apply pending bookmark clip (when navigating from bookmarks browser)
  if (bookmarkClip) useBookmarkForClip(bookmarkClip);
}

function renderStreams(streams) {
  const ul = $("streams");
  ul.innerHTML = "";
  for (const s of streams) {
    const li = document.createElement("li");
    li.innerHTML = `<code>${escapeHtml(s.selector)}</code> ${escapeHtml(s.label.replace(s.selector, "").trim())}`;
    ul.appendChild(li);
  }
}

function renderSubs(info, path) {
  const box = $("subs-controls");
  box.innerHTML = "";
  const tracks = info.subtitle_tracks || [];
  const hasSubs = tracks.length > 0 || info.has_sidecar;
  // Only offer reindex when there are subtitles to reindex.
  $("reindex-item-row").hidden = !hasSubs;
  $("reindex-item-status").hidden = true;

  if (!hasSubs) {
    box.innerHTML = '<span class="muted">No subtitles found (sidecar or embedded).</span>';
    return;
  }

  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = info.has_sidecar
    ? "Using the sidecar subtitle file."
    : `${tracks.length} embedded subtitle track(s).`;
  box.appendChild(note);

  // A picker when there are embedded tracks; otherwise just load the default.
  if (tracks.length > 1) {
    // The backend is the single source of truth for auto-selection (full
    // dialogue > SDH > forced). Requesting its chosen index means our request
    // matches the cache key that pre-indexing warmed — no duplicate extraction.
    const bestIdx = info.best_track != null ? info.best_track : tracks[0].index;

    const sel = document.createElement("select");
    tracks.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.index;
      opt.textContent = `s:${t.index} ${t.codec}${t.language ? " " + t.language : ""}${t.title ? " — " + t.title : ""}`;
      if (t.index === bestIdx) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.onchange = () => {
      // transcodeOffset is module-level and 0 when not transcoding.
      loadSubtitleTrack(path, sel.value, transcodeOffset);
      loadScript(path, sel.value);
    };
    box.appendChild(sel);
    sel.value = bestIdx;
    loadSubtitleTrack(path, bestIdx);
    loadScript(path, bestIdx);
  } else {
    loadSubtitleTrack(path, null);
    loadScript(path, null);
  }
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

function getSelectedTrack() {
  const sel = $("subs-controls")?.querySelector("select");
  return sel ? sel.value : null;
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
    results.innerHTML = `<li class="error">Search failed: ${err.message}</li>`;
    $("search-btn").disabled = false;
    return;
  }
  $("search-btn").disabled = false;

  if (!data.matches.length) {
    empty.hidden = false;
    return;
  }

  lastSearchQuery = query;
  lastSearchMatches = data.matches;

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
      selectMatchForClip(m);
    };
    results.appendChild(li);
  }
}

// seekTo is defined above (module-level, handles transcode offset)

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
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

// Legacy compat — other parts of the code read markIn/markOut
Object.defineProperty(window, "markIn", { get: clipStart });
Object.defineProperty(window, "markOut", { get: clipEnd });

function setClipIn() {
  if (scriptActiveIdx < 0) return;
  setClipInAt(scriptActiveIdx);
}

function setClipOut() {
  if (scriptActiveIdx < 0) return;
  setClipOutAt(scriptActiveIdx);
}

function setClipInAt(idx) {
  clipFirst = idx;
  if (clipLast < clipFirst) clipLast = clipFirst;
  updateClipUI();
}

function setClipOutAt(idx) {
  clipLast = idx;
  if (clipFirst < 0 || clipFirst > clipLast) clipFirst = clipLast;
  updateClipUI();
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
    // Hide clip panel only if it wasn't opened by a search result
    if (!selectedMatch) $("clip-panel").hidden = true;
  } else {
    const s = clipStart(), e = clipEnd();
    const dur = e - s;
    const nLines = clipLast - clipFirst + 1;
    el.innerHTML =
      `<span class="range-times">${formatTime(s)} – ${formatTime(e)}</span> ` +
      `<span>(${dur.toFixed(1)}s, ${nLines} line${nLines > 1 ? "s" : ""})</span>` +
      ` <button class="mark-clear-inline" title="Clear selection">×</button>`;
    el.querySelector(".mark-clear-inline").onclick = clearClip;
    $("mark-save").disabled = false;
    // Auto-show clip panel and populate range
    selectedMatch = null;
    clipRangeStart = s;
    clipRangeEnd = e;
    $("clip-panel").hidden = false;
    $("clip-range-display").textContent =
      `${formatTime(s)} – ${formatTime(e)} (${dur.toFixed(1)}s)`;
  }
  // Highlight script lines
  updateScriptHighlight();
}

function formatTime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(1);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${sec.padStart(4, "0")}`
    : `${m}:${sec.padStart(4, "0")}`;
}

async function saveBookmark() {
  if (!currentItem || !hasClipRange()) return;
  const s = clipStart(), e = clipEnd();
  // Build default label: time range + first dialogue line
  let defaultLabel = `${formatTime(s)} – ${formatTime(e)}`;
  if (clipFirst >= 0 && scriptCues[clipFirst]) {
    const firstLine = scriptCues[clipFirst].text.slice(0, 60);
    defaultLabel += ` — ${firstLine}`;
  }
  const label = prompt("Bookmark label:", defaultLabel);
  if (label === null) return;
  try {
    await postJSON("/api/bookmarks", {
      path: currentItem.path,
      label: label || defaultLabel,
      start: s,
      end: e,
    });
    loadBookmarks();
  } catch (err) {
    alert("Failed to save bookmark: " + err.message);
  }
}

$("mark-save").onclick = saveBookmark;

// --- subtitle script view ---------------------------------------------------

let scriptCues = [];       // full cue list [{start, end, text}, ...]
let scriptAutoScroll = true;
let scriptScrollTimer = null;
let scriptActiveIdx = -1;  // index of the currently highlighted cue
let scriptProgScroll = false; // true when we're doing a programmatic scroll
let scriptLoadId = 0;      // generation token to ignore stale script loads

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
    row.innerHTML =
      `<span class="script-ts">${formatTime(cue.start)}</span>` +
      `<span class="script-text">${escapeHtml(cue.text)}</span>` +
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
  const list = $("bookmarks-list");
  const empty = $("bookmarks-empty");
  list.innerHTML = "";
  empty.hidden = true;

  let data;
  try {
    data = await getJSON(`/api/bookmarks${qp(currentItem.path)}`);
  } catch { return; }

  if (!data.bookmarks.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  for (const bm of data.bookmarks) {
    const li = document.createElement("li");
    li.className = "bookmark-item";
    li.innerHTML =
      `<span class="bookmark-label">${escapeHtml(bm.label)}</span>` +
      `<span class="bookmark-range">${formatTime(bm.start)} – ${formatTime(bm.end)}</span>` +
      `<button class="bookmark-use" title="Load as clip range">Clip</button>` +
      `<button class="bookmark-seek" title="Seek to start">▶</button>` +
      `<button class="bookmark-del" title="Delete">✕</button>`;
    li.querySelector(".bookmark-use").onclick = (e) => {
      e.stopPropagation();
      useBookmarkForClip(bm);
    };
    li.querySelector(".bookmark-seek").onclick = (e) => {
      e.stopPropagation();
      seekTo(bm.start);
    };
    li.querySelector(".bookmark-del").onclick = (e) => {
      e.stopPropagation();
      deleteBookmark(bm.id);
    };
    list.appendChild(li);
  }
}

function useBookmarkForClip(bm) {
  selectedMatch = null;
  clipRangeStart = bm.start;
  clipRangeEnd = bm.end;
  $("clip-panel").hidden = false;
  $("clip-range-display").textContent =
    `"${bm.label}" — ${formatTime(bm.start)} – ${formatTime(bm.end)}`;
}

async function deleteBookmark(id) {
  try {
    await fetch(`/api/bookmarks/${id}`, { method: "DELETE" });
    loadBookmarks();
  } catch {}
}

// --- clipping ---------------------------------------------------------------

let lastSearchQuery = null;
let lastSearchMatches = [];
let selectedMatch = null;
let activeJobId = null;
let jobPollTimer = null;
let clipRangeStart = null;
let clipRangeEnd = null;

function selectMatchForClip(m) {
  selectedMatch = m;
  clipRangeStart = null;
  clipRangeEnd = null;
  $("clip-panel").hidden = false;
  $("clip-range-display").textContent =
    `"${m.text.length > 80 ? m.text.slice(0, 77) + "…" : m.text}" — ${m.start_ts} – ${m.end_ts}`;
}

async function makeClip() {
  if (!currentItem) return;
  // Need either a search match or explicit range
  if (!selectedMatch && (clipRangeStart === null || clipRangeEnd === null)) return;
  $("clip-btn").disabled = true;
  $("job-panel").hidden = false;
  $("job-status").innerHTML = '<span class="job-running">Submitting…</span>';

  const audioOnly = $("clip-audio-only").checked;
  const fmt = $("clip-format").value;
  const splitCh = $("clip-split-channels").checked;

  const body = {
    path: currentItem.path,
    kind: audioOnly ? "audio" : "video",
    lossless: fmt === "lossless",
    before: parseFloat($("clip-before").value) || 2,
    after: parseFloat($("clip-after").value) || 2,
    backend: "auto",
    embed_subs: !audioOnly && $("clip-embed-subs").checked,
    save_to_library: $("clip-save-lib") ? $("clip-save-lib").checked : false,
    split_channels: splitCh,
    split_format: fmt === "lossless" ? "wav" : fmt,
  };

  if (selectedMatch) {
    body.query = lastSearchQuery;
    body.match_index = selectedMatch.index;
    body.track = getSelectedTrack() !== null ? parseInt(getSelectedTrack()) : null;
  } else {
    body.start = clipRangeStart;
    body.end = clipRangeEnd;
  }

  try {
    const data = await postJSON("/api/clip", body);
    activeJobId = data.job_id;
    $("job-status").innerHTML = '<span class="job-running">Queued…</span>';
    pollJob();
  } catch (err) {
    $("job-status").innerHTML = `<span class="job-failed">Failed: ${escapeHtml(err.message)}</span>`;
    $("clip-btn").disabled = false;
  }
}

function pollJob() {
  if (!activeJobId) return;
  if (jobPollTimer) clearTimeout(jobPollTimer);

  getJSON(`/api/jobs/${activeJobId}`)
    .then((job) => {
      if (job.status === "queued" || job.status === "running") {
        const elapsed = job.started ? `${Math.round(Date.now() / 1000 - job.started)}s` : "";
        $("job-status").innerHTML =
          `<span class="job-running">Processing${elapsed ? " (" + elapsed + ")" : ""}…</span>`;
        jobPollTimer = setTimeout(pollJob, 1000);
      } else if (job.status === "done") {
        let html = `<span class="job-done">Done${job.elapsed ? " (" + job.elapsed + "s)" : ""}!</span>`;
        if (job.files) {
          for (const f of job.files) {
            const size = f.size > 1048576
              ? (f.size / 1048576).toFixed(1) + " MB"
              : (f.size / 1024).toFixed(0) + " KB";
            html += `<br><a class="download-link" href="/api/jobs/${activeJobId}/download/${encodeURIComponent(f.name)}" download>Download ${escapeHtml(f.name)} (${size})</a>`;
          }
        }
        $("job-status").innerHTML = html;
        $("clip-btn").disabled = false;
      } else {
        $("job-status").innerHTML =
          `<span class="job-failed">Failed: ${escapeHtml(job.error || "unknown error")}</span>`;
        $("clip-btn").disabled = false;
      }
    })
    .catch(() => {
      jobPollTimer = setTimeout(pollJob, 2000);
    });
}

$("clip-btn").onclick = makeClip;

// Clip option interactions
function updateClipOptionVisibility() {
  const audioOnly = $("clip-audio-only").checked;
  // Embed subs only makes sense for video
  $("clip-embed-subs").disabled = audioOnly;
  if (audioOnly) $("clip-embed-subs").checked = false;
}
$("clip-audio-only").onchange = updateClipOptionVisibility;

// --- clips library ----------------------------------------------------------

function showClips() {
  stopPlayer();
  $("browser").hidden = true;
  $("item").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("clips-browser").hidden = false;
}

async function browseClips(folder) {
  showClips();
  const list = $("clips-entries");
  const empty = $("clips-empty");
  list.innerHTML = "";
  empty.hidden = true;
  $("clips-folder-crumb").textContent = folder ? ` / ${folder}` : "";

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
      `<span class="icon">🎬</span>` +
      `<span class="label">${escapeHtml(c.name)}</span>` +
      `<span class="badge">${size}</span>` +
      `<a class="download-link clip-dl" href="${escapeHtml(c.download_url)}" download>Download</a>`;
    li.onclick = (e) => {
      if (e.target.closest("a")) return; // let download link work normally
      openItem(c.path, c.name);
    };
    list.appendChild(li);
  }
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
      `<span class="bm-browser-file">${escapeHtml(fileName)}</span>` +
      `<button class="bm-browser-open" title="Open file">Open ▶</button>`;
    header.querySelector(".bm-browser-open").onclick = () => openItem(path, fileName);
    list.appendChild(header);

    for (const bm of bookmarks) {
      const li = document.createElement("li");
      li.className = "bm-browser-item";
      li.innerHTML =
        `<span class="bookmark-label">${escapeHtml(bm.label)}</span>` +
        `<span class="bookmark-range">${formatTime(bm.start)} – ${formatTime(bm.end)}</span>` +
        `<button class="bookmark-use" title="Open & clip">Clip</button>` +
        `<button class="bookmark-seek" title="Open & seek">▶</button>` +
        `<button class="bookmark-del" title="Delete">✕</button>`;
      li.querySelector(".bookmark-use").onclick = (e) => {
        e.stopPropagation();
        // Set before openItem — it consumes the value in its prologue.
        pendingBookmarkClip = bm;
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
  // Load config for save_to_library and jellyfin_enabled flags
  try {
    appConfig = await getJSON("/api/config");
    // Always show the "Save to library" checkbox — it can be toggled per-clip
    $("clip-save-lib-label").hidden = false;
    $("clip-save-lib").checked = appConfig.save_to_library || false;
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
