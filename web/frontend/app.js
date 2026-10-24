// quipclipper-web frontend

const $ = (id) => document.getElementById(id);

// Every API request carries this header. The backend requires it on
// state-changing methods as a CSRF guard: a cross-site page can't set a custom
// header without triggering a CORS preflight the API never grants. Sending it on
// GETs too keeps every call going through one wrapper.
const API_HEADER = "X-Quipclipper";
function apiFetch(url, opts = {}) {
  return fetch(url, { ...opts, headers: { ...(opts.headers || {}), [API_HEADER]: "1" } });
}

async function getJSON(url) {
  const resp = await apiFetch(url);
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw Object.assign(new Error(detail), { status: resp.status });
  }
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  if (/^\/api\/(clip|youtube|media|items\/transcribe|search\/folder\/(index|fetch-subs|transcribe)|search\/library\/index|index\/(file|changes))/.test(url)) nudgeJobs();
  return resp.json();
}

const qp = (path) => `?path=${encodeURIComponent(path)}`;

/** Say something to screen-reader users through the one polite live region (#announcer):
 *  jobs finishing or failing, the selection changing, how many matches a search found. */
let announceTimer = null;
function announce(msg) {
  const region = $("announcer");
  region.textContent = "";  // an identical message twice in a row is still read
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => { region.textContent = msg; }, 60);
}

// --- views ------------------------------------------------------------------

/** Stop playback and release the source so audio doesn't keep playing
 *  after navigating away from the item view. */
function stopPlayer() {
  // Leaving the item view: abort this item's listeners/pollers (transcode range
  // loop, the embed poller, and a running download poll) and forget the item,
  // so a background poll can't yank the user back in or drive a stale player.
  if (itemListeners) { itemListeners.abort(); itemListeners = null; }
  currentItem = null;
  sttUnfollow();  // its transcription's poll too: the bar follows it again when it's reopened
  scriptLoadId++;  // a script still loading for the old item must not land anywhere
  wfHide();
  destroyYtEmbed();
  destroyHls();
  const player = $("player");
  if (!player) return;
  player.pause();
  player.removeAttribute("src");
  // Drop any subtitle tracks too, then reset the media element.
  player.querySelectorAll("track").forEach((t) => t.remove());
  player.load();
}

// --- official YouTube embed (optional per-browser player for yt: items) ------
// When active, `ytEmbed` supplants the <video id="player"> element: playerTime/
// seekTo/updatePlaybackUI read the IFrame API instead, and a poller in openItem
// stands in for the timeupdate events (script sync + range preview loop).
let ytEmbed = null;      // { player: YT.Player, ready: bool, pendingSeek: number|null }
let ytApiPromise = null; // one-time IFrame API loader

// "official" (default on iOS, where the server stream can't play) or "stream".
function ytPlayerPref() {
  return localStorage.getItem("qcYtPlayer") || (IS_IOS ? "official" : "stream");
}

function loadYtApi() {
  if (ytApiPromise) return ytApiPromise;
  ytApiPromise = new Promise((resolve, reject) => {
    if (window.YT && window.YT.Player) { resolve(); return; }
    window.onYouTubeIframeAPIReady = () => resolve();
    const s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    s.onerror = () => {
      ytApiPromise = null;  // let a later open retry
      reject(new Error("YouTube IFrame API failed to load"));
    };
    document.head.appendChild(s);
  });
  return ytApiPromise;
}

function ytEmbedPlaying() {
  // YT.PlayerState.PLAYING === 1 (also treat 3 = buffering as "not paused").
  if (!ytEmbed || !ytEmbed.ready) return false;
  const st = ytEmbed.player.getPlayerState();
  return st === 1 || st === 3;
}

function destroyYtEmbed() {
  if (ytEmbed) {
    try { ytEmbed.player.destroy(); } catch { /* already gone */ }
    ytEmbed = null;
  }
  const holder = $("yt-embed");
  if (holder) { holder.hidden = true; holder.innerHTML = ""; }
}

// --- HLS playback (desktop) -------------------------------------------------
// /api/media/transcode is an unbounded ffmpeg pipe — `Accept-Ranges: none`, no
// Content-Length — so it is unseekable and a browser that drops the connection
// can only re-open it from 0, restarting the encode. On a long source the
// re-fetch lands before enough is buffered to start, and playback never
// begins. The HLS path segments to disk instead: segments are individually
// fetchable and range-served, so a dropped connection resumes, and seeking is
// native (no custom seek bar). Safari plays HLS itself; everyone else needs
// hls.js, loaded on demand — 543 KB is not worth paying on a page view that
// never opens a transcoded item.
let hls = null;           // the active Hls instance, if any
let hlsLibPromise = null; // one-time hls.js loader

/** True when the element claims it plays HLS without hls.js (Safari, iOS).
 *
 *  Only trustworthy *after* hls.js has ruled itself out: Chromium answers
 *  "maybe" to this MIME and then cannot play the playlist at all (it reports
 *  readyState 4 with duration Infinity and never decodes a frame), so checking
 *  it first would hand every Chrome user a dead player. */
function hlsNative() {
  return !!$("player").canPlayType("application/vnd.apple.mpegurl");
}

function loadHlsLib() {
  if (hlsLibPromise) return hlsLibPromise;
  hlsLibPromise = new Promise((resolve, reject) => {
    if (window.Hls) { resolve(); return; }
    const s = document.createElement("script");
    s.src = "vendor/hls.min.js";
    s.onload = () => resolve();
    s.onerror = () => {
      hlsLibPromise = null;  // let a later open retry
      reject(new Error("hls.js failed to load"));
    };
    document.head.appendChild(s);
  });
  return hlsLibPromise;
}

function destroyHls() {
  if (hls) {
    try { hls.destroy(); } catch { /* already gone */ }
    hls = null;
  }
}

let currentView = "browser";  // browser | item | clips | bookmarks
const NAV_FOR_VIEW = { browser: "library-link", item: "library-link", clips: "clips-link", bookmarks: "bookmarks-link" };
function setView(view) {
  currentView = view;
  for (const id of new Set(Object.values(NAV_FOR_VIEW))) {
    const on = NAV_FOR_VIEW[view] === id;
    $(id).classList.toggle("active", on);
    if (on) $(id).setAttribute("aria-current", "page"); else $(id).removeAttribute("aria-current");
  }
  autoScope();
}

function showBrowser() {
  setView("browser");
  stopPlayer();
  $("item").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("browser").hidden = false;
}

function showItem() {
  setView("item");
  $("browser").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("item").hidden = false;
}

// --- deep links (M18) -------------------------------------------------------
//
// Every view has a hash route, so a page can be reloaded, bookmarked and shared, and Back and
// Forward (a phone's back gesture too) move between views:
//   #/library?path=…                       a folder (no path: the library home)
//   #/search?q=…&scope=library|folder[&path=…]  dialogue search; path repeats per folder
//   #/item?path=…&t=…[&sel=a-b][&track=N]  the workspace, seeked to t, a-b selected
//   #/bookmarks   #/clips?folder=…   #/nowplaying (what Jellyfin is playing)
// Each view's section in index.html lists its routes in data-route, and
// test_frontend_routes.py checks this table against them. The server builds the same
// links for the CLI and Hermes (web/backend/quipclipper_web/deeplinks.py): keep the encoders
// in step. Paths aren't checked here; the API refuses a bad one and the view falls back.
const ROUTES = {
  library: "browser",
  search: "browser",
  nowplaying: "browser",
  item: "item",
  bookmarks: "bookmarks-browser",
  clips: "clips-browser",
};

// encodeURIComponent, but / and : stay as they are (a fragment may hold them), so a path
// reads as a path. Times are seconds with at most three decimals.
const routeValue = (v) => encodeURIComponent(v).replace(/%2F/g, "/").replace(/%3A/g, ":");
const routeTime = (t) => String(+(+t).toFixed(3));

function routeHash(view, params = {}) {
  const pairs = [];
  for (const [k, v] of Object.entries(params)) {
    for (const one of [].concat(v)) {
      if (one != null && one !== "") pairs.push(`${k}=${routeValue(one)}`);
    }
  }
  return `#/${view}` + (pairs.length ? `?${pairs.join("&")}` : "");
}

/** The workspace route for `path`: seek to `t`, select `sel` ([start, end]), show `track`. */
function itemRoute(path, { t, sel, track } = {}) {
  if (t == null && sel) t = sel[0];
  return routeHash("item", {
    path,
    t: t != null ? routeTime(t) : null,
    sel: sel ? `${routeTime(sel[0])}-${routeTime(sel[1])}` : null,
    track,
  });
}

/** {view, params} for a hash, or null when it isn't one of ours. */
function parseRoute(hash) {
  const m = /^#\/([a-z]+)(?:\?(.*))?$/.exec(hash || "");
  if (!m || !Object.hasOwn(ROUTES, m[1])) return null;
  return { view: m[1], params: new URLSearchParams(m[2] || "") };
}

const routeLink = (hash) => location.origin + location.pathname + hash;

/** Whether the page shows `view` with `key` = `value` (null: the parameter absent). */
function showingRoute(view, key, value) {
  const r = parseRoute(location.hash);
  return !!r && r.view === view && (r.params.get(key) || null) === (value || null);
}

// While a URL is being restored (on load, Back/Forward), views replace the history entry
// instead of pushing one, so restoring doesn't grow the history.
let routeRestoring = false;

/** Record the view on screen: a new history entry, unless it's the same, or `replace`. */
function setRoute(hash, { replace = false } = {}) {
  if (hash === location.hash) return;
  if (replace || routeRestoring) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);
}

/** A link to a bad or out-of-roots path (or a folder that's gone): say so over the library. */
async function linkFailed(err) {
  routeRestoring = true;
  try { await browse(null); } finally { routeRestoring = false; }
  const msg = `Couldn't open that link (${err.message}), so here's the library.`;
  $("route-note").textContent = msg;
  $("route-note").hidden = false;
  announce(msg);
}

/** Show the view a URL names: on load, and on Back / Forward. */
async function applyRoute(hash, { focus = false } = {}) {
  const r = parseRoute(hash);
  const p = r ? r.params : new URLSearchParams();
  const num = (k) => { const v = parseFloat(p.get(k)); return Number.isFinite(v) && v >= 0 ? v : null; };
  routeRestoring = true;
  try {
    switch (r ? r.view : "library") {
      case "library":
        await browse(p.get("path") || null, { fromLink: true });
        break;
      case "search":
        await restoreSearch(p.get("q") || "", p.get("scope"), p.getAll("path"));
        break;
      case "item": {
        const path = p.get("path");
        if (!path) { await browse(null); break; }
        const m = /^([\d.]+)-([\d.]+)$/.exec(p.get("sel") || "");
        const sel = m && +m[1] <= +m[2] ? { start: +m[1], end: +m[2] } : null;
        const t = num("t") ?? (sel ? sel.start : null);
        await openItem(path, path.split(/[\\/]/).pop(), {
          seekTo: t ?? undefined, markRange: sel, track: p.get("track"), fromLink: true,
        });
        break;
      }
      case "bookmarks":
        await browseBookmarks();
        break;
      case "clips":
        await browseClips(p.get("folder") || null, { fromLink: true });
        break;
      case "nowplaying":
        await jumpToNowPlaying();
        break;
    }
  } finally {
    routeRestoring = false;
  }
  // Back / Forward: focus lands on the new view's heading (where the skip link would take
  // it), even when the control that had it is still on screen, like a top-bar link.
  if (focus) skipTarget()[0].focus();
}

/** A search URL: the folder (or library) first, then the search, as the search box runs it. */
async function restoreSearch(query, scope, folders) {
  if (scope === "item" && folders.length) {  // in-video search lives in the workspace
    await openItem(folders[0], folders[0].split(/[\\/]/).pop(), { searchQuery: query, fromLink: true });
    return;
  }
  const inFolders = scope === "folder" && folders.length > 0;
  if (!(await browse(inFolders && folders.length === 1 ? folders[0] : null, { fromLink: true }))) return;
  if (!query) return;
  if (!mediaRoots.length) {
    try { mediaRoots = (await getJSON("/api/library/roots")).roots || []; } catch {}
  }
  if (inFolders && folders.length > 1) dialogueSearchScope = folders;
  searchScope = inFolders ? "folder" : "library";
  updateScopeUI();
  $("global-search-input").value = query;
  await runGlobalSearch();
}

window.addEventListener("popstate", () => applyRoute(location.hash, { focus: true }));

/** Put `text` on the clipboard (the async API needs https; fall back to a hidden field). */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = el("textarea", { title: "Link to copy", className: "visually-hidden" });
    area.value = text;
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch {}
    area.remove();
    return ok;
  }
}

/** Copy a link and say so on the button for a moment (and to a screen reader). */
async function copyLink(link, btn) {
  const ok = await copyText(link);
  if (!ok) { window.prompt("Copy this link:", link); return; }
  announce("Link copied");
  const label = btn.querySelector(".btn-label");
  if (label && !btn.classList.contains("copied")) { btn.dataset.label = label.textContent; label.textContent = "Copied"; }
  btn.classList.add("copied");
  clearTimeout(btn._copiedTimer);
  btn._copiedTimer = setTimeout(() => {
    btn.classList.remove("copied");
    if (label) label.textContent = btn.dataset.label;
  }, 1500);
}

// Copy link (workspace): the selection, else the playhead. The address bar shows it too.
$("copy-link").onclick = () => {
  if (!currentItem) return;
  const sel = hasClipRange() ? [clipStart(), clipEnd()]
    : !$("clip-btn").disabled && clipRangeStart != null ? [clipRangeStart, clipRangeEnd] : null;
  const hash = itemRoute(currentItem.path, { t: sel ? sel[0] : playerTime(), sel, track: getSelectedTrack() });
  setRoute(hash, { replace: true });
  copyLink(routeLink(hash), $("copy-link"));
};

// --- library browser --------------------------------------------------------

let currentBrowsePath = null;
let librarySearchTimer = null;
let librarySearchId = 0;  // generation token so a slow search can't overwrite a newer one
// When a library search surfaces folders, holds their paths so dialogue
// search can span all of them. null = search the current folder only.
let dialogueSearchScope = null;

/** A control that a view switch or a re-render removes or hides takes keyboard focus with it,
 *  to <body>, and the next Tab starts from nowhere useful. Put focus on `target` instead,
 *  unless `had` (what was focused before) is still on screen. */
function refocus(had, target) {
  if (!had || had === document.body || !target) return;
  if (had.isConnected && had.offsetParent !== null) return;
  target.focus();
}

/** Show a folder (null: the library home). Resolves false when it couldn't be listed;
 *  `fromLink` (a URL being restored) then falls back to the library with a message. */
async function browse(path, { fromLink = false } = {}) {
  const had = document.activeElement;
  setRoute(routeHash("library", { path }));
  showBrowser();
  $("route-note").hidden = true;
  currentBrowsePath = path;
  dialogueSearchScope = null;
  // One listing at a time: a slower browse, name filter or dialogue search started
  // before this one must not land in it.
  clearTimeout(librarySearchTimer);
  const myId = ++librarySearchId;
  refreshNowPlaying();  // the Now playing card shows on the library home only
  const list = $("entries");
  list.innerHTML = "";
  $("search-batch").hidden = true;
  $("hit-preview").hidden = true;
  $("browser").classList.remove("hits");
  $("browser-empty").hidden = true;
  $("library-search").value = "";
  $("global-search-input").value = "";
  $("dialogue-search-status").hidden = true;
  $("reindex-folder-status").hidden = true;
  hideChangedBanner();
  resetIndexCard();
  libIndexReset();
  // The dialogue search bar: a folder's Dialogue index card, or the library's at home.
  $("dialogue-search-bar").hidden = false;
  const isYtFolder = path === "yt:";
  // The add-URL bar lives in the ▶ YouTube pseudo-folder; the Dialogue index card
  // doesn't apply there (each item has its own ↻ in the item view).
  $("yt-add-bar").hidden = !isYtFolder;
  $("yt-add-status").hidden = true;
  $("index-card").hidden = isYtFolder;
  // Reset the folder subtitle-backfill controls when entering a folder. A job
  // already running keeps going server-side; we just stop mirroring it here.
  if (fetchFolderPoll) { clearInterval(fetchFolderPoll); fetchFolderPoll = null; }
  sttFolderReset();
  $("fetch-folder-subs-btn").disabled = false;
  $("fetch-folder-cancel").hidden = true;
  $("fetch-folder-status").hidden = true;
  let data;
  try {
    data = await getJSON("/api/library/browse" + (path ? qp(path) : ""));
  } catch (err) {
    if (myId !== librarySearchId) return false;
    if (fromLink && path) { await linkFailed(err); return false; }
    list.innerHTML = `<li class="error">Could not browse: ${escapeHtml(err.message)}</li>`;
    return false;
  }
  if (myId !== librarySearchId) return false;  // a newer listing is on screen

  renderBreadcrumb(path);
  renderRootsRail(path);
  renderEntries(data.entries);
  if (had && had !== document.body && !(had.isConnected && had.offsetParent !== null)) {
    const here = path === "yt:" ? "YouTube" : path ? path.split("/").pop() : "Library";
    announce(`${here}, ${data.entries.length} item${data.entries.length === 1 ? "" : "s"}`);
  }
  refocus(had, $("entries").querySelector(".entry-main") || $("entries"));
  autoScope();
  if (!path) libIndexHome();
  else if (!isYtFolder) checkFolderIndex(path);  // index-status is for real folders
  return true;
}

/** Build an element. Every control gets hover text (`title`: what it does, and its
 *  shortcut key) and, when its visible text isn't a good name on its own, an accessible
 *  name (`label`, set as aria-label). test_frontend_a11y.py checks each call passes a title.
 *  Other props: className, text, html, type (buttons default to "button"), and any
 *  attribute by name. */
function el(tag, { title, label, className, text, html, type, ...attrs } = {}) {
  const node = document.createElement(tag);
  if (tag === "button") node.type = type || "button";
  else if (type) node.type = type;
  if (className) node.className = className;
  if (title) node.title = title;
  if (label) node.setAttribute("aria-label", label);
  if (text != null) node.textContent = text;
  if (html != null) node.innerHTML = html;
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/** Make a clickable non-button element reachable and operable from the keyboard, with hover
 *  text saying what activating it does. */
function makeActivatable(node, title, role = "button") {
  node.tabIndex = 0;
  node.setAttribute("role", role);
  node.title = title;
  node.addEventListener("keydown", (e) => {
    if (e.target !== node) return;  // not a key aimed at a control inside the row
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); node.click(); }
  });
  return node;
}

/** An icon from the sprite in index.html. */
const iconSvg = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

// --- Jellyfin artwork (M18) ---------------------------------------------------
//
// Library rows, the workspace and Now playing carry `jellyfin.art` / `art` ({id, type,
// tag, wide}) when Jellyfin knows the file. The image comes through the server's proxy
// (GET /api/jellyfin/image/{id}), which keeps the API key there; with its tag the
// browser caches it for good.

/** The proxy URL for an artwork entry, `w` CSS pixels wide (asked for at 2x). */
function jfArtUrl(art, w) {
  return `/api/jellyfin/image/${encodeURIComponent(art.id)}?type=${encodeURIComponent(art.type || "Primary")}` +
    `&w=${Math.round(w * 2)}` + (art.tag ? `&tag=${encodeURIComponent(art.tag)}` : "");
}

/** A lazy-loaded poster (or a 16:9 still when `art.wide`) on a row. Decorative (empty
 *  alt): the row it sits in already says the title, which "Poster: <title>" only repeated. */
function jfArtHtml(art, className) {
  const wide = !!art.wide;
  return `<img class="jf-art${wide ? " wide" : ""} ${className}" src="${escapeHtml(jfArtUrl(art, wide ? 64 : 32))}"` +
    ` alt="" loading="lazy" decoding="async" width="${wide ? 64 : 32}" height="${wide ? 36 : 48}">`;
}

/** A poster that fails to load goes back to `fallback` (the row's icon), or away. */
function jfArtFallback(root, fallback) {
  for (const img of root.querySelectorAll("img.jf-art")) {
    img.addEventListener("error", () => {
      if (fallback) img.outerHTML = fallback;
      else img.remove();
    }, { once: true });
  }
}

/** The dialogue index chip for a library row (library.browse's index_state): "indexed"
 *  (subtitles cached, so a search reads them at once), "changed" (indexed, but its
 *  subtitles changed since: search leaves it out) or "no subs"; none before indexing. */
function indexChipHtml(state) {
  const chip = (text, name, title, cls = "") => `<span class="badge idx-chip${cls}" title="${title}">` +
    `<span aria-hidden="true">${text}</span><span class="visually-hidden">${name}</span></span>`;
  if (state === "indexed") {
    return chip("indexed", "dialogue indexed", "Dialogue indexed: its subtitles are cached, so a search reads them at once");
  }
  if (state === "changed") {
    return chip("changed", "subtitles changed since indexing",
      "Its subtitles changed since it was indexed, so search leaves it out until it is re-indexed (Re-index them, above)",
      " idx-changed");
  }
  if (state === "no_subs") {
    return chip("no subs", "no subtitles to index",
      "Indexing found no text subtitles in this file (no sidecar, no text track): nothing to search"
        + (sttAvailable() ? " until it is transcribed" : ""));
  }
  return "";
}

/** After indexing `folder`, bring its rows' index chips up to date (one listing, no re-render). */
async function refreshIndexChips(folder) {
  let data;
  try {
    data = await getJSON(`/api/library/browse?path=${encodeURIComponent(folder)}`);
  } catch { return; }
  if (currentBrowsePath !== folder) return;
  const state = new Map(data.entries.map((e) => [e.path, e.index_state]));
  for (const li of $("entries").querySelectorAll("li[data-path]")) {
    if (!state.has(li.dataset.path)) continue;
    const chips = li.querySelector(".chips");
    chips.querySelector(".idx-chip")?.remove();
    chips.insertAdjacentHTML("beforeend", indexChipHtml(state.get(li.dataset.path)));
  }
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
    if (!intoFolder) li.dataset.path = e.path;
    const icon = e.is_dir ? "folder" : e.is_book ? "book" : e.is_youtube ? "youtube" : e.is_audio ? "music" : "film";
    // Jellyfin's poster (a show, season or movie folder) or still (an episode) when it knows it.
    const art = e.jellyfin && e.jellyfin.art;
    let chips = (!intoFolder && e.has_sidecar) ? '<span class="badge">sub</span>' : "";
    // A machine transcript (M21): the "auto" chip, read out as what it is.
    if (!intoFolder && e.has_transcript) {
      chips += `<span class="auto-chip" title="Has a machine transcript (whisper): searchable, but it may be wrong"><span aria-hidden="true">auto</span><span class="visually-hidden">machine transcript</span></span>`;
    }
    if (!intoFolder) chips += indexChipHtml(e.index_state);
    // A locally downloaded YouTube video plays/cuts from the server's own copy.
    if (e.is_youtube && e.downloaded) chips += '<span class="badge" title="Downloaded to the server">on server</span>';
    // The row's action is one button; other controls (YouTube's remove) sit beside it, not in it.
    const main = el("button", {
      className: "entry-main",
      title: e.is_book ? "Open this book's chapters" : intoFolder ? "Open this folder"
        : "Open in the workspace: play it, read its script, cut a clip",
      html: `<span class="icon-wrap${art ? " has-art" : ""}">` +
        `${art ? jfArtHtml(art, "entry-art") : iconSvg(icon)}</span>` +
        `<span class="label">${escapeHtml(e.name)}</span>` +
        `<span class="chips">${chips}</span>` + (intoFolder ? `<span class="go">${iconSvg("chevron-r")}</span>` : ""),
    });
    if (intoFolder) main.dataset.action = "library.browse";
    if (art) jfArtFallback(main, iconSvg(icon));
    main.onclick = () => (intoFolder ? browse(e.path) : openItem(e.path, e.name));
    li.appendChild(main);
    // A YouTube item row gets a remove button (forgets the video + transcript).
    if (e.is_youtube && !e.is_dir) {
      const del = el("button", {
        className: "yt-remove", text: "✕",
        title: "Remove this video from the YouTube list (forgets its subtitles too)",
        label: `Remove ${e.name}`,
      });
      del.dataset.action = "youtube.remove";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        const id = e.path.slice("yt:".length);
        try {
          await apiFetch(`/api/youtube/${encodeURIComponent(id)}`, { method: "DELETE" });
        } catch { /* non-fatal; the re-browse below shows the truth */ }
        browse("yt:");
      };
      li.appendChild(del);
    }
    list.appendChild(li);
  }
}

/** The roots rail: each media root and the YouTube pseudo-folder, current one marked. */
function renderRootsRail(path) {
  const list = $("roots-list");
  list.innerHTML = "";
  const items = mediaRoots.map((r) => ({ path: r, name: r.split("/").filter(Boolean).pop() || r, icon: "folder" }));
  items.push({ path: "yt:", name: "YouTube", icon: "youtube" });
  for (const it of items) {
    const on = path === it.path || (!!path && it.path !== "yt:" && path.startsWith(it.path + "/"));
    const li = document.createElement("li");
    const b = el("button", {
      className: "root-link" + (on ? " active" : ""),
      title: it.path === "yt:" ? "The YouTube videos added by URL" : `Browse ${it.path}`,
    });
    b.dataset.action = "library.browse";
    if (on) b.setAttribute("aria-current", "true");
    b.innerHTML = `${iconSvg(it.icon)}<span>${escapeHtml(it.name)}</span>`;
    b.onclick = () => browse(it.path);
    li.appendChild(b);
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
  resetIndexCard();
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
  $("hit-preview").hidden = true;
  $("browser").classList.remove("hits");
  renderEntries(data.entries);

  // Enable cross-folder dialogue search over the folders this search surfaced.
  const folderPaths = data.entries.filter((e) => e.is_dir).map((e) => e.path);
  if (folderPaths.length) {
    dialogueSearchScope = folderPaths;
    $("dialogue-search-bar").hidden = false;
    showIndexScope(folderPaths.length);
  } else {
    dialogueSearchScope = null;
    if (!currentBrowsePath) libIndexHome();
  }
  autoScope();  // a name search that found folders makes "folders" the scope
}

$("library-search").addEventListener("input", (e) => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(() => librarySearch(e.target.value), 300);
});

// --- YouTube: add a video by URL ---------------------------------------------

async function addYouTubeUrl() {
  const input = $("yt-url");
  const url = input.value.trim();
  if (!url) return;
  const btn = $("yt-add-btn");
  const status = $("yt-add-status");
  btn.disabled = true;
  status.hidden = false;
  status.textContent = "Fetching subtitles + metadata…";
  let job;
  try {
    // The add runs server-side as a job (yt-dlp can be slow); we poll it.
    job = await postJSON("/api/youtube", { url });
  } catch (err) {
    status.textContent = `Could not add: ${err.message}`;
    btn.disabled = false;
    return;
  }
  if (!job.job_id) {  // already added — show it immediately
    input.value = "";
    status.hidden = true;
    btn.disabled = false;
    browse("yt:");
    return;
  }
  const poll = setInterval(async () => {
    const resp = await apiFetch(`/api/jobs/${encodeURIComponent(job.job_id)}`);
    if (resp.status === 404) {  // job lost (e.g. server restart)
      clearInterval(poll);
      status.textContent = "Add didn't complete — try again.";
      btn.disabled = false;
      return;
    }
    if (!resp.ok) return;  // transient — keep polling
    const j = await resp.json();
    if (j.status === "done") {
      clearInterval(poll);
      input.value = "";
      status.hidden = true;
      btn.disabled = false;
      // The new entry appears in the listing, if the user is still looking at it: a
      // background job never pulls them away from what they're doing now.
      if (showingRoute("library", "path", "yt:")) browse("yt:");
    } else if (j.status === "failed" || j.status === "cancelled") {
      clearInterval(poll);
      status.textContent = `Could not add: ${j.error || "see server logs"}`;
      btn.disabled = false;
    }
  }, 1500);
}
$("yt-add-btn").addEventListener("click", addYouTubeUrl);
$("yt-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !$("yt-add-btn").disabled) addYouTubeUrl();
});

/** Wire the per-item Download control (in #yt-player-row). Downloading runs as
 *  a server job; once the file exists, playback seeks natively and clips cut
 *  from the local copy — the control then offers to remove it. */
// Download job ids in flight this page session, keyed by item path — so
// reopening an item mid-download (a Player-mode toggle, a bookmark seek)
// resumes the "Downloading…" state instead of offering a duplicate download.
const ytDownloadJobs = {};

function wireYtDownload(path, name, info, signal) {
  const btn = $("yt-download-btn");
  const status = $("yt-download-status");
  btn.hidden = false;
  status.hidden = true;
  btn.disabled = false;
  const vid = path.slice("yt:".length);

  if (info.downloaded) {
    delete ytDownloadJobs[path];
    const mb = info.download_size ? ` (${(info.download_size / 1048576).toFixed(1)} MB)` : "";
    btn.innerHTML = `<span aria-hidden="true">💾</span> Remove download${mb}`;
    btn.title = "Delete the server's copy of this video; it plays from YouTube again";
    btn.dataset.action = "youtube.delete-download";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await apiFetch(`/api/youtube/${encodeURIComponent(vid)}/download`, { method: "DELETE" });
      } catch { /* re-open shows the truth */ }
      openItem(path, name, {});
    };
    return;
  }

  btn.innerHTML = '<span aria-hidden="true">⬇</span> Download video';
  btn.title = "Download the whole video to the server. Playback then seeks natively and clips cut from the local file, with no streaming from YouTube at all.";
  btn.dataset.action = "youtube.download";

  // Follow a download job to completion: reload the item on success (native
  // playback from the local file), surface failures, and stop cleanly when the
  // job is lost (backend restart) or the user navigates away.
  const followJob = (jobId) => {
    ytDownloadJobs[path] = jobId;
    btn.disabled = true;
    status.hidden = false;
    status.textContent = "Downloading…";
    const poll = setInterval(async () => {
      if (signal.aborted) { clearInterval(poll); return; }
      const resp = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (resp.status === 404) {  // job registry lost it (e.g. server restart)
        clearInterval(poll);
        delete ytDownloadJobs[path];
        status.textContent = "Download state lost — try again.";
        btn.disabled = false;
        return;
      }
      if (!resp.ok) return;  // transient — keep polling
      const j = await resp.json();
      if (j.status === "done") {
        clearInterval(poll);
        delete ytDownloadJobs[path];
        if (!signal.aborted && currentItem && currentItem.path === path) openItem(path, name, {});
      } else if (j.status === "failed" || j.status === "cancelled") {
        clearInterval(poll);
        delete ytDownloadJobs[path];
        status.textContent = `Download failed: ${j.error || "see server logs"}`;
        btn.disabled = false;
      }
    }, 1500);
  };

  // Already downloading (reopened mid-flight): resume following, don't offer a
  // second download.
  if (ytDownloadJobs[path]) { followJob(ytDownloadJobs[path]); return; }

  btn.onclick = async () => {
    btn.disabled = true;
    status.hidden = false;
    status.textContent = "Downloading…";
    let job;
    try {
      job = await postJSON(`/api/youtube/${encodeURIComponent(vid)}/download`, {});
    } catch (err) {
      status.textContent = `Download failed: ${err.message}`;
      btn.disabled = false;
      return;
    }
    if (!job.job_id) { openItem(path, name, {}); return; }  // already downloaded
    followJob(job.job_id);
  };
}

