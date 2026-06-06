# quipclipper-web

The web backend for [quipclipper](../../README.md): a small FastAPI app that wraps
the quipclipper engine so clips can be found and cut from a browser. See
[`docs/WEBAPP_PLAN.md`](../../docs/WEBAPP_PLAN.md) for the full design.

This is **Phase 0**: a booting skeleton (health + config endpoints) that the two
deployment paths — Docker and the NixOS module — both stand up against a static
"hello" page served by nginx.

## Deploy with Docker

Two compose files live in [`../`](..):

- **`docker-compose.omv.yml`** — *self-contained.* Builds both images straight
  from the public GitHub repo, so you only need this one file on the server
  (OpenMediaVault, a NAS, etc.). Edit the two `<-- CHANGE` host paths (your media
  share, read-only; and a clips folder) and optionally the port, then bring it
  up. First start builds the images (installs ffmpeg + mkvtoolnix); then browse
  `http://<host>:8896`.
- **`docker-compose.yml`** — *for a local checkout.* Builds from the working tree
  and bind-mounts the frontend/nginx config, so edits show up without a rebuild.

In OMV: **Services → Compose → Files → +**, paste `docker-compose.omv.yml`,
Save, then **Up**.

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
| `QC_STATE_DIR` | `./state` | bookmark store, etc. |
| `QC_SAVE_TO_LIBRARY` | `false` | also file clips into the clips library |
| `QC_BIND` | `127.0.0.1` | backend listen address |
| `QC_PORT` | `8000` | backend listen port |
| `QC_MAX_CONCURRENT_JOBS` | `2` | ffmpeg job cap |
| `QC_PASSWORD` | (unset) | presence enables the auth gate (handled by nginx) |
| `QC_JELLYFIN_URL` | (unset) | optional metadata enrichment |

## Test

```bash
pytest
```
