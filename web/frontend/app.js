// quipclipper-web frontend — Phase 1: browse the library and inspect a file.

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

  const player = $("player");
  player.src = "/api/media" + qp(path);
  player.querySelectorAll("track").forEach((t) => t.remove());
  player.onerror = () => {
    $("preview-note").textContent =
      "Preview can't play this file in the browser (codec/container not supported). " +
      "Dialogue search and clipping still work — that's coming next.";
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