// --- folder dialogue search ------------------------------------------------

const countOf = (n, noun) => `${(n || 0).toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;

/** What a folder search left out (it reads only indexed files): "" when nothing. */
function searchSkipped(data) {
  const parts = [];
  if (data.not_indexed) parts.push(`${countOf(data.not_indexed, "file")} not indexed yet (Index a folder to include it)`);
  if (data.changed) parts.push(`${countOf(data.changed, "file")} with subtitles changed since indexing (Re-index them, above)`);
  if (data.books_not_searched) parts.push(`${countOf(data.books_not_searched, "read-along book")} (search a book on its own)`);
  return parts.length ? `. Not searched: ${parts.join("; ")}.` : "";
}

async function dialogueSearch(query, folders) {
  if (!query || !folders.length) return;

  const list = $("entries");
  list.innerHTML = "";
  $("search-batch").hidden = true;
  $("hit-preview").hidden = true;
  $("browser").classList.remove("hits");
  $("browser-empty").hidden = true;
  const status = $("dialogue-search-status");
  hideChangedBanner();
  status.textContent = "Searching subtitles…";
  status.hidden = false;
  clearTimeout(librarySearchTimer);
  const myId = ++librarySearchId;  // a browse or filter after this one wins

  const pathParams = folders.map((f) => `path=${encodeURIComponent(f)}`).join("&");
  const url = `/api/search/folder?${pathParams}&query=${encodeURIComponent(query)}`;
  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    if (myId !== librarySearchId) return;
    list.innerHTML = `<li class="error">Dialogue search failed: ${escapeHtml(err.message)}</li>`;
    status.hidden = true;
    return;
  }
  if (myId !== librarySearchId) return;  // the user went elsewhere meanwhile

  status.textContent = `${data.count} match${data.count !== 1 ? "es" : ""} across ${countOf(data.files_scanned, "indexed file")}` +
    (data.truncated ? ` (the best ${data.matches.length} shown)` : "") +
    searchSkipped(data);
  showChangedBanner(data.changed_files, data.changed);

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
  updateBatchState("search");
  $("browser").classList.add("hits");

  // Group the hits by file, in order of first appearance.
  const groups = new Map();
  for (const hit of data.matches) {
    if (!groups.has(hit.path)) groups.set(hit.path, { file: hit.file, hits: [] });
    groups.get(hit.path).hits.push(hit);
  }
  let first = null;
  let gi = 0;
  for (const g of groups.values()) {
    const gid = `g${gi++}`;
    const head = document.createElement("li");
    head.className = "hit-group";
    head.innerHTML =
      `<button type="button" class="hit-group-toggle" aria-expanded="true" title="Show or hide this file's matches">${iconSvg("chevron-r")}` +
      `<span class="hit-group-icon">${iconSvg("film")}</span><span class="hit-group-name">${escapeHtml(g.file)}</span>` +
      `<span class="hit-group-count">${g.hits.length} ${g.hits.length === 1 ? "match" : "matches"}</span></button>`;
    const rows = [];
    head.querySelector("button").onclick = (e) => {
      const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
      e.currentTarget.setAttribute("aria-expanded", String(open));
      rows.forEach((r) => { r.hidden = !open; });
    };
    list.appendChild(head);
    for (const hit of g.hits) {
      const li = document.createElement("li");
      li.className = hit.machine ? "dialogue-hit hit-machine" : "dialogue-hit";
      li.dataset.group = gid;
      li.innerHTML =
        `<label class="hit-check"><input type="checkbox" class="search-select" title="Select this line for Clip selected and Bookmark selected" aria-label="Select the line at ${hit.start_ts}" /></label>` +
        `<div class="hit-main" tabindex="0" role="button" title="${previewOn() ? "Show this line in context" : "Open this file in the workspace with the line selected"}">` +
          `<span class="hit-text">${hit.machine ? `${AUTO_CHIP}<span class="visually-hidden">Machine transcript: </span>` : ""}${hit.speaker ? `<span class="hit-speaker">${escapeHtml(hit.speaker)}</span>` : ""}${highlightMatch(hit.text, query)}</span>` +
          `<span class="hit-time">${hit.start_ts} – ${hit.end_ts}</span>` +
        `</div>` +
        `<span class="hit-score" title="${SCORE_TITLE}">${hit.score}%</span>` +
        `<div class="hit-actions">` +
          `<button type="button" class="mark-icon-btn hit-bookmark" data-action="bookmark.add" title="Save this line as a bookmark" aria-label="Bookmark the line at ${hit.start_ts}"><span class="star" aria-hidden="true">★</span></button>` +
          `<button type="button" class="mark-icon-btn make-clip-btn hit-clip" data-action="clip.create" title="Cut a clip of this line with the bar's options" aria-label="Clip the line at ${hit.start_ts}"><span class="scissors" aria-hidden="true">✂</span></button>` +
          `<button type="button" class="mark-icon-btn hit-link" data-action="item.link" title="Copy a link to this line: it opens the file here with the line selected" aria-label="Copy a link to the line at ${hit.start_ts}">${iconSvg("link")}</button>` +
          `<span class="hit-status" data-action="job.get"></span>` +
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
      li.querySelector(".hit-link").onclick = (e) => {
        e.stopPropagation();
        copyLink(routeLink(itemRoute(hit.path, { sel: [hit.start, hit.end] })), e.currentTarget);
      };
      const open = () => openItem(hit.path, hit.file, {
        searchQuery: query,
        seekTo: hit.start,
        markRange: { start: hit.start, end: hit.end },
      });
      // Wide screens: a click previews the line in context; the preview's button opens it.
      // Narrow screens (no preview panel): a click opens the file with the cue selected.
      const main = li.querySelector(".hit-main");
      const activate = () => (previewOn() ? focusHit(li, hit, open) : open());
      main.onclick = activate;
      main.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } };
      rows.push(li);
      list.appendChild(li);
      if (!first) first = [li, hit, open];
    }
  }
  if (first && previewOn()) focusHit(first[0], first[1], first[2]);
}

/** Wrap each word of the query found in `text` in <mark>; the rest is escaped. */
function highlightMatch(text, query) {
  const words = [...new Set(query.toLowerCase().split(/\s+/)
    .map((w) => w.replace(/[^\p{L}\p{N}']/gu, "")).filter((w) => w.length >= 2))];
  if (!words.length) return escapeHtml(text);
  const re = new RegExp("(" + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  return text.split(re).map((part, i) => (i % 2 ? `<mark>${escapeHtml(part)}</mark>` : escapeHtml(part))).join("");
}

// The preview panel needs room beside the list.
const previewMQ = window.matchMedia("(min-width: 1250px)");
const previewOn = () => previewMQ.matches;
// Narrowed past the breakpoint (a window resized, an iPad turned): the panel has no room,
// and a click on a hit now opens it instead, so put the preview away.
previewMQ.addEventListener("change", () => {
  if (previewOn()) return;
  $("hit-preview").hidden = true;
  document.querySelectorAll(".dialogue-hit.focus").forEach((li) => li.classList.remove("focus"));
});
const cueCache = new Map();  // path -> cues, so moving between hits of one file is instant
let previewToken = 0;

async function focusHit(li, hit, open) {
  document.querySelectorAll("#entries .dialogue-hit.focus").forEach((e) => e.classList.remove("focus"));
  li.classList.add("focus");
  const panel = $("hit-preview");
  panel.hidden = false;
  $("hit-preview-title").textContent = hit.file;
  $("hit-preview-time").textContent = `${hit.start_ts} – ${hit.end_ts}`;
  $("hit-preview-open").onclick = open;
  const list = $("hit-preview-lines");
  const my = ++previewToken;
  list.innerHTML = '<li class="muted">Loading the lines around it…</li>';
  let cues = cueCache.get(hit.path);
  if (!cues) {
    try {
      cues = await getJSON(`/api/items/subtitles?fmt=json&path=${encodeURIComponent(hit.path)}`);
      cueCache.set(hit.path, cues);
    } catch { cues = []; }
  }
  if (my !== previewToken) return;
  let idx = cues.findIndex((c) => Math.abs(c.start - hit.start) < 0.6);
  if (idx < 0) {
    list.innerHTML = `<li class="preview-line hit"><span class="ts">${hit.start_ts}</span><span>${escapeHtml(hit.text)}</span></li>`;
    return;
  }
  list.innerHTML = "";
  for (let i = Math.max(0, idx - 3); i <= Math.min(cues.length - 1, idx + 3); i++) {
    const row = document.createElement("li");
    const machine = cues[i].source === "machine";
    row.className = "preview-line" + (i === idx ? " hit" : "") + (machine ? " machine" : "");
    if (machine) row.title = MACHINE_TITLE;
    row.innerHTML = `<span class="ts">${formatTime(cues[i].start)}</span><span>${machine ? `${AUTO_CHIP}<span class="visually-hidden">Machine transcript: </span>` : ""}${escapeHtml(cues[i].text)}</span>`;
    list.appendChild(row);
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
          ? `<a class="clip-dl-mini" data-action="job.download" href="/api/jobs/${jobId}/download/${encodeURIComponent(f.name)}" download title="Download ${escapeHtml(f.name)}" aria-label="Download clip">⤓</a>`
          : '<span class="job-done">✓</span>';
      } else {
        statusEl.innerHTML = `<span class="job-failed" title="${escapeHtml(job.error || "failed")}">✗</span>`;
      }
    })
    .catch((err) => {
      if (!statusEl.isConnected) return;
      if (err.status === 404) {  // cleared from the Jobs list
        statusEl.innerHTML = '<span class="muted" title="This job was cleared from the Jobs list">–</span>';
        return;
      }
      setTimeout(() => pollHitClip(jobId, statusEl), 2000);
    });
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

// --- folder subtitle index: the "Dialogue index" card -------------------------
//
// GET /api/search/folder/index-status counts the media files under the folder and
// how many have cached subtitles; POST /api/search/folder/index (re)builds them.

let indexCheckId = 0;
let indexBusy = false;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function setIndexBar(frac) {
  const bar = $("index-bar");
  bar.hidden = frac == null;
  if (frac == null) return;
  const pct = Math.round(Math.max(0, Math.min(1, frac)) * 100);
  $("index-bar-fill").style.width = pct + "%";
  bar.setAttribute("aria-valuenow", String(pct));
}

function resetIndexCard() {
  $("index-card-text").textContent = "Checking subtitles…";
  setIndexBar(null);
  $("index-btn").hidden = true;
  $("reindex-folder-btn").hidden = false;
}

/** A name search found folders: no status to show, but Re-index covers them. */
function showIndexScope(n) {
  $("index-card-text").textContent = `${plural(n, "matching folder")}. Re-index covers all of them.`;
  setIndexBar(null);
  $("index-btn").hidden = true;
  $("lib-index-btn").hidden = true;
  $("reindex-folder-btn").hidden = false;
  $("fetch-folder-subs-btn").hidden = false;
}

// A folder with more files than one request may index (index-status' `cap`) is indexed
// by the library index job instead; Re-index all isn't offered there.
let indexInBackground = false;

function renderIndexStatus(data) {
  showChangedBanner(data.changed_files, data.changed);
  const text = $("index-card-text");
  const index = $("index-btn");
  index.hidden = true;
  indexInBackground = !!data.cap && data.total > data.cap;
  if (data.skipped) {
    text.textContent = `Too many files here to index at once (over ${data.total}). Open a subfolder to index it.`;
    setIndexBar(null);
    $("reindex-folder-btn").hidden = true;
    return;
  }
  showSttFolder(data);
  // Machine transcripts (M21): which files have one, and audio that could.
  const heard = (data.transcribed ? ` ${plural(data.transcribed, "file")} ${data.transcribed === 1 ? "has" : "have"} a machine transcript.` : "")
    + (data.audio_untranscribed ? ` ${plural(data.audio_untranscribed, "audio file")} ${data.audio_untranscribed === 1 ? "has" : "have"} no transcript yet.` : "");
  if (data.total === 0) {
    text.textContent = "No video or audio files under this folder." + heard;
    setIndexBar(null);
    $("reindex-folder-btn").hidden = true;
    return;
  }
  $("reindex-folder-btn").hidden = indexInBackground;
  // Files indexing found to have no subtitles at all (no sidecar, no text
  // track) aren't "missing": there is nothing to index, so don't offer it.
  const noSubs = data.no_subs || 0;
  const withSubs = data.total - noSubs;
  const none = noSubs
    ? ` ${plural(noSubs, "file")} ${noSubs === 1 ? "has" : "have"} no subtitles.`
    : "";
  if (withSubs <= 0) {
    text.textContent = (data.transcribed
      ? `None of the ${plural(data.total, "file")} here has subtitles.`
      : `None of the ${plural(data.total, "file")} here has subtitles, so there is nothing to search.`) + heard;
    setIndexBar(null);
    return;
  }
  setIndexBar(data.indexed / withSubs);
  const missing = withSubs - data.indexed;
  if (missing <= 0) {
    text.textContent = (noSubs
      ? `All ${plural(withSubs, "file")} with subtitles indexed.${none}`
      : `All ${plural(data.total, "file")} indexed. Dialogue search sees every subtitle here.`) + heard;
  } else {
    text.textContent = (data.indexed === 0
      ? `None of the ${plural(withSubs, "file")} ${noSubs ? "with subtitles " : ""}are indexed yet. Indexing makes dialogue search fast.`
      : `${data.indexed} of ${plural(withSubs, "file")} have searchable subtitles.`) + none + heard;
    index.textContent = `Index ${plural(missing, "file")}` + (indexInBackground ? " in the background" : "");
    index.title = indexInBackground
      ? "Too many files here to index in one go: index them one after another in the background at low priority (Jobs shows it; Cancel keeps what's done)"
      : "Extract and cache the subtitles of the files here that aren't indexed yet, so dialogue search is fast";
    index.hidden = false;
  }
}

async function checkFolderIndex(path) {
  resetIndexCard();
  if (!path) return;
  const myId = ++indexCheckId;
  try {
    const data = await getJSON(`/api/search/folder/index-status?path=${encodeURIComponent(path)}`);
    if (myId !== indexCheckId) return;
    renderIndexStatus(data);
    refreshLibIndex();  // a library index in progress shows on every folder's card
  } catch {
    if (myId === indexCheckId) $("index-card-text").textContent = "Couldn't read the index status.";
  }
}

/** Stream a (re)index of one folder, reporting progress via a callback. */
async function streamFolderIndex(folder, force, onProgress) {
  let url = `/api/search/folder/index?path=${encodeURIComponent(folder)}`;
  if (force) url += "&force=true";
  const resp = await apiFetch(url, { method: "POST" });
  if (!resp.ok) {  // e.g. a folder over the index cap (400)
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
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

function setIndexBusy(on) {
  indexBusy = on;
  $("index-btn").disabled = on;
  $("reindex-folder-btn").disabled = on;
}

// Index the files in this folder that aren't cached yet.
$("index-btn").addEventListener("click", async () => {
  const folder = currentBrowsePath;
  if (!folder || indexBusy) return;
  if (indexInBackground) { startLibIndex(folder); return; }
  const text = $("index-card-text");
  setIndexBusy(true);
  text.textContent = "Indexing…";
  // A status region: busy while the per-file progress ticks by, so a screen reader
  // hears the outcome rather than every file.
  text.setAttribute("aria-busy", "true");
  try {
    await streamFolderIndex(folder, false, (d, t, name) => {
      if (currentBrowsePath !== folder) return;  // user moved on; the work continues server-side
      text.textContent = `Indexing ${d}/${t}: ${name}`;
      setIndexBar(d / t);
    });
  } catch (err) {
    text.removeAttribute("aria-busy");
    if (currentBrowsePath === folder) text.textContent = `Indexing failed: ${err.message}`;
    setIndexBusy(false);
    return;
  }
  text.removeAttribute("aria-busy");
  setIndexBusy(false);
  if (currentBrowsePath === folder) { checkFolderIndex(folder); refreshIndexChips(folder); }
});

// Force-reindex the current folder(s) — clears cache then re-extracts.
$("reindex-folder-btn").addEventListener("click", async () => {
  const folders = dialogueSearchScope || (currentBrowsePath ? [currentBrowsePath] : []);
  if (!folders.length || indexBusy) return;
  const status = $("reindex-folder-status");
  setIndexBusy(true);
  status.hidden = false;
  try {
    for (let i = 0; i < folders.length; i++) {
      const prefix = folders.length > 1 ? `Folder ${i + 1}/${folders.length} — ` : "";
      await streamFolderIndex(folders[i], true, (d, t, name) => {
        status.textContent = `${prefix}Reindexing ${d}/${t}: ${name}`;
        setIndexBar(d / t);
      });
    }
    status.textContent = "Reindexed.";
    if (currentBrowsePath && !dialogueSearchScope) {
      checkFolderIndex(currentBrowsePath);
      refreshIndexChips(currentBrowsePath);
    }
  } catch (err) {
    status.textContent = `Reindex failed: ${err.message}`;
  }
  setIndexBusy(false);
});

// --- subtitles changed since indexing (M20) ---------------------------------------------
// A folder's index status and a folder search report the files whose subtitles changed
// since they were indexed (search leaves them out); the banner offers to re-index exactly
// those. The library home shows what the hourly timestamp-only check found ("Index out of
// date: N files"). Both only check: re-indexing starts from these buttons, never by itself,
// and runs as the server's idle-priority re-index job (POST /api/index/file).
let changedPaths = [];
let changedGen = 0;  // a newer banner (another folder or search) wins over a finished poll

function hideChangedBanner() {
  changedGen++;
  changedPaths = [];
  $("changed-banner").hidden = true;
}

function showChangedBanner(paths, total) {
  hideChangedBanner();
  const n = total || 0;
  if (!n) return;
  changedPaths = paths || [];
  $("changed-banner-text").textContent =
    `${plural(n, "file")} ${n === 1 ? "has" : "have"} new or changed subtitles since indexing —`;
  const btn = $("changed-banner-btn");
  btn.hidden = false;
  btn.disabled = false;
  $("changed-banner").hidden = false;
  // One Re-index button on screen: the banner names these files, so the home badge waits.
  $("changes-badge").hidden = $("changes-reindex-btn").hidden = true;
}

/** Follow the server's re-index job (GET /api/index/file) until nothing is waiting, telling
 *  `say` how far it got; then `done()`. `alive()` false stops it (the view moved on). */
async function followReindex(say, done, alive = () => true) {
  while (alive()) {
    await new Promise((r) => setTimeout(r, document.hidden ? 5000 : 1500));
    if (!alive()) return;
    let st;
    try { st = await getJSON("/api/index/file"); } catch { continue; }
    if (!st.job_id && !st.pending.length) { done(st); return; }
    const p = st.job && st.job.progress ? ` · ${st.job.progress}` : "";
    say(`Re-indexing at low priority: ${plural(st.pending.length, "file")} waiting${p}`);
  }
}

/** The folder on screen, after a re-index: its card, its rows' chips and the badge. */
function refreshAfterReindex() {
  if (currentBrowsePath && !dialogueSearchScope && currentBrowsePath !== "yt:") {
    checkFolderIndex(currentBrowsePath);
    refreshIndexChips(currentBrowsePath);
  } else if (!currentBrowsePath && !dialogueSearchScope) {
    refreshChanges();
  }
}

$("changed-banner-btn").addEventListener("click", async () => {
  const paths = changedPaths.slice();
  if (!paths.length) return;
  const gen = changedGen;
  const btn = $("changed-banner-btn"), text = $("changed-banner-text");
  btn.disabled = true;
  try {
    await postJSON("/api/index/file", { paths });
  } catch (err) {
    text.textContent = `Couldn't re-index them: ${err.message}`;
    btn.disabled = false;
    return;
  }
  btn.hidden = true;
  text.textContent = `Re-indexing ${plural(paths.length, "file")} in the background at low priority…`;
  followReindex((msg) => { if (gen === changedGen) text.textContent = msg; }, () => {
    if (gen !== changedGen) return;
    text.textContent = `Re-indexed ${plural(paths.length, "file")}. Search reads their new subtitles now.`;
    changedPaths = [];
    refreshAfterReindex();
  }, () => gen === changedGen);
});

let changesCheckId = 0;

/** The library home's "Index out of date: N files" badge (GET /api/index/changes). */
async function refreshChanges() {
  const myId = ++changesCheckId;
  let data;
  try { data = await getJSON("/api/index/changes"); } catch { return; }
  if (myId !== changesCheckId || currentBrowsePath || dialogueSearchScope) return;
  if (!$("changed-banner").hidden) return;  // the banner offers the same button
  const badge = $("changes-badge"), btn = $("changes-reindex-btn");
  badge.hidden = btn.hidden = !data.changed;
  if (!data.changed) return;
  const last = data.last_check;
  $("changes-badge-chip").textContent = `Index out of date: ${plural(data.changed, "file")}`;
  $("changes-badge-note").textContent = last && last.at
    ? `Checked ${new Date(last.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`
    : "";
  badge.title = "Files whose subtitles changed since they were indexed, found by the hourly check "
    + "(file dates only) and by the folders you opened. Search leaves them out until they are "
    + "re-indexed; nothing re-indexes on its own.";
  btn.disabled = false;
}

$("changes-reindex-btn").addEventListener("click", async () => {
  const btn = $("changes-reindex-btn"), note = $("changes-badge-note");
  btn.disabled = true;
  let r;
  try {
    r = await postJSON("/api/index/changes/reindex", {});
  } catch (err) {
    note.textContent = `Couldn't re-index them: ${err.message}`;
    btn.disabled = false;
    return;
  }
  note.textContent = `Re-indexing ${plural(r.queued || 0, "file")} in the background at low priority…`;
  const home = () => !currentBrowsePath && !dialogueSearchScope;
  followReindex((msg) => { if (home()) note.textContent = msg; }, () => refreshChanges(), home);
});

// --- library index (M20) -------------------------------------------------------------
// The whole library, or a folder over the index cap, as one background job at low
// priority (POST /api/search/library/index). It starts only from these buttons, after a
// confirm; starting it again picks up where the last run stopped. Every Dialogue index
// card shows a run in progress; the library's home also shows how the last one ended.
let libIndexTimer = null;
let libIndexCheckId = 0;
let libIndexJob = null;  // id of the running job, for Cancel

function libIndexReset() {
  clearTimeout(libIndexTimer);
  libIndexTimer = null;
  libIndexCheckId++;
  libIndexJob = null;
  $("lib-index-btn").hidden = true;
  $("lib-index-cancel").hidden = true;
  $("lib-index-status").hidden = true;
  $("changes-badge").hidden = true;
  $("changes-reindex-btn").hidden = true;
  sttOrphansCheckId++;
  $("stt-orphans").hidden = true;
  $("stt-orphans-btn").hidden = true;
}

/** The library home: the card is the library's own. */
function libIndexHome() {
  $("index-card").hidden = false;
  $("index-card-text").textContent = "Dialogue search reads only indexed files. Index the whole library to search all of it.";
  setIndexBar(null);
  for (const id of ["index-btn", "reindex-folder-btn", "fetch-folder-subs-btn", "stt-folder-nosubs", "stt-folder-gaps"]) $(id).hidden = true;
  $("lib-index-btn").hidden = false;
  refreshLibIndex();
  refreshChanges();
  refreshSttOrphans();
}

// Stored machine transcripts of files gone from the library (M21). Only counted here, on
// the library's home; deleted only by this button, after a confirm (D-037: nothing is
// swept on its own).
let sttOrphansCheckId = 0;

async function refreshSttOrphans() {
  const myId = ++sttOrphansCheckId;
  const line = $("stt-orphans"), btn = $("stt-orphans-btn");
  if (!appConfig.transcribe) { line.hidden = btn.hidden = true; return; }  // off here (D-040)
  let data;
  try { data = await getJSON("/api/transcripts/orphans"); } catch { return; }
  if (myId !== sttOrphansCheckId) return;
  const n = data.count || 0;
  line.textContent = n ? `${plural(n, "machine transcript")} belong${n === 1 ? "s" : ""} to files no longer in the library.` : "";
  line.hidden = btn.hidden = !n;
}

$("stt-orphans-btn").onclick = async () => {
  const btn = $("stt-orphans-btn"), line = $("stt-orphans");
  if (!confirm("Delete the stored machine transcripts of files that are gone from the library? " +
      "Transcripts copied into the library stay.")) return;
  btn.disabled = true;
  try {
    const r = await postJSON("/api/transcripts/clean", {});
    line.textContent = `Deleted ${plural(r.count || 0, "orphaned transcript")}.`;
    announce(line.textContent);
    btn.hidden = true;
  } catch (err) {
    line.textContent = `Couldn't delete them: ${err.message}`;
    announce(line.textContent);
  } finally {
    btn.disabled = false;
  }
};

const libClock = (s) => (s < 60 ? "under a minute" : s < 3600 ? `~${Math.round(s / 60)} min` : `~${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`);

/** "" for a run over the whole library, else " of <folder>". */
const libRunWhere = (r) => (r.whole_library === false && r.paths?.length ? ` of ${r.paths[0].split("/").pop()}` : "");

function libRunSummary(r) {
  const parts = [`${r.indexed || 0} indexed`, `${r.no_subs || 0} without subtitles`];
  if (r.failed) parts.push(`${r.failed} failed`);
  return `${parts.join(", ")} (${r.already || 0} of ${plural(r.files || 0, "file")} were done already)`;
}

async function refreshLibIndex() {
  clearTimeout(libIndexTimer);
  const myId = ++libIndexCheckId;
  const home = !currentBrowsePath && !dialogueSearchScope;
  let data;
  try { data = await getJSON("/api/search/library/index"); } catch { return; }
  if (myId !== libIndexCheckId) return;
  const status = $("lib-index-status"), cancel = $("lib-index-cancel"), btn = $("lib-index-btn");
  const job = data.job, last = data.last;
  libIndexJob = job ? job.id : null;
  if (job) {
    const r = job.result || {};
    status.textContent = job.status === "queued" ? "Library index: waiting to start…"
      : r.status === "indexing" ? `Library index${libRunWhere(r)} at low priority: ${r.done || 0} of ${plural(r.todo || 0, "file")}` +
        (r.eta_s != null ? `, ${libClock(r.eta_s)} left` : "") + (r.current ? ` · ${r.current.split("/").pop()}` : "")
      : `Library index: ${job.progress || "starting"}`;
    status.hidden = false;
    cancel.hidden = false;
    btn.hidden = true;
    $("index-btn").disabled = true;  // one index at a time on this server
    if (home && r.todo) setIndexBar((r.done || 0) / r.todo);
    libIndexTimer = setTimeout(refreshLibIndex, document.hidden ? 10000 : 3000);
    return;
  }
  cancel.hidden = true;
  $("index-btn").disabled = indexBusy;
  if (!home) { status.hidden = true; return; }
  btn.hidden = false;
  if (!last) { status.hidden = true; return; }
  const ended = last.finished || last.updated;
  const when = ended ? new Date(ended * 1000).toLocaleString() : "";
  const run = `Last run${libRunWhere(last)}`;
  status.textContent = last.status === "done" ? `${run} finished ${when}: ${libRunSummary(last)}.`
    : last.status === "cancelled" ? `${run} stopped ${when} at ${last.done || 0} of ${plural(last.todo || 0, "file")}: ${libRunSummary(last)}. Index again to pick up where it stopped.`
    : last.status === "interrupted" ? `${run} was cut short by a server restart (${when}) at ${last.done || 0} of ${plural(last.todo || 0, "file")}. Index again to pick up where it stopped.`
    : `${run}: ${last.status}.`;
  status.hidden = false;
}

async function startLibIndex(folder) {
  const what = folder ? "every file in this folder that isn't indexed yet" : "every file in the library that isn't indexed yet";
  if (!confirm(`Index ${what}? It runs one file after another in the background at low ` +
      "priority, so it can take hours (days for a whole library); Cancel keeps what's done, " +
      "and starting it again picks up where it stopped.")) return;
  const status = $("lib-index-status");
  status.hidden = false;
  status.textContent = "Starting…";
  let r;
  try {
    r = await postJSON("/api/search/library/index" + (folder ? `?path=${encodeURIComponent(folder)}` : ""), {});
  } catch (err) {
    status.textContent = `Couldn't start: ${err.message}`;
    return;
  }
  announce(r.existing ? "A library index is already running" : "Indexing in the background");
  refreshLibIndex();
}

$("lib-index-btn").onclick = () => startLibIndex(null);
$("lib-index-cancel").onclick = async () => {
  if (!libIndexJob) return;
  const cancel = $("lib-index-cancel");
  cancel.disabled = true;
  $("lib-index-status").textContent = "Stopping…";
  try { await apiFetch(`/api/jobs/${encodeURIComponent(libIndexJob)}`, { method: "DELETE" }); } catch {}
  cancel.disabled = false;
  refreshLibIndex();
};

