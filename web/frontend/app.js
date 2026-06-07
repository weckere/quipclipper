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

function showBrowser() {
  $("item").hidden = true;
  $("clips-browser").hidden = true;
  $("browser").hidden = false;
}

function showItem() {
  $("browser").hidden = true;
  $("clips-browser").hidden = true;
  $("item").hidden = false;
}

// --- library browser --------------------------------------------------------

let currentBrowsePath = null;
let librarySearchTimer = null;

async function browse(path) {
  showBrowser();
  currentBrowsePath = path;
  const list = $("entries");
  list.innerHTML = "";
  $("browser-empty").hidden = true;
  $("library-search").value = "";
  $("dialogue-search").value = "";
  $("dialogue-search-status").hidden = true;
  // Show dialogue search bar only when inside a folder (not at root)
  $("dialogue-search-bar").hidden = !path;
  let data;
  try {
    data = await getJSON("/api/library/browse" + (path ? qp(path) : ""));
  } catch (err) {
    list.innerHTML = `<li class="error">Could not browse: ${err.message}</li>`;
    return;
  }

  renderBreadcrumb(path, data.entries);
  renderEntries(data.entries);
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
    li.innerHTML = `<span class="icon">${icon}</span><span class="label">${e.name}</span>${tag}`;
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

  let url = `/api/library/search?query=${encodeURIComponent(query)}`;
  if (currentBrowsePath) url += `&path=${encodeURIComponent(currentBrowsePath)}`;

  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    list.innerHTML = `<li class="error">Search failed: ${err.message}</li>`;
    return;
  }
  renderEntries(data.entries);
}

$("library-search").addEventListener("input", (e) => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => librarySearch(e.target.value), 300);
});

// --- folder dialogue search ------------------------------------------------

async function dialogueSearch() {
  const query = $("dialogue-search").value.trim();
  if (!query || !currentBrowsePath) return;

  const list = $("entries");
  list.innerHTML = "";
  $("browser-empty").hidden = true;
  const status = $("dialogue-search-status");
  status.textContent = "Searching subtitles…";
  status.hidden = false;
  $("dialogue-search-btn").disabled = true;

  const url = `/api/search/folder?path=${encodeURIComponent(currentBrowsePath)}&query=${encodeURIComponent(query)}`;
  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    list.innerHTML = `<li class="error">Dialogue search failed: ${err.message}</li>`;
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
      <span class="hit-file">${hit.file}</span>
      <span class="hit-text">"${hit.text}"</span>
      <span class="hit-time">${hit.start_ts} – ${hit.end_ts}  <span class="hit-score">${hit.score}%</span></span>
    `;
    li.onclick = () => openItem(hit.path, hit.file);
    list.appendChild(li);
  }
}

$("dialogue-search-btn").addEventListener("click", dialogueSearch);
$("dialogue-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") dialogueSearch();
});

function renderBreadcrumb(path, entries) {
  const bc = $("breadcrumb");
  bc.innerHTML = "";
  const root = document.createElement("a");
  root.textContent = "Library";
  root.onclick = () => browse(null);
  bc.appendChild(root);
  if (path) {
    const cur = document.createElement("span");
    cur.textContent = " / " + path;
    cur.className = "muted";
    bc.appendChild(cur);
  }
}

// --- item / inspection view -------------------------------------------------

let currentItem = null;

async function openItem(path, name) {
  showItem();
  currentItem = { path, name };
  $("item-name").textContent = name;
  $("subs-controls").textContent = "Loading…";
  $("streams").innerHTML = "";
  $("preview-note").textContent = "";
  $("search-input").value = "";
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
  markIn = null;
  markOut = null;
  $("mark-in-display").textContent = "—";
  $("mark-out-display").textContent = "—";
  $("mark-clip").disabled = true;
  $("mark-save").disabled = true;
  if (jobPollTimer) { clearTimeout(jobPollTimer); jobPollTimer = null; }

  const player = $("player");
  const mediaUrl = "/api/media" + qp(path);
  const transcodeUrl = "/api/media/transcode" + qp(path);
  player.src = mediaUrl;
  player.querySelectorAll("track").forEach((t) => t.remove());
  player.onerror = () => {
    if (player.src.includes("/api/media/transcode")) {
      $("preview-note").textContent =
        "Preview can't play this file in the browser. " +
        "Dialogue search still works — try the search box below.";
      return;
    }
    // Fall back to on-the-fly transcode
    player.src = transcodeUrl;
    $("preview-note").textContent = "Transcoding for browser playback…";
  };

  let info;
  try {
    info = await getJSON("/api/items" + qp(path));
  } catch (err) {
    $("subs-controls").innerHTML = `<span class="error">${err.message}</span>`;
    return;
  }
  renderStreams(info.streams);
  renderSubs(info, path);
  loadBookmarks();
}

function renderStreams(streams) {
  const ul = $("streams");
  ul.innerHTML = "";
  for (const s of streams) {
    const li = document.createElement("li");
    li.innerHTML = `<code>${s.selector}</code> ${s.label.replace(s.selector, "").trim()}`;
    ul.appendChild(li);
  }
}

function renderSubs(info, path) {
  const box = $("subs-controls");
  box.innerHTML = "";
  const tracks = info.subtitle_tracks || [];

  if (!tracks.length && !info.has_sidecar) {
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
    const sel = document.createElement("select");
    tracks.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.index;
      opt.textContent = `s:${t.index} ${t.codec}${t.language ? " " + t.language : ""}${t.title ? " — " + t.title : ""}`;
      sel.appendChild(opt);
    });
    sel.onchange = () => loadSubtitleTrack(path, sel.value);
    box.appendChild(sel);
    loadSubtitleTrack(path, tracks[0].index);
  } else {
    loadSubtitleTrack(path, null);
  }
}

function loadSubtitleTrack(path, track) {
  const player = $("player");
  player.querySelectorAll("track").forEach((t) => t.remove());
  let url = "/api/items/subtitles" + qp(path);
  if (track !== null && track !== undefined) url += `&track=${track}`;
  const track_el = document.createElement("track");
  track_el.kind = "subtitles";
  track_el.label = "Dialogue";
  track_el.src = url;
  track_el.default = true;
  player.appendChild(track_el);
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

function seekTo(seconds) {
  const player = $("player");
  player.currentTime = seconds;
  player.play().catch(() => {});
}

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

let markIn = null;
let markOut = null;

function setMarkIn() {
  const player = $("player");
  markIn = player.currentTime;
  $("mark-in-display").textContent = formatTime(markIn);
  updateMarkButtons();
}

function setMarkOut() {
  const player = $("player");
  markOut = player.currentTime;
  $("mark-out-display").textContent = formatTime(markOut);
  updateMarkButtons();
}

function updateMarkButtons() {
  const hasRange = markIn !== null && markOut !== null && markOut > markIn;
  $("mark-clip").disabled = !hasRange;
  $("mark-save").disabled = !hasRange;
}

function formatTime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(1);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${sec.padStart(4, "0")}`
    : `${m}:${sec.padStart(4, "0")}`;
}

