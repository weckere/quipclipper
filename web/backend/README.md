# quipclipper-web

The web backend for [quipclipper](../../README.md): a small FastAPI app that wraps
the quipclipper engine so clips can be found and cut from a browser. See
[`docs/WEBAPP_PLAN.md`](../../docs/WEBAPP_PLAN.md) for the full design.

The backend serves the API and (in dev) the static frontend; in a real
deployment nginx fronts it. Two deployment paths are supported: Docker Compose
and a NixOS module.

## Deploy with Docker

See the [Web App quick start](../../README.md#web-app) in the root README for a
ready-to-paste `docker-compose.yml` that builds both images directly from the
GitHub repo — you only need that one file on the server. For local development,
[`../docker-compose.yml`](../docker-compose.yml) builds from the working tree and
bind-mounts the frontend/nginx config so edits show up without a rebuild.

## Run locally (dev)

```bash
pip install -e ..        # install the quipclipper engine (repo root)
pip install -e ".[dev]"  # install this backend + test deps
quipclipper-web          # serves on QC_BIND:QC_PORT (default 127.0.0.1:8000)
```

Then open the API directly, e.g. <http://127.0.0.1:8000/api/health>. In a real
deployment nginx serves the frontend and proxies `/api` to this process.

## Configuration

Environment variables (mirrored 1:1 by the NixOS module options):

| Var | Default | Purpose |
|---|---|---|
| `QC_MEDIA_ROOTS` | (empty) | `:`-separated whitelist of media dirs |
| `QC_CLIPS_DIR` | `./clips` | where finished clips are written |
| `QC_CLIPS_URL_PREFIX` | (empty) | URL prefix where a front proxy serves the clips dir directly; empty = download via the backend API |
| `QC_STATE_DIR` | `./state` | bookmark store, subtitle cache, etc. |
| `QC_BIND` | `127.0.0.1` | backend listen address |
| `QC_PORT` | `8000` | backend listen port |
| `QC_MAX_CONCURRENT_JOBS` | `2` | ffmpeg job cap |
| `QC_PASSWORD` | (unset) | reserved for the planned password gate (phase 6) — **not yet enforced**; only reported via `/api/config` |
| `QC_JELLYFIN_URL` | (unset) | optional Jellyfin metadata enrichment |
| `QC_JELLYFIN_API_KEY` | (unset) | Jellyfin API key (required if the URL is set) |

## Test

```bash
pytest
```