// --- folder transcription (M21) -----------------------------------------------------
// Only from these two buttons, after a confirm: the folder's files one after another, in
// the background at low priority (one transcription at a time on the server).
let sttFolderJob = null;   // { id, folder } in flight, for Cancel
let sttFolderTimer = null;

function sttFolderReset() {
  clearTimeout(sttFolderTimer);
  sttFolderTimer = null;
  sttFolderJob = null;
  $("stt-folder-status").hidden = true;
  $("stt-folder-cancel").hidden = true;
  $("stt-folder-nosubs").hidden = true;
  $("stt-folder-gaps").hidden = true;
}

/** Offer the folder transcriptions that have something to do here. */
function showSttFolder(data) {
  if (!sttAvailable() || data.skipped || sttFolderJob) return;
  const bare = (data.no_subs || 0) + (data.audio_untranscribed || 0);
  $("stt-folder-nosubs").hidden = bare === 0;
  $("stt-folder-gaps").hidden = data.total - (data.no_subs || 0) <= 0;
}

async function startFolderTranscribe(mode) {
  const folder = currentBrowsePath;
  if (!folder) return;
  const what = mode === "gaps" ? "the gaps between the subtitle lines of every file here"
    : "every file here that has no subtitles";
  if (!confirm(`Transcribe ${what}? It runs one file after another in the background at low ` +
      "priority, so it can take hours on this server; Cancel keeps what's done.")) return;
  const status = $("stt-folder-status");
  status.hidden = false;
  status.textContent = "Starting…";
  let r;
  try {
    r = await postJSON("/api/search/folder/transcribe",
      { path: folder, mode, model: sttDefaultModel() || undefined });
  } catch (err) {
    if (currentBrowsePath === folder) status.textContent = `Couldn't start: ${err.message}`;
    return;
  }
  const here = currentBrowsePath === folder;
  if (!r.job_id) { if (here) status.textContent = "Nothing here to transcribe."; return; }
  announce(`Transcribing ${plural(r.candidates, "file")} in the background`);
  // Another folder opened meanwhile: its buttons and status aren't this job's to hide (Jobs
  // shows it).
  if (here) followFolderTranscribe(r.job_id, folder);
}

