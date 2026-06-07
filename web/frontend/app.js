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
  $("browser").hidden = false;
}

function showItem() {
  $("browser").hidden = true;
  $("item").hidden = false;
}

// --- library browser --------------------------------------------------------

async function browse(path) {
  showBrowser();
  const list = $("entries");
  list.innerHTML = "";
  $("browser-empty").hidden = true;
  let data;
  try {
    data = await getJSON("/api/library/browse" + (path ? qp(path) : ""));
  } catch (err) {
    list.innerHTML = `<li class="error">Could not browse: ${err.message}</li>`;
    return;
  }

  renderBreadcrumb(path, data.entries);

  if (!data.entries.length) {
    $("browser-empty").hidden = false;
    return;
  }
  for (const e of data.entries) {
    const li = document.createElement("li");
    li.className = "entry " + (e.is_dir ? "dir" : "video");
    const icon = e.is_dir ? "📁" : "🎬";
    const tag = !e.is_dir && e.has_sidecar ? ' <span class="badge">sub</span>' : "";
    li.innerHTML = `<span class="icon">${icon}</span><span class="label">${e.name}</span>${tag}`;
    li.onclick = () => (e.is_dir ? browse(e.path) : openItem(e.path, e.name));
    list.appendChild(li);
  }
}

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
  if (jobPollTimer) { clearTimeout(jobPollTimer); jobPollTimer = null; }

  const player = $("player");
  player.src = "/api/media" + qp(path);
  player.querySelectorAll("track").forEach((t) => t.remove());
  player.onerror = () => {
    $("preview-note").textContent =
      "Preview can't play this file in the browser (codec/container not supported). " +
      "Dialogue search still works — try the search box below.";
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

// --- clipping ---------------------------------------------------------------

let lastSearchQuery = null;
let lastSearchMatches = [];
let selectedMatch = null;
let activeJobId = null;
let jobPollTimer = null;

function selectMatchForClip(m) {
  selectedMatch = m;
  $("clip-panel").hidden = false;
  $("clip-range-display").textContent =
    `"${m.text.length > 80 ? m.text.slice(0, 77) + "…" : m.text}" — ${m.start_ts} – ${m.end_ts}`;
}

async function makeClip() {
  if (!currentItem || !selectedMatch) return;
  $("clip-btn").disabled = true;
  $("job-panel").hidden = false;
  $("job-status").innerHTML = '<span class="job-running">Submitting…</span>';

  const body = {
    path: currentItem.path,
    query: lastSearchQuery,
    match_index: selectedMatch.index,
    track: getSelectedTrack() !== null ? parseInt(getSelectedTrack()) : null,
    kind: $("clip-kind").value,
    lossless: $("clip-lossless").checked,
    before: parseFloat($("clip-before").value) || 2,
    after: parseFloat($("clip-after").value) || 2,
    backend: $("clip-backend").value,
    embed_subs: $("clip-embed-subs").checked,
  };

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

// --- boot -------------------------------------------------------------------

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
