# quipclipper-web

The web backend for [quipclipper](../../README.md): a small FastAPI app that wraps
the quipclipper engine so clips can be found and cut from a browser. See
[`docs/WEBAPP_PLAN.md`](../../docs/WEBAPP_PLAN.md) for the full design.

The backend serves the API and (in dev) the static frontend; in a real
deployment nginx fronts it. Two deployment paths are supported: Docker Compose
and a NixOS module.

## Deploy with Docker

See the [Quick start (Docker Compose)](../../README.md#quick-start-docker-compose)
in the root README for a ready-to-edit `docker-compose.yml` using the prebuilt
GHCR images (a build-from-source variant is shown there too). For local development,
[`../docker-compose.yml`](../docker-compose.yml) builds from the working tree and
bind-mounts the frontend/nginx config so edits show up without a rebuild.

## Run locally (dev)

```bash
pip install -e ../..     # install the quipclipper engine (repo root)
pip install -e ".[dev]"  # install this backend + test deps
quipclipper-web          # serves on QC_BIND:QC_PORT (default 127.0.0.1:8000)
```

Then open the API directly, e.g. <http://127.0.0.1:8000/api/health>. In a real
deployment nginx serves the frontend and proxies `/api` to this process.

External tools on PATH: `ffmpeg`/`ffprobe` (required), `mkvmerge` (optional —
the preferred lossless cutting backend), and `yt-dlp` (optional — required for
YouTube sources). `/api/health` reports which are present.

## Configuration

Environment variables (mirrored by the NixOS module options):

| Var | Default | Purpose |
|---|---|---|
| `QC_MEDIA_ROOTS` | (empty) | `:`-separated whitelist of media dirs |
| `QC_CLIPS_DIR` | `./clips` | where finished clips are written |
| `QC_CLIPS_URL_PREFIX` | (empty) | URL prefix where a front proxy serves the clips dir directly; empty = download via the backend API |
| `QC_STATE_DIR` | `./state` | bookmark store, subtitle cache, etc. |
| `QC_BIND` | `127.0.0.1` | backend listen address |
| `QC_PORT` | `8000` | backend listen port |
| `QC_MAX_CONCURRENT_JOBS` | `2` | ffmpeg job cap |
| `QC_PASSWORD` | (unset) | when set on the **nginx** service, gates the whole site with HTTP basic auth (also reported via `/api/config`) |
| `QC_USERNAME` | `quip` | basic-auth username (used only when `QC_PASSWORD` is set) |
| `QC_SUBTITLE_LANGS` | `en` | ordered subtitle-language auto-select preference (comma-separated, e.g. `eng,spa`); UI Auto-lang box overrides per browser |
| `QC_YTDLP_ARGS` | `--socket-timeout 10 --extractor-retries 1` | extra args prepended to every `yt-dlp` call (fail-fast so a hung YouTube endpoint doesn't stall a fetch for ~80s). Override to tune; set empty for yt-dlp's defaults |
| `QC_PROXY_SECRET` | (unset) | defence-in-depth: when set on **both** services, nginx injects it and the backend rejects any request that reaches port 8000 without it (except `/api/health`), blocking direct hits that bypass nginx |
| `QC_API_TOKEN` | (unset) | token(s) for programmatic clients (comma-separated for rotation); a client sends it as `X-API-Key`/`Bearer`/`-u api:<token>` and is then exempt from the CSRF header. Set on **both** services so `-u api:<token>` also passes the basic-auth gate. See [`../../docs/API.md`](../../docs/API.md) |

The backend has **no auth of its own** — the nginx front is the only gate. Two
built-in defences back that up: every state-changing request must carry an
`X-Quipclipper` header (a CSRF guard the frontend sends automatically), and
`QC_PROXY_SECRET` (above) lets the backend refuse traffic that didn't come
through nginx. Keep `QC_BIND` on loopback unless another proxy fronts it.

**Driving the API from agents/scripts:** set `QC_API_TOKEN` and use it as shown
in [`../../docs/API.md`](../../docs/API.md) — the full endpoint reference, auth
options, and worked examples. The live schema is at `/openapi.json` (`/docs` for
Swagger UI).

**Hardware video transcode:** the backend auto-detects an Intel iGPU at
`/dev/dri/renderD128` (probed once, reported as `hw_encode` in `/api/config`) and
uses `h264_vaapi` (Quick Sync via VAAPI) — for both the **browser-preview** video
re-encode and the **clip output** re-encode (Exact / `--no-lossless`) — falling
back to software `libx264` if the GPU encode fails. (Lossless cuts are stream
copies, so they never hit the encoder.) Pass the device into the container to
enable it (see the root README's Docker quick start). Override the node with
`QC_VAAPI_DEVICE` (e.g. a second GPU's `/dev/dri/renderD129`); set
`LIBVA_DRIVER_NAME` (`iHD` for Gen8+ Intel) if libva doesn't auto-select.

## Test

```bash
pytest
```