function followFolderTranscribe(id, folder) {
  sttFolderJob = { id, folder };
  const status = $("stt-folder-status"), cancel = $("stt-folder-cancel");
  $("stt-folder-nosubs").hidden = $("stt-folder-gaps").hidden = true;
  cancel.hidden = false;
  cancel.disabled = false;
  const tick = async () => {
    if (!sttFolderJob || sttFolderJob.id !== id || currentBrowsePath !== folder) return;
    let job;
    try { job = await getJSON(`/api/jobs/${encodeURIComponent(id)}`); }
    catch (err) {
      if (err.status === 404) { sttFolderReset(); return; }
      sttFolderTimer = setTimeout(tick, 5000);
      return;
    }
    if (jobActive(job)) {
      status.textContent = job.status === "queued" ? "Waiting for another transcription to finish…"
        : `Transcribing at low priority: ${job.progress || "starting"}`;
      sttFolderTimer = setTimeout(tick, document.hidden ? 10000 : 3000);
      return;
    }
    const res = job.result || {};
    const msg = job.status === "done" ? `${res.note || "Done"}: ${plural(res.new_lines || 0, "new line")}.`
      : job.status === "cancelled" ? "Stopped; the files done so far keep their transcripts."
      : `Failed: ${job.error || "unknown error"}`;
    announce(msg);
    sttFolderJob = null;
    cancel.hidden = true;
    status.textContent = msg;
    checkFolderIndex(folder);
  };
  cancel.onclick = async () => {
    cancel.disabled = true;
    status.textContent = "Stopping after this chunk…";
    try { await apiFetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch {}
  };
  sttFolderTimer = setTimeout(tick, 800);
}
$("stt-folder-nosubs").onclick = () => startFolderTranscribe("nosubs");
$("stt-folder-gaps").onclick = () => startFolderTranscribe("gaps");

// Backfill YouTube subtitles for every id'd, subtitle-less video in the folder.
let fetchFolderJob = null;   // in-flight job id, for cancel
let fetchFolderPoll = null;  // its poll interval (cleared when leaving the folder)
$("fetch-folder-subs-btn").addEventListener("click", async () => {
  if (!currentBrowsePath) return;
  const btn = $("fetch-folder-subs-btn");
  const cancel = $("fetch-folder-cancel");
  const status = $("fetch-folder-status");
  btn.disabled = true;
  status.hidden = false;
  status.textContent = "Scanning folder…";
  let resp;
  try {
    resp = await postJSON(`/api/search/folder/fetch-subs?path=${encodeURIComponent(currentBrowsePath)}`, {});
  } catch (err) {
    status.textContent = `Failed: ${err.message}`;
    btn.disabled = false;
    return;
  }
  if (!resp.job_id) {  // nothing to do
    status.textContent = "No YouTube videos here need subtitles.";
    btn.disabled = false;
    return;
  }
  fetchFolderJob = resp.job_id;
  cancel.hidden = false;
  status.textContent = `Fetching subtitles for ${resp.candidates} video(s)…`;
  fetchFolderPoll = setInterval(async () => {
    const r = await apiFetch(`/api/jobs/${encodeURIComponent(resp.job_id)}`);
    if (!r.ok) { if (r.status === 404) { clearInterval(fetchFolderPoll); fetchFolderPoll = null; fetchFolderJob = null; } return; }
    const j = await r.json();
    if (j.progress) status.textContent = j.progress;
    if (["done", "failed", "cancelled"].includes(j.status)) {
      clearInterval(fetchFolderPoll);
      fetchFolderPoll = null;
      fetchFolderJob = null;
      cancel.hidden = true;
      btn.disabled = false;
      if (j.status === "failed") status.textContent = `Failed: ${j.error || "see server logs"}`;
      else if (j.status === "cancelled") status.textContent = "Cancelled.";
    }
  }, 2000);
});
$("fetch-folder-cancel").addEventListener("click", async () => {
  if (!fetchFolderJob) return;
  $("fetch-folder-status").textContent = "Cancelling…";
  try { await apiFetch(`/api/jobs/${encodeURIComponent(fetchFolderJob)}`, { method: "DELETE" }); } catch {}
});

// Force-reindex the current file's subtitles, then reload the script.
$("ss-reindex").addEventListener("click", async () => {
  if (!currentItem) return;
  const btn = $("ss-reindex");
  const status = $("ss-reindex-status");
  if (btn.disabled) return;
  btn.disabled = true;
  status.hidden = false;
  status.textContent = "Reindexing…";
  try {
    const data = await postJSON(
      `/api/items/subtitles/reindex?path=${encodeURIComponent(currentItem.path)}`, {},
    );
    status.textContent = `Reindexed (${data.cues} cues).`;
    cueCache.delete(currentItem.path);  // the search preview shows the new lines too
    // Reload the script and subtitle track so the new subs show immediately.
    const track = getSelectedTrack();
    loadScript(currentItem.path, track);
    loadSubtitleTrack(currentItem.path, track, transcodeOffset || 0);
  } catch (err) {
    status.textContent = `Reindex failed: ${err.message}`;
  }
  btn.disabled = false;
});

/** Wire the per-item "Fetch subtitles from YouTube" action (backfill). Runs as
 *  a server job; on success reload the script so the transcript appears. */
function wireFetchSubs(path) {
  const btn = $("ss-fetch-subs");
  const status = $("ss-fetch-subs-status");
  btn.onclick = async () => {
    if (!currentItem || currentItem.path !== path || btn.disabled) return;
    btn.disabled = true;
    status.hidden = false;
    status.textContent = "Fetching subtitles from YouTube…";
    let job;
    try {
      job = await postJSON(`/api/media/fetch-subs?path=${encodeURIComponent(path)}`, {});
    } catch (err) {
      status.textContent = `Failed: ${err.message}`;
      btn.disabled = false;
      return;
    }
    const poll = setInterval(async () => {
      const resp = await apiFetch(`/api/jobs/${encodeURIComponent(job.job_id)}`).catch(() => null);
      if (!resp) return;  // offline for a moment: try again
      if (!resp.ok) {
        if (resp.status === 404) {  // cleared from the Jobs list
          clearInterval(poll);
          status.textContent = "";
          btn.disabled = false;
        }
        return;
      }
      const j = await resp.json();
      if (j.status === "done") {
        clearInterval(poll);
        if (j.progress === "none") {
          status.textContent = "YouTube has no subtitles for this video.";
          btn.disabled = false;
        } else if (currentItem && currentItem.path === path) {
          openItem(currentItem.path, currentItem.name, {});  // reload so the script/search light up
        }
      } else if (j.status === "failed" || j.status === "cancelled") {
        clearInterval(poll);
        status.textContent = j.status === "cancelled" ? "Cancelled." : `Failed: ${j.error || "see server logs"}`;
        btn.disabled = false;
      }
    }, 1500);
  };
}

function renderBreadcrumb(path) {
  const bc = $("breadcrumb");
  bc.innerHTML = "";

  const addLink = (label, target) => {
    const a = el("button", { className: "crumb", text: label,
      title: target ? `Go to ${label}` : "Back to the top of the library" });
    a.dataset.action = "library.browse";
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
  // The YouTube pseudo-folder isn't a filesystem path — one fixed crumb.
  if (path === "yt:") {
    addLink("Library", null);
    addSep();
    addCurrent("▶ YouTube");
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
let keyframeProbe = 0;     // generation of the transcode keyframe probes (newest wins)
// Per-item listeners on the persistent #player / seek slider are scoped to this
// controller; openItem aborts the previous one so they're removed, not just
// neutralized — a previous item's loop can't drive the new player.
let itemListeners = null;
// Range-aware playback controls, rebuilt per item by openItem (see there).
let pb = null;

/** Reflect the player's play/pause state on the custom control + range note. */
function updatePlaybackUI() {
  const player = $("player");
  const paused = ytEmbed ? !ytEmbedPlaying() : player.paused;
  $("pb-playpause-glyph").textContent = paused ? "▶" : "⏸";
  $("pb-playpause").setAttribute("aria-pressed", String(!paused));
  const note = $("pb-range-note");
  const range = wfSel();
  if (range) {
    note.hidden = false;
    note.textContent = `▶ ${formatTime(range[0])} – ${formatTime(range[1])}`;
  } else {
    note.hidden = true;
  }
}
$("player").addEventListener("play", updatePlaybackUI);
$("player").addEventListener("pause", updatePlaybackUI);
$("pb-restart").onclick = () => pb && pb.restart();
$("pb-playpause").onclick = () => pb && pb.toggle();
// Loop selection turned on inside a free range holds playback to it, as ▶ does.
$("pb-loop").addEventListener("change", (e) => {
  const r = wfSel(), t = playerTime();
  if (e.target.checked && r && t >= r[0] - 0.1 && t < r[1]) freeRangeArmed = true;
});
// Lock the player box to the real video aspect once known, so subsequent
// transcode-segment reloads don't resize it and reflow the page.
$("player").addEventListener("loadedmetadata", () => {
  const p = $("player");
  if (p.videoWidth && p.videoHeight) p.style.aspectRatio = `${p.videoWidth} / ${p.videoHeight}`;
});

/** Absolute playback position in the source file. */
function playerTime() {
  if (ytEmbed) return ytEmbed.ready ? ytEmbed.player.getCurrentTime() : 0;
  return $("player").currentTime + transcodeOffset;
}

/** Seek to an absolute position in the source file. */
function seekTo(seconds) {
  if (ytEmbed) {
    rangeSought(seconds);  // the <video> reports its seeks itself ("seeking")
    if (ytEmbed.ready) {
      ytEmbed.player.seekTo(seconds, true);
      ytEmbed.player.playVideo();
    } else {
      // The IFrame API is still loading — remember the target and apply it in
      // onReady, so an early script-line click isn't silently dropped.
      ytEmbed.pendingSeek = seconds;
    }
    return;
  }
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

/** Text reduced to its letters and digits, lowercased and without accents, so
 *  "Amélie (2001)" and "amelie 2001" compare equal. */
function sameTitle(a, b) {
  const norm = (s) => String(s || "").normalize("NFKD").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  return norm(a) !== "" && norm(a) === norm(b);
}

/** The workspace header's poster and Jellyfin title line ("S03E26 · The Best of Both
 *  Worlds", or a movie's title and year), or neither when Jellyfin doesn't know the file.
 *  A movie's line never repeats the heading: in a "Title (Year)" folder it is hidden, and
 *  in a bare "Title" folder it keeps only the year. */
function showItemArtwork(jf) {
  const img = $("item-art");
  const line = $("item-jf-title");
  const two = (n) => String(n).padStart(2, "0");
  let text = "";
  if (jf && jf.type === "Episode") {
    const se = jf.season != null && jf.episode != null ? `S${two(jf.season)}E${two(jf.episode)}` : "";
    text = [se, jf.title].filter(Boolean).join(" · ");
  } else if (jf && jf.title) {
    text = jf.title + (jf.year ? ` (${jf.year})` : "");
  }
  let shown = text;
  if (jf && jf.type === "Movie") {
    const heading = $("item-name").textContent;
    if (sameTitle(heading, text)) shown = "";
    else if (sameTitle(heading, jf.title)) shown = jf.year ? String(jf.year) : "";
  }
  line.textContent = shown;
  line.hidden = !shown;
  // The file's own artwork: an episode's still, a movie's poster.
  const art = jf && jf.art;
  if (!art) {
    img.hidden = true;
    img.removeAttribute("src");
    img.alt = "";
    return;
  }
  img.classList.toggle("wide", !!art.wide);
  img.onerror = () => { img.hidden = true; };
  img.alt = `${art.wide ? "Still" : "Poster"}: ${jf.show && jf.type === "Episode" ? `${jf.show}, ${text}` : (jf.title || "artwork")}`;
  img.src = jfArtUrl(art, art.wide ? 128 : 64);
  img.hidden = false;
}

async function openItem(path, name, opts) {
  const { searchQuery, seekTo: seekTarget, selectWholeClip, markRange, track, fromLink } = opts || {};
  const had = document.activeElement;
  // Re-opening the file on screen (a player switch, fresh subtitles) keeps its history entry.
  const shown = parseRoute(location.hash);
  setRoute(itemRoute(path, {
    t: seekTarget, sel: markRange && markRange.start != null ? [markRange.start, markRange.end] : null, track,
  }), { replace: !!shown && shown.view === "item" && shown.params.get("path") === path });
  showItem();
  // A dialogue-search match to auto-select in Marks once the script loads.
  pendingCueSelect = markRange || null;
  // Consume any pending bookmark clip immediately so an early return (e.g.
  // probe failure) can't leak it into the next item opened.
  const bookmarkClip = pendingBookmarkClip;
  pendingBookmarkClip = null;
  currentItem = { path, name, wantTrack: track ?? null };
  // Remove the previous item's player/slider listeners before wiring this one's.
  if (itemListeners) itemListeners.abort();
  itemListeners = new AbortController();
  const signal = itemListeners.signal;
  $("item-name").textContent = cleanItemName(path, name);
  $("item-filename").textContent = name;
  showItemArtwork(null);
  refocus(had, $("item-name"));  // a keyboard user lands on the workspace's title
  $("stream-selector").hidden = true;
  $("preview-note").textContent = "";
  $("global-search-input").value = searchQuery || "";
  $("search-results").innerHTML = "";
  $("search-empty").hidden = true;
  showClipTarget(false);
  // Reset the per-page Clips list (in-flight polls stop when their row is gone).
  $("clips-made-list").innerHTML = "";
  resetItemTabs();
  $("dialogue-map").hidden = true;
  wfHide();
  setClipIndicator("hidden");
  activeJobId = null;
  clipRangeStart = null;
  clipRangeEnd = null;
  loadedBookmarkCue = null;
  clipFirst = -1;
  clipLast = -1;
  restoreLinePads();  // the last item's free range took its pads with it
  // Tear down a previous item's YouTube embed and restore the <video> element
  // (the embed branch below re-hides it when this item uses the official player).
  destroyYtEmbed();
  destroyHls();
  $("player").hidden = false;
  $("yt-player-row").hidden = true;
  freeRangeArmed = false;
  updatePlaybackUI();  // clear any stale range note / reset the play icon
  $("mark-range-display").innerHTML = "";
  $("mark-save").disabled = true;
  closeBookmarkLabel();
  scriptLoadId++;  // the previous item's script, still loading, must not land here
  scriptCues = [];
  scriptActiveIdx = -1;
  $("script-wrap").hidden = true;
  $("marks-hint").hidden = true;
  $("script-list").innerHTML = "";
  $("stt-bar").hidden = true;
  delete $("stt-bar").dataset.path;  // an item opened anew starts with a clean status line
  $("wf-stt-group").hidden = true;

  let info;
  try {
    info = await getJSON("/api/items" + qp(path) +
      (subtitleLangs() ? `&langs=${encodeURIComponent(subtitleLangs())}` : ""));
  } catch (err) {
    if (signal.aborted) return;  // superseded while the request was in flight
    if (fromLink && [400, 403, 404].includes(err.status)) { await linkFailed(err); return; }
    $("preview-note").innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    return;
  }
  // A newer openItem may have started (aborting our signal) while /api/items was
  // in flight. Bail before touching the shared player/pb/onerror/stream state —
  // otherwise this stale response wires the previous item's media into the new
  // item's view (and a clip would be cut from the wrong file). The per-item
  // listeners below are already signal-scoped; these direct assignments are not.
  if (signal.aborted) return;
  showItemArtwork(info.jellyfin || null);

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
  // A YouTube item has no local file: it plays either through the official
  // YouTube embed (per-browser Player setting; the default on iOS) or the
  // server transcode stream (ffmpeg reading resolved googlevideo URLs).
  const isYouTube = !!info.is_youtube;
  const useEmbed = isYouTube && ytPlayerPref() === "official";
  const ytDownloaded = isYouTube && !!info.downloaded;
  if (isYouTube) {
    const row = $("yt-player-row");
    const sel = $("yt-player-mode");
    row.hidden = false;
    sel.value = ytPlayerPref();
    sel.onchange = () => {
      localStorage.setItem("qcYtPlayer", sel.value);
      // Rewire playback with the new player, keeping the current position.
      const at = playerTime();
      openItem(path, name, at > 0.5 ? { seekTo: at } : {});
    };
    wireYtDownload(path, name, info, signal);
  }
  // Does the primary video codec need re-encoding to H.264 for this (desktop)
  // browser? (HEVC on Firefox, MPEG-4 ASP/XviD, MPEG-2, VC1, …) — B20/B22.
  const videoReencode = !IS_IOS && primaryVideo && needsVideoReencode(primaryVideo.codec);
  const transcodeUrl = "/api/media/transcode" + qp(path) + (videoReencode ? "&venc=1" : "");
  const needsTranscode = !IS_IOS && !useEmbed &&
    ((isYouTube && !ytDownloaded) ||
     (primaryAudio && !BROWSER_AUDIO.has(primaryAudio.codec)) || videoReencode);
  // Desktop transcodes go through HLS rather than the pipe — see the hls block
  // near the top for why. It segments a file on disk, so it needs one: an
  // un-downloaded YouTube ref (ffmpeg reading resolved googlevideo URLs) has
  // none and stays on the pipe. Falls back to the pipe if hls.js won't load.
  const useHls = needsTranscode && (!isYouTube || ytDownloaded);
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
  wfReset(path, info, primaryVideo);

  let firstLoad = true;
  // `autoplay`: undefined → play unless this is the very first load; or an
  // explicit boolean (used by the range loop to seek without auto-playing).
  function loadTranscode(startTime, autoplay) {
    // A new segment is a seek the <video> never reports ("seeking"): one outside a free range
    // lets playback run on past it. The range loop's own return lands on its start, inside.
    rangeSought(startTime);
    isTranscoding = true;
    settingSrc = true;
    transcodeOffset = startTime;
    if (autoplay === undefined) autoplay = !firstLoad;
    firstLoad = false;
    let url = transcodeUrl;
    if (startTime > 0) url += `&start=${startTime}`;
    url += streamParams();  // selected audio stream / channel subset (B17)
    // The transcode is an unbounded ffmpeg pipe: `Accept-Ranges: none`, no
    // Content-Length, so `seekable` is empty. A browser that drops the fetch
    // can't resume with a Range request — it re-opens the URL, restarting the
    // encode from 0. `preload="metadata"` guarantees one such round trip (load
    // metadata, close, re-open on play); "auto" keeps a single connection open
    // and buffering instead. The native path below stays on "metadata".
    player.preload = "auto";
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
      // Only the newest probe of this item may set the offset: an older seek's answer,
      // or one that lands after another item opened, would shift that player's clock.
      const probe = ++keyframeProbe;
      const current = () => !signal.aborted && probe === keyframeProbe;
      getJSON(`/api/media/keyframe?path=${encodeURIComponent(path)}&time=${startTime}`)
        .then((kf) => {
          if (!current()) return;
          transcodeOffset = kf.actual;
          loadSubtitleTrack(path, getSelectedTrack(), kf.actual);
        })
        .catch(() => {
          // Fall back to requested time if probe fails
          if (current()) loadSubtitleTrack(path, getSelectedTrack(), startTime);
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

  // The desktop HLS playlist for the current stream selection. A different
  // audio stream / channel subset is a different token server-side, so this
  // starts (or reuses) its own set of segments.
  function hlsDesktopUrl() {
    return "/api/media/hls" + qp(path) + (videoReencode ? "&venc=1" : "") + streamParams();
  }

  /** Point the element at an HLS playlist, optionally seeking once it loads.
   *  Falls back to the transcode pipe if hls.js is needed but unavailable. */
  function startHls(url, at, autoplay) {
    const applySeek = () => {
      if (at) player.currentTime = at;
      if (autoplay) player.play().catch(() => {});
    };
    destroyHls();
    // hls.js first, native second — see hlsNative() for why the reverse order
    // silently breaks Chrome.
    loadHlsLib().then(() => {
      if (signal.aborted) return;  // superseded while the library loaded
      if (window.Hls && window.Hls.isSupported()) {
        // No hls.js text-track controllers: our playlists carry no subtitles,
        // and they clear the cues of EVERY labelled text track on attach,
        // manifest load and detach — wiping our own <track> whenever its VTT
        // loaded before hls.js attached (a race Firefox kept losing).
        // Start loading at the seek target, and seek once the element has metadata: a
        // seek on MANIFEST_PARSED came before the element could take it and was lost,
        // so a search hit or a link opened at 0:00 instead of its line.
        const inst = hls = new window.Hls({
          startPosition: at || -1,
          timelineController: null,
          subtitleTrackController: null,
          subtitleStreamController: null,
        });
        player.addEventListener("loadedmetadata", applySeek, { once: true, signal });
        inst.on(window.Hls.Events.ERROR, (_evt, data) => {
          // Non-fatal errors are hls.js's own recoverable stalls — it retries.
          if (data && data.fatal && hls === inst) fallbackToPipe(playerTime());
        });
        inst.loadSource(url);
        inst.attachMedia(player);
        return;
      }
      if (hlsNative()) {  // Safari: the element plays the playlist itself
        player.src = url;
        player.addEventListener("loadedmetadata", applySeek, { once: true, signal });
        return;
      }
      fallbackToPipe(at);
    }).catch(() => {
      if (!signal.aborted) fallbackToPipe(at);
    });
  }

  /** Last resort when HLS can't run: the original unseekable pipe, with the
   *  custom seek bar it needs. */
  function fallbackToPipe(at) {
    destroyHls();
    isTranscoding = true;
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    loadTranscode(at || 0);
  }

  // Switching audio stream / channel subset re-encodes; reload at the same
  // position with the new params (a new HLS playlist, or a new pipe segment).
  function reloadStreams() {
    const pos = isTranscoding ? (player.currentTime + transcodeOffset) : player.currentTime;
    const wasPlaying = !player.paused;
    if (useHls && !isTranscoding) {
      startHls(hlsDesktopUrl(), pos || 0, wasPlaying);
      return;
    }
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
    seekSlider.setAttribute("aria-valuetext", `${formatTimeWhole(absTime)} of ${formatTimeWhole(hi)}`);
  }

  // Re-render the seek bar bounds (called when the selected range changes).
  function refreshSeekBar() {
    if (!isTranscoding) return;
    const [, hi] = seekBounds();
    seekDurLabel.textContent = formatTime(hi);
    updateSeekBar();
  }

  player.addEventListener("timeupdate", updateSeekBar, { signal });
  player.addEventListener("seeking", () => rangeSought(player.currentTime + transcodeOffset), { signal });

  // Range loop: when playback reaches the end of the range it is held to (rangeEnded),
  // pause and return to the start so the clip can be previewed again.
  player.addEventListener("timeupdate", () => {
    if (settingSrc) return;
    const absTime = player.currentTime + transcodeOffset;
    if (rangeEnded(absTime, 0.04)) {
      const start = wfSel()[0];
      if ($("pb-loop").checked) {
        seekAbs(start, true);   // keep playing — continuous loop
      } else {
        player.pause();         // paused attribute flips synchronously
        seekAbs(start, false);  // return to start, paused, ready to replay
        updatePlaybackUI();     // reflect the paused state on the icon now
      }
    }
  }, { signal });

  seekSlider.addEventListener("input", () => {
    const [lo, hi] = seekBounds();
    const t = lo + (seekSlider.value / 100) * (hi - lo);
    seekTimeLabel.textContent = formatTime(t);
    seekSlider.setAttribute("aria-valuetext", `${formatTimeWhole(t)} of ${formatTimeWhole(hi)}`);
  }, { signal });

  // Arrow keys step in seconds (1 s, Shift 10 s), not in the range's 0.1 % steps, and the
  // reload waits for the keys to stop.
  let sliderSeekTimer = null;
  signal.addEventListener("abort", () => clearTimeout(sliderSeekTimer));
  seekSlider.addEventListener("keydown", (e) => {
    const [lo, hi] = seekBounds();
    const step = seekKeyStep(e, lo, hi);
    if (!step || !isTranscoding) return;
    e.preventDefault();
    const cur = lo + (seekSlider.value / 100) * (hi - lo);
    const t = Math.max(lo, Math.min(hi, step.to ?? cur + step.delta));
    seekSlider.value = ((t - lo) / Math.max(0.001, hi - lo)) * 100;
    seekSlider.dispatchEvent(new Event("input"));
    clearTimeout(sliderSeekTimer);
    sliderSeekTimer = setTimeout(() => loadTranscode(t), 400);
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
    restart() { const r = playRange(); seekAbs(r ? r[0] : 0, true); },
    toggle() {
      if (!player.paused) { player.pause(); return; }
      const r = playRange();
      if (r) {
        const t = player.currentTime + transcodeOffset;
        if (t < r[0] - 0.1 || t >= r[1] - 0.05) {
          seekAbs(r[0], true);
          return;
        }
      }
      player.play().catch(() => {});
    },
    pause() { player.pause(); },
    refreshSeekBar,
  };

  const seekHint = $("transcode-hint");

  if (useEmbed) {
    // Official YouTube player: the IFrame API supplants the <video> element.
    // A poller stands in for timeupdate (script sync, range preview loop,
    // play/pause icon); marks/bookmarks/clipping are cue-driven and unchanged.
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    // Stop and detach the <video> so a previous server-stream doesn't keep
    // playing (audio) underneath the embed, and so seekTo takes the embed path.
    player.pause();
    player.removeAttribute("src");
    player.load();
    player.hidden = true;
    // Mark embed mode active SYNCHRONOUSLY (player filled in when the API
    // loads): playerTime/seekTo/pb all gate on `.ready`, so an early script
    // click queues into pendingSeek instead of driving the hidden <video>.
    const embed = ytEmbed = { player: null, ready: false, pendingSeek: seekTarget ?? null };
    const holder = $("yt-embed");
    holder.hidden = false;
    const videoId = path.slice("yt:".length);
    loadYtApi().then(() => {
      if (signal.aborted || ytEmbed !== embed) return;  // superseded
      const mount = holder.appendChild(document.createElement("div"));
      embed.player = new YT.Player(mount, {
        videoId,
        playerVars: { playsinline: 1, rel: 0 },
        events: {
          onReady: () => {
            if (ytEmbed !== embed) return;  // superseded before ready
            embed.ready = true;
            if (embed.pendingSeek != null) embed.player.seekTo(embed.pendingSeek, true);
          },
          onStateChange: () => updatePlaybackUI(),
        },
      });
    }).catch(() => {
      if (ytEmbed !== embed) return;
      $("preview-note").textContent =
        "Couldn't load the YouTube player — switch Player to “Server stream”.";
    });
    let lastT = null, lastAt = 0;
    const poll = setInterval(() => {
      if (!ytEmbed || !ytEmbed.ready) return;
      // The embed's own seek bar reports no seeks: a jump more than a second beyond what
      // playback explains is one, and one outside a free range lets playback run on.
      const t = playerTime(), now = performance.now();
      if (lastT !== null) {
        const ran = ytEmbedPlaying() ? ((now - lastAt) / 1000) * (ytEmbed.player.getPlaybackRate() || 1) : 0;
        if (t < lastT - 1 || t > lastT + ran + 1) rangeSought(t);
      }
      lastT = t;
      lastAt = now;
      updateScript();
      updatePlaybackUI();
      // Range preview loop: pause (or loop) at the end of the selection.
      if (ytEmbedPlaying() && rangeEnded(playerTime(), 0.1)) {
        const start = wfSel()[0];
        if ($("pb-loop").checked) {
          ytEmbed.player.seekTo(start, true);
        } else {
          ytEmbed.player.pauseVideo();
          ytEmbed.player.seekTo(start, true);
          updatePlaybackUI();
        }
      }
    }, 250);
    signal.addEventListener("abort", () => clearInterval(poll));
    // Range-aware controls on the embed (replaces the <video>-backed pb above).
    pb = {
      restart() { const r = playRange(); seekTo(r ? r[0] : 0); },
      toggle() {
        if (!ytEmbed || !ytEmbed.ready) return;
        if (ytEmbedPlaying()) { ytEmbed.player.pauseVideo(); return; }
        const r = playRange();
        if (r) {
          const t = playerTime();
          if (t < r[0] - 0.1 || t >= r[1] - 0.05) { seekTo(r[0]); return; }
        }
        ytEmbed.player.playVideo();
      },
      pause() { if (ytEmbed && ytEmbed.ready) ytEmbed.player.pauseVideo(); },
      refreshSeekBar() {},
    };
  } else if (useHls) {
    // Segments are range-served and seeking is native, so the custom seek bar
    // and its offset bookkeeping stay out of it — `transcodeOffset` is 0 and
    // playerTime() is just currentTime.
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    player.preload = "metadata";
    startHls(hlsDesktopUrl(), seekTarget, false);
  } else if (needsTranscode) {
    seekBar.hidden = false;
    seekHint.hidden = false;
    seekDurLabel.textContent = formatTime(probedDuration);
    seekSlider.max = 100;
    loadTranscode(seekTarget || 0);
  } else if (IS_IOS && isYouTube && !ytDownloaded) {
    // The user explicitly chose the server stream on iOS — without a local
    // download that path can't play there (no Matroska/Opus). The official
    // player (the default) can; a downloaded video plays natively via HLS.
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    $("preview-note").textContent =
      "The server stream can't play on iOS — switch Player to “Official YouTube” or download the video.";
  } else {
    // Native playback: raw source on desktop, HLS on iOS (Safari seeks both
    // natively, so the custom transcode seek bar stays hidden).
    seekBar.hidden = true;
    seekHint.hidden = true;
    transcodeOffset = 0;
    // Range-served (or segmented, on iOS) — cheap to open lazily, and the
    // element is shared across items, so undo any "auto" a transcode set.
    player.preload = "metadata";
    player.src = (IS_IOS && !isAudioOnly) ? hlsUrl : mediaUrl;
  }

  player.onerror = () => {
    // hls.js owns its own errors (it retries recoverable ones and calls
    // fallbackToPipe on a fatal), so don't race it with a second fallback.
    if (hls) return;
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
  // A link's selection in a file with no subtitles: no lines to select, so take the range as is.
  if (pendingCueSelect && pendingCueSelect.start != null && $("ss-subs").hidden) {
    const { start, end } = pendingCueSelect;
    pendingCueSelect = null;
    showFreeRange(start, end);
  }

  autoScope();
  if (searchQuery) doSearch(searchQuery, { focus: false });  // the hit's own cue is already selected
  if (seekTarget != null && !needsTranscode) {
    player.addEventListener("loadedmetadata", () => {
      player.currentTime = seekTarget;
    }, { once: true, signal });
  }

  // When opening a clip from the clips library, pre-select the whole file
  // so the clip options are immediately ready for re-export.
  if (selectWholeClip && info.duration) showFreeRange(0, info.duration, "Entire file");

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
  // A YouTube-sourced library video with no subtitles can have its transcript
  // backfilled from YouTube (writable folders only) — offer it here.
  const canBackfill = !hasSubs && info.youtube_id && info.backfill_writable;
  $("ss-fetch-subs").hidden = !canBackfill;
  $("ss-fetch-subs-status").hidden = true;
  if (canBackfill) wireFetchSubs(path);

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
    // A link's track (&track=N) wins when this file has it as a text track.
    const want = currentItem && currentItem.wantTrack;
    if (want != null && tracks.some((t) => String(t.index) === String(want))) {
      subSel.value = String(want); initialTrack = +want;
    } else if (info.has_sidecar) { subSel.value = ""; initialTrack = null; }
    else { subSel.value = String(bestIdx); initialTrack = bestIdx; }
  }
  subSel.onchange = () => {
    const t = getSelectedTrack();
    loadSubtitleTrack(path, t, transcodeOffset || 0);
    loadScript(path, t);
  };
  if (hasSubs) { loadSubtitleTrack(path, initialTrack); loadScript(path, initialTrack); }
  else if (sttHasLines(info.transcript)) {
    // No subtitles, but machine lines (M21): the script and search show them, and the
    // captions once they are copied to the library.
    if (info.transcript.library.length) loadSubtitleTrack(path, null);
    loadScript(path, null);
  }
  renderSttBar(info.transcript, path, hasSubs);

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


// --- machine transcription (M21) ------------------------------------------------------
// whisper.cpp on the server hears what the subtitles leave out. It only ever runs when one
// of these controls is pressed (D-037): Transcribe selection (waveform), Fill gaps or
// Transcribe file (the transcript bar). A run is a background job at low priority; the bar
// follows it and reloads the script when it's done. Machine lines are marked everywhere:
// the "auto" chip, their own colour (--machine) and MACHINE_TITLE.

const STT_MODEL_KEY = "qc_stt_model";
const sttJobs = new Map();  // path -> id of a transcription started from this page, still running
let sttFollowing = null;    // { jobId, path } the bar is following
let sttTimer = null;
const STT_POLL_TRIES = 6;   // a job poll that fails this many times in a row stops asking

/** Is `path` the file open in the item view? A request for another file must not write here. */
const sttHere = (path) => !!currentItem && currentItem.path === path;

/** Stop following a transcription (leaving the item view): sttJobs keeps it, so the bar
 *  follows it again when its file is opened. */
function sttUnfollow() {
  clearTimeout(sttTimer);
  sttTimer = null;
  sttFollowing = null;
}

/** Say `msg` in the bar's status line. Cancel stays only while the open file's job runs. */
function sttStatus(msg) {
  $("stt-job").hidden = false;
  $("stt-cancel").hidden = !(sttFollowing && currentItem && sttFollowing.path === currentItem.path);
  $("stt-job-text").textContent = msg;
}

const sttConfig = () => appConfig.transcribe || {};
const sttAvailable = () => !!sttConfig().available;
const sttModels = () => (sttConfig().models || []).filter((m) => m.present).map((m) => m.name);
function sttDefaultModel() {
  const want = readPref(STT_MODEL_KEY), have = sttModels();
  return have.includes(want) ? want : (sttConfig().default_model || have[0] || "");
}
/** Whether a file's transcript info has any machine lines (stored, or copied to the library). */
const sttHasLines = (t) => !!t && ((t.stored && t.stored.lines > 0) || (t.library || []).length > 0);
const STT_MODEL_LABEL = { "small.en": "small.en (quicker)", "medium.en": "medium.en (better, slower)" };

function fillModelSelect(sel, value) {
  sel.innerHTML = "";
  for (const name of sttModels()) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = STT_MODEL_LABEL[name] || name;
    sel.appendChild(o);
  }
  sel.value = value;
}

/** The transcript bar for the open file (`t`: /api/items' `transcript`): what has been
 *  transcribed, and the actions on it. Hidden for YouTube items and EPUB chapters. */
function renderSttBar(t, path, hasSubs) {
  const bar = $("stt-bar");
  // The status line belongs to the file it was written for: it goes when another file's bar
  // is drawn, not when this one's is redrawn after an action or a job.
  const fresh = bar.dataset.path !== path;
  bar.dataset.path = path;
  const avail = sttAvailable();
  $("wf-stt-group").hidden = !t || !avail;
  bar.hidden = !t || !(avail || sttHasLines(t));
  if (bar.hidden) return;
  bar.dataset.hasSubs = hasSubs ? "1" : "";
  const stored = t.stored, lib = t.library || [];
  const parts = [], brief = [];
  if (stored) {
    parts.push(`${formatTimeWhole(stored.covered)} transcribed with ${stored.models.join(", ")}, ${plural(stored.lines, "line")}`);
    brief.push(plural(stored.lines, "line"));
  }
  if (lib.length) {
    parts.push(`in the library as ${lib.join(", ")}`);
    brief.push("in the library");
  }
  if (!parts.length) {
    parts.push(hasSubs ? "None yet. Lines the subtitles leave out, like a song, can be transcribed."
      : "This file has no subtitles: transcribe it to search what's said.");
    brief.push(hasSubs ? "none yet" : "no subtitles: transcribe to search");
  }
  // The slim row says it in a few words; the open bar in full.
  $("stt-summary").textContent = brief.join(" · ");
  $("stt-detail").textContent = parts.join(" · ");
  const running = sttJobs.get(path);
  $("stt-gaps").hidden = !avail || !hasSubs;
  $("stt-file").hidden = !avail || hasSubs;
  for (const id of ["stt-gaps", "stt-file", "wf-transcribe"]) $(id).disabled = !!running;
  $("stt-model-field").hidden = !avail || sttModels().length < 2;
  if (!$("stt-model-field").hidden) {
    const cur = $("stt-model").value;
    fillModelSelect($("stt-model"), sttModels().includes(cur) ? cur : sttDefaultModel());
  }
  const copy = $("stt-copy");
  copy.hidden = !(stored && stored.lines);
  copy.textContent = lib.length ? "Update the library copy" : "Copy to library";
  copy.disabled = !t.writable;
  copy.title = t.writable
    ? `Copy the machine lines next to the file as ${t.sidecar_name}, so the player's captions and Jellyfin show them too`
    : "This folder is read-only here: add it to the server's writableMediaRoots to copy transcripts into it";
  // A read-only folder says so in words, not only in the disabled button's hover text (D-038).
  $("stt-readonly").hidden = copy.hidden || t.writable;
  $("stt-uncopy").hidden = !(lib.length && t.writable);
  $("stt-export").hidden = !sttHasLines(t);
  $("stt-export").href = `/api/items/transcript${qp(path)}&fmt=srt`;
  $("stt-clear").hidden = !stored;
  // Back on a file whose transcription still runs: show its progress and Cancel again.
  if (running && (fresh || !sttFollowing || sttFollowing.jobId !== running)) followSttJob(running, path);
  else if (!running && fresh) {
    $("stt-job").hidden = true;  // another file's status doesn't carry over
    $("stt-job-text").textContent = "";
  }
}

// The bar is one slim row unless opened (the toggle, or Enter on it); Esc or the toggle
// closes it again. Each browser remembers which.
const STT_OPEN_KEY = "qc_stt_open";
function sttSetOpen(open, save = true) {
  $("stt-body").hidden = !open;
  $("stt-toggle").setAttribute("aria-expanded", String(open));
  $("stt-bar").classList.toggle("open", open);
  if (save) writePref(STT_OPEN_KEY, open ? "1" : null);
}
sttSetOpen(readPref(STT_OPEN_KEY) === "1", false);
$("stt-toggle").onclick = () => sttSetOpen($("stt-body").hidden);
$("stt-bar").addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || $("stt-body").hidden) return;
  e.stopPropagation();  // only the bar closes, nothing behind it
  sttSetOpen(false);
  $("stt-toggle").focus();
  $("stt-toggle").closest(".tip-wrap").classList.add("tip-off");
});

/** Fetch the open file's transcript info again and redraw the bar; with `script`, reload the
 *  script too (the machine lines changed), keeping a line selection where it was. */
async function refreshStt(path, { script = false, captions = false } = {}) {
  let t;
  try { t = await getJSON(`/api/items/transcript${qp(path)}`); } catch { return; }
  if (!currentItem || currentItem.path !== path) return;
  const hasSubs = $("stt-bar").dataset.hasSubs === "1";
  renderSttBar(t, path, hasSubs);
  const track = hasSubs ? getSelectedTrack() : null;
  if (captions) {
    if (hasSubs || t.library.length) loadSubtitleTrack(path, track, transcodeOffset || 0);
    else $("player").querySelectorAll("track").forEach((el) => el.remove());  // nothing left
  }
  if (!script) return;
  // The cue indexes change as lines come and go: carry a line selection over by its times.
  if (hasClipRange()) pendingCueSelect = { start: clipStart(), end: clipEnd() };
  clipFirst = clipLast = -1;
  if (hasSubs || sttHasLines(t)) {
    loadScript(path, track);
  } else {
    // No subtitles and no machine lines left: no script either.
    scriptLoadId++;
    scriptCues = [];
    $("script-list").innerHTML = "";
    $("script-wrap").hidden = true;
    $("marks-hint").hidden = true;
    updateClipUI();
    renderDialogueMap();
  }
}

async function startTranscribe(extra, what) {
  const item = currentItem;
  if (!item) return;
  const path = item.path;
  const model = !$("stt-model-field").hidden && $("stt-model").value ? $("stt-model").value : sttDefaultModel();
  let r;
  try {
    r = await postJSON("/api/items/transcribe", { path, model, audio: getSelectedAudio() ?? 0, ...extra });
  } catch (err) {
    announce(`Couldn't start the transcription: ${err.message}`);
    if (sttHere(path)) sttStatus(`Couldn't start the transcription: ${err.message}`);
    return;
  }
  sttJobs.set(path, r.job_id);
  announce(`Transcribing ${what} in the background`);
  // Another file opened meanwhile: leave its bar (and the job it follows) alone. This one's
  // bar follows the job when the file is opened again (renderSttBar).
  if (sttHere(path)) followSttJob(r.job_id, path);
}

/** Follow a transcription job in the bar: progress, Cancel, and the script reloaded when it
 *  ends. Stops polling when the job ends (or is gone); one job is followed at a time. */
function followSttJob(jobId, path) {
  clearTimeout(sttTimer);
  // This follow's own token: following again (the file opened anew) or unfollowing ends this
  // poll, even a tick still waiting on its answer, so one job never has two polls.
  const follow = sttFollowing = { jobId, path };
  const mine = () => sttFollowing === follow;
  const box = $("stt-job"), text = $("stt-job-text"), cancel = $("stt-cancel");
  const here = () => sttHere(path);
  const enable = () => { for (const id of ["stt-gaps", "stt-file", "wf-transcribe"]) $(id).disabled = false; };
  if (here()) {  // the bar shows the open file: another's job only polls
    box.hidden = false;
    cancel.hidden = false;
    cancel.disabled = false;
    for (const id of ["stt-gaps", "stt-file", "wf-transcribe"]) $(id).disabled = true;
    text.textContent = "Transcribing in the background, at low priority…";
    cancel.onclick = async () => {
      cancel.disabled = true;
      text.textContent = "Stopping…";
      try { await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }); } catch {}
    };
  }
  let failures = 0;
  const tick = async () => {
    if (!mine()) return;
    let job;
    try {
      job = await getJSON(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (err) {
      if (!mine()) return;
      if (err.status === 404) {  // the server restarted, or the job was cleared
        sttJobs.delete(path);
        sttFollowing = null;
        if (here()) { box.hidden = true; enable(); }
        return;
      }
      // Unreachable for a while: stop asking. sttJobs keeps the job, so opening the file
      // again follows it afresh.
      if (++failures >= STT_POLL_TRIES) {
        sttFollowing = null;
        if (here()) text.textContent = "Lost touch with the transcription; open the file again to check on it.";
        return;
      }
      sttTimer = setTimeout(tick, 5000);
      return;
    }
    if (!mine()) return;  // followed afresh, or unfollowed, while asking
    failures = 0;
    if (jobActive(job)) {
      if (here()) {
        text.textContent = job.status === "queued" ? "Waiting for another transcription to finish…"
          : `Transcribing in the background, at low priority: ${job.progress || "starting"}…`;
      }
      sttTimer = setTimeout(tick, here() && !document.hidden ? 1500 : 5000);
      return;
    }
    sttJobs.delete(path);
    sttFollowing = null;
    const res = job.result || {};
    const msg = job.status === "done"
      ? (res.note || `Transcription done: ${plural(res.new_lines || 0, "new line")}, marked auto in the script and search.`)
      : job.status === "cancelled" ? "Transcription stopped; what it had done is kept."
      : `Transcription failed: ${job.error || "unknown error"}`;
    announce(msg);
    if (!here()) return;
    text.textContent = msg;
    cancel.hidden = true;
    enable();
    refreshStt(path, { script: true });
  };
  sttTimer = setTimeout(tick, 600);
}

$("wf-transcribe").onclick = () => {
  const sel = wfSel();
  if (!sel) {
    $("wf-note").textContent = "Select a stretch on the waveform to transcribe it.";
    announce("Select a stretch on the waveform first");
    return;
  }
  // A selection is transcribed again even where it was before: that's how a song gets a
  // second go with the bigger model. Fill gaps and Transcribe file skip what's done.
  startTranscribe({ mode: "range", start: round6(sel[0]), end: round6(sel[1]), redo: true },
    `${formatTime(sel[0])} to ${formatTime(sel[1])}`);
};
$("stt-gaps").onclick = () => startTranscribe({ mode: "gaps" }, "the gaps between the subtitles");
$("stt-file").onclick = () => startTranscribe({ mode: "file" }, "the whole file");

/** Copy to library / Remove from library / Delete transcript: one request, then the bar.
 *  Returns the file's path to refresh, or null when another file has opened since (its bar,
 *  and its buttons, aren't this request's to touch). */
async function sttAction(btn, url, method, done) {
  const path = currentItem && currentItem.path;
  if (!path) return null;
  btn.disabled = true;
  let msg;
  try {
    const resp = await apiFetch(url + qp(path), { method });
    let body = {};
    try { body = await resp.json(); } catch {}
    if (!resp.ok) throw new Error(body.detail || `${resp.status}`);
    msg = done(body);
  } catch (err) {
    msg = `That didn't work: ${err.message}`;
  }
  if (!sttHere(path)) return null;
  btn.disabled = false;
  announce(msg);
  sttStatus(msg);
  return path;
}
$("stt-copy").onclick = async () => {
  const path = await sttAction($("stt-copy"), "/api/items/transcript/library", "POST",
    (b) => `Copied ${plural(b.lines, "machine line")} to the library as ${b.sidecar}; the captions show them now.`);
  if (path) refreshStt(path, { captions: true });
};
$("stt-uncopy").onclick = async () => {
  const path = await sttAction($("stt-uncopy"), "/api/items/transcript/library", "DELETE",
    (b) => b.removed && b.removed.length ? `Removed ${b.removed.join(", ")} from the library.` : "There was no copy in the library.");
  if (path) refreshStt(path, { captions: true, script: true });
};
$("stt-clear").onclick = async () => {
  if (!confirm("Delete this file's machine transcript? A copy in the library stays.")) return;
  const path = await sttAction($("stt-clear"), "/api/items/transcript", "DELETE",
    (b) => b.deleted ? "Deleted the machine transcript." : "There was no transcript to delete.");
  if (path) refreshStt(path, { script: true });
};

// The default model, per browser (the CLI's stt_model setting).
function showSttSetting() {
  const avail = sttAvailable() && sttModels().length > 1;
  $("stt-setting").hidden = !avail;
  if (avail) fillModelSelect($("stt-default-model"), sttDefaultModel());
}
$("stt-default-model").addEventListener("change", () => {
  const v = $("stt-default-model").value;
  writePref(STT_MODEL_KEY, v === sttConfig().default_model ? null : v);
  if (!$("stt-model-field").hidden) $("stt-model").value = v;
});

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

/** "smooth", unless the user asked for reduced motion (a script() call overrides the CSS). */
const scrollBehavior = () =>
  matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

let itemSearchId = 0;  // generation of the in-video search (doSearch)
async function doSearch(query, { focus = true } = {}) {
  if (!currentItem || !query) return;

  const results = $("search-results");
  const empty = $("search-empty");
  results.innerHTML = "";
  empty.hidden = true;
  $("tab-matches").hidden = false;
  if (focus) showTab("matches");

  let url = `/api/search${qp(currentItem.path)}&query=${encodeURIComponent(query)}`;
  const track = getSelectedTrack();
  if (track !== null) url += `&track=${track}`;
  // The item and this search may be replaced while it runs: only the newest search of
  // the item still open may fill the Matches tab.
  const item = currentItem, mine = ++itemSearchId;
  const stale = () => mine !== itemSearchId || currentItem !== item;

  let data;
  try {
    data = await getJSON(url);
  } catch (err) {
    if (stale()) return;
    results.innerHTML = `<li class="error">Search failed: ${escapeHtml(err.message)}</li>`;
    return;
  }
  if (stale()) return;

  setTabCount("matches", data.matches.length);
  const n = data.matches.length;
  announce(n ? `${n} match${n === 1 ? "" : "es"} in this video` : "No matches in this video");
  if (!data.matches.length) {
    empty.hidden = false;
    return;
  }

  for (const m of data.matches) {
    const li = document.createElement("li");
    li.className = m.machine ? "search-result result-machine" : "search-result";
    li.innerHTML =
      `<span class="result-text">${m.machine ? `${AUTO_CHIP}<span class="visually-hidden">Machine transcript: </span>` : ""}${escapeHtml(m.text)}</span>` +
      `<span class="result-meta">` +
        `<span class="result-score" title="${SCORE_TITLE}">${m.score}%</span>` +
        `<span>${m.start_ts} – ${m.end_ts}</span>` +
      `</span>`;
    makeActivatable(li, "Play from this line and select it");
    if (m.machine) li.title = `${MACHINE_TITLE}. ${li.title}`;
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
/** Dialogue of the first *real* selected cue ("" if only the synthetic start line). */
function selectedCueText() {
  if (!hasClipRange()) return "";
  for (let i = clipFirst; i <= clipLast; i++) {
    const c = scriptCues[i];
    if (c && !c.synthetic) return c.text;
  }
  return "";
}
/** Number of real dialogue lines in the selection (excludes the synthetic start line). */
function selectedLineCount() {
  if (!hasClipRange()) return 0;
  let n = 0;
  for (let i = clipFirst; i <= clipLast; i++) if (scriptCues[i] && !scriptCues[i].synthetic) n++;
  return n;
}

// Buffer (before/after padding) — part of the selection (B16). The previewed /
// looped range is the cue range expanded by the buffer (clamped to the file).
let currentDuration = 0;  // set per item in openItem (probed duration)
// Negative padding trims into the line (the waveform's handles can go inside it).
function clipPadBefore() { const v = parseFloat($("clip-before").value); return Number.isFinite(v) ? v : 0; }
function clipPadAfter() { const v = parseFloat($("clip-after").value); return Number.isFinite(v) ? v : 0; }

// Before / After are the padding every line selection gets. A free range (a drag on the
// waveform, I / O, a bookmark) brings its own, and so does I / O on a line: the session's
// pads are set aside meanwhile and come back when another line selection (or none) replaces
// that one, so a drag's 0 s or an I 80 s before the line doesn't stick to every later line.
let linePads = null;  // {before, after, owner}: the pads set aside, and whose pads show now

/** The line selection's key, by its times (they survive the script reloading). */
const lineKey = () => `${clipStart()}-${clipEnd()}`;

/** Set the session's pads aside before a free range (owner null) or I / O on the line
 *  selection (its lineKey) writes its own. */
function setAsidePads(owner) {
  if (!linePads) linePads = { before: $("clip-before").value, after: $("clip-after").value };
  linePads.owner = owner;
}

/** The selection is now lines, or nothing: bring the session's pads back, unless these
 *  lines are the ones I / O padded. */
function restoreLinePads() {
  if (!linePads || (hasClipRange() && lineKey() === linePads.owner)) return;
  $("clip-before").value = linePads.before;
  $("clip-after").value = linePads.after;
  linePads = null;
}

/** The range as the server takes it: padding within 0-60 s. A negative pad, or one over
 *  60 s, moves start / end instead, so the cut lands on the same in and out points. */
function wireRange(start, end) {
  const fold = (pad) => Math.min(60, Math.max(0, pad));
  let before = clipPadBefore(), after = clipPadAfter();
  start = Math.max(0, start - (before - fold(before)));
  end += after - fold(after);
  before = fold(before);
  after = fold(after);
  return { start: round6(start), end: round6(end), before: round6(before), after: round6(after) };
}
/** Buffered start of the previewed clip (cue start − before, clamped ≥ 0). */
function bufStart() { const s = clipStart(); return s === null ? null : Math.max(0, s - clipPadBefore()); }
/** Buffered end of the previewed clip (cue end + after, clamped ≤ duration). */
function bufEnd() {
  const e = clipEnd();
  if (e === null) return null;
  const padded = e + clipPadAfter();
  return currentDuration ? Math.min(currentDuration, padded) : padded;
}

// ▶, ⟲ and Loop selection play wfSel(): the selected lines first, else a free range (a
// waveform drag or I / O, a bookmark, a link's span), with Before / After. A line
// selection always holds playback to it. A free range does once the transport starts it
// (▶ or ⟲, or Loop selection turned on inside it), until a seek outside it or a new free
// range away from the playhead: so playback after I, or from a click elsewhere, runs on
// and O can follow it.
let freeRangeArmed = false;

/** ▶ / ⟲: the range to play (null: the whole file), which now holds playback. */
function playRange() {
  const r = wfSel();
  freeRangeArmed = !!r;
  return r;
}

/** A seek to `t` outside the free range lets playback run on past it. */
function rangeSought(t) {
  const r = wfSel();
  if (r && (t < r[0] - 0.25 || t > r[1] + 0.05)) freeRangeArmed = false;
}

/** Playback at `t` has reached the end of the range it is held to (`slack` s early). */
function rangeEnded(t, slack) {
  const r = wfSel();
  return !!r && (hasClipRange() || freeRangeArmed) && t >= r[1] - slack;
}

function setClipInAt(idx) {
  clipFirst = idx;
  if (clipLast < clipFirst) clipLast = clipFirst;
  updateClipUI();
  announceSelection();
  if (pb) pb.pause();  // stop playback when a clip boundary is set
}

function setClipOutAt(idx) {
  clipLast = idx;
  if (clipFirst < 0 || clipFirst > clipLast) clipFirst = clipLast;
  updateClipUI();
  announceSelection();
  if (pb) pb.pause();
}

function clearClip() {
  clipFirst = -1;
  clipLast = -1;
  updateClipUI();
  announce("Selection cleared");
}

/** Say the new selection: its range and how many lines it holds. */
function announceSelection() {
  if (!hasClipRange()) return;
  const n = selectedLineCount();
  announce(`Selection ${formatTime(bufStart())} to ${formatTime(bufEnd())}, ` +
    (n ? `${n} line${n > 1 ? "s" : ""}` : "before the first line"));
}

function updateClipUI() {
  restoreLinePads();
  // Range display
  const el = $("mark-range-display");
  if (!hasClipRange()) {
    el.innerHTML = "";
    $("mark-save").disabled = true;
    closeBookmarkLabel();  // nothing left to bookmark
    showClipTarget(false);
  } else {
    // Display the buffered range (cue range ± before/after) — what actually
    // plays and gets clipped. The raw cue range still drives clipRangeStart/End
    // (the server applies the padding once at cut time).
    const s = clipStart(), e = clipEnd();
    const bs = bufStart(), be = bufEnd();
    const dur = be - bs;
    const nLines = selectedLineCount();
    const lines = nLines === 0 ? "before the first line" : `${nLines} line${nLines > 1 ? "s" : ""}`;
    el.innerHTML =
      `<span class="range-times">${formatTime(bs)} – ${formatTime(be)}</span> ` +
      `<span>(${dur.toFixed(1)}s, ${lines})</span>` +
      ` <button type="button" class="mark-clear-inline" title="Clear the selection" aria-label="Clear selection">×</button>`;
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
  wfOnSelection();
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

/** The dialogue a selection carries for {cue}: its first line, else a loaded bookmark's. */
const selectionCue = () => selectedCueText() || (clipFirst < 0 ? loadedBookmarkCue || "" : "");

/** Default bookmark label: time range (whole seconds) + first dialogue line. */
function defaultBookmarkLabel() {
  const [s, e] = wfAnchor();
  let label = `${formatTimeWhole(s)} – ${formatTimeWhole(e)}`;
  const firstLine = selectionCue().slice(0, 60);
  if (firstLine) label += ` — ${firstLine}`;
  return label;
}

/** ★ Bookmark: open the inline label field, filled with the default and selected,
 *  so Enter saves it as is and typing replaces it. A second click closes it. */
function openBookmarkLabel() {
  if (!currentItem || !wfAnchor()) return;
  const form = $("bookmark-label-form");
  if (!form.hidden) { closeBookmarkLabel(); return; }
  const input = $("bookmark-label-input");
  input.value = defaultBookmarkLabel();
  form.hidden = false;
  $("mark-save").setAttribute("aria-expanded", "true");
  input.focus();
  input.select();
}

function closeBookmarkLabel() {
  $("bookmark-label-form").hidden = true;
  $("mark-save").setAttribute("aria-expanded", "false");
}

async function saveBookmark() {
  if (!currentItem || !wfAnchor()) return;
  const [s, e] = wfAnchor();
  const defaultLabel = defaultBookmarkLabel();
  const label = $("bookmark-label-input").value.trim();
  closeBookmarkLabel();
  $("mark-save").focus();  // the field it was in is gone
  try {
    const t = getSelectedTrack();
    await postJSON("/api/bookmarks", {
      path: currentItem.path,
      label: label || defaultLabel,
      ...wireRange(s, e),
      sub_track: t != null ? parseInt(t) : null,
      audio_track: getSelectedAudio(),
      channel_subset: getSelectedChan(),
      // Keep the matched dialogue so exporting the bookmark fills the {cue} token.
      cue: selectionCue(),
    });
    announce("Bookmark saved");
    loadBookmarks();
  } catch (err) {
    alert("Failed to save bookmark: " + err.message);
  }
}

$("mark-save").onclick = openBookmarkLabel;
$("bookmark-label-form").addEventListener("submit", (ev) => { ev.preventDefault(); saveBookmark(); });
$("bookmark-label-cancel").onclick = () => { closeBookmarkLabel(); $("mark-save").focus(); };
$("bookmark-label-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); closeBookmarkLabel(); $("mark-save").focus(); }
});

// Before/After buffer inputs (B16): re-render the previewed range live.
["clip-before", "clip-after"].forEach((id) =>
  $(id).addEventListener("input", padsChanged)
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

// One tooltip for every script line (not one per line): what a click does.
const SCRIPT_LINE_TITLE = "Click or Enter to play from here; Start / End (S / E) set the selection; ↑ ↓ move between lines";
// A machine transcript line (M21) carries the "auto" chip, its own colour and this hover text.
// What a search score means (D-041), on every score shown.
const SCORE_TITLE = "Match score: 100% only when the line holds your words verbatim, in order; below 100 they differ (a typo, a word missing or added); 75% and under, the words in any order";
const MACHINE_TITLE = "Machine transcript (whisper), not the subtitles: it may be wrong";
const AUTO_CHIP = `<span class="auto-chip" aria-hidden="true" title="${MACHINE_TITLE}">auto</span>`;

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
  if (!cues.length) {
    list.innerHTML = '<div class="script-loading muted">No subtitle cues found.</div>';
    return;
  }
  // Every script opens with a synthetic "start of video" line so a clip can
  // begin at 0:00 — before any dialogue is shown. It runs from the very start
  // up to the first real cue, so selecting it alone clips the cold open.
  // `synthetic: true` keeps it out of {cue} naming, line counts and search
  // matching; everything else (seek, Start/End, highlight) treats it as a cue.
  scriptCues = [
    { start: 0, end: Math.max(0, cues[0].start), text: "Start of video", synthetic: true },
    ...cues,
  ];

  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  scriptCues.forEach((cue, i) => {
    const row = document.createElement("div");
    const machine = cue.source === "machine";
    row.className = cue.synthetic ? "script-line script-synthetic"
      : machine ? "script-line script-machine" : "script-line";
    row.setAttribute("role", "listitem");
    row.dataset.idx = i;
    const speaker = cue.speaker
      ? `<span class="script-speaker">${escapeHtml(cue.speaker)}</span>` : "";
    const text = cue.synthetic
      ? `${escapeHtml(cue.text)} <span class="muted">(before the first line)</span>`
      : machine ? `${AUTO_CHIP}${escapeHtml(cue.text)}` : escapeHtml(cue.text);
    const at = formatTime(cue.start);
    // Named "time, text" so a screen reader hears where the line is before what it says;
    // a machine line says so first (M21), starting with the chip's visible "auto" (SC 2.5.3).
    const name = `${at}, ${machine ? "auto, machine transcript: " : ""}${cue.speaker ? cue.speaker + ": " : ""}${cue.text}${cue.synthetic ? " (before the first line)" : ""}`;
    const title = machine ? `${MACHINE_TITLE}. ${SCRIPT_LINE_TITLE}` : SCRIPT_LINE_TITLE;
    row.innerHTML =
      `<button type="button" class="script-play" tabindex="${i === 0 ? 0 : -1}" title="${escapeHtml(title)}" aria-label="${escapeHtml(name)}" data-name="${escapeHtml(name)}">` +
        `<span class="script-ts">${at}</span>` +
        `<span class="script-text">${speaker}${text}</span>` +
      `</button>` +
      `<span class="script-actions">` +
        `<button type="button" class="script-mark-btn script-start-btn" tabindex="-1" title="Start the selection at ${at} (S)" aria-label="Start selection at ${at}">Start</button>` +
        `<button type="button" class="script-mark-btn script-end-btn" tabindex="-1" title="End the selection at ${formatTime(cue.end)} (E)" aria-label="End selection at ${formatTime(cue.end)}">End</button>` +
      `</span>`;
    row.onclick = () => scriptLineClick(i);
    row.querySelector(".script-start-btn").onclick = (e) => { e.stopPropagation(); setClipInAt(i); };
    row.querySelector(".script-end-btn").onclick = (e) => { e.stopPropagation(); setClipOutAt(i); };
    frag.appendChild(row);
  });
  list.appendChild(frag);
  renderDialogueMap();

  // Pause auto-scroll when the user manually scrolls, resume after 5 s idle.
  list.addEventListener("scroll", onScriptManualScroll, { passive: true });

  // Auto-select the matching cue range when this item was opened from a
  // dialogue search (consumed once — track switches afterward don't re-select).
  if (pendingCueSelect) {
    const { start, end, at } = pendingCueSelect;
    pendingCueSelect = null;
    if (at != null) {
      // Now playing: the line at or before the playhead (it lags while playing).
      const cue = cueAtOrBefore(at);
      if (cue) selectCueRange(cue.start, cue.end);
    } else {
      selectCueRange(start, end);
    }
  }
}

/** The last real cue that has started by `t`, or null. */
function cueAtOrBefore(t) {
  let found = null;
  for (const c of scriptCues) {
    if (c.synthetic) continue;
    if (c.start > t) break;
    found = c;
  }
  return found;
}

/** Move the script's single tab stop to line `idx` (and focus it when asked). */
function scriptTabStop(idx, focus) {
  const list = $("script-list");
  const row = list.children[idx];
  const play = row && row.querySelector(".script-play");
  if (!play) return;
  list.querySelectorAll('.script-play[tabindex="0"]').forEach((b) => { if (b !== play) b.tabIndex = -1; });
  play.tabIndex = 0;
  if (focus) { play.focus(); row.scrollIntoView({ block: "nearest" }); }
}

// The script is one tab stop: ↑ ↓ (Page Up/Down, Home/End) move between lines, Enter plays
// from the focused line, S and E set the selection's start and end on it.
$("script-list").addEventListener("keydown", (e) => {
  const row = e.target.closest(".script-line");
  if (!row || e.ctrlKey || e.metaKey || e.altKey) return;
  const i = parseInt(row.dataset.idx, 10);
  const last = scriptCues.length - 1;
  const moves = { ArrowDown: i + 1, ArrowUp: i - 1, PageDown: i + 10, PageUp: i - 10, Home: 0, End: last };
  if (e.key in moves) {
    e.preventDefault();
    scriptTabStop(Math.max(0, Math.min(last, moves[e.key])), true);
  } else if (e.key === "s" || e.key === "S") {
    e.preventDefault(); setClipInAt(i);
  } else if (e.key === "e" || e.key === "E") {
    e.preventDefault(); setClipOutAt(i);
  }
});
$("script-list").addEventListener("focusin", (e) => {
  const row = e.target.closest(".script-line");
  if (row && e.target.classList.contains("script-play")) scriptTabStop(parseInt(row.dataset.idx, 10), false);
});

/** Index of the cue whose `edge` ("start"|"end") time is nearest to `t`. */
function nearestCueIndex(t, edge) {
  let best = -1, bestDelta = Infinity;
  for (let i = 0; i < scriptCues.length; i++) {
    if (scriptCues[i].synthetic) continue;  // never match the "start of video" line
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
  scriptTabStop(first, false);  // Tab into the script lands on the selection
  scriptCenter(first);
}

/** Scroll line `idx` to the middle of the script panel. */
function scriptCenter(idx) {
  const list = $("script-list");
  const row = list.children[idx];
  if (row) {
    scriptProgScroll = true;
    const listRect = list.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    list.scrollTo({
      top: list.scrollTop + (rowRect.top - listRect.top) - listRect.height / 2,
      behavior: scrollBehavior(),
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
  updateMapPlayhead();
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
  if (prev) { prev.classList.remove("active"); prev.firstElementChild?.removeAttribute("aria-current"); }

  if (idx >= 0) {
    const row = list.children[idx];
    if (row) {
      row.classList.add("active");
      row.firstElementChild?.setAttribute("aria-current", "time");
      if (scriptAutoScroll && $("script-follow").checked) {
        scriptProgScroll = true;
        const listRect = list.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        const targetTop = list.scrollTop + (rowRect.top - listRect.top) - listRect.height / 3;
        list.scrollTo({ top: targetTop, behavior: scrollBehavior() });
        setTimeout(() => { scriptProgScroll = false; }, 600);
      }
    }
  }
}

/** Highlight the selected clip range on the script. */
function updateScriptHighlight() {
  updateMapSelection();
  const list = $("script-list");
  if (!list || !scriptCues.length) return;
  for (let i = 0; i < scriptCues.length; i++) {
    const row = list.children[i];
    if (!row) continue;
    const inRange = hasClipRange() && i >= clipFirst && i <= clipLast;
    if (row.classList.contains("in-range") !== inRange) {
      const play = row.firstElementChild;
      if (play) play.setAttribute("aria-label", play.dataset.name + (inRange ? ", selected" : ""));
    }
    row.classList.toggle("in-range", inRange);
    row.classList.toggle("range-first", i === clipFirst && hasClipRange());
    row.classList.toggle("range-last", i === clipLast && hasClipRange());
  }
}

$("player").addEventListener("timeupdate", updateScript);

// --- workspace tabs: Script / Matches / Bookmarks / Clips ---------------------

const TAB_PANES = { script: "pane-script", matches: "item-search-panel", bookmarks: "bookmarks-panel", clips: "clips-made-panel" };

function showTab(name) {
  for (const [tab, pane] of Object.entries(TAB_PANES)) {
    const on = tab === name;
    $(pane).hidden = !on;
    $("tab-" + tab).setAttribute("aria-selected", String(on));
    $("tab-" + tab).tabIndex = on ? 0 : -1;  // the tab list is one tab stop; arrows move in it
  }
  if (name === "script") keepSelectionInView();
}

/** A hidden pane loses its scroll position: bring the selection (or playing line) back. */
function keepSelectionInView() {
  const list = $("script-list");
  const idx = hasClipRange() && clipFirst >= 0 ? clipFirst : scriptActiveIdx;
  const row = idx >= 0 ? list.children[idx] : null;
  if (!row || !list.clientHeight) return;
  const listRect = list.getBoundingClientRect();
  const rowRect = row.getBoundingClientRect();
  list.scrollTop += (rowRect.top - listRect.top) - listRect.height / 3;
}

function setTabCount(tab, n) {
  $("count-" + tab).textContent = n ? String(n) : "";
}

/** A new item starts on the Script tab with no matches and no clips made yet. */
function resetItemTabs() {
  $("tab-matches").hidden = true;
  $("tab-clips").hidden = true;
  for (const t of ["matches", "bookmarks", "clips"]) setTabCount(t, 0);
  showTab("script");
}

document.querySelector(".tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});
// Arrow keys move between the visible tabs (and select them), as in the ARIA tabs pattern.
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
  const tabs = [...document.querySelectorAll(".tabs .tab")].filter((t) => !t.hidden);
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1
    : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  showTab(tabs[next].dataset.tab);
});

$("clip-options-toggle").onclick = () => {
  const bar = document.querySelector(".ws-clip");
  const open = bar.classList.toggle("opts-open");
  $("clip-options-toggle").setAttribute("aria-expanded", String(open));
};

// --- dialogue map: where the dialogue is across the whole runtime ---------------
//
// One bar per slice of the runtime, as tall as the number of cues starting in it,
// built from the script that is already loaded (no extra request). The selected
// range (lines or a free range, as the transport plays it) is amber, the playhead blue;
// clicking seeks.

const DM_BARS = 160;

function mapDuration() {
  if (currentDuration) return currentDuration;
  return scriptCues.length ? scriptCues[scriptCues.length - 1].end : 0;
}

function renderDialogueMap() {
  const map = $("dialogue-map");
  const dur = mapDuration();
  const real = scriptCues.filter((c) => !c.synthetic);
  if (!dur || real.length < 2) { map.hidden = true; return; }
  const counts = new Array(DM_BARS).fill(0);
  for (const c of real) counts[Math.min(DM_BARS - 1, Math.floor((c.start / dur) * DM_BARS))]++;
  const max = Math.max(...counts) || 1;
  const frag = document.createDocumentFragment();
  for (const n of counts) {
    const bar = document.createElement("i");
    if (n) bar.style.height = (14 + 86 * Math.sqrt(n / max)).toFixed(1) + "%";
    else bar.className = "empty";
    frag.appendChild(bar);
  }
  const holder = $("dialogue-map-bars");
  holder.innerHTML = "";
  holder.appendChild(frag);
  $("dialogue-map-total").textContent = formatTimeWhole(dur);
  map.hidden = false;
  updateMapSelection();
  updateMapPlayhead();
  if (wf.path) wfViewChanged();
}

/** The amber band: wfSel(), the range the transport plays (the selected lines, else a
 *  free range: a waveform drag or I / O, a bookmark, a link's span), with Before / After. */
function updateMapSelection() {
  const map = $("dialogue-map");
  const sel = $("dialogue-map-sel");
  const dur = mapDuration();
  const range = map.hidden || !dur ? null : wfSel();
  if (!range) { sel.hidden = true; return; }
  const [s, e] = range;
  sel.hidden = false;
  sel.style.setProperty("--f", String(Math.min(1, s / dur)));
  sel.style.setProperty("--w", String(Math.max(0.004, (e - s) / dur)));
}

function updateMapPlayhead() {
  const dur = mapDuration();
  if ($("dialogue-map").hidden || !dur) return;
  const t = mapPending ?? playerTime();
  $("dialogue-map-head").style.setProperty("--f", String(Math.max(0, Math.min(1, t / dur))));
  setSliderValue($("dialogue-map"), t, dur);
}

/** A seek slider's value for assistive technology: whole seconds, "3:42 of 1:19:16". */
function setSliderValue(node, t, dur) {
  const now = String(Math.round(Math.max(0, Math.min(dur, t))));
  if (node.getAttribute("aria-valuenow") === now && node.getAttribute("aria-valuemax") === String(Math.round(dur))) return;
  node.setAttribute("aria-valuemax", String(Math.round(dur)));
  node.setAttribute("aria-valuenow", now);
  node.setAttribute("aria-valuetext", `${formatTimeWhole(+now)} of ${formatTimeWhole(dur)}`);
}

/** Arrow-key seeking: ← → step 1 s, Shift 10 s, Page Up/Down 1 min, Home/End the ends. */
function seekKeyStep(e, lo, hi) {
  const big = e.shiftKey ? 10 : 1;
  const d = { ArrowRight: big, ArrowUp: big, ArrowLeft: -big, ArrowDown: -big, PageUp: 60, PageDown: -60 }[e.key];
  if (d !== undefined) return { delta: d };
  if (e.key === "Home") return { to: lo };
  if (e.key === "End") return { to: hi };
  return null;
}

// The map's keyboard seeks land after a short pause, so holding an arrow key doesn't reload a
// transcode once per step.
let mapPending = null;
let mapSeekTimer = null;
$("dialogue-map").addEventListener("keydown", (e) => {
  const dur = mapDuration();
  const step = dur && seekKeyStep(e, 0, dur);
  if (!step) return;
  e.preventDefault();
  const from = mapPending ?? playerTime();
  mapPending = Math.max(0, Math.min(dur, step.to ?? from + step.delta));
  updateMapPlayhead();
  clearTimeout(mapSeekTimer);
  mapSeekTimer = setTimeout(() => { const t = mapPending; mapPending = null; seekTo(t); }, 300);
});

$("dialogue-map").addEventListener("click", (e) => {
  const dur = mapDuration();
  const r = $("dialogue-map-bars").getBoundingClientRect();
  if (!dur || !r.width) return;
  const t = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur;
  if (wf.path) { const span = wfSpan(); wfSetView(t - span / 2, t + span / 2); }
  seekTo(t);
});
$("dialogue-map").addEventListener("mousemove", (e) => {
  const dur = mapDuration();
  const r = $("dialogue-map-bars").getBoundingClientRect();
  if (dur && r.width) $("dialogue-map").title = "Seek to " + formatTimeWhole(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur);
});

// --- waveform strip (M18): exact in and out points ------------------------------------
//
// The script and the dialogue map make the rough selection; this strip sets the exact cut.
// It shows a window of the file, from the whole runtime down to ~20 ms across, drawn from
// GET /api/items/peaks (item.peaks): a cached overview for long windows, a decode of just
// the window for short ones, raw samples at the deepest zoom. The in and out handles are
// the selection's padded start and end, so dragging one rewrites Before / After (negative
// when it trims into the line). Snapping asks GET /api/items/snap (item.snap).
//
// A drag across the strip (a long press, then a drag, on touch), or I / O at the playhead,
// makes a selection that isn't made of lines: a free range, as a bookmark loads one. It
// is cut by start and end (clip.create --start/--end), so it needs no command of its own.

const WF_MIN_SPAN = 0.02;     // s across the strip at the deepest zoom
const WF_ZOOM_STEP = 2;       // per button press or + / - key
const WF_ZERO_SPAN = 10;      // s: zero-crossing snap applies at this zoom or closer
const WF_CUE_SNAP_PX = 8;     // cue-edge snap reach while dragging
const WF_CUE_LANE = 18;       // px: subtitle spans along the top
const WF_RULER = 16;          // px: time ruler along the bottom
const WF_DEFAULT_FPS = 25;    // the Alt-nudge frame when the source has no video
const WF_DRAG_PX = 4;         // px a press moves before it is a drag, not a click
const WF_HOLD_MS = 450;       // a touch held this long without moving drags out a selection
const WF_POINT_SPAN = 2;      // s: I or O with nothing selected selects this much from the playhead

const wf = {
  path: null, audio: 0, dur: 0, fps: 0, hasVideo: false,
  view: [0, 0],      // seconds shown
  data: null,        // the last peaks response for (roughly) this view
  base: null,        // the whole-file response: drawn again when the view goes back to it
  filePeak: 0,       // largest sample in the file (from the overview), int16 units
  keyframes: null,   // {start, end, times} for the last window that listed them
  req: 0, loadTimer: null, pollTimer: null,
  abort: null,       // the AbortController of the peaks request in flight
  failed: false,     // the last load failed: say so in the note instead of looking busy
  drag: null,        // {kind: "in"|"out"|"pan"|"pinch", ...}
  pointers: new Map(),
  snapSeq: 0,
  fromHandle: false, // the selection change came from a handle: don't move the view
  raf: 0, lastHead: null, nudgeTimer: null,
  auditionEnd: null, auditionTimer: null,
  cue: -1,           // the line the cue lane's keyboard focus is on (an index into scriptCues)
  hover: -1,         // the line whose label the pointer is over
};
let clipTargetOn = false;  // showClipTarget's state: a range is ready to clip

const wfSpan = () => wf.view[1] - wf.view[0];
const wfWidth = () => $("wf-stage").clientWidth || 1;
const wfX = (t) => ((t - wf.view[0]) / wfSpan()) * wfWidth();
const wfT = (x) => wf.view[0] + (x / wfWidth()) * wfSpan();
const round6 = (v) => Math.round(v * 1e6) / 1e6;

/** The selection's unpadded range: the selected lines, else a loaded bookmark or link range. */
function wfAnchor() {
  if (hasClipRange()) return [clipStart(), clipEnd()];
  if (clipTargetOn && clipRangeStart != null && clipRangeEnd != null) return [clipRangeStart, clipRangeEnd];
  return null;
}

/** [in, out]: the anchor widened (or narrowed) by Before / After, or null. */
function wfSel() {
  const a = wfAnchor();
  if (!a) return null;
  const i = Math.max(0, a[0] - clipPadBefore());
  let o = a[1] + clipPadAfter();
  const dur = wf.dur || currentDuration;
  if (dur) o = Math.min(dur, o);
  return [i, Math.max(o, i + 0.001)];
}

/** Time with as many decimals as the zoom calls for: "1:02.345". */
function formatTimePrecise(s, decimals) {
  const neg = s < 0;
  s = Math.abs(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(decimals).padStart(decimals ? decimals + 3 : 2, "0");
  const out = h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  return neg ? "-" + out : out;
}
const wfDecimals = () => (wfSpan() <= 0.5 ? 4 : wfSpan() <= 5 ? 3 : wfSpan() <= 60 ? 2 : 1);

function formatSpan(s) {
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  return formatTimeWhole(s);
}

/** Drop the peaks request in flight: whatever it answers belongs to a view no longer shown. */
function wfCancel() {
  wf.req++;
  if (wf.abort) wf.abort.abort();
  wf.abort = null;
}

function wfHide() {
  wf.path = null;
  wfCancel();
  wf.snapSeq++;  // a snap still on its way belongs to the item being left
  clearTimeout(wf.nudgeTimer);
  wf.pointers.clear();
  if (wf.drag) clearTimeout(wf.drag.hold);
  wf.drag = null;
  $("wf-stage").classList.remove("wf-holding", "wf-panning");
  clearTimeout(wf.loadTimer);
  clearTimeout(wf.pollTimer);
  cancelAnimationFrame(wf.raf);
  wf.raf = 0;
  wf.auditionEnd = null;
  clearInterval(wf.auditionTimer);
  $("waveform").hidden = true;
  $("dialogue-map-view").hidden = true;
}

/** A new item: show its whole runtime and start loading peaks. */
function wfReset(path, info, primaryVideo) {
  wfHide();
  const dur = info.duration || 0;
  if (!dur) return;
  // A negative pad is one line's exact trim (a handle pulled inside it), not a preference.
  ["clip-before", "clip-after"].forEach((id) => { if (parseFloat($(id).value) < 0) $(id).value = "0"; });
  Object.assign(wf, {
    path, dur, view: [0, dur], data: null, base: null, filePeak: 0, keyframes: null, failed: false,
    audio: getSelectedAudio() ?? 0, hasVideo: !!primaryVideo,
    fps: (primaryVideo && primaryVideo.frame_rate) || 0, lastHead: null, cue: -1, hover: -1,
  });
  $("waveform").hidden = false;
  wfNote("");
  wfCutNote();
  wfViewChanged();
  setTimeout(wfLoad, 0);  // after openItem fills the stream selector (the audio stream)
  wf.raf = requestAnimationFrame(wfTick);
}

/** Show `a..b` (clamped to the file, at least WF_MIN_SPAN wide). */
function wfSetView(a, b) {
  const dur = wf.dur;
  let span = Math.max(WF_MIN_SPAN, Math.min(dur, b - a));
  a = Math.max(0, Math.min(dur - span, a));
  wf.view = [a, a + span];
  // Only the newest view's peaks are drawn: a response still on its way for an older one
  // would be a zoom (or a page) behind.
  wfCancel();
  wf.failed = false;
  wfViewChanged();
  clearTimeout(wf.loadTimer);
  wf.loadTimer = setTimeout(wfLoad, 120);
}

function wfViewChanged() {
  const [a, b] = wf.view;
  $("wf-span").textContent = `${formatSpan(b - a)} across`;
  $("wf-zoom-in").disabled = b - a <= WF_MIN_SPAN + 1e-9;
  $("wf-zoom-out").disabled = $("wf-zoom-all").disabled = a <= 0 && b >= wf.dur - 1e-6;
  // The dialogue map outlines the window the strip shows.
  const view = $("dialogue-map-view");
  const whole = a <= 0 && b >= wf.dur - 1e-6;
  view.hidden = $("dialogue-map").hidden || whole;
  if (!view.hidden) {
    view.style.setProperty("--f", String(a / wf.dur));
    view.style.setProperty("--w", String(Math.max(0.003, (b - a) / wf.dur)));
  }
  wfDraw();
}

/** Zoom by `factor` (>1 closer) keeping time `at` where it is on screen. */
function wfZoom(factor, at) {
  const [a, b] = wf.view;
  const span = b - a;
  if (at == null) at = wfFocusTime();
  const next = Math.max(WF_MIN_SPAN, Math.min(wf.dur, span / factor));
  const f = span ? (at - a) / span : 0.5;
  wfSetView(at - f * next, at - f * next + next);
}

/** What + / - zoom around: a focused handle, else the playhead when it's in view, else the middle. */
function wfFocusTime() {
  const sel = wfSel();
  const act = document.activeElement;
  if (sel && act === $("wf-in")) return sel[0];
  if (sel && act === $("wf-out")) return sel[1];
  const t = playerTime();
  if (t >= wf.view[0] && t <= wf.view[1]) return t;
  return (wf.view[0] + wf.view[1]) / 2;
}

/** Bring the selection into view with a margin (when it changes from the script, a bookmark…). */
function wfFollowSelection() {
  const sel = wfSel();
  if (!sel || !wf.path) return;
  const margin = Math.max(0.15, (sel[1] - sel[0]) * 0.25);
  wfSetView(sel[0] - margin, sel[1] + margin);
}

/** The selection changed: redraw, and move the view to it unless a handle did it. */
function wfOnSelection() {
  updateMapSelection();
  if (!wf.path) return;
  if (!wf.fromHandle) wfFollowSelection();
  wfDraw();
}

// --- loading ---------------------------------------------------------------------------------

function b64ToInt16(s) {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Int16Array(bytes.buffer, 0, bytes.length >> 1);
}

/** How many bins to ask for across `span` seconds. Under ~170 ms, every sample (the server
 *  sends raw samples when they fit in the bins). */
function wfBins(span) {
  return span <= 0.17 ? 8192
    : Math.max(16, Math.min(8192, Math.round(wfWidth() * Math.min(2, window.devicePixelRatio || 1))));
}

async function wfLoad() {
  clearTimeout(wf.pollTimer);
  if (!wf.path) return;
  const audio = getSelectedAudio() ?? 0;
  if (audio !== wf.audio) Object.assign(wf, { audio, base: null, data: null, filePeak: 0 });
  const [a, b] = wf.view;
  const bins = wfBins(b - a);
  const kf = wf.hasVideo && b - a <= 60;
  wfCancel();
  const req = wf.req;
  const ctl = wf.abort = new AbortController();
  wf.failed = false;
  const url = `/api/items/peaks${qp(wf.path)}&start=${a.toFixed(6)}&end=${b.toFixed(6)}` +
    `&bins=${bins}&audio=${audio}${kf ? "&keyframes=true" : ""}`;
  let resp, body;
  try {
    resp = await apiFetch(url, { signal: ctl.signal });
    body = await resp.json();
  } catch {
    if (req === wf.req) wfFailed("Couldn't load the waveform.");
    return;
  }
  if (req !== wf.req || !wf.path) return;  // superseded by a newer view or item
  wf.abort = null;
  if (!resp.ok) {
    wfFailed(`No waveform: ${body.detail || resp.status}`);
    return;
  }
  const ov = body.overview || {};
  if (resp.status === 202) {
    // The overview is still being built: say how far along, and ask again shortly. A
    // failed build stops asking (the server retries it when the view next asks).
    const failed = ov.state === "error";
    wf.failed = failed;
    wfNote(failed ? `The waveform overview failed: ${ov.error || "ffmpeg error"}.`
      : `Building the waveform overview… ${Math.round((ov.progress || 0) * 100)}% ` +
        "(zoom in under 2 minutes to see the audio now).", !failed);
    if (!failed) wf.pollTimer = setTimeout(wfLoad, 1500);
    wfDraw();
    return;
  }
  const building = ov.state === "queued" || ov.state === "building";
  wfNote(building ? `Building the waveform overview… ${Math.round((ov.progress || 0) * 100)}%` : "",
    building);
  const data = { ...body, values: b64ToInt16(body.data) };
  wf.data = data;
  if (data.source === "overview" && data.peak) wf.filePeak = data.peak;
  if (data.start <= 0 && data.end >= Math.min(wf.dur, data.duration || wf.dur) - 0.05) wf.base = data;
  if (body.keyframes) wf.keyframes = { start: data.start, end: data.end, times: body.keyframes };
  wfCutNote();
  wfDraw();
  // Keep asking while the overview builds, so zooming out later is instant.
  if (ov.state === "queued" || ov.state === "building") {
    wf.pollTimer = setTimeout(() => { if (wf.path && !wf.filePeak) wfLoad(); }, 3000);
  }
}

/** A load failed: say why, and stop looking busy (there's nothing on its way). */
function wfFailed(text) {
  wf.failed = true;
  wfNote(text);
  wfDraw();
}

/** The strip's status line. While `busy` (progress ticking by) its region is aria-busy, so
 *  a screen reader waits for the outcome instead of reading every percentage. */
function wfNote(text, busy = false) {
  const note = $("wf-note");
  note.textContent = text;
  if (busy) note.setAttribute("aria-busy", "true");
  else note.removeAttribute("aria-busy");
}

/** Say what the cut will do with the in point: frame/sample exact, keyframe, or codec frame. */
function wfCutNote() {
  const note = $("wf-cut-note");
  if (!wf.path) { note.textContent = ""; return; }
  const audioOut = currentItem && (currentItem.audioOnly || $("clip-audio-only").checked);
  const fmt = $("clip-format").value;
  if (!audioOut && wf.hasVideo) {
    note.textContent = $("clip-exact").checked
      ? "Exact is on: the clip starts on the in point."
      : "Lossless video starts on a keyframe (◆): the clip begins at the keyframe before the in point.";
  } else {
    note.textContent = fmt === "lossless"
      ? "Passthrough audio starts on a codec frame (about 20–30 ms); choose FLAC or WAV to start on the sample."
      : `${fmt.toUpperCase()} starts on the in point, to the sample.`;
  }
}

// --- drawing ---------------------------------------------------------------------------------

function wfColors() {
  const cs = getComputedStyle($("waveform"));
  const v = (n) => cs.getPropertyValue(n).trim();
  return {
    wave: v("--dim"), faint: v("--faint"), line: v("--line"), strong: v("--line-strong"),
    accent: v("--accent"), soft: v("--accent-soft"), text: v("--text"), raised: v("--raised"),
    head: v("--playhead"), machine: v("--machine"), machineSoft: v("--machine-soft"),
  };
}

// While the peaks for a new view load, the strip is dimmed (aria-busy) rather than showing a
// coarser response stretched to the new zoom, which draws as blocks (M20). Only a response at
// least as sharp as the view asks for is drawn: the one for this view, or an older one with
// bins as narrow (after a pan) or narrower (after zooming out), over the part it covers.

/** Within a pixel of the view's edges (the server echoes them rounded, the file's end clamped). */
const wfNear = (x, y) => Math.abs(x - y) <= Math.max(1e-5, wfSpan() / wfWidth());

/** Is response `d` sharp enough to draw at this zoom? */
function wfSharp(d) {
  if (!d) return false;
  const [a, b] = wf.view;
  if (d.kind === "samples" || (wfNear(d.start, a) && wfNear(d.end, b))) return true;
  return (d.end - d.start) / (d.values.length >> 1) <= ((b - a) / wfBins(b - a)) * 1.1;
}

/** What to draw: {src, full}. `src` is the sharp response that covers the view (`full`), else
 *  one that covers part of it, else null. */
function wfSource() {
  const [a, b] = wf.view;
  const sharp = [wf.data, wf.base].filter(wfSharp);
  // The server clamps `end` to the file's duration, which can fall short of the probed one
  // the view runs to: a response that reaches the file's end covers the view.
  const reach = (d) => (d.duration ? Math.min(b, d.duration) : b);
  const full = sharp.find((d) => (d.start <= a || wfNear(d.start, a)) && (d.end >= reach(d) || wfNear(d.end, reach(d))));
  if (full) return { src: full, full: true };
  return { src: sharp.find((d) => d.end > a && d.start < b) || null, full: false };
}

/** Dim the strip (and tell assistive tech it's updating) until the view's peaks arrive. */
function wfBusy(on) {
  const stage = $("wf-stage");
  if (on) stage.setAttribute("aria-busy", "true");
  else stage.removeAttribute("aria-busy");
}

function wfDraw() {
  if (!wf.path) return;
  const stage = $("wf-stage");
  const canvas = $("wf-canvas");
  const w = stage.clientWidth, h = stage.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const c = wfColors();
  const [a, b] = wf.view;
  const span = b - a;
  const top = WF_CUE_LANE, bottom = h - WF_RULER;
  const mid = (top + bottom) / 2, half = (bottom - top) / 2 - 2;

  // Selection: shaded between the handles.
  const sel = wfSel();
  if (sel) {
    ctx.fillStyle = c.soft;
    const x0 = Math.max(0, wfX(sel[0])), x1 = Math.min(w, wfX(sel[1]));
    if (x1 > x0) ctx.fillRect(x0, 0, x1 - x0, bottom);
  }

  // Subtitle lines as labelled spans along the top.
  ctx.font = `11px ${getComputedStyle(document.body).fontFamily}`;
  ctx.textBaseline = "middle";
  const lane = $("wf-cues");
  lane.hidden = !scriptCues.some((cue) => !cue.synthetic);
  for (let i = 0; i < scriptCues.length; i++) {
    const cue = scriptCues[i];
    if (cue.synthetic || cue.end < a || cue.start > b) continue;
    const x0 = wfX(cue.start), x1 = wfX(cue.end);
    const l = Math.max(0, x0), r = Math.min(w, x1);
    if (r - l < 0.5) continue;
    const machine = cue.source === "machine";  // a machine line keeps its own colour here too
    // Opaque first, so the selection's shade doesn't show through a machine line's tint
    // and take its text under 4.5:1 (test_frontend_contrast).
    ctx.fillStyle = c.raised;
    ctx.fillRect(l, 2, r - l, WF_CUE_LANE - 4);
    if (machine) {
      ctx.fillStyle = c.machineSoft;
      ctx.fillRect(l, 2, r - l, WF_CUE_LANE - 4);
    }
    ctx.fillStyle = machine ? c.machine : c.strong;
    if (x0 >= 0) ctx.fillRect(x0, 2, 1, WF_CUE_LANE - 4);
    if (x1 <= w) ctx.fillRect(x1 - 1, 2, 1, WF_CUE_LANE - 4);
    if (r - l > 24) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(l + 3, 0, r - l - 6, WF_CUE_LANE);
      ctx.clip();
      ctx.fillStyle = machine ? c.machine : c.text;
      ctx.fillText((machine ? "auto: " : "") + wfCueText(cue), l + 4, WF_CUE_LANE / 2);
      ctx.restore();
    }
    if (i === wf.hover) {  // the label under the pointer: a click selects its line
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 1;
      ctx.strokeRect(l + 0.5, 1.5, r - l - 1, WF_CUE_LANE - 3);
    }
    // Faint cue edges down through the wave, so you see where the line sits in the sound.
    ctx.fillStyle = c.line;
    if (x0 >= 0) ctx.fillRect(x0, top, 1, bottom - top);
    if (x1 <= w) ctx.fillRect(x1 - 1, top, 1, bottom - top);
  }
  // The keyboard's line in the lane gets a focus ring, at least 8 px wide however short it is.
  const fc = scriptCues[wf.cue];
  if (fc && document.activeElement === lane && lane.matches(":focus-visible") && fc.end >= a && fc.start <= b) {
    let l = Math.max(0, wfX(fc.start)), r = Math.min(w, wfX(fc.end));
    if (r - l < 8) { const m = (l + r) / 2; l = Math.max(0, m - 4); r = Math.min(w, m + 4); }
    ctx.strokeStyle = c.text;
    ctx.lineWidth = 2;
    ctx.strokeRect(l + 1, 1, r - l - 2, WF_CUE_LANE - 2);
  }

  // Centre line, then the wave.
  ctx.fillStyle = c.line;
  ctx.fillRect(0, Math.round(mid), w, 1);
  const { src, full } = wfSource();
  wfBusy(!full && !wf.failed);
  if (src) {
    // Auto-gain to the view, but never more than 24 dB above the file's loudest peak, so
    // room tone doesn't fill the strip.
    const vals = src.values;
    let vmax = 1;
    if (src.kind === "samples") {
      for (let i = 0; i < vals.length; i++) vmax = Math.max(vmax, Math.abs(vals[i]));
    } else {
      const n = vals.length >> 1, s0 = src.start, s1 = src.end;
      const i0 = Math.max(0, Math.floor(((a - s0) / (s1 - s0)) * n));
      const i1 = Math.min(n, Math.ceil(((b - s0) / (s1 - s0)) * n));
      for (let i = i0; i < i1; i++) vmax = Math.max(vmax, -vals[2 * i], vals[2 * i + 1]);
    }
    if (wf.filePeak) vmax = Math.max(vmax, wf.filePeak / 16);
    const scale = half / vmax;
    ctx.fillStyle = c.wave;
    ctx.strokeStyle = c.wave;
    if (src.kind === "samples") {
      const rate = src.rate, t0 = src.t0;
      const pxPer = w / (span * rate);
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      const first = Math.max(0, Math.floor((a - t0) * rate) - 1);
      const last = Math.min(vals.length - 1, Math.ceil((b - t0) * rate) + 1);
      for (let i = first; i <= last; i++) {
        const x = wfX(t0 + i / rate), y = mid - vals[i] * scale;
        if (i === first) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      if (pxPer >= 5) {  // single samples are far enough apart to show as dots
        for (let i = first; i <= last; i++) {
          ctx.beginPath();
          ctx.arc(wfX(t0 + i / rate), mid - vals[i] * scale, 1.8, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    } else {
      const n = vals.length >> 1, s0 = src.start, s1 = src.end;
      const per = (s1 - s0) / n;
      for (let x = 0; x < w; x++) {
        const t0 = a + (x / w) * span, t1 = a + ((x + 1) / w) * span;
        let i0 = Math.floor((t0 - s0) / per), i1 = Math.floor((t1 - s0) / per);
        if (i1 <= i0) i1 = i0 + 1;
        i0 = Math.max(0, i0); i1 = Math.min(n, i1);
        if (i0 >= i1) continue;
        let lo = 0, hi = 0;
        for (let i = i0; i < i1; i++) { lo = Math.min(lo, vals[2 * i]); hi = Math.max(hi, vals[2 * i + 1]); }
        const y0 = mid - hi * scale, y1 = mid - lo * scale;
        ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
      }
    }
  }

  // Keyframes (lossless video cuts start on one), as small diamonds on the ruler.
  const kf = wf.keyframes;
  if (wf.hasVideo && kf && kf.start <= a + 1e-6 && kf.end >= b - 1e-6) {
    ctx.fillStyle = c.accent;
    for (const t of kf.times) {
      const x = wfX(t);
      if (x < -4 || x > w + 4) continue;
      ctx.beginPath();
      ctx.moveTo(x, bottom - 1); ctx.lineTo(x + 4, bottom + 3); ctx.lineTo(x, bottom + 7); ctx.lineTo(x - 4, bottom + 3);
      ctx.closePath();
      ctx.fill();
    }
  }

  // Ruler: ticks at a round step, labelled at a precision that fits the zoom.
  ctx.fillStyle = c.strong;
  ctx.fillRect(0, bottom, w, 1);
  const steps = [0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  const step = steps.find((s) => (s / span) * w >= 80) || 3600;
  const dec = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step) - 1e-9));
  ctx.fillStyle = c.faint;
  ctx.textBaseline = "alphabetic";
  ctx.font = `10px ${getComputedStyle(document.body).getPropertyValue("--font-mono") || "monospace"}`;
  for (let t = Math.ceil(a / step) * step; t <= b + 1e-9; t += step) {
    const x = Math.round(wfX(t));
    ctx.fillRect(x, bottom, 1, 4);
    if (x > 2 && x < w - 40) ctx.fillText(dec ? formatTimePrecise(t, dec) : formatTimeWhole(t), x + 3, h - 3);
  }

  wfPlaceHandles();
  wfPlaceHead(true);
}

/** Put the handles where the selection's ends are (pinned to an edge when off screen). */
function wfPlaceHandles() {
  const sel = wfSel();
  const w = wfWidth();
  [["wf-in", 0], ["wf-out", 1]].forEach(([id, k]) => {
    const node = $(id);
    node.hidden = !sel;
    if (!sel) return;
    const t = sel[k];
    const x = wfX(t);
    node.style.left = `${Math.max(0, Math.min(w, x))}px`;
    node.classList.toggle("off-left", x < 0);
    node.classList.toggle("off-right", x > w);
    const a = wfAnchor();
    const pad = k === 0 ? a[0] - t : t - a[1];
    const rel = Math.abs(pad) < 0.0005 ? "on the line's edge"
      : `${Math.abs(pad).toFixed(3)} s ${(k === 0) === (pad > 0) ? "outside" : "inside"} the line`;
    node.setAttribute("aria-valuemin", "0");
    node.setAttribute("aria-valuemax", wf.dur.toFixed(3));
    node.setAttribute("aria-valuenow", t.toFixed(3));
    node.setAttribute("aria-valuetext", `${k === 0 ? "In" : "Out"} ${formatTimePrecise(t, 3)}, ${rel}`);
  });
}

function wfPlaceHead(force) {
  const t = playerTime();
  if (!force && t === wf.lastHead) return;
  wf.lastHead = t;
  const head = $("wf-head");
  const x = wfX(t);
  head.hidden = x < 0 || x > wfWidth();
  head.style.left = `${x}px`;
}

/** Each frame: move the playhead, and page the view along while playing. */
function wfTick() {
  if (!wf.path) return;
  wf.raf = requestAnimationFrame(wfTick);
  if (document.hidden || $("waveform").hidden) return;
  const t = playerTime();
  const playing = ytEmbed ? ytEmbedPlaying() : !$("player").paused;
  const [a, b] = wf.view;
  if (playing && !wf.drag && wf.lastHead != null && wf.lastHead <= b && t > b && t < b + (b - a)) {
    wfSetView(b, b + (b - a));  // the playhead ran off the right edge: next page
  }
  wfPlaceHead(false);
}

// --- moving the handles --------------------------------------------------------------------

function wfSetPad(id, v) {
  $(id).value = String(round6(v));
}

/** Move the in (k=0) or out (k=1) point to `t`, rewriting Before / After to match. */
function wfMoveHandle(k, t) {
  const a = wfAnchor();
  const sel = wfSel();
  if (!a || !sel) return;
  if (k === 0) {
    t = Math.max(0, Math.min(sel[1] - 0.005, t));
    wfSetPad("clip-before", a[0] - t);
  } else {
    t = Math.min(wf.dur || Infinity, Math.max(sel[0] + 0.005, t));
    wfSetPad("clip-after", t - a[1]);
  }
  wf.fromHandle = true;
  try {
    padsChanged();
  } finally {
    wf.fromHandle = false;
  }
}

/** Before / After changed (typed or by a handle): refresh everything that shows the range. */
function padsChanged() {
  if (!wf.fromHandle) {
    wf.snapSeq++;  // typed Before / After: a pending snap is stale
    if (hasClipRange()) linePads = null;  // typed for lines: these are the session's pads now
  }
  if (hasClipRange()) { updateClipUI(); return; }
  if (clipTargetOn && clipRangeStart != null) renderFreeRange();
  wfOnSelection();
}

/** The nearest cue edge within reach of `t` (in px), else `t`. */
function wfCueSnap(t) {
  if (!$("wf-snap-cue").checked) return t;
  const reach = (WF_CUE_SNAP_PX / wfWidth()) * wfSpan();
  let best = t, dist = reach;
  for (const c of scriptCues) {
    if (c.synthetic) continue;
    for (const e of [c.start, c.end]) {
      if (Math.abs(e - t) < dist) { dist = Math.abs(e - t); best = e; }
    }
  }
  return best;
}

const wfZeroOn = () => $("wf-snap-zero").checked && wfSpan() <= WF_ZERO_SPAN;

/** Snap `t` on the server: to the nearest quiet point (Silence), then zero crossing. */
async function wfServerSnap(t, { zeroRadius, silence = $("wf-snap-silence").checked } = {}) {
  let out = t;
  const base = `/api/items/snap${qp(wf.path)}&audio=${wf.audio}`;
  if (silence) {
    const radius = Math.max(0.05, Math.min(1, (24 / wfWidth()) * wfSpan()));
    try {
      const r = await getJSON(`${base}&t=${out.toFixed(6)}&mode=silence&radius=${radius.toFixed(4)}`);
      if (r.found) out = r.snapped;
    } catch { /* keep the unsnapped time */ }
  }
  if (wfZeroOn()) {
    try {
      const r = await getJSON(`${base}&t=${out.toFixed(6)}&mode=zero` +
        (zeroRadius ? `&radius=${zeroRadius}` : ""));
      if (r.found) out = r.snapped;
    } catch { /* keep it */ }
  }
  return out;
}

/** After a drag or a nudge: snap the handle, unless it has moved again meanwhile. */
async function wfSnapHandle(k, opts = {}) {
  const sel = wfSel();
  const silence = opts.silence ?? $("wf-snap-silence").checked;
  if (!sel || (!silence && !wfZeroOn())) return;
  const seq = ++wf.snapSeq;
  const t = await wfServerSnap(sel[k], opts);
  if (seq !== wf.snapSeq || !wfSel()) return;
  if (Math.abs(t - wfSel()[k]) > 1e-7) wfMoveHandle(k, t);
}

function wfAnnounceHandle(k) {
  const sel = wfSel();
  if (sel) announce(`${k === 0 ? "In" : "Out"} point ${formatTimePrecise(sel[k], 3)}`);
}

/** ← → nudge 10 ms, Shift 100 ms, Alt one frame; Page Up/Down 1 s. */
function wfHandleKey(e, k) {
  const sel = wfSel();
  if (!sel) return;
  const frame = 1 / (wf.fps || WF_DEFAULT_FPS);
  const step = e.altKey ? frame : e.shiftKey ? 0.1 : 0.01;
  const d = { ArrowRight: step, ArrowUp: step, ArrowLeft: -step, ArrowDown: -step, PageUp: 1, PageDown: -1 }[e.key];
  if (d === undefined) return;
  e.preventDefault();
  wf.snapSeq++;  // a drag's snap still on its way would undo this nudge
  wfMoveHandle(k, sel[k] + d);
  wfReveal(wfSel()[k]);
  // A nudge stays a nudge: no silence snap, and the zero-crossing snap only reaches 1 ms.
  clearTimeout(wf.nudgeTimer);
  wf.nudgeTimer = setTimeout(() => {
    wfSnapHandle(k, { zeroRadius: 0.001, silence: false }).then(() => wfAnnounceHandle(k));
  }, 250);
}

/** Pan so `t` is on screen (with a little room). */
function wfReveal(t) {
  const [a, b] = wf.view, span = b - a, room = span * 0.1;
  if (t < a + room) wfSetView(t - room, t - room + span);
  else if (t > b - room) wfSetView(t + room - span, t + room);
}

/** [ and ]: focus the in or out handle, bringing it into view. */
function wfJumpTo(k) {
  const sel = wfSel();
  if (!sel) return;
  wfReveal(sel[k]);
  $(k === 0 ? "wf-in" : "wf-out").focus();
}

// --- selecting on the strip -------------------------------------------------------------------

/** Select `a..b` as a free range with no padding, replacing any line selection. The view
 *  stays where it is: the range was made on screen. */
function wfSetFreeRange(a, b) {
  setAsidePads(null);
  $("clip-before").value = "0";
  $("clip-after").value = "0";
  wf.fromHandle = true;
  try {
    showFreeRange(round6(a), round6(b));
  } finally {
    wf.fromHandle = false;
  }
}

/** The free range `wfSetFreeRange` made is still the selection (nothing replaced it). */
const wfIsRange = (a, b) => clipFirst < 0 && clipTargetOn && clipRangeStart === a && clipRangeEnd === b;

/** After a drag-select: snap both ends, unless the selection has changed meanwhile. */
async function wfSnapRange() {
  const a = clipRangeStart, b = clipRangeEnd;
  const seq = ++wf.snapSeq;
  if ($("wf-snap-silence").checked || wfZeroOn()) {
    const [s, e] = await Promise.all([wfServerSnap(a), wfServerSnap(b)]);
    if (seq !== wf.snapSeq || !wfIsRange(a, b)) return;
    if (e - s >= 0.005 && (Math.abs(s - a) > 1e-7 || Math.abs(e - b) > 1e-7)) wfSetFreeRange(s, e);
  }
  const sel = wfSel();
  if (sel) announce(`Selected ${formatTimePrecise(sel[0], 1)} to ${formatTimePrecise(sel[1], 1)}`);
}

/** I (k=0) or O (k=1): put the in or out point at the playhead. With nothing selected, or
 *  the playhead beyond the other end, it starts a new range of WF_POINT_SPAN from there. */
function wfSetPoint(k) {
  if (!currentItem) return;
  if (!wf.path) {
    // No waveform (the file's length isn't known): say why the button does nothing.
    const msg = "Set in and Set out work on the waveform, which this file doesn't have (its length isn't known).";
    $("preview-note").textContent = msg;
    announce(msg);
    return;
  }
  const t = Math.max(0, Math.min(wf.dur, playerTime()));
  const sel = wfSel();
  const name = k === 0 ? "In" : "Out";
  wf.snapSeq++;  // a snap still on its way would move the point just set
  let fresh = false;
  if (sel && (k === 0 ? t < sel[1] - 0.005 : t > sel[0] + 0.005)) {
    if (hasClipRange()) setAsidePads(lineKey());  // this line's pad, not every later line's
    wfMoveHandle(k, t);
  } else {
    // The point stays on the playhead; near the file's ends the range is shorter.
    const a = k === 0 ? t : Math.max(0, t - WF_POINT_SPAN);
    const b = k === 0 ? Math.min(wf.dur, t + WF_POINT_SPAN) : t;
    if (b - a < 0.005) return;
    wfSetFreeRange(a, b);
    fresh = true;
  }
  wfReveal(wfSel()[k]);
  wfSnapHandle(k).then(() => {
    const now = wfSel();
    if (!now) return;
    announce(`${name} at ${formatTimePrecise(now[k], 1)}` +
      (fresh ? `, ${k === 0 ? "out" : "in"} at ${formatTimePrecise(now[1 - k], 1)}` : ""));
  });
}

$("pb-set-in").onclick = () => wfSetPoint(0);
$("pb-set-out").onclick = () => wfSetPoint(1);

// --- the cue lane: a line's label selects it --------------------------------------------------
//
// A click (or a tap) on a line's label selects that line, as picking it in the script does,
// and Shift-click widens the selection to take it in; a drag that starts on a label still
// drag-selects. The lane is one tab stop (#wf-cues, its ring drawn on the canvas): ← → move
// between lines, Enter or Space selects, Shift adds, S and E set the start and end as in the
// script. It is a line selection, cut by clip.create --start/--end: no command of its own.

/** The text a line's label shows. */
const wfCueText = (cue) => (cue.speaker ? cue.speaker + ": " : "") + cue.text.replace(/\s+/g, " ");

function wfPointerY(e) { return e.clientY - $("wf-stage").getBoundingClientRect().top; }

/** The real line whose label is at (x, y) on the strip, or -1. A later line draws on top. */
function wfCueAt(x, y) {
  if (y < 0 || y >= WF_CUE_LANE) return -1;
  const t = wfT(x);
  for (let i = scriptCues.length - 1; i >= 0; i--) {
    const c = scriptCues[i];
    if (!c.synthetic && t >= c.start && t <= c.end) return i;
  }
  return -1;
}

/** Select line `i` as the script would. `extend` widens the selection to take it in (a
 *  free range grows to cover it, keeping its own pads). The view stays: the label is on screen. */
function wfSelectCue(i, extend) {
  const cue = scriptCues[i];
  if (!cue || cue.synthetic) return;
  wf.cue = i;
  wf.snapSeq++;  // a snap still on its way belongs to the selection this replaces
  if (pb) pb.pause();
  const lines = extend && hasClipRange();
  const free = extend && !hasClipRange() ? wfAnchor() : null;
  wf.fromHandle = true;
  try {
    if (free) {
      showFreeRange(round6(Math.min(free[0], cue.start)), round6(Math.max(free[1], cue.end)));
    } else {
      clipFirst = lines ? Math.min(clipFirst, i) : i;
      clipLast = lines ? Math.max(clipLast, i) : i;
      updateClipUI();
      scriptTabStop(i, false);
      scriptCenter(i);
    }
  } finally {
    wf.fromHandle = false;
  }
  wfCueName();
  if (free) {
    const sel = wfSel();
    if (sel) announce(`Selected ${formatTimePrecise(sel[0], 1)} to ${formatTimePrecise(sel[1], 1)}`);
  } else {
    announceSelection();
  }
}

/** Name the lane after the line it is on. */
function wfCueName() {
  const cue = scriptCues[wf.cue];
  $("wf-cues").setAttribute("aria-label", cue && !cue.synthetic
    ? `Select line: ${wfCueText(cue)}${cue.source === "machine" ? " (machine transcript)" : ""}`
    : "Subtitle lines");
}

/** The pointer is over line `i`'s label (-1: none): outline it, and say what a click does. */
function wfHover(i) {
  const cue = scriptCues[i];
  $("wf-cues").title = cue ? `${wfCueText(cue)}: click to select this line, Shift-click to add it to the selection`
    : "Subtitle lines: click a label to select its line, Shift-click to add it";
  if (i === wf.hover) return;
  wf.hover = i;
  $("wf-stage").classList.toggle("wf-on-cue", i >= 0);
  wfDraw();
}

/** Put the lane's focus on line `i`, bringing it on screen, and say it when asked. */
function wfFocusCue(i, say) {
  const cue = scriptCues[i];
  if (!cue || cue.synthetic) return;
  wf.cue = i;
  const [a, b] = wf.view, span = b - a;
  if (cue.start < a || cue.end > b) {
    if (cue.end - cue.start < span * 0.8) {
      const m = (cue.start + cue.end) / 2;
      wfSetView(m - span / 2, m + span / 2);
    } else {
      wfReveal(cue.start);
    }
  }
  wfCueName();
  wfDraw();
  if (say) announce(`${cue.source === "machine" ? "Auto: " : ""}${wfCueText(cue)}, ${formatTime(cue.start)}`);
}

/** The next real line from `i` in direction `dir` (±1), or `i` at the ends. */
function wfCueStep(i, dir) {
  for (let j = i + dir; j >= 0 && j < scriptCues.length; j += dir) if (!scriptCues[j].synthetic) return j;
  return i;
}

$("wf-cues").addEventListener("focus", () => {
  // Start on the line already in hand if it is on screen, else the first one that is.
  const [a, b] = wf.view;
  const shown = (c) => c && !c.synthetic && c.end >= a && c.start <= b;
  if (!shown(scriptCues[wf.cue])) {
    const i = scriptCues.findIndex(shown);
    wf.cue = i >= 0 ? i : wfCueStep(-1, 1);
  }
  wfFocusCue(wf.cue, false);
});
$("wf-cues").addEventListener("blur", wfDraw);
// A mouse press is the strip's (a click selects, a drag drag-selects): it doesn't focus the lane.
$("wf-cues").addEventListener("mousedown", (e) => e.preventDefault());
// A screen reader's activation arrives as a click with no pointer behind it (detail 0).
$("wf-cues").addEventListener("click", (e) => { if (!e.detail && wf.cue >= 0) wfSelectCue(wf.cue, e.shiftKey); });
$("wf-cues").addEventListener("keydown", (e) => {
  if (!wf.path || e.ctrlKey || e.metaKey || e.altKey || wf.cue < 0) return;
  const last = wfCueStep(scriptCues.length, -1);
  const to = { ArrowRight: wfCueStep(wf.cue, 1), ArrowLeft: wfCueStep(wf.cue, -1),
    Home: wfCueStep(-1, 1), End: last }[e.key];
  if (to !== undefined) {
    e.preventDefault();
    wfFocusCue(to, true);
  } else if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();  // no click: the key is the activation
    wfSelectCue(wf.cue, e.shiftKey);
  } else if (e.key === "s" || e.key === "S" || e.key === "e" || e.key === "E") {
    e.preventDefault();
    wf.fromHandle = true;
    try {
      if (e.key === "s" || e.key === "S") setClipInAt(wf.cue); else setClipOutAt(wf.cue);
    } finally {
      wf.fromHandle = false;
    }
  }
});

// --- audition ---------------------------------------------------------------------------------

/** Play `a..b` once and stop. A 20 ms timer watches the end (it keeps running in a
 *  background tab, unlike animation frames), once playback has actually got there. */
function wfAudition(a, b) {
  if (!pb) return;
  clearInterval(wf.auditionTimer);
  wf.auditionEnd = b;
  let started = false;
  wf.auditionTimer = setInterval(() => {
    const t = playerTime();
    if (!wf.path || wf.auditionEnd !== b) { clearInterval(wf.auditionTimer); return; }
    if (!started) { started = t >= a - 0.05 && t < b; return; }
    if (t >= b) {
      clearInterval(wf.auditionTimer);
      wf.auditionEnd = null;
      pb.pause();
    }
  }, 20);
  seekTo(Math.max(0, a));
}

$("wf-play-sel").onclick = () => { const s = wfSel(); if (s) wfAudition(s[0], s[1]); };
$("wf-play-in").onclick = () => { const s = wfSel(); if (s) wfAudition(s[0], Math.min(s[1], s[0] + 1)); };
$("wf-play-out").onclick = () => { const s = wfSel(); if (s) wfAudition(Math.max(s[0], s[1] - 1), s[1]); };

// --- zoom and pan -----------------------------------------------------------------------------

$("wf-zoom-in").onclick = () => wfZoom(WF_ZOOM_STEP);
$("wf-zoom-out").onclick = () => wfZoom(1 / WF_ZOOM_STEP);
$("wf-zoom-sel").onclick = () => (wfSel() ? wfFollowSelection() : wfSetView(0, wf.dur));
$("wf-zoom-all").onclick = () => wfSetView(0, wf.dur);
["wf-snap-zero", "wf-snap-silence", "wf-snap-cue"].forEach((id) => $(id).addEventListener("change", wfDraw));
["clip-exact", "clip-format", "clip-audio-only"].forEach((id) => $(id).addEventListener("change", wfCutNote));
$("ss-audio").addEventListener("change", () => { wf.base = null; wfLoad(); });

$("wf-stage").addEventListener("wheel", (e) => {
  if (!wf.path) return;
  e.preventDefault();
  const r = $("wf-stage").getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) {  // Ctrl/⌘-wheel, and a trackpad pinch, zoom at the pointer
    wfZoom(Math.exp(-e.deltaY * 0.01), wfT(e.clientX - r.left));
  } else {
    const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    const dt = (d / wfWidth()) * wfSpan();
    wfSetView(wf.view[0] + dt, wf.view[1] + dt);
  }
}, { passive: false });

function wfPointerX(e) { return e.clientX - $("wf-stage").getBoundingClientRect().left; }

// A mouse or pen drag selects and Shift-drag (or the middle button) pans; on touch a drag
// pans and a long press, then a drag, selects. A click or a tap plays from there.
$("wf-stage").addEventListener("pointerdown", (e) => {
  if (!wf.path || (e.pointerType === "mouse" && e.button !== 0 && e.button !== 1)) return;
  const handle = e.target.closest(".wf-handle");
  wf.pointers.set(e.pointerId, wfPointerX(e));
  $("wf-stage").setPointerCapture(e.pointerId);
  if (wf.pointers.size === 2) {
    // Two fingers: pinch to zoom around their midpoint.
    if (wf.drag) clearTimeout(wf.drag.hold);
    const [x1, x2] = [...wf.pointers.values()];
    wf.drag = { kind: "pinch", dist: Math.max(8, Math.abs(x2 - x1)), view: [...wf.view],
      at: wfT((x1 + x2) / 2), f: (x1 + x2) / 2 / wfWidth() };
    return;
  }
  if (handle) {
    e.preventDefault();
    const k = handle.id === "wf-in" ? 0 : 1;
    handle.focus();
    if (pb) pb.pause();
    wf.snapSeq++;  // a pending snap must not move the handle out from under the drag
    wf.drag = { kind: "handle", k, grab: wfX(wfSel()[k]) - wfPointerX(e) };
    return;
  }
  if (e.button === 1) e.preventDefault();  // no autoscroll
  const touch = e.pointerType === "touch";
  const d = { kind: touch || e.shiftKey || e.button === 1 ? "pan" : "select",
    x: wfPointerX(e), t: wfT(wfPointerX(e)), view: [...wf.view], moved: false, middle: e.button === 1,
    cue: wfCueAt(wfPointerX(e), wfPointerY(e)), shift: e.shiftKey };
  if (touch) {
    d.hold = setTimeout(() => {
      if (wf.drag !== d || d.moved) return;
      d.kind = "select";
      d.held = true;
      $("wf-stage").classList.add("wf-holding");
      if (navigator.vibrate) navigator.vibrate(10);
    }, WF_HOLD_MS);
  }
  wf.drag = d;
});

// A long press would open the context menu (Android) before the drag starts.
$("wf-stage").addEventListener("contextmenu", (e) => { if (wf.drag) e.preventDefault(); });

$("wf-stage").addEventListener("pointermove", (e) => {
  if (!wf.drag || !wf.pointers.has(e.pointerId)) {
    const free = wf.path && !e.target.closest(".wf-handle");
    if (wf.path) wfHover(free ? wfCueAt(wfPointerX(e), wfPointerY(e)) : -1);
    if (free) {
      $("wf-stage").title = `${formatTimePrecise(wfT(wfPointerX(e)), wfDecimals())}: click to play from here, ` +
        "drag to select, Shift-drag or scroll to pan, Ctrl/⌘-scroll or pinch to zoom";
    }
    return;
  }
  wf.pointers.set(e.pointerId, wfPointerX(e));
  const d = wf.drag;
  if (d.kind === "pinch" && wf.pointers.size >= 2) {
    const [x1, x2] = [...wf.pointers.values()];
    const factor = Math.max(8, Math.abs(x2 - x1)) / d.dist;
    const span = Math.max(WF_MIN_SPAN, Math.min(wf.dur, (d.view[1] - d.view[0]) / factor));
    wfSetView(d.at - d.f * span, d.at - d.f * span + span);
  } else if (d.kind === "handle") {
    wfMoveHandle(d.k, wfCueSnap(wfT(wfPointerX(e) + d.grab)));
  } else if (d.kind === "pan") {
    const dx = wfPointerX(e) - d.x;
    if (Math.abs(dx) > WF_DRAG_PX) d.moved = true;
    if (!d.moved) return;
    clearTimeout(d.hold);
    $("wf-stage").classList.add("wf-panning");
    if (wf.hover >= 0) wfHover(-1);
    const dt = (-dx / wfWidth()) * (d.view[1] - d.view[0]);
    wfSetView(d.view[0] + dt, d.view[1] + dt);
  } else if (d.kind === "select") {
    const x = wfPointerX(e);
    if (!d.moved && Math.abs(x - d.x) <= WF_DRAG_PX) return;
    if (!d.moved) {
      d.moved = true;
      if (wf.hover >= 0) wfHover(-1);  // a drag from a label selects across, not the line
      d.t = wfCueSnap(d.t);
      if (pb) pb.pause();
      wf.snapSeq++;  // a snap still on its way belongs to the selection this replaces
    }
    const t = Math.max(0, Math.min(wf.dur, wfCueSnap(wfT(x))));
    const a = Math.max(0, Math.min(d.t, t)), b = Math.min(wf.dur, Math.max(d.t, t));
    if (b - a >= 0.005) wfSetFreeRange(a, b);
  }
});

function wfPointerEnd(e) {
  if (!wf.pointers.has(e.pointerId)) return;
  wf.pointers.delete(e.pointerId);
  const d = wf.drag;
  if (!d) return;
  if (d.kind === "pinch") {
    if (wf.pointers.size === 0) wf.drag = null;
    else wf.drag = { kind: "pan", x: [...wf.pointers.values()][0], view: [...wf.view], moved: true };
    return;
  }
  wf.drag = null;
  clearTimeout(d.hold);
  $("wf-stage").classList.remove("wf-holding", "wf-panning");
  if (d.kind === "handle") {
    wfSnapHandle(d.k).then(() => wfAnnounceHandle(d.k));
  } else if (d.kind === "select" && d.moved) {
    wfSnapRange();
  } else if (!d.moved && !d.held && !d.middle && e.type === "pointerup") {
    if (d.cue >= 0) wfSelectCue(d.cue, d.shift);  // a click on a line's label selects the line
    else seekTo(Math.max(0, Math.min(wf.dur, wfT(wfPointerX(e)))));  // elsewhere it plays from there
  }
}
$("wf-stage").addEventListener("pointerup", wfPointerEnd);
$("wf-stage").addEventListener("pointerleave", () => { if (wf.path && !wf.drag) wfHover(-1); });
$("wf-stage").addEventListener("pointercancel", wfPointerEnd);

$("wf-in").addEventListener("keydown", (e) => wfHandleKey(e, 0));
$("wf-out").addEventListener("keydown", (e) => wfHandleKey(e, 1));

// + / - zoom, [ / ] jump to the handles and I / O set them at the playhead, anywhere in
// the workspace but a text field.
document.addEventListener("keydown", (e) => {
  if (currentView !== "item" || !wf.path || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  const act = { "+": () => wfZoom(WF_ZOOM_STEP), "=": () => wfZoom(WF_ZOOM_STEP),
    "-": () => wfZoom(1 / WF_ZOOM_STEP), "[": () => wfJumpTo(0), "]": () => wfJumpTo(1),
    i: () => wfSetPoint(0), I: () => wfSetPoint(0), o: () => wfSetPoint(1), O: () => wfSetPoint(1) }[e.key];
  if (!act) return;
  e.preventDefault();
  act();
});

new ResizeObserver(() => { if (wf.path) { wfDraw(); clearTimeout(wf.loadTimer); wf.loadTimer = setTimeout(wfLoad, 200); } })
  .observe($("wf-stage"));
// Redraw in the other theme's colours.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", wfDraw);
new MutationObserver(wfDraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// --- bookmarks --------------------------------------------------------------

async function loadBookmarks() {
  if (!currentItem) return;
  const had = document.activeElement;
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

  setTabCount("bookmarks", data.bookmarks.length);
  if (!data.bookmarks.length) {
    empty.hidden = false;
    refocus(had, $("tab-bookmarks"));
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
      `<button type="button" class="bookmark-use" title="Load this bookmark as the selection, ready to clip" aria-label="Clip ${escapeHtml(bm.label)}">Clip</button>` +
      `<button type="button" class="bookmark-seek" title="Play from the bookmark's start" aria-label="Play ${escapeHtml(bm.label)}"><span aria-hidden="true">▶</span></button>` +
      `<button type="button" class="bookmark-del" data-action="bookmark.delete" title="Delete this bookmark" aria-label="Delete ${escapeHtml(bm.label)}"><span aria-hidden="true">✕</span></button>`;
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
  refocus(had, list.querySelector("button") || $("tab-bookmarks"));
}

/** Buffered [start, end] of a bookmark (cue range ± its saved before/after). */
function bmBuffered(bm) {
  return [Math.max(0, bm.start - (bm.before || 0)), bm.end + (bm.after || 0)];
}

/** Inline before/after editors for a bookmark row (PATCH on change). */
function bmBufferControls(bm) {
  return (
    `<span class="bm-buffer" data-action="bookmark.update" title="Padding around the bookmark (seconds)">` +
    `<label><span aria-hidden="true">−</span><input class="bm-before" type="number" min="0" max="60" step="0.5" value="${bm.before || 0}" title="Seconds kept before the bookmark (saved when you change it)" aria-label="Seconds before" /></label>` +
    `<label><span aria-hidden="true">+</span><input class="bm-after" type="number" min="0" max="60" step="0.5" value="${bm.after || 0}" title="Seconds kept after the bookmark (saved when you change it)" aria-label="Seconds after" /></label>` +
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
      await apiFetch(`/api/bookmarks/${bm.id}`, {
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
  // Restore the bookmark's saved buffer into the inputs (B16), the session's set aside.
  setAsidePads(null);
  $("clip-before").value = bm.before ?? 0;
  $("clip-after").value = bm.after ?? 0;
  // Restore the saved stream selection (B17), best-effort (only if the option
  // exists for this file).
  restoreSelect("ss-subs", bm.sub_track);
  restoreSelect("ss-audio", bm.audio_track);
  restoreSelect("ss-chan", bm.channel_subset);
  showFreeRange(bm.start, bm.end, `"${bm.label}"`);
  // No cue is selected in the script for a bookmark-loaded range, so remember the
  // bookmark's dialogue for the {cue} naming token (makeClip falls back to it).
  loadedBookmarkCue = bm.cue || null;
}

/** Select a range that isn't made of script lines (a bookmark, a whole clip, a link's
 *  span in a file without subtitles): it replaces any line selection, and shows where
 *  a line selection does, padded by Before / After. */
function showFreeRange(start, end, label) {
  clipFirst = -1;
  clipLast = -1;
  loadedBookmarkCue = null;
  clipRangeStart = start;
  clipRangeEnd = end;
  freeRangeLabel = label || "";
  $("mark-save").disabled = false;
  showClipTarget(true);
  renderFreeRange();
  updateScriptHighlight();
  if (pb) pb.refreshSeekBar();
  if (currentItem) rangeSought(playerTime());  // a new range away from the playhead waits for ▶
}
let freeRangeLabel = "";

/** Show the loaded free range (see showFreeRange) with its padding. */
function renderFreeRange() {
  const sel = wfSel() || [clipRangeStart, clipRangeEnd];
  const text = `${formatTime(sel[0])} – ${formatTime(sel[1])}`;
  const dur = `(${(sel[1] - sel[0]).toFixed(1)}s)`;
  const el = $("mark-range-display");
  el.innerHTML =
    (freeRangeLabel ? `<span>${escapeHtml(freeRangeLabel)} —</span> ` : "") +
    `<span class="range-times">${text}</span> <span>${dur}</span>` +
    ` <button type="button" class="mark-clear-inline" title="Clear the selection" aria-label="Clear selection">×</button>`;
  el.querySelector(".mark-clear-inline").onclick = () => {
    clipRangeStart = clipRangeEnd = null;
    loadedBookmarkCue = null;
    clearClip();
  };
  $("clip-range-display").textContent =
    `${freeRangeLabel ? freeRangeLabel + " — " : ""}${text} ${dur}`;
  updatePlaybackUI();
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
    await apiFetch(`/api/bookmarks/${id}`, { method: "DELETE" });
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
  $("ls-send").disabled = !on;
  $("clip-name-row").hidden = !on;
  clipTargetOn = on;
  wfOnSelection();
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
    el.innerHTML = `<a class="clip-dl-mini" data-action="job.download" href="${opts.href}" download ` +
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

  Object.assign(body, wireRange(clipRangeStart, clipRangeEnd));
  // The first selected cue's dialogue drives the {cue} naming token.
  // The {cue} naming token: the selected cue's dialogue, or — when a bookmark
  // range is loaded (no cue selected in the script) — the bookmark's saved cue.
  const selCue = selectedCueText();
  if (selCue) body.cue_text = selCue;
  else if (clipFirst < 0 && loadedBookmarkCue) body.cue_text = loadedBookmarkCue;

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
  $("tab-clips").hidden = false;
  setTabCount("clips", $("clips-made-list").children.length + 1);
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
          html += ` <a class="download-link" data-action="job.download" href="/api/jobs/${jobId}/download/${encodeURIComponent(f.name)}" download title="Download this clip">${escapeHtml(f.name)} (${size})</a>`;
        }
        const link = job.result && job.result.link;
        if (link) html += ` <a class="ls-again" href="${escapeHtml(link)}" title="Open this line in LeadStinger again">Open in LeadStinger</a>`;
        statusEl.innerHTML = html;
        // A Send to LeadStinger job: hand its leadstinger:// link to the system, once.
        if (link && !row.dataset.lsOpened) {
          row.dataset.lsOpened = "1";
          followLink(link);
        }
        const first = job.files && job.files[0];
        if (isLatest) {
          if (first) setClipIndicator("done", {
            href: `/api/jobs/${jobId}/download/${encodeURIComponent(first.name)}`, name: first.name });
          else setClipIndicator("hidden");
        }
      } else if (job.status === "cancelled") {
        statusEl.innerHTML = '<span class="muted">Cancelled</span>';
        if (isLatest) setClipIndicator("hidden");
      } else {
        statusEl.innerHTML = `<span class="job-failed">Failed: ${escapeHtml(job.error || "unknown error")}</span>`;
        if (isLatest) setClipIndicator("failed", { error: job.error });
      }
    })
    .catch((err) => {
      if (!row.isConnected) return;
      if (err.status === 404) {  // cleared from the Jobs list
        row.querySelector(".clip-made-status").innerHTML =
          '<span class="muted">Cleared from the Jobs list</span>';
        if (jobId === activeJobId) setClipIndicator("hidden");
        return;
      }
      setTimeout(() => pollClipJob(jobId, row), 2000);
    });
}

$("clip-btn").onclick = makeClip;

// Send to LeadStinger beside Make clip: one click cuts the selection with its padding and
// sends it (or, with no public URL, makes a one-tile set file). Its tooltip shows on hover
// and keyboard focus; Esc hides it until the pointer or focus leaves (WCAG 1.4.13).
$("ls-send").onclick = () => {
  if (!currentItem || clipRangeStart === null || clipRangeEnd === null) return;
  announce(appConfig.public_url ? "Sending to LeadStinger" : "Making a LeadStinger set");
  return appConfig.public_url ? sendLeadStingerClip() : makeLeadStingerClip();
};
for (const wrap of document.querySelectorAll(".tip-wrap")) {  // ls-send's, the transcript toggle's
  const reset = () => wrap.classList.remove("tip-off");
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && (wrap.matches(":hover") || wrap.contains(document.activeElement))) {
      wrap.classList.add("tip-off");
    }
  });
  wrap.addEventListener("pointerleave", reset);
  wrap.addEventListener("focusout", reset);
}

// --- clip name template (B9) ------------------------------------------------
// {folder} is the name {title} gave before titles came from Jellyfin (D-032), so a
// default-named clip lands where it always did; the old default reads as this one.
const DEFAULT_CLIP_TEMPLATE = "{source}/{timestamp}_{cue}_{folder}";
const LEGACY_CLIP_TEMPLATES = ["{source}/{timestamp}_{cue}_{title}"];
const CLIP_TEMPLATE_KEY = "qc_clip_template";
if (LEGACY_CLIP_TEMPLATES.includes((localStorage.getItem(CLIP_TEMPLATE_KEY) || "").trim())) {
  localStorage.removeItem(CLIP_TEMPLATE_KEY);
}

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
  setView("clips");
  stopPlayer();
  $("browser").hidden = true;
  $("item").hidden = true;
  $("bookmarks-browser").hidden = true;
  $("clips-browser").hidden = false;
}

let currentClipsFolder = null;

async function browseClips(folder, { fromLink = false } = {}) {
  const had = document.activeElement;
  setRoute(routeHash("clips", { folder }));
  showClips();
  currentClipsFolder = folder;
  const list = $("clips-entries");
  const empty = $("clips-empty");
  list.innerHTML = "";
  empty.hidden = true;
  $("clips-folder-crumb").textContent = folder ? ` / ${folder}` : "";
  $("clips-title").textContent = folder || "Clips";
  $("clips-count").textContent = "";
  $("clips-batch").hidden = true;
  $("clips-select-all").checked = false;
  $("clips-batch-status").textContent = "";

  let data;
  try {
    const url = folder ? `/api/clips?folder=${encodeURIComponent(folder)}` : "/api/clips";
    data = await getJSON(url);
  } catch (err) {
    if (fromLink && folder) { await linkFailed(err); return; }
    list.innerHTML = `<li class="error">Could not load clips: ${escapeHtml(err.message)}</li>`;
    return;
  }

  if (!data.folders.length && !data.clips.length) {
    empty.hidden = false;
    refocus(had, $("clips-title"));
    return;
  }

  // LeadStinger set files (.lsset) are listed too (M18): download-only rows.
  const sets = data.clips.filter((c) => c.kind === "set");
  const clipRows = data.clips.filter((c) => c.kind !== "set");
  if (data.clips.length) {
    const bytes = data.clips.reduce((n, c) => n + c.size, 0);
    const counts = [clipRows.length ? plural(clipRows.length, "clip") : "", sets.length ? plural(sets.length, "set") : ""];
    $("clips-count").textContent = `${counts.filter(Boolean).join(" · ")} · ${fmtSize(bytes)}`;
  }
  for (const f of data.folders) {
    const li = document.createElement("li");
    li.className = "entry dir";
    const main = el("button", {
      className: "entry-main", title: "Open this folder of clips",
      html: `<span class="icon-wrap">${iconSvg("folder")}</span><span class="label">${escapeHtml(f)}</span>` +
        `<span class="chips"></span><span class="go">${iconSvg("chevron-r")}</span>`,
    });
    main.dataset.action = "clips.list";
    main.onclick = () => browseClips(f);
    li.appendChild(main);
    list.appendChild(li);
  }

  for (const c of sets) {
    const li = document.createElement("li");
    li.className = "entry set";
    const tiles = (c.meta && Array.isArray(c.meta.items)) ? c.meta.items.length : null;
    li.innerHTML =
      `<span class="icon-wrap"><svg class="icon" aria-hidden="true"><use href="#i-leadstinger"/></svg></span>` +
      `<span class="label clip-name">${escapeHtml(c.name)}</span>` +
      `<span class="chips"><span class="badge">LeadStinger set${tiles != null ? ` · ${plural(tiles, "tile")}` : ""}</span><span class="badge">${fmtSize(c.size)}</span></span>` +
      `<a class="download-link clip-dl" data-action="clips.download" href="${escapeHtml(c.download_url)}" download title="Download this set file to open in LeadStinger" aria-label="Download ${escapeHtml(c.name)}">Download</a>`;
    list.appendChild(li);
  }

  for (const c of clipRows) {
    const li = document.createElement("li");
    li.className = "entry video";
    const size = c.size > 1048576
      ? (c.size / 1048576).toFixed(1) + " MB"
      : (c.size / 1024).toFixed(0) + " KB";
    li.innerHTML =
      `<input type="checkbox" class="entry-select clip-select" title="Select for Export selected" aria-label="Select ${escapeHtml(c.name)}" />` +
      `<span class="icon-wrap">${iconSvg(/\.(flac|wav|mp3|m4a|opus|ogg|aac)$/i.test(c.name) ? "music" : "film")}</span>` +
      `<button type="button" class="label clip-name clip-open" title="Open this clip in the workspace to re-cut it">${escapeHtml(c.name)}</button>` +
      `<span class="chips"><span class="badge">${size}</span></span>` +
      (appConfig.public_url
        ? `<button type="button" class="btn clip-ls" data-action="leadstinger.link" title="Send to LeadStinger: this clip in a new tile" aria-label="Send ${escapeHtml(c.name)} to LeadStinger"><svg class="icon" aria-hidden="true"><use href="#i-leadstinger"/></svg></button>`
        : "") +
      `<a class="download-link clip-dl" data-action="clips.download" href="${escapeHtml(c.download_url)}" download title="Download this clip" aria-label="Download ${escapeHtml(c.name)}">Download</a>`;
    const cb = li.querySelector(".clip-select");
    cb.dataset.path = c.path;
    cb.dataset.name = c.name;
    cb.dataset.folder = c.folder || "";
    const ls = li.querySelector(".clip-ls");
    if (ls) {
      ls.onclick = async (e) => {
        e.stopPropagation();
        ls.disabled = true;
        await sendLeadStingerLink(lsClipItem(c.path, c.name, c.folder), $("clips-batch-status"));
        ls.disabled = false;
      };
    }
    cb.onclick = (e) => { e.stopPropagation(); updateBatchState("clips"); };
    // A click anywhere on the row opens the clip; the name is the button that does it by keyboard.
    li.onclick = (e) => {
      if (e.target.closest("a, input, .clip-ls")) return; // let the row's own controls work
      openItem(c.path, c.name, { selectWholeClip: true });
    };
    list.appendChild(li);
  }

  // Batch export bar only when there are clips to select.
  if (clipRows.length) $("clips-batch").hidden = false;
  refocus(had, list.querySelector(".entry-main, .clip-open") || $("clips-title"));
}

$("clips-link").onclick = () => browseClips(null);
$("clips-home").onclick = () => browseClips(null);
$("clips-back-lib").onclick = () => browse(null);

// --- bookmarks browser ------------------------------------------------------

function showBookmarksBrowser() {
  setView("bookmarks");
  stopPlayer();
  $("browser").hidden = true;
  $("item").hidden = true;
  $("clips-browser").hidden = true;
  $("bookmarks-browser").hidden = false;
}

async function browseBookmarks() {
  const had = document.activeElement;
  setRoute(routeHash("bookmarks"));
  showBookmarksBrowser();
  const list = $("all-bookmarks-list");
  const empty = $("all-bookmarks-empty");
  list.innerHTML = "";
  empty.hidden = true;
  $("bm-batch").hidden = true;
  $("bm-select-all").checked = false;
  $("bm-count").textContent = "";
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
    refocus(had, $("bm-title"));
    return;
  }
  $("bm-batch").hidden = false;
  const nSources = new Set(data.bookmarks.map((b) => b.path)).size;
  $("bm-count").textContent = `${plural(data.bookmarks.length, "bookmark")} in ${plural(nSources, "source")}`;

  // Group by source path
  const groups = new Map();
  for (const bm of data.bookmarks) {
    if (!groups.has(bm.path)) groups.set(bm.path, []);
    groups.get(bm.path).push(bm);
  }

  for (const [path, bookmarks] of groups) {
    // Prefer the backend's display name (a YouTube ref's title, else the file
    // name); the path-derived fallback shows an ugly "yt:<id>" for YouTube items.
    const fileName = bookmarks[0].source_name || path.split("/").pop() || path;
    const header = document.createElement("li");
    header.className = "bm-browser-group";
    header.innerHTML =
      `<span class="bm-browser-icon">${iconSvg(path.startsWith("yt:") ? "youtube" : "film")}</span>` +
      `<span class="bm-browser-file clickable">${escapeHtml(fileName)}</span>` +
      `<span class="muted bm-browser-count">${plural(bookmarks.length, "bookmark")}</span>` +
      `<button type="button" class="bm-browser-open" title="Open this video in the workspace" aria-label="Open video ${escapeHtml(fileName)}">Open video</button>`;
    // Clicking the filename (or the button) opens the source video.
    makeActivatable(header.querySelector(".bm-browser-file"), "Open this video in the workspace");
    header.querySelector(".bm-browser-file").onclick = () => openItem(path, fileName);
    header.querySelector(".bm-browser-open").onclick = () => openItem(path, fileName);
    list.appendChild(header);

    for (const bm of bookmarks) {
      const li = document.createElement("li");
      li.className = "bm-browser-item";
      const [bs, be] = bmBuffered(bm);
      li.innerHTML =
        `<input type="checkbox" class="entry-select bm-select" title="Select for Export selected" aria-label="Select ${escapeHtml(bm.label)}" />` +
        `<span class="bookmark-label clickable">${escapeHtml(bm.label)}</span>` +
        `<span class="bookmark-range">${formatTime(bs)} – ${formatTime(be)}</span>` +
        bmBufferControls(bm) +
        `<button type="button" class="bookmark-seek" title="Open the video and play from the bookmark" aria-label="Play ${escapeHtml(bm.label)}"><span aria-hidden="true">▶</span></button>` +
        `<button type="button" class="bookmark-del" data-action="bookmark.delete" title="Delete this bookmark" aria-label="Delete ${escapeHtml(bm.label)}"><span aria-hidden="true">✕</span></button>`;
      const cb = li.querySelector(".bm-select");
      cb.dataset.path = bm.path;
      cb.dataset.start = bm.start;
      cb.dataset.end = bm.end;
      cb.dataset.before = bm.before || 0;
      cb.dataset.after = bm.after || 0;
      cb.dataset.cue = bm.cue || "";
      cb.dataset.label = bm.label || "";
      cb.onclick = (e) => { e.stopPropagation(); updateBatchState("bm"); };
      wireBmBufferControls(li, bm, () => browseBookmarks());
      // Clicking the bookmark name opens the video with this bookmark preloaded
      // for clipping.
      makeActivatable(li.querySelector(".bookmark-label"), "Open the video with this bookmark loaded for clipping");
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
          await apiFetch(`/api/bookmarks/${bm.id}`, { method: "DELETE" });
          browseBookmarks();
        } catch {}
      };
      list.appendChild(li);
    }
  }
  refocus(had, $("bm-title"));
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
        `<label class="checkbox-label"><input type="checkbox" title="Export the ${label} channels" id="${prefix}-chan-${cat}"${cat === "center" ? " checked" : ""} /> ${label}</label>`
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

  // Options live behind a toggle; their current values show as chips so the bar stays short.
  const bar = $(`${prefix}-batch`);
  const toggle = bar.querySelector(".batch-options-toggle");
  const panel = $(`${prefix}-batch-options`);
  toggle.onclick = () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
  };
  panel.addEventListener("change", () => updateBatchSummary(prefix));
  panel.addEventListener("input", () => updateBatchSummary(prefix));
  updateBatchSummary(prefix);
}

/** The bar's chips: what a clip made from it will be. */
function updateBatchSummary(prefix) {
  const chips = [];
  if ($(`${prefix}-batch-audio`).checked) chips.push("Audio only");
  if ($(`${prefix}-batch-split`).checked) chips.push("Split channels");
  const exact = $(`${prefix}-batch-exact`);
  if (exact && !$(`${prefix}-batch-audio`).checked && exact.checked) chips.push("Exact");
  const fmt = $(`${prefix}-batch-format`);
  chips.push(fmt.options[fmt.selectedIndex].text);
  const tpl = $(`${prefix}-batch-template`).value.trim();
  if (tpl) chips.push(tpl.length > 18 ? tpl.slice(0, 17) + "…" : tpl);
  $(`${prefix}-batch-summary`).innerHTML = chips.map((c) => `<span class="badge">${escapeHtml(c)}</span>`).join("");
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
    // A job cleared from the Jobs list (404) or cancelled is finished as far as this
    // bar is concerned: count it, or the bar waits for it forever.
    let done = 0, failed = 0, gone = 0;
    for (const id of jobIds) {
      try {
        const j = await getJSON(`/api/jobs/${id}`);
        if (j.status === "done") done++;
        else if (j.status === "failed") failed++;
        else if (j.status === "cancelled") gone++;
      } catch (err) {
        if (err.status === 404) gone++;
      }
    }
    if (done + failed + gone >= total) {
      const extra = [failed && `${failed} failed`, gone && `${gone} cancelled or cleared`]
        .filter(Boolean).join(", ");
      statusEl.textContent = extra
        ? `${done}/${total} exported, ${extra}`
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
  if (lsOn("clips-batch-ls")) return runLeadStingerBatch("clips");
  const opts = batchOptions("clips");
  runBatchExport(
    "clips",
    // Whole file: send start=0 with no end — the backend fills in the duration.
    (cb) => ({ path: cb.dataset.path, start: 0, before: 0, after: 0, ...opts }),
    // Refresh so newly exported files appear (exports land in the clips root);
    // restore the status text the refresh would otherwise wipe.
    () => {
      const msg = $("clips-batch-status").textContent;
      if (!showingRoute("clips", "folder", currentClipsFolder)) return;  // the user moved on
      browseClips(currentClipsFolder).then(() => {
        $("clips-batch-status").textContent = msg;
      });
    },
  );
};

$("bm-batch-export").onclick = () => {
  if (lsOn("bm-batch-ls")) return runLeadStingerBatch("bm");
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
    await apiFetch("/api/bookmarks", { method: "DELETE" });
    browseBookmarks();
  } catch (err) {
    alert("Failed to clear bookmarks: " + err.message);
  }
};

// Clip / bookmark the selected dialogue-search hits straight from the results.
$("search-batch-export").onclick = () => {
  if (lsOn("search-batch-ls")) return runLeadStingerBatch("search");
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

// --- Send to LeadStinger (M14, D-026) ------------------------------------------
//
// A small toggle beside each batch bar's Clip/Export selected: when it is on, that same
// button makes a LeadStinger set file (.lsset) instead of plain clips (the workspace has
// its own Send to LeadStinger button beside Make clip). POST /api/leadstinger/set cuts each line as FLAC with its own padding
// (the Before/After fields, a bookmark's saved padding, 0 for search hits and clips),
// kept as handle audio around a tile trimmed to the line, and names the set after
// the movie or show.

const lsOn = (id) => { const el = $(id); return !!(el && el.checked); };

/** A tile title: the bookmark's own label, unless it is just "<times> — <cue>". */
function lsTitle(label, cue) {
  label = (label || "").trim();
  cue = (cue || "").trim();
  if (cue && (!label || label.endsWith(cue))) return cue;
  return label || cue || undefined;
}

/** A batch bar's checked rows as /api/leadstinger/set items, each with its padding. */
function lsItemsFrom(prefix) {
  return batchBoxes(prefix, true).map((cb) => {
    const d = cb.dataset;
    if (prefix === "clips") return lsClipItem(d.path, d.name, d.folder);
    return {
      path: d.path, start: parseFloat(d.start), end: parseFloat(d.end),
      title: lsTitle(d.label, d.cue),
      before: parseFloat(d.before) || 0, after: parseFloat(d.after) || 0,
    };
  });
}

/** One set job for a batch bar's selection; offers (and starts) the .lsset download. */
async function runLeadStingerBatch(prefix, onDone) {
  const items = lsItemsFrom(prefix);
  if (!items.length) return;
  const btn = $(`${prefix}-batch-export`);
  const status = $(`${prefix}-batch-status`);
  btn.disabled = true;
  if (items.length === 1 && appConfig.public_url) {
    // One line or clip goes straight into LeadStinger by link; several make a set file.
    await sendLeadStingerLink(items[0], status);
    btn.disabled = false;
    if (onDone) onDone();
    return;
  }
  status.textContent = "Making a LeadStinger set…";
  let job;
  try {
    job = await postJSON("/api/leadstinger/set", { items: items.map(({ clip, ...it }) => it) });
  } catch (err) {
    status.textContent = `LeadStinger set failed: ${err.message}`;
    btn.disabled = false;
    return;
  }
  const tick = async () => {
    let j;
    try {
      j = await getJSON(`/api/jobs/${job.job_id}`);
    } catch (err) {
      if (err.status === 404) {  // cleared from the Jobs list
        status.textContent = "LeadStinger set cleared from the Jobs list.";
        btn.disabled = false;
        return;
      }
      setTimeout(tick, 1500);
      return;
    }
    if (j.status === "done" && j.files && j.files.length) {
      const file = j.files[0].name;
      const href = `/api/jobs/${encodeURIComponent(job.job_id)}/download/${encodeURIComponent(file)}`;
      status.innerHTML = `LeadStinger set ready: <a data-action="job.download" href="${escapeHtml(href)}" download title="Download the set file again">${escapeHtml(file)}</a>`;
      status.querySelector("a").click();
      btn.disabled = false;
      if (onDone) onDone();
    } else if (j.status === "failed" || j.status === "cancelled") {
      status.textContent = `LeadStinger set failed: ${j.error || j.status}`;
      btn.disabled = false;
    } else {
      status.textContent = `Making a LeadStinger set… ${j.progress || ""}`;
      setTimeout(tick, 1000);
    }
  };
  tick();
}

/** Send to LeadStinger with no public URL: the selection becomes a one-tile LeadStinger set. */
async function makeLeadStingerClip() {
  const cue = (selectedCueText() || (clipFirst < 0 ? loadedBookmarkCue : "") || "").trim();
  const item = {
    path: currentItem.path, ...wireRange(clipRangeStart, clipRangeEnd),
    title: cue || undefined,
  };
  const what = cue
    ? `"${cue.length > 60 ? cue.slice(0, 57) + "…" : cue}"`
    : `${formatTime(clipRangeStart)} – ${formatTime(clipRangeEnd)}`;
  const label = `LeadStinger set: ${what}`;
  setClipIndicator("processing");
  try {
    const data = await postJSON("/api/leadstinger/set", { items: [item] });
    activeJobId = data.job_id;
    pollClipJob(data.job_id, addClipRow(data.job_id, label));
  } catch (err) {
    const row = addClipRow(null, label);
    row.querySelector(".clip-made-status").innerHTML =
      `<span class="job-failed">Failed: ${escapeHtml(err.message)}</span>`;
    setClipIndicator("failed", { error: err.message });
  }
}

// Send to LeadStinger by link (M14, LeadStinger's D-076). When the server has a public
// URL (QC_PUBLIC_URL, appConfig.public_url), one line or clip goes straight into
// LeadStinger: POST /api/leadstinger/quip cuts the line as FLAC with its padding into
// the clips library (a job whose result is the link), GET /api/leadstinger/link links
// a clip already there, and the browser opens the leadstinger://quip link. LeadStinger
// downloads the clip with its own API token and trims the tile to the line.

/** A clips-library file as a LeadStinger item: its path, and its path inside the library. */
function lsClipItem(path, name, folder) {
  return {
    path, clip: (folder ? `${folder}/` : "") + name,
    title: (name || "").replace(/\.[^.]+$/, "") || undefined,
  };
}

/** Open a link (here a leadstinger:// one) as a click on it would. */
function followLink(link) {
  const a = document.createElement("a");  // notitle: never put on the page, only clicked
  a.href = link;
  a.click();
}

/** Show a sent link in `status` (to click again if LeadStinger didn't come up) and open it. */
function launchLeadStinger(link, status, what) {
  status.innerHTML = `Sent ${escapeHtml(what)} to LeadStinger · <a class="ls-again" href="${escapeHtml(link)}" title="Open LeadStinger with it again, if it didn't come up">open again</a>`;
  followLink(link);
}

/** One item into LeadStinger by link, reporting in a batch bar's `status`. */
async function sendLeadStingerLink(it, status) {
  const what = it.title ? `“${it.title.length > 60 ? it.title.slice(0, 57) + "…" : it.title}”` : "the clip";
  try {
    if (it.start == null) {
      const q = new URLSearchParams({ clip: it.clip });
      if (it.title) q.set("title", it.title);
      launchLeadStinger((await getJSON(`/api/leadstinger/link?${q}`)).link, status, what);
      return;
    }
    status.textContent = "Sending to LeadStinger…";
    const job = await postJSON("/api/leadstinger/quip", {
      path: it.path, start: it.start, end: it.end, title: it.title,
      before: it.before || 0, after: it.after || 0,
    });
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      let j;
      try {
        j = await getJSON(`/api/jobs/${job.job_id}`);
      } catch (err) {
        if (err.status === 404) throw new Error("the job was cleared from the Jobs list");
        continue;
      }
      if (j.status === "done" && j.result && j.result.link) {
        launchLeadStinger(j.result.link, status, what);
        return;
      }
      if (j.status === "done" || j.status === "failed" || j.status === "cancelled") {
        status.textContent = `Send to LeadStinger failed: ${j.error || "no link"}`;
        return;
      }
      status.textContent = `Sending to LeadStinger… ${j.progress || ""}`;
    }
  } catch (err) {
    status.textContent = `Send to LeadStinger failed: ${err.message}`;
  }
}

/** Send to LeadStinger with a public URL: the selection goes straight into
 *  LeadStinger. The clip row follows the job and opens the link when it is done. */
async function sendLeadStingerClip() {
  const cue = (selectedCueText() || (clipFirst < 0 ? loadedBookmarkCue : "") || "").trim();
  const what = cue
    ? `"${cue.length > 60 ? cue.slice(0, 57) + "…" : cue}"`
    : `${formatTime(clipRangeStart)} – ${formatTime(clipRangeEnd)}`;
  const label = `To LeadStinger: ${what}`;
  setClipIndicator("processing");
  try {
    const data = await postJSON("/api/leadstinger/quip", {
      path: currentItem.path, ...wireRange(clipRangeStart, clipRangeEnd),
      title: cue || undefined,
    });
    activeJobId = data.job_id;
    pollClipJob(data.job_id, addClipRow(data.job_id, label));
  } catch (err) {
    const row = addClipRow(null, label);
    row.querySelector(".clip-made-status").innerHTML =
      `<span class="job-failed">Failed: ${escapeHtml(err.message)}</span>`;
    setClipIndicator("failed", { error: err.message });
  }
}

// --- top bar: one search box with a scope chip, nav state, Jobs pill --------
//
// The chip decides where the box looks: this video (GET /api/search), this
// folder or the whole library (GET /api/search/folder, results in the library
// list). It follows the page you are on until you pick a scope yourself.

let searchScope = "library";  // item | folder | library
const SCOPE_LABEL = { item: "This video", folder: "This folder", library: "Whole library" };
const SCOPE_HINT = {
  item: "Search this video's dialogue…",
  folder: "Search dialogue in this folder…",
  library: "Search the dialogue: type a line you remember",
};

/** The folder a "this folder" search means: the one on screen, or the item's. */
function folderForSearch() {
  if (currentView === "item" && currentItem) return currentParent();
  if (currentView === "browser") return currentBrowsePath;
  return null;
}

function autoScope() {
  if (currentView === "item" && currentItem) searchScope = "item";
  else if (currentView === "browser" && (currentBrowsePath || dialogueSearchScope)) searchScope = "folder";
  else searchScope = "library";
  updateScopeUI();
}

function updateScopeUI() {
  let label = SCOPE_LABEL[searchScope];
  if (searchScope === "folder" && dialogueSearchScope && currentView === "browser") {
    label = `${dialogueSearchScope.length} folder${dialogueSearchScope.length > 1 ? "s" : ""}`;
  }
  $("search-scope-label").textContent = label;
  $("search-scope").setAttribute("aria-label", `Search in: ${label}`);
  $("global-search-input").placeholder = SCOPE_HINT[searchScope];
  const inItem = currentView === "item" && !!currentItem;
  for (const b of $("scope-menu").querySelectorAll("button")) {
    const s = b.dataset.scope;
    b.disabled = (s === "item" && !inItem) || (s === "folder" && !folderForSearch() && !dialogueSearchScope);
    if (b.getAttribute("role") === "menuitemradio") b.setAttribute("aria-checked", String(s === searchScope));
  }
}

function toggleScopeMenu(open, restore) {
  const menu = $("scope-menu");
  const show = open === undefined ? menu.hidden : open;
  const wasOpen = !menu.hidden;
  menu.hidden = !show;
  $("search-scope").setAttribute("aria-expanded", String(show));
  if (show) (menu.querySelector('[aria-checked="true"]:not(:disabled)') || menu.querySelector("button:not(:disabled)"))?.focus();
  else if (wasOpen && (restore || menu.contains(document.activeElement))) $("search-scope").focus();
}

$("search-scope").onclick = (e) => { e.stopPropagation(); toggleJobs(false); toggleScopeMenu(); };
// ↓ / ↑ on the chip open the menu, as on a menu button.
$("search-scope").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  e.preventDefault();
  toggleScopeMenu(true);
});
// In the menu: ↑ ↓ Home End move, Esc closes (back to the chip), Tab closes and moves on.
$("scope-menu").addEventListener("keydown", (e) => {
  if (e.key === "Tab") { toggleScopeMenu(false); return; }
  const items = [...$("scope-menu").querySelectorAll("button:not(:disabled)")]
    .filter((b) => !b.closest("[hidden]"));
  const i = items.indexOf(document.activeElement);
  const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  items[(next + items.length) % items.length].focus();
});
$("scope-menu").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-scope]");
  if (!b || b.disabled) return;
  if (b.dataset.scope === "nowplaying") {  // not a search: jump to what's playing
    toggleScopeMenu(false);
    jumpToNowPlaying();
    return;
  }
  searchScope = b.dataset.scope;
  updateScopeUI();
  toggleScopeMenu(false);
  $("global-search-input").focus();
});

let searching = false;
let searchRun = 0;  // generation of runGlobalSearch: only the newest clears the busy state
async function runGlobalSearch() {
  const query = $("global-search-input").value.trim();
  if (!query) return;
  // A new search supersedes one still running (its results are dropped when they
  // arrive), so Back / Forward to a search during one isn't swallowed.
  const run = ++searchRun;
  searching = true;
  $("global-search").classList.add("busy");
  let browsed = false;
  try {
    if (searchScope === "item" && currentItem) {
      await doSearch(query);
      return;
    }
    // Folder and library hits render in the library list, so bring it up first.
    const wholeLibrary = searchScope === "library";
    const wantedFolders = dialogueSearchScope;  // a name search's folders, if any
    const goTo = wholeLibrary ? null : folderForSearch();
    if (currentView !== "browser" || (wholeLibrary && currentBrowsePath !== null)) {
      if (!wholeLibrary && !goTo && !wantedFolders) return;
      await browse(goTo);
      browsed = true;
      searchScope = wholeLibrary ? "library" : "folder";
      updateScopeUI();
    }
    $("global-search-input").value = query;  // browse() clears the box
    const folders = wholeLibrary ? mediaRoots.slice()
      : (currentView === "browser" && dialogueSearchScope) || [currentBrowsePath].filter(Boolean);
    if (!folders.length) return;
    // The folder browse() just showed is a step of this search, not a page of its own.
    setRoute(routeHash("search", wholeLibrary ? { q: query, scope: "library" }
      : { q: query, scope: "folder", path: folders }), { replace: browsed });
    await dialogueSearch(query, folders);
  } finally {
    if (run === searchRun) {
      searching = false;
      $("global-search").classList.remove("busy");
    }
  }
}

$("global-search").addEventListener("submit", (e) => { e.preventDefault(); runGlobalSearch(); });

// "/" jumps to the search box from anywhere that isn't a text field.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { toggleScopeMenu(false, true); toggleJobs(false, true); toggleSettings(false, true); return; }
  if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  e.preventDefault();
  $("global-search-input").focus();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#global-search")) toggleScopeMenu(false);
  if (!e.target.closest("#jobs-popover") && !e.target.closest("#jobs-pill")) toggleJobs(false);
  if (!e.target.closest("#settings-popover") && !e.target.closest("#settings-btn")) toggleSettings(false);
});