function clipFromMarks() {
  if (markIn === null || markOut === null || markOut <= markIn) return;
  // Open clip panel with explicit range
  selectedMatch = null;
  $("clip-panel").hidden = false;
  $("clip-range-display").textContent =
    `Manual range: ${formatTime(markIn)} – ${formatTime(markOut)}`;
  clipRangeStart = markIn;
  clipRangeEnd = markOut;
}

async function saveBookmark() {
  if (!currentItem || markIn === null || markOut === null) return;
  const label = prompt("Bookmark label:", `${formatTime(markIn)} – ${formatTime(markOut)}`);
  if (label === null) return; // cancelled
  try {
    await postJSON("/api/bookmarks", {
      path: currentItem.path,
      label: label || `${formatTime(markIn)} – ${formatTime(markOut)}`,
      start: markIn,
      end: markOut,
    });
    loadBookmarks();
  } catch (err) {
    alert("Failed to save bookmark: " + err.message);
  }
}

$("mark-in").onclick = setMarkIn;
$("mark-out").onclick = setMarkOut;
$("mark-clip").onclick = clipFromMarks;
$("mark-save").onclick = saveBookmark;

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

  const body = {
    path: currentItem.path,
    kind: $("clip-kind").value,
    lossless: $("clip-lossless").checked,
    before: parseFloat($("clip-before").value) || 2,
    after: parseFloat($("clip-after").value) || 2,
    backend: $("clip-backend").value,
    embed_subs: $("clip-embed-subs").checked,
    save_to_library: $("clip-save-lib") ? $("clip-save-lib").checked : false,
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

// --- clips library ----------------------------------------------------------

function showClips() {
  $("browser").hidden = true;
  $("item").hidden = true;
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
    list.innerHTML = `<li class="error">Could not load clips: ${err.message}</li>`;
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
      `<a class="download-link clip-dl" href="${c.download_url}" download>Download</a>`;
    list.appendChild(li);
  }
}

$("clips-link").onclick = () => browseClips(null);
$("clips-home").onclick = () => browseClips(null);
$("clips-back-lib").onclick = () => browse(null);

// --- boot -------------------------------------------------------------------

let appConfig = {};

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
