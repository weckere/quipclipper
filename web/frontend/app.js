// Phase 0: confirm the frontend can reach the backend through nginx's /api proxy.

async function getJSON(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json();
}

async function loadHealth() {
  const health = document.getElementById("health");
  const toolsEl = document.getElementById("tools");
  try {
    const data = await getJSON("/api/health");
    health.textContent = `${data.service} v${data.version} — ${data.status}`;
    health.classList.add("ok");
    toolsEl.hidden = false;
    toolsEl.innerHTML = "";
    for (const [name, present] of Object.entries(data.tools)) {
      const li = document.createElement("li");
      li.textContent = `${present ? "✓" : "✗"} ${name}`;
      li.className = present ? "ok" : "missing";
      toolsEl.appendChild(li);
    }
  } catch (err) {
    health.textContent = `Backend unreachable: ${err.message}`;
    health.classList.add("error");
  }
}

async function loadConfig() {
  const el = document.getElementById("config-body");
  try {
    const cfg = await getJSON("/api/config");
    const roots = cfg.media_roots.length ? cfg.media_roots.join(", ") : "(none configured)";
    el.innerHTML = `
      Media roots: <code>${roots}</code><br />
      Save to library: <strong>${cfg.save_to_library}</strong><br />
      Auth required: <strong>${cfg.auth_required}</strong><br />
      Jellyfin enrichment: <strong>${cfg.jellyfin_enabled}</strong>`;
  } catch (err) {
    el.textContent = `Could not load config: ${err.message}`;
  }
}

loadHealth();
loadConfig();