// --- Now playing: what Jellyfin clients are playing (M15) --------------------
//
// GET /api/jellyfin/sessions (jellyfin.sessions) lists each Jellyfin client that is
// playing or paused, its playhead and the file in the library. The card on the library
// home polls it while it is on screen. Open (clip.nowplaying in the CLI) opens that file
// in the workspace at the playhead with the line at or before it selected, so ✂ Make
// clip cuts what was just heard. The "Now playing" scope jumps straight there.

const NOWPLAYING_POLL_MS = 5000;
let nowPlayingTimer = null;
let nowPlayingSessions = [];

const nowPlayingShown = () =>
  !!appConfig.jellyfin && currentView === "browser" && currentBrowsePath === null;

function nowPlayingTitle(s) {
  if (!s.series) return s.title || "Untitled";
  const two = (n) => String(n).padStart(2, "0");
  const ep = s.season != null && s.episode != null ? ` S${two(s.season)}E${two(s.episode)}` : "";
  return `${s.series}${ep}${s.title ? " · " + s.title : ""}`;
}

async function refreshNowPlaying() {
  clearTimeout(nowPlayingTimer);
  nowPlayingTimer = null;
  $("nowplaying-card").hidden = !nowPlayingShown();
  if (!nowPlayingShown()) return;
  if (!document.hidden) {
    try {
      nowPlayingSessions = (await getJSON("/api/jellyfin/sessions")).sessions || [];
      renderNowPlaying();
    } catch (err) {
      nowPlayingSessions = [];
      $("nowplaying-list").innerHTML = "";
      $("nowplaying-status").hidden = false;
      $("nowplaying-status").textContent = `Couldn't ask Jellyfin: ${err.message}`;
    }
  }
  // Still on the library home (a browse may have left it while we waited)? Keep polling.
  if (nowPlayingShown() && !nowPlayingTimer) {
    nowPlayingTimer = setTimeout(refreshNowPlaying, NOWPLAYING_POLL_MS);
  }
}

