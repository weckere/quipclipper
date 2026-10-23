# Deploying the quipclipper web app

Two ways to run the web app: **Docker Compose** with the prebuilt images, or the
**NixOS module** from the flake. Both run the same backend (FastAPI) behind nginx.
For the CLI on its own, see the [README](../README.md#install).

- [Docker Compose](#docker-compose)
- [Configuration](#configuration)
- [NixOS module](#nixos-module)
  - [Sharing the clips directory](#sharing-the-clips-directory)
- [Architecture](#architecture)
- [Versions and upgrades](#versions-and-upgrades)

## Docker Compose

The repo ships a ready-to-edit [`docker-compose.example.yml`](../docker-compose.example.yml)
(fully commented, with optional hardware transcode and the password gate). Copy
it, set your media paths, and `docker compose up -d`.

Every release publishes prebuilt images to the GitHub Container Registry, tagged
with the version (e.g. `0.2.0`), the minor series (`0.2`) and `latest`, so the simplest
deploy just **pulls** them — no local build. Pin a version tag to choose when you
upgrade, or follow `latest`. A minimal version:

```yaml
# docker-compose.yml (minimal example, prebuilt images)
services:
  app:
    image: ghcr.io/weckere/quipclipper-app:latest
    environment:
      QC_MEDIA_ROOTS: /media/movies:/media/shows
      QC_CLIPS_DIR: /clips
      QC_CLIPS_URL_PREFIX: /clips
      QC_STATE_DIR: /state
    volumes:
      - /path/to/movies:/media/movies:ro
      - /path/to/shows:/media/shows:ro
      - /path/to/clips:/clips
      - quip-state:/state
    expose:
      - "8000"

  web:
    image: ghcr.io/weckere/quipclipper-web:latest
    depends_on:
      - app
    ports:
      - "8896:80"
    volumes:
      - /path/to/clips:/clips:ro

volumes:
  quip-state:
```

Then `docker compose pull && docker compose up -d` (update with `pull` again),
and browse at `http://localhost:8896`.

**Optional — hardware video transcode (Intel Quick Sync):** to re-encode
undecodable video (HEVC for Firefox, XviD/MPEG-4 AVIs, …) on an Intel iGPU
instead of the CPU, pass the render device into the `app` service:

```yaml
  app:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "<render-group-gid>"   # `getent group render` on the host
```

The image ships the Intel `iHD` VAAPI driver; without the device it falls back
to software encoding automatically.

**Optional — password gate:** set `QC_PASSWORD` on the **web** (nginx) service to
gate the whole site behind HTTP basic auth (username defaults to `quip`, override
with `QC_USERNAME`); leave it unset for an open LAN instance:

```yaml
  web:
    environment:
      QC_PASSWORD: "your-secret"   # or ${QC_PASSWORD}
      QC_USERNAME: "quip"          # optional
```

<details><summary>Build from source instead (no published images)</summary>

Swap each service's `image:` for a `build:` block pointing at the repo:

```yaml
  app:
    build:
      context: "https://github.com/weckere/quipclipper.git#main"
      dockerfile: web/Dockerfile
  web:
    build:
      context: "https://github.com/weckere/quipclipper.git#main"
      dockerfile: web/nginx/Dockerfile
```

</details>

## Configuration

All settings are environment variables on the `app` service:

| Variable | Default | Description |
|---|---|---|
| `QC_MEDIA_ROOTS` | *(required)* | Colon-separated list of media directories (in-container paths) |
| `QC_CLIPS_DIR` | `/clips` | Where finished clips are saved |
| `QC_CLIPS_URL_PREFIX` | *(empty)* | URL prefix where a front proxy (nginx) serves the clips dir directly; empty = download via the backend API |
| `QC_STATE_DIR` | `/state` | Bookmarks, subtitle cache, and other persistent state |
| `QC_MAX_CONCURRENT_JOBS` | `2` | Clip-job thread-pool size |
| `QC_PASSWORD` | *(none)* | When set, nginx gates the whole site with HTTP basic auth (username `QC_USERNAME`). Must be set on the **web** (nginx) service. Unset = open. |
| `QC_USERNAME` | `quip` | Basic-auth username (only used when `QC_PASSWORD` is set). |
| `QC_SUBTITLE_LANGS` | `en` | Ordered subtitle-language preference for auto-selection (comma-separated, e.g. `eng,spa`). The UI's **Subtitle languages** setting (the sliders button in the top bar) overrides it per browser; the same menu switches between light and dark. |
| `QC_API_TOKEN` | *(none)* | Token(s) for programmatic clients — agents and scripts (comma-separated to rotate). Set the same value on **both** services so `-u api:<token>` passes the basic-auth gate too. See [API.md](API.md). |
| `QC_PUBLIC_URL` | *(none)* | This server's base URL as other machines reach it (e.g. `https://quipclipper.example.net`). **Send to LeadStinger** puts it in a `leadstinger://quip` link, and LeadStinger downloads the clip from there with `QC_API_TOKEN`. Unset = Send to LeadStinger makes set files only. See [docs/MANUAL.md](MANUAL.md#sending-clips-to-leadstinger). |
| `QC_JELLYFIN_URL` | *(none)* | Jellyfin's base URL, for **Now playing** (what each Jellyfin client is playing, opened at its playhead). Needs the key below. See [docs/MANUAL.md](MANUAL.md#now-playing-jellyfin). |
| `QC_JELLYFIN_API_KEY` / `QC_JELLYFIN_API_KEY_FILE` | *(none)* | A Jellyfin API key, or a file holding one. |
| `QC_JELLYFIN_PATH_MAP` | *(none)* | Jellyfin's paths → quipclipper's, e.g. `/movies=/media/movies:/shows=/media/shows`, when Jellyfin sees the media elsewhere (its own container). |
| `QC_YOUTUBE_DIR` | *(none)* | Where downloaded YouTube videos land (each as `<Title> [<id>].mp4` with a `.vtt` sidecar, a normal library item). Unset = private in the state dir. See [YouTube sources](MANUAL.md#youtube-sources-experimental). |
| `QC_PROXY_SECRET` | *(none)* | Defence-in-depth: a shared secret nginx injects on every proxied request. When set on **both** services, the backend rejects anything that reaches its port without it (except `/api/health`) — blocking direct hits that bypass nginx. |

(`QC_BIND`, `QC_PORT`, and `QC_VAAPI_DEVICE` also exist for non-default network
binds and hardware-transcode setups — see
[`web/backend/README.md`](../web/backend/README.md) for the full table.)

## NixOS module

On NixOS, deploy the web app declaratively with the flake's module instead of
Docker — it runs the backend as a hardened systemd service and configures the
host nginx (a VM test in CI exercises the whole flow).

Add the flake as an input and pull the module into your host. The repository's
default branch, `main`, is always the latest release; pin a tag
(e.g. `github:weckere/quipclipper/v0.2.0`) to stay on one, or follow `dev` for work in
progress. A minimal `flake.nix`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    quipclipper.url = "github:weckere/quipclipper";
  };

  outputs = { nixpkgs, quipclipper, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        quipclipper.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Then enable the service in `configuration.nix` (or any imported module). The
minimal config is just `enable` + `mediaRoots`:

```nix
{
  services.quipclipper-web = {
    enable = true;
    mediaRoots = [ "/srv/media/movies" "/srv/media/tv" ];  # required
    openFirewall = true;   # then browse at http://<host>/ on the LAN
  };
}
```

That's a complete deployment: nginx fronts the app on `:80`, finished clips are
written to `/var/lib/quipclipper-web/clips` and served from there.

Optional settings (all off/defaulted unless set):

| Option | Purpose |
|---|---|
| `passwordFile` | Gate the whole site behind HTTP basic auth — path to a ready-made nginx htpasswd file (kept out of the store). Unlike Docker's plaintext `QC_PASSWORD`, the module takes the htpasswd directly. |
| `apiTokenFile` | API token(s) for agents/scripts — path to a systemd `EnvironmentFile` containing `QC_API_TOKEN=<token>` (kept out of the store). Add an `api` user with the token as password to `passwordFile` so one secret passes the basic-auth gate too. |
| `publicUrl` | This server's base URL as other machines reach it (`QC_PUBLIC_URL`), for **Send to LeadStinger** links. LeadStinger downloads with the token from `apiTokenFile`. |
| `jellyfin.url` / `jellyfin.apiKeyFile` / `jellyfin.pathMap` | **Now playing** from Jellyfin: its URL, a file holding an API key (root-only is fine: it's passed as a systemd credential) and Jellyfin's paths → this host's (`{ "/movies" = "/srv/movies"; }`). |
| `hardwareAcceleration.enable` | Intel Quick Sync H.264 encoding (else software `libx264`). Adds the service user to the `render` group, exposes the render node, and enables the Intel media driver. |
| `virtualHost` | nginx server name for a public hostname; pair with `enableACME`/`forceSSL` on that vhost for TLS. Default (null) is a catch-all vhost for LAN-by-IP over `:80`. |
| `nginx.enable` | `false` runs **only** the hardened backend on `listenAddress:listenPort` and skips the bundled vhost — bring your own front (nginx/Caddy/Tailscale) proxying to it (the backend then serves clips itself). |
| `nginx.port` / `nginx.listen` | Move the bundled vhost off `:80`. `nginx.port` sets the port; `nginx.listen` (e.g. `[ { addr = "172.17.0.1"; port = 8080; } ]`) sets explicit listen entries to bind a specific address/port. `openFirewall` tracks whichever it binds. |
| `nginx.defaultServer` | `false` so the vhost isn't nginx's catch-all `default_server` — lets quipclipper coexist with another web service on the host without grabbing every unmatched request. |
| `clipsDir` | Where finished clips live (default `/var/lib/quipclipper-web/clips`, private to the service). |
| `clipsGroup` / `clipsMode` | Share `clipsDir` with other users/services (SMB export, Jellyfin library). See below. |
| `manageClipsDir` | `false` = `clipsDir` is owned/created elsewhere (e.g. an existing SMB mount); the module won't chown/chmod it. See below. |
| `writableMediaRoots` | Media roots the app may **write into** (bound read-write, not read-only) — so it can drop subtitle sidecars next to your videos (downloaded transcripts and subtitle backfill). Browsed and clipped exactly like `mediaRoots`. Use it for a library you want quipclipper to enrich (e.g. a Pinchflat/yt-dlp folder). |
| `youtubeDir` | Where downloaded YouTube videos land (default `null` = private in the state dir). Point it at a folder — ideally under a `writableMediaRoots` entry — and each download becomes `<Title> [<id>].mp4` with a `.vtt` sidecar: a browsable, dialogue-searchable library item, visible to Jellyfin/Samba too. |
| `subtitleLangs`, `listenAddress`, `listenPort`, `maxConcurrentJobs` | Mirror the Docker env vars `QC_SUBTITLE_LANGS` / `QC_BIND` / `QC_PORT` / `QC_MAX_CONCURRENT_JOBS`. `youtubeDir` mirrors `QC_YOUTUBE_DIR`. |

The module builds the app from quipclipper's own pinned nixpkgs, so you don't
need an `inputs.nixpkgs.follows`.

### Sharing the clips directory

By default `clipsDir` is private (`0750`, owned by the service) and served only
through quipclipper's own web UI. To make finished clips land in a folder other
users and services can read — an SMB export, a directory Jellyfin indexes — there
are two supported flows:

**Let the module own a shared directory.** Give it a shared group and a setgid,
group-readable mode; new clips inherit the group and are group-readable:

```nix
services.quipclipper-web = {
  enable = true;
  mediaRoots = [ "/srv/media/movies" ];
  clipsDir   = "/srv/clips";
  clipsGroup = "users";   # the group your SMB/Jellyfin runs as (must exist)
  clipsMode  = "2775";    # setgid + group-writable
};
```

**Point it at a directory provisioned elsewhere** (e.g. a pre-existing SMB share
that is already `root:users` setgid `2775`). Set `manageClipsDir = false` so the
module leaves the directory's owner and mode alone — it only adds the service
user (and nginx) to `clipsGroup` so they can write and serve through the share's
group-writable/setgid bits:

```nix
services.quipclipper-web = {
  enable = true;
  mediaRoots = [ "/srv/media/movies" ];
  clipsDir   = "/mnt/share/clips";   # mounted + owned externally
  clipsGroup = "users";
  manageClipsDir = false;
};
```

In both cases nginx is added to `clipsGroup`, so the web UI keeps serving the same
clips, and the service writes with a group-writable umask (`0002`) so clips land
**group-readable and group-writable** — members of `clipsGroup` (samba, Jellyfin,
your user) can both read *and* delete/manage them. With `manageClipsDir = false`,
ensure the directory exists and is group-writable (setgid `2775`) before the
service starts — the unit waits for its mount via `RequiresMountsFor`, but it
won't create or fix permissions on it.

## Architecture

The web app is two containers:

- **app** — Python (FastAPI + Uvicorn) serving the API (the image holds no frontend).
  Runs the quipclipper engine for search and clipping via a thread-pool job queue.
- **web** — Nginx serving the UI (its image holds the frontend and hls.js), large file downloads
  (clips), and forwarding API requests to the app.

Media directories are mounted read-only. All file access is realpath-checked
against the configured media roots — path traversal and symlink escapes are
rejected.

## Versions and upgrades

quipclipper follows [Semantic Versioning](https://semver.org/) (still 0.x: a minor
version for features, a patch for fixes). [CHANGELOG.md](../CHANGELOG.md) lists what
changed in each release, and every release is on the
[releases page](https://github.com/weckere/quipclipper/releases) with its images.

| You want | Docker image tag | Flake URL |
|---|---|---|
| One exact release | `:X.Y.Z` (e.g. `:0.2.0`) | `github:weckere/quipclipper/vX.Y.Z` |
| Fixes within a series | `:X.Y` (e.g. `:0.2`) | (pin a tag and bump it) |
| The latest release | `:latest` | `github:weckere/quipclipper` (`main`) |
| Work in progress | (build from `dev`) | `github:weckere/quipclipper/dev` |
