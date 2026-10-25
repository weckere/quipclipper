# Deploying the quipclipper web app

Two ways to run the web app: **Docker Compose** with the prebuilt images, or the
**NixOS module** from the flake. Both run the same backend (FastAPI) behind nginx.
For the CLI on its own, see the [README](../README.md#install).

- [Docker Compose](#docker-compose)
- [Configuration](#configuration)
- [NixOS module](#nixos-module)
  - [Sharing the clips directory](#sharing-the-clips-directory)
- [Pairing behind the password gate](#pairing-behind-the-password-gate)
- [Advertising on the local network](#advertising-on-the-local-network)
- [Architecture](#architecture)
- [Versions and upgrades](#versions-and-upgrades)

## Docker Compose

The repo ships a ready-to-edit [`docker-compose.example.yml`](../docker-compose.example.yml)
(fully commented, with optional hardware transcode and the password gate). Copy
it, set your media paths, and `docker compose up -d`.

Every release publishes prebuilt images to the GitHub Container Registry, tagged
with the version (e.g. `0.3.1`), the minor series (`0.3`) and `latest`, so the simplest
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
| `QC_PUBLIC_URL` | *(none)* | This server's base URL as other machines reach it (e.g. `https://quipclipper.example.net`). **Send to LeadStinger** puts it in a `leadstinger://quip` link, and LeadStinger downloads the clip from there with the key it got by pairing (or `QC_API_TOKEN`). Unset = links name the advertised URL, else the address the browser is using. Deep links (Copy link) need it. See [docs/MANUAL.md](MANUAL.md#sending-clips-to-leadstinger). |
| `QC_PAIRING` | `1` | **Pairing**: an app such as LeadStinger asks for access and a person allows it in the web UI; it gets its own client token, revoked under Settings ▸ Connected apps. `0` turns every `/api/pair` route into 404 (apps already paired keep working). See [Pairing](API.md#pairing). |
| `QC_SERVER_NAME` | *(host name)* | The name apps show for this server, at most 63 characters. In Docker the app's host name is the container's, so set it, or run the advertiser (whose host name is the machine's). |
| `QC_ADVERTISE` / `QC_ADVERTISE_PORT` | *(off)* | For the `quipclipper-web advertise` process only: `1` to announce the server on the LAN, and the port browsers use. See [Advertising on the local network](#advertising-on-the-local-network). |
| `QC_JELLYFIN_URL` | *(none)* | Jellyfin's base URL, for **Now playing** (what each Jellyfin client is playing, opened at its playhead). Needs the key below. See [docs/MANUAL.md](MANUAL.md#now-playing-jellyfin). |
| `QC_JELLYFIN_API_KEY` / `QC_JELLYFIN_API_KEY_FILE` | *(none)* | A Jellyfin API key, or a file holding one. |
| `QC_HOOK_TOKEN` / `QC_HOOK_TOKEN_FILE` | *(none)* | A low-privilege token for the **re-index hooks** (Bazarr, Sonarr, Radarr tell quipclipper a file's subtitles changed): it may only queue re-indexes. Behind a password, set it on the **web** service too, which adds a `hook` login. See [HOOKS.md](HOOKS.md). |
| `QC_INDEX_CHECK_INTERVAL` | `3600` | Seconds between the background checks for files whose subtitles changed since they were indexed (folder listings and file dates only, at the idle I/O priority, skipped while a library run or re-index runs; it never re-indexes, the library page offers a button). `0` = off. See [MANUAL.md](MANUAL.md#changed-subtitles). |
| `QC_PATH_MAP` | *(none)* | The hooks' paths → quipclipper's, e.g. `/tv=/media/shows:/movies=/media/movies`, when Bazarr, Sonarr or Radarr see the media elsewhere (their own containers). Or JSON, `{"/tv": "/media/shows"}`, for paths holding `:`, `;` or `=` (or a backslash before the character). |
| `QC_JELLYFIN_PATH_MAP` | *(none)* | Jellyfin's paths → quipclipper's, e.g. `/movies=/media/movies:/shows=/media/shows`, when Jellyfin sees the media elsewhere (its own container). Same syntax as `QC_PATH_MAP` (JSON too). |
| `QC_YOUTUBE_DIR` | *(none)* | Where downloaded YouTube videos land (each as `<Title> [<id>].mp4` with a `.vtt` sidecar, a normal library item). Unset = private in the state dir. See [YouTube sources](MANUAL.md#youtube-sources-experimental). |
| `QC_TRANSCRIBE` | *(automatic)* | [Machine transcription](TRANSCRIPTION.md) is on where `whisper-cli` and a model file are installed (the two rows below), off elsewhere. `0` turns it off even there; `1` forces it on (without whisper it then reports itself not set up). |
| `QC_WHISPER_MODEL` / `QC_WHISPER_MODELS` | *(none)* | [Machine transcription](TRANSCRIPTION.md) with whisper.cpp: a ggml file (`/models/ggml-small.en.bin`), or `small.en=/models/ggml-small.en.bin,medium.en=/models/ggml-medium.en.bin` for a choice with `QC_WHISPER_MODEL` naming the default. A bare name needs `QC_WHISPER_MODELS` to say where its file is. No model file = off. It runs only when asked, at idle priority, one run at a time. |
| `QC_WHISPER_BIN` / `QC_WHISPER_THREADS` / `QC_WHISPER_VAD_MODEL` / `QC_WHISPER_LANGUAGE` | `whisper-cli` / `2` / *(none)* / `en` | The whisper.cpp binary, its threads per run (capped at 3, whatever you set), a Silero VAD model, used for whole-file runs only, and the language (English models only for now). The Docker images don't include whisper.cpp: transcription isn't supported there yet, so it stays off ([TRANSCRIPTION.md](TRANSCRIPTION.md#turning-it-on) has an untested outline for adding it). |
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
(e.g. `github:weckere/quipclipper/v0.3.1`) to stay on one, or follow `dev` for work in
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
| `publicUrl` | This server's base URL as other machines reach it (`QC_PUBLIC_URL`), for **Send to LeadStinger** links and deep links. Unset, Send to LeadStinger links name the address the browser is using (or the advertised one). |
| `pairing.enable` | Pairing (default `true`): apps ask for access and a person allows them; behind `passwordFile`, nginx lets exactly `POST /api/pair` and `GET /api/pair/<id>` through, and requests with a current client token. `false` sets `QC_PAIRING=0`. See [below](#pairing-behind-the-password-gate). |
| `serverName` | The name apps show for this server (`QC_SERVER_NAME`; default the host name), and the advertised name. |
| `advertise.enable` / `advertise.port` | Announce the server on the LAN with Avahi (default off; the port defaults to the first `nginx.listen` port). **Leave it off on a tailnet-only install.** See [below](#advertising-on-the-local-network). |
| `jellyfin.url` / `jellyfin.apiKeyFile` / `jellyfin.pathMap` | **Now playing** from Jellyfin: its URL, a file holding an API key (root-only is fine: it's passed as a systemd credential) and Jellyfin's paths → this host's (`{ "/movies" = "/srv/movies"; }`). |
| `indexCheckInterval` | Seconds between the checks for subtitles changed since indexing (`QC_INDEX_CHECK_INTERVAL`, default 3600, `0` = off). |
| `hookTokenFile` / `pathMap` | **Re-index hooks** (Bazarr, Sonarr, Radarr; see [HOOKS.md](HOOKS.md)): a file holding a hook token that may only queue re-indexes (root-only is fine: a systemd credential), and the hooks' paths → this host's (`{ "/tv" = "/srv/shows"; }`). With `passwordFile`, add a `hook` line with the token as its password. |
| `transcribe.enable` | [Machine transcription](TRANSCRIPTION.md) with whisper.cpp, on request only (no timer, no hook): `transcribe.models` (`[ "small.en" ]`; add `"medium.en"`, 1.5 GB) installs ggml models pinned by hash from Hugging Face (`transcribe.modelFiles` points a name at your own file; the others keep their pinned download, and a file outside the store is bound into the sandbox), `transcribe.defaultModel`, `transcribe.threads` (2; **1 to 3 only**: whisper shares the host with playback), `transcribe.vad` (Silero, for whole-file runs) and `transcribe.package` (`pkgs.whisper-cpp`; a CUDA override for a GPU). Every run is idle priority. Enabling it sets `QC_TRANSCRIBE=1`; left off, the service has no whisper and transcription stays out of sight. |
| `hardwareAcceleration.enable` | Intel Quick Sync H.264 encoding (else software `libx264`). Adds the service user to the `render` group, exposes the render node, and enables the Intel media driver. |
| `virtualHost` | nginx server name for a public hostname; pair with `enableACME`/`forceSSL` on that vhost for TLS. Default (null) is a catch-all vhost for LAN-by-IP over `:80`. |
| `nginx.enable` | `false` runs **only** the hardened backend on `listenAddress:listenPort` and skips the bundled vhost — bring your own front (nginx/Caddy/Tailscale) proxying to it (the backend then serves clips itself). |
| `nginx.port` / `nginx.listen` | Move the bundled vhost off `:80`. `nginx.port` sets the port; `nginx.listen` (e.g. `[ { addr = "172.17.0.1"; port = 8080; } ]`) sets explicit listen entries to bind a specific address/port. `openFirewall` tracks whichever it binds. |
| `nginx.defaultServer` | `false` so the vhost isn't nginx's catch-all `default_server` — lets quipclipper coexist with another web service on the host without grabbing every unmatched request. |
| `clipsDir` | Where finished clips live (default `/var/lib/quipclipper-web/clips`, private to the service). |
| `clipsGroup` / `clipsMode` | Share `clipsDir` with other users/services (SMB export, Jellyfin library). See below. |
| `manageClipsDir` | `false` = `clipsDir` is owned/created elsewhere (e.g. an existing SMB mount); the module won't chown/chmod it. See below. |
| `writableMediaRoots` | Media roots the app may **write into** (bound read-write, not read-only) — so it can drop subtitle sidecars next to your videos (downloaded transcripts, subtitle backfill, and **Copy to library** of a machine transcript, which says so on a read-only root). Browsed and clipped exactly like `mediaRoots`. Use it for a library you want quipclipper to enrich (e.g. a Pinchflat/yt-dlp folder). |
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

## Pairing behind the password gate

[Pairing](API.md#pairing) lets an app such as LeadStinger connect without anyone copying
`QC_API_TOKEN`: it asks, a person allows it in the web UI, and it gets a client token of
its own. With a password gate (`QC_PASSWORD` on the web service, or `passwordFile`), the
bundled nginx (the Docker image and the NixOS module alike) makes exactly these exceptions:

- `POST /api/pair` (an app asking) and `GET /api/pair/<id>` (the app polling its own
  request, which needs that request's poll secret) pass without the password. Listing,
  approving and denying requests (`GET /api/pair`, `POST /api/pair/<id>/approve|deny`) and
  everything else still need it. A request carries no authority until someone signed in
  approves it; at most 5 wait at once and 10 a minute come from one address.
- A request with a client token (`qcc_…`) in `X-API-Key` or `Authorization: Bearer` passes
  once the backend says the token is current (`satisfy any` with an `auth_request` to
  `/api/clients/check`); a revoked or unknown one gets `401`. Requests without a client
  token are answered by nginx alone, so browsers don't cost the backend anything extra.
  The gate's `api` login still knows only `QC_API_TOKEN`.

The API locations also pass the `Host` the browser sent, port included, as `X-Quip-Host`,
so links without `QC_PUBLIC_URL` name the address the browser used. A proxy in front of
nginx (TLS, a path prefix) should send `X-Forwarded-Proto`, `X-Forwarded-Host` and, for a
prefix, `X-Forwarded-Prefix`; nginx passes them on and they win. With `nginx.enable = false`
(your own front), add the same two exemptions and, if you want paired apps behind your
gate, the same token check; without them, paired apps work on an open server only.

`QC_PAIRING=0` (NixOS: `pairing.enable = false`) turns pairing off; the module then leaves
the exemptions out.

### Behind another proxy: the caller's real address

nginx tells the backend who is calling (`X-Real-IP`), which the pairing banner shows as
"From" and the pairing rate limit counts by. Behind another reverse proxy (a Tailscale
serve sidecar, Caddy, Traefik) every request seems to come from that proxy. Name it, and
nginx takes the caller from the proxy's header instead:

- Docker: `QC_TRUSTED_PROXIES` on the web service: addresses or CIDR ranges, comma- or
  space-separated (`QC_TRUSTED_PROXIES: "172.18.0.5"`); `QC_REAL_IP_HEADER` if the proxy
  uses something other than `X-Forwarded-For`.
- NixOS: `nginx.trustedProxies = [ "172.30.88.2" ];` (and `nginx.realIpHeader`).

With `X-Forwarded-For`, nginx reads the last entry, the one the nearest proxy added, so a
caller can't forge it. List only proxies you run: anything that can reach nginx from a
listed address can claim to be anyone, including to nginx's own `allow`/`deny` rules.

## Advertising on the local network

Opt-in. The server announces itself over mDNS/Bonjour as `_quipclipper._tcp` (TXT `v=1`,
`url`, `id`), so LeadStinger lists "QuipClipper on nas" without anyone typing an address,
and Send to LeadStinger links name the advertised URL.

**Tailnet-only installs: leave advertising off.** It tells every device on the LAN that
quipclipper runs here, and where; for an install reached only over a tailnet or VPN that is
information the LAN shouldn't get, and nothing on the LAN can use it anyway. Turn it on
only when the site is meant to be used from the LAN (and for an install already open on the
LAN it reveals nothing a port scan wouldn't).

- **Docker:** uncomment the `advertise` service in
  [`docker-compose.example.yml`](../docker-compose.example.yml). It runs the app image's
  `quipclipper-web advertise` with `network_mode: host`, because multicast doesn't cross
  Docker's bridge network; it shares the state volume (for the server's id and
  `advertise.json`) and needs `QC_ADVERTISE: "1"` and `QC_ADVERTISE_PORT` (the web
  service's published port, `8896` in the example). The URL it announces is `QC_PUBLIC_URL`
  if you set it on that service, else `http://<host>.local:<port>`, which needs the host to
  answer for `<host>.local` (most NAS systems run Avahi; the advertiser also answers for it).
  It announces on LAN interfaces only: not loopback, Docker bridges, VPNs or the tailnet.
- **NixOS:** `services.quipclipper-web.advertise.enable = true` turns on `services.avahi`
  with publishing (service and address records) and a static service file. The URL is
  `publicUrl`, else `http://<networking.hostName>.local:<advertise.port>`. The record leaves
  out `id` (the server's id is made at run time; LeadStinger reads it from `/api/config`).
  The module warns when every `nginx.listen` address looks unreachable from the LAN
  (loopback, a Docker or private bridge, the tailnet).
- **Anything else:** run `QC_ADVERTISE=1 QC_ADVERTISE_PORT=<port> quipclipper-web advertise`
  beside the app, with the same `QC_STATE_DIR`.

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
| One exact release | `:X.Y.Z` (e.g. `:0.3.1`) | `github:weckere/quipclipper/vX.Y.Z` |
| Fixes within a series | `:X.Y` (e.g. `:0.2`) | (pin a tag and bump it) |
| The latest release | `:latest` | `github:weckere/quipclipper` (`main`) |
| Work in progress | (build from `dev`) | `github:weckere/quipclipper/dev` |