function renderNowPlaying() {
  const list = $("nowplaying-list");
  const status = $("nowplaying-status");
  // It re-renders every poll: keep keyboard focus on the same row's Open button.
  const hadIdx = [...list.querySelectorAll("button")].indexOf(document.activeElement);
  list.innerHTML = "";
  status.hidden = nowPlayingSessions.length > 0;
  status.textContent = "Nothing is playing on Jellyfin.";
  for (const s of nowPlayingSessions) {
    const li = document.createElement("li");
    li.className = "nowplaying-row";
    const who = [s.device, s.user].filter(Boolean).map(escapeHtml).join(" · ");
    const at = formatTimeWhole(s.position || 0);
    li.innerHTML = (s.art ? jfArtHtml(s.art, "nowplaying-art") : "") +
      `<div class="nowplaying-what"><strong>${escapeHtml(nowPlayingTitle(s))}</strong>` +
      `<span class="muted">${who} · ${s.paused ? "paused" : "playing"} at ${at}</span>` +
      (s.path ? "" : `<span class="muted">${escapeHtml(s.note || "")}</span>`) +
      `</div>` +
      (s.path ? `<button type="button" class="btn primary" data-action="clip.nowplaying"` +
        ` title="Open in the workspace at ${at}, with the line just heard selected"` +
        ` aria-label="Open ${escapeHtml(nowPlayingTitle(s))} at ${at}">Open at ${at}</button>` : "");
    const open = li.querySelector("button");
    if (open) open.onclick = () => openNowPlaying(s);
    jfArtFallback(li, null);
    list.appendChild(li);
  }
  if (hadIdx >= 0) {
    const buttons = list.querySelectorAll("button");
    (buttons[Math.min(hadIdx, buttons.length - 1)] || $("library-search")).focus();
  }
}

function openNowPlaying(s) {
  if (!s.path) return;
  const name = s.path.split(/[\\/]/).pop();
  openItem(s.path, name, { seekTo: s.position || 0, markRange: { at: s.position || 0 } });
}

/** The "Now playing" scope: open the one session playing a library file, or show the
 *  card to choose from (or to say why there's nothing to open). */
async function jumpToNowPlaying() {
  try {
    const sessions = (await getJSON("/api/jellyfin/sessions")).sessions || [];
    const playable = sessions.filter((s) => s.path);
    if (playable.length === 1) { openNowPlaying(playable[0]); return; }
  } catch {}  // the card says what went wrong
  await browse(null);
  $("nowplaying-card").scrollIntoView({ block: "nearest" });
}

// --- Jobs: the pill counts running jobs, the popover lists them -------------
//
// GET /api/jobs (job.list) returns the 20 most recent jobs of every kind: clips,
// downloads, YouTube adds and subtitle backfills. Each row can be cancelled or
// dismissed (DELETE /api/jobs/{id}, job.cancel) and a finished clip downloaded.

let jobsOpen = false;
let jobsTimer = null;
let lastJobs = [];
let jobsFailures = 0;  // consecutive failed polls: back off while the server is away
const jobActive = (j) => j.status === "queued" || j.status === "running";

function fmtSize(n) {
  return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
}

// Each job's last-seen status, so a job that finishes (or fails) is announced once.
const jobSeen = new Map();
let jobsPrimed = false;     // the first poll only learns what's already there
const pageOpened = Date.now() / 1000;

async function refreshJobs() {
  try {
    lastJobs = (await getJSON("/api/jobs")).jobs || [];
    jobsFailures = 0;
  } catch { jobsFailures++; return; }
  for (const j of lastJobs) {
    const prev = jobSeen.get(j.id);
    const ended = !jobActive(j) && (prev ? jobActive({ status: prev }) : j.created >= pageOpened);
    if (jobsPrimed && ended && prev !== j.status) announceJob(j);
    jobSeen.set(j.id, j.status);
  }
  jobsPrimed = true;
  renderJobsPill();
  if (jobsOpen) renderJobsList();
}

function announceJob(j) {
  const what = j.label || "job";
  if (j.status === "done") announce(`Finished: ${what}`);
  else if (j.status === "failed") announce(`Failed: ${what}. ${j.error || ""}`.trim());
  else if (j.status === "cancelled") announce(`Cancelled: ${what}`);
}

function renderJobsPill() {
  const n = lastJobs.filter(jobActive).length;
  $("jobs-pill-label").textContent = n ? `${n} running` : "Jobs";
  $("jobs-pill").setAttribute("aria-label", n ? `Jobs, ${n} running` : "Jobs");
  $("jobs-pill").classList.toggle("running", n > 0);
}

function renderJobsList() {
  const list = $("jobs-list");
  // The list re-renders every poll while open: put focus back on the same control after.
  const had = list.contains(document.activeElement) ? document.activeElement : null;
  const keep = had && { id: had.closest(".job-row")?.dataset.jobId, cls: had.classList.contains("job-act") ? "job-act" : "job-file" };
  list.innerHTML = "";
  $("jobs-empty").hidden = lastJobs.length > 0;
  $("jobs-clear").hidden = !lastJobs.some((j) => !jobActive(j));
  for (const j of lastJobs) {
    const li = document.createElement("li");
    li.className = `job-row job-state-${j.status}`;
    li.dataset.jobId = j.id;
    let sub;
    if (j.status === "queued") sub = "Queued";
    else if (j.status === "running") {
      const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
      sub = `Working… ${secs}s` + (j.progress ? ` · ${escapeHtml(j.progress)}` : "");
    } else if (j.status === "done") {
      sub = (j.files || []).map((f) =>
        `<a class="job-file" data-action="job.download" href="/api/jobs/${encodeURIComponent(j.id)}/download/${encodeURIComponent(f.name)}" download title="Download this file"><span aria-hidden="true">⤓</span> ${escapeHtml(f.name)} (${fmtSize(f.size)})</a>`
      ).join(" ") || (j.result?.summary ? escapeHtml(j.result.summary) : "Done");
    } else if (j.status === "failed") sub = `<span class="job-failed">${escapeHtml(j.error || "Failed")}</span>`;
    else sub = j.result?.summary ? `Cancelled · ${escapeHtml(j.result.summary)}` : "Cancelled";
    li.innerHTML =
      `<span class="job-mark" aria-hidden="true"></span>` +
      `<div class="job-main"><div class="job-title">${escapeHtml(j.label || j.id)}</div><div class="job-sub muted">${sub}</div></div>` +
      `<button type="button" class="link-btn job-act" data-action="job.cancel" ` +
        `title="${jobActive(j) ? "Stop this job" : "Remove this job from the list"}" ` +
        `aria-label="${jobActive(j) ? "Cancel" : "Dismiss"} ${escapeHtml(j.label || j.id)}">${jobActive(j) ? "Cancel" : "Dismiss"}</button>`;
    li.querySelector(".job-act").onclick = async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      const resp = await apiFetch(`/api/jobs/${encodeURIComponent(j.id)}`, { method: "DELETE" }).catch(() => null);
      if (resp && resp.status === 409) li.querySelector(".job-sub").textContent = "Can't be stopped while it runs; it will finish.";
      refreshJobs();
    };
    list.appendChild(li);
  }
  if (keep) {
    const again = [...list.children].find((r) => r.dataset.jobId === keep.id)?.querySelector("." + keep.cls);
    (again || $("jobs-popover")).focus();
  }
}

function toggleJobs(open, restore) {
  const pop = $("jobs-popover");
  const show = open === undefined ? pop.hidden : open;
  const was = !pop.hidden;
  jobsOpen = show;
  setPopover(pop, $("jobs-pill"), show, restore);
  if (show && !was) { toggleScopeMenu(false); toggleSettings(false); renderJobsList(); refreshJobs(); focusFirstIn(pop); }
  // Only an open or close changes the poll rate: every click on the page closes the
  // popover, and rescheduling then would keep pushing the next poll back.
  if (show !== was) scheduleJobs();
}

// --- popovers (Jobs, Settings): focus moves in, Tab stays in, Esc closes and returns focus --

const FOCUSABLE = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex="0"]';
// Out of the Tab order (tabindex -1: a radio group's unchecked radios) doesn't count, or
// Shift+Tab off the checked radio slips past a trap that thinks "System" comes first.
const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((n) =>
  !n.closest("[hidden]") && n.offsetParent !== null && n.getAttribute("tabindex") !== "-1");

function focusFirstIn(pop) {
  const first = focusables(pop)[0];
  (first || pop).focus();
}

/** Show or hide a popover from its button. Hiding it returns focus to the button when focus
 *  was inside (or `restore` asks, as Esc does), not when a click elsewhere closed it. */
function setPopover(pop, btn, show, restore) {
  const wasOpen = !pop.hidden;
  pop.hidden = !show;
  btn.setAttribute("aria-expanded", String(show));
  if (!show && wasOpen && (restore || pop.contains(document.activeElement))) btn.focus();
}

function trapTab(pop) {
  pop.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const items = focusables(pop);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === pop)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}
trapTab($("jobs-popover"));
trapTab($("settings-popover"));
$("jobs-pill").onclick = (e) => { e.stopPropagation(); toggleJobs(); };

// --- Settings popover: appearance and subtitle languages ----------------------
//
// Per-browser preferences. The theme follows the OS until you pick Dark or Light;
// the choice is applied before first paint by a small script in index.html's head.
// Subtitle languages is the #ss-langs field, wired where the stream selector is.

const THEME_KEY = "qc_theme";

function currentThemeChoice() {
  let t = null;
  try { t = localStorage.getItem(THEME_KEY); } catch {}
  return t === "light" || t === "dark" ? t : "system";
}

function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
  for (const b of $("theme-seg").querySelectorAll("button")) {
    const on = b.dataset.themeChoice === choice;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
  }
}

/** Arrow keys move the choice in a role=radiogroup of buttons, as in the ARIA radio pattern. */
function radioArrows(group) {
  group.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    const radios = [...group.querySelectorAll('[role="radio"]')];
    const i = radios.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const next = radios[(i + step + radios.length) % radios.length];
    next.focus();
    next.click();
  });
}
radioArrows($("theme-seg"));

$("theme-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-theme-choice]");
  if (!b) return;
  const choice = b.dataset.themeChoice;
  try {
    if (choice === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, choice);
  } catch {}
  applyTheme(choice);
});
applyTheme(currentThemeChoice());

// Subtitle size and background on the video (M16), per browser. Normal leaves the player's
// own size; Large and Extra large set it from the player's height, so it follows the window
// and full screen. The official YouTube embed draws its own captions and ignores these.
const SUB_SIZE_KEY = "qc_sub_size";
const SUB_BG_KEY = "qc_sub_bg";
const CUE_SCALE = { large: [0.07, 20], xl: [0.095, 26] };  // share of the player's height, minimum px

function readPref(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writePref(key, value) {
  try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch {}
}

function applyCueStyle() {
  const size = readPref(SUB_SIZE_KEY);
  const rules = [];
  const h = $("player").clientHeight;
  if (CUE_SCALE[size] && h) {
    const [share, min] = CUE_SCALE[size];
    rules.push(`font-size: ${Math.max(min, Math.round(h * share))}px`);
  }
  if (readPref(SUB_BG_KEY) === "solid") rules.push("background: #000", "color: #fff");
  $("cue-style").textContent = rules.length ? `#player::cue { ${rules.join("; ")}; }` : "";
}

function showSubPrefs() {
  const size = CUE_SCALE[readPref(SUB_SIZE_KEY)] ? readPref(SUB_SIZE_KEY) : "normal";
  for (const b of $("sub-size-seg").querySelectorAll("button")) {
    const on = b.dataset.subSize === size;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  $("sub-bg").checked = readPref(SUB_BG_KEY) === "solid";
  applyCueStyle();
}

$("sub-size-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sub-size]");
  if (!b) return;
  writePref(SUB_SIZE_KEY, b.dataset.subSize === "normal" ? null : b.dataset.subSize);
  showSubPrefs();
});
radioArrows($("sub-size-seg"));
$("sub-bg").addEventListener("change", () => {
  writePref(SUB_BG_KEY, $("sub-bg").checked ? "solid" : null);
  showSubPrefs();
});
new ResizeObserver(applyCueStyle).observe($("player"));
showSubPrefs();

function toggleSettings(open, restore) {
  const pop = $("settings-popover");
  const show = open === undefined ? pop.hidden : open;
  const was = !pop.hidden;
  setPopover(pop, $("settings-btn"), show, restore);
  if (show && !was) {
    toggleScopeMenu(false); toggleJobs(false);
    // Into the appearance choice (its checked option is the group's tab stop).
    (pop.querySelector('[role="radio"][aria-checked="true"]') || pop).focus();
  }
}
$("settings-btn").onclick = (e) => { e.stopPropagation(); toggleSettings(); };
$("jobs-clear").onclick = async () => {
  await Promise.all(lastJobs.filter((j) => !jobActive(j)).map((j) =>
    apiFetch(`/api/jobs/${encodeURIComponent(j.id)}`, { method: "DELETE" }).catch(() => null)));
  refreshJobs();
};

function scheduleJobs() {
  clearTimeout(jobsTimer);
  let delay = document.hidden ? 15000 : (jobsOpen || lastJobs.some(jobActive)) ? 1500 : 5000;
  if (jobsFailures) delay = Math.min(60000, delay * 2 ** jobsFailures);
  jobsTimer = setTimeout(async () => { await refreshJobs(); scheduleJobs(); }, delay);
}
/** Called after a request that starts a job, so the pill doesn't wait for the next poll. */
function nudgeJobs() { setTimeout(async () => { await refreshJobs(); scheduleJobs(); }, 400); }
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refreshJobs(); scheduleJobs(); refreshNowPlaying(); }
});

// --- boot -------------------------------------------------------------------

let appConfig = {};
let mediaRoots = [];  // absolute media-root paths, for breadcrumb segmentation

/** Config and media roots, which the first view needs (the breadcrumb's clickable
 *  segments, Send to LeadStinger buttons): the page routes once they're in. The health
 *  line in the footer arrives when it does. */
async function loadStatus() {
  getJSON("/api/health").then((h) => {
    const tools = Object.entries(h.tools)
      .map(([k, v]) => `${v ? "✓" : "✗"} ${k}`)
      .join("  ");
    $("status").textContent = `${h.service} v${h.version} — ${tools}`;
  }).catch((err) => {
    $("status").textContent = `backend unreachable: ${err.message}`;
  });
  const roots = getJSON("/api/library/roots").catch(() => null);
  // Load config (subtitle_langs, hw_encode, etc.)
  try {
    appConfig = await getJSON("/api/config");
    // Show the server default subtitle-language preference as the placeholder.
    if (appConfig.subtitle_langs && $("ss-langs")) {
      $("ss-langs").placeholder = appConfig.subtitle_langs.join(",");
    }
    // No public URL: Send to LeadStinger makes a set file instead of sending by link.
    if (!appConfig.public_url) {
      $("ls-send-tip").textContent = "Send to LeadStinger: cuts the selection with the " +
        "Before/After padding into a LeadStinger set file to import. The padding becomes the " +
        "tile's handles. (Sending straight into LeadStinger needs the server's QC_PUBLIC_URL.)";
    }
    // Jellyfin configured: offer the Now playing scope and card (M15).
    $("scope-nowplaying").hidden = !appConfig.jellyfin;
    showSttSetting();  // the Transcription model setting, when the server transcribes (M21)
    refreshNowPlaying();
  } catch {}
  // Load media roots so the breadcrumb can offer clickable path segments.
  const r = await roots;
  if (r) {
    mediaRoots = r.roots || [];
    renderRootsRail(currentBrowsePath);
  }
}

// The skip link goes to whatever the page is showing: the workspace, the results, the list.
function skipTarget() {
  if (currentView === "item") return [$("item-name"), "Skip to the workspace"];
  if (currentView === "clips") return [$("clips-title"), "Skip to the clips"];
  if (currentView === "bookmarks") return [$("bm-title"), "Skip to the bookmarks"];
  if ($("browser").classList.contains("hits")) return [$("entries"), "Skip to the results"];
  return [$("entries"), "Skip to the library"];
}
$("skip-link").addEventListener("focus", () => { $("skip-link").textContent = skipTarget()[1]; });
$("skip-link").addEventListener("click", (e) => {
  e.preventDefault();
  const [target] = skipTarget();
  target.focus();
  target.scrollIntoView({ block: "start" });
});

// Space or K plays and pauses in the workspace, unless focus is on something Space operates.
document.addEventListener("keydown", (e) => {
  if (currentView !== "item" || !pb || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  const onControl = t && t.closest && t.closest('button, a[href], summary, video, [role="button"], [role="slider"], [role="tab"]');
  if (e.key === "k" || e.key === "K" || (e.key === " " && !onControl)) {
    e.preventDefault();
    pb.toggle();
  }
});

$("back").onclick = () => browse(currentParent());
$("home-link").onclick = () => browse(null);
$("library-link").onclick = () => browse(null);

// Derive the parent directory of the current item for the Back button.
function currentParent() {
  if (!currentItem) return null;
  const p = currentItem.path;
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i > 0 ? p.slice(0, i) : null;
}

loadStatus().finally(() => applyRoute(location.hash));
refreshJobs().then(scheduleJobs);
