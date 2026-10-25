# Declarative NixOS module: services.quipclipper-web.
#
# Runs the backend as a hardened systemd service and (by default) configures the
# host nginx to serve the frontend, proxy /api, and serve /clips — optionally
# behind basic-auth. Set `nginx.enable = false` to run only the backend and bring
# your own front. The nginx vhost's port/bind/default_server are configurable
# (`nginx.{port,listen,defaultServer}`) so quipclipper can share a host. Option
# names mirror the Docker env vars (see the option table in the README).
#
# Imported via `inputs.quipclipper.nixosModules.default`, which passes `self`
# so the module can resolve the packages built by the flake.
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.quipclipper-web;
  webPkg = self.packages.${pkgs.stdenv.hostPlatform.system}.quipclipper-web;
  frontendPkg = self.packages.${pkgs.stdenv.hostPlatform.system}.quipclipper-web-frontend;
  vhostName = if cfg.virtualHost != null then cfg.virtualHost else "quipclipper-web";
  stateDir = "/var/lib/quipclipper-web/state";
  # Group owning the clips dir: an explicit shared group, else the service's own.
  clipsGroupName = if cfg.clipsGroup != null then cfg.clipsGroup else cfg.group;
  # youtubeDir needs its own writable bind + tmpfiles dir only when it isn't
  # already inside a writableMediaRoot (which is bound rw as a whole). When it
  # is nested, the app creates the subdir lazily in that already-writable tree.
  ytDirOwnBind = cfg.youtubeDir != null
    && !(lib.any (r: lib.hasPrefix (toString r + "/") (toString cfg.youtubeDir + "/"))
          cfg.writableMediaRoots);
  # Backend URL for the nginx reverse proxy. Bracket IPv6 literals (e.g. ::1)
  # so proxy_pass gets http://[::1]:8000, not the invalid http://::1:8000.
  proxyAddr = if lib.hasInfix ":" cfg.listenAddress
    then "[${cfg.listenAddress}]" else cfg.listenAddress;
  proxyTarget = "http://${proxyAddr}:${toString cfg.listenPort}";
  # A proxy in front (nginx.trustedProxies) names the caller in realIpHeader; nginx then
  # uses that as $remote_addr, which X-Real-IP passes to the backend.
  realIpConfig = lib.optionalString (cfg.nginx.trustedProxies != [ ]) (
    lib.concatMapStrings (p: "set_real_ip_from ${p};\n") cfg.nginx.trustedProxies
    + "real_ip_header ${cfg.nginx.realIpHeader};\n");
  # nginx vhost listen entries: explicit `nginx.listen`, else derived from
  # `nginx.port`. The vhost is a catch-all default_server only when asked and no
  # server_name is set (a named vhost shouldn't grab every unmatched request).
  nginxListen =
    if cfg.nginx.listen != null then cfg.nginx.listen
    else [ { addr = "0.0.0.0"; port = cfg.nginx.port; } ];
  # A listen entry may omit `port` (nginx then defaults to 80) — default it here
  # so `openFirewall` doesn't throw "attribute 'port' missing" on such entries.
  nginxFirewallPorts = map (l: l.port or 80) nginxListen;
  # A shared clipsGroup only reaches other users if new subdirs inherit it, which
  # needs the setgid bit on clipsDir. That's the leading digit of a 4-digit mode
  # having the "2" bit set (2/3/6/7) — e.g. 2775, 6755. A 3-digit mode (0750) has
  # no special-bits digit, so no setgid.
  clipsModeHasSetgid =
    let s = cfg.clipsMode; in
    lib.stringLength s >= 4
    && (let d = lib.toInt (lib.substring 0 1 s); in d == 2 || d == 3 || d == 6 || d == 7);
  # Machine transcription (M21): the whisper.cpp ggml models, each pinned to a Hugging Face
  # revision by hash, so a rebuild is reproducible and needs no download at run time.
  hf = repo: rev: file: hash: pkgs.fetchurl {
    url = "https://huggingface.co/${repo}/resolve/${rev}/${file}";
    inherit hash;
  };
  whisperModels = {
    "small.en" = hf "ggerganov/whisper.cpp" "5359861c739e955e79d9a303bcbc70fb988958b1"
      "ggml-small.en.bin" "sha256-xhONbVjsyDIgl+D5h8MvG+i7ChhTKj+I9zTRu/nEHl0=";
    "medium.en" = hf "ggerganov/whisper.cpp" "5359861c739e955e79d9a303bcbc70fb988958b1"
      "ggml-medium.en.bin" "sha256-zDfpNHgzjsdwAoGnrDChASiSnrj0J92i6GX6qPbaQ1Y=";
  };
  sileroVad = hf "ggml-org/whisper-vad" "9ffd54a1e1ee413ddf265af9913beaf518d1639b"
    "ggml-silero-v5.1.2.bin" "sha256-KZQNmNQrkfvQXOSJ8+z3xy8KQvAn5IdZGaKPtMBOos8=";
  stt = cfg.transcribe;
  # The pinned downloads, with any name pointed at a local file instead.
  sttFiles = whisperModels // stt.modelFiles;
  # Local model files outside the store must be bound into the sandbox (ProtectHome).
  sttLocalFiles = lib.filter (p: !lib.hasPrefix builtins.storeDir p)
    (map (name: toString sttFiles.${name}) stt.models);
  # Proxy headers for the API locations: X-Quip-Host is the Host the browser sent, port
  # included ($host drops it; and nixpkgs' config check refuses `Host $http_host`), so
  # links without publicUrl name the address it reached this site at; X-Real-IP is where a
  # pairing request came from. Set here, with recommendedProxySettings off on these
  # locations, so the global recommended settings don't put their own X-Forwarded-Proto
  # and -Host over those of a proxy in front.
  apiProxyHeaders = ''
    proxy_set_header Host $host;
    proxy_set_header X-Quip-Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  '';
  gated = cfg.passwordFile != null;
  # A pairing request and its poll get past the password gate; nothing else does.
  pairOpen = ''
    auth_basic off;
  '' + lib.optionalString gated ''
    auth_request off;
  '';
  # LAN advertising (Avahi): where browsers reach this server, and what to call it.
  advertisePort = cfg.advertise.port;
  advertiseUrl =
    if cfg.publicUrl != null then lib.removeSuffix "/" cfg.publicUrl
    else "http://${config.networking.hostName}.local"
      + lib.optionalString (advertisePort != 80) ":${toString advertisePort}";
  # nginx listen addresses no other machine on the LAN can reach (loopback, a Docker or
  # other private bridge, the tailnet): advertising those only tells the LAN it's there.
  unreachableFromLan = addr:
    let a = lib.removeSuffix "]" (lib.removePrefix "[" addr); in
    a == "localhost" || a == "::1" || lib.hasPrefix "127." a
    || builtins.match "172\\.(1[6-9]|2[0-9]|3[01])\\..*" a != null
    || builtins.match "100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\..*" a != null
    || lib.hasPrefix "fd7a:115c:a1e0:" a;
in
{
  options.services.quipclipper-web = {
    enable = lib.mkEnableOption "the quipclipper web app";

    mediaRoots = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      example = [ "/srv/media/movies" "/srv/media/tv" ];
      description = "Whitelist of media directories the app may read and clip from.";
    };

    writableMediaRoots = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      example = [ "/srv/media/youtube" ];
      description = ''
        Media roots the app may also **write into** — bound read-write instead of
        read-only, so quipclipper can drop subtitle sidecars (e.g. transcripts it
        downloads, or the subtitle-backfill feature) next to your videos. Browsed
        and clipped exactly like `mediaRoots`; only the mount mode differs. Use
        this for a folder you want quipclipper to enrich (a Pinchflat/yt-dlp
        library); keep everything you want protected in `mediaRoots`. "Copy to
        library" of a machine transcript (`<name>.<lang>.whisper.srt`) also works
        only here; on a read-only root it says so.
      '';
    };

    youtubeDir = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/srv/media/youtube/one-offs";
      description = ''
        Where downloaded YouTube videos are written. `null` keeps them private in
        the state dir. Set it to a real folder — ideally under a
        `writableMediaRoots` entry — so a download lands as `<Title> [<id>].mp4`
        with a `.vtt` sidecar and becomes a browsable, dialogue-searchable library
        item. The module creates it and binds it read-write.
      '';
    };

    publicUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "https://quip.example";
      description = ''
        This server's base URL as other machines reach it (`QC_PUBLIC_URL`).
        "Send to LeadStinger" puts it in the `leadstinger://quip` link, and
        LeadStinger downloads the clip from there with the API token it was
        connected with (`apiTokenFile`). `null` turns those links off.
      '';
    };

    serverName = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "Living room";
      description = ''
        The name paired apps show for this server (`QC_SERVER_NAME`, at most 63
        characters), and the advertised name with `advertise.enable`. `null` uses the
        host name.
      '';
    };

    pairing.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Pairing: an app such as LeadStinger asks for access (`POST /api/pair`), a person
        approves it in the web UI (or with `pair.approve`), and the app gets its own
        client token, revoked one at a time under Settings ▸ Connected apps. Behind
        `passwordFile` the bundled nginx lets exactly `POST /api/pair` and
        `GET /api/pair/<id>` through without the password, and lets a request with a
        current client token through as the `api` login is. `false` sets
        `QC_PAIRING=0`: every `/api/pair` route answers 404 (apps already paired keep
        their tokens).
      '';
    };

    advertise = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Announce this server on the local network with mDNS/Bonjour (Avahi), as
          `_quipclipper._tcp` with TXT `v=1` and `url` (`publicUrl`, else
          `http://<hostName>.local:<port>`), so LeadStinger lists it under "Find on
          network". It turns on `services.avahi` with publishing (service and address
          records). Off by default.

          **Tailnet-only installs must leave this off**: it tells every device on the
          LAN that quipclipper runs here, and where. Turn it on only when the site is
          meant to be used from this LAN anyway (nginx listening on a LAN address). The
          record leaves out the server's id (it is made at run time); LeadStinger reads
          it from `/api/config`.
        '';
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = if cfg.nginx.enable then (builtins.head nginxListen).port or 80 else cfg.listenPort;
        defaultText = lib.literalMD "the first `nginx.listen` port (`listenPort` without nginx)";
        description = "The port browsers use, advertised in the record and its `url`.";
      };
    };

    jellyfin = {
      url = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://127.0.0.1:8096";
        description = ''
          Jellyfin's base URL, as this host reaches it (`QC_JELLYFIN_URL`). With
          `apiKeyFile`, it turns on "Now playing": the library page lists what each
          Jellyfin client is playing, and one tap opens that file in the workspace at
          the playhead (CLI: `nowplaying`, `clip.nowplaying`); and Jellyfin's titles
          and posters on library rows and in clip names, plus a scan of the clips
          library after each clip. Media is still read from the media roots. `null` = off.
        '';
      };

      apiKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "/run/secrets/jellyfin_api_key";
        description = ''
          File holding a Jellyfin API key (Dashboard → API Keys), and nothing else.
          systemd reads it as root and hands it to the service as a credential, so
          the file can stay root-only (e.g. a sops-nix secret). Kept out of the Nix
          store.
        '';
      };

      pathMap = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        example = {
          "/movies" = "/srv/media/movies";
          "/shows" = "/srv/media/shows";
        };
        description = ''
          Jellyfin's paths → the same folders here (`QC_JELLYFIN_PATH_MAP`). Jellyfin
          reports the paths it sees, which inside a container are its mount points,
          not the host's. The longest matching prefix wins; a mapped path must still
          be inside `mediaRoots`/`writableMediaRoots` to open, except the clips
          library's folder (map it to `clipsDir` so new clips are scanned). Leave
          empty when Jellyfin runs on this host and sees the same paths. The module
          writes it as JSON, so a path may hold `:`, `;` or `=`.
        '';
      };
    };

    hookTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/quipclipper-hook-token";
      description = ''
        File holding a hook token, and nothing else (`QC_HOOK_TOKEN_FILE`): a
        low-privilege token Bazarr, Sonarr and Radarr use to tell quipclipper that a
        file's subtitles changed (`POST /api/index/file`). It may only queue
        re-indexes; every other endpoint refuses it. systemd reads the file as root
        and hands the service a copy as a credential, so it can stay root-only (e.g. a
        sops-nix secret). With `passwordFile`, add a `hook` line with the token as its
        password. See docs/HOOKS.md.
      '';
    };

    indexCheckInterval = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 3600;
      example = 0;
      description = ''
        Seconds between the background checks for files whose subtitles changed since
        they were indexed (`QC_INDEX_CHECK_INTERVAL`); `0` turns the timed check off
        (opening a folder or searching still checks what it shows). A check lists
        folders and reads file dates only, at idle priority, and skips its turn while
        the last one still runs. It never re-indexes: the library page shows "Index out
        of date: N files" with a button for that.
      '';
    };

    pathMap = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        "/tv" = "/srv/media/shows";
        "/movies" = "/srv/media/movies";
      };
      description = ''
        The re-index hooks' paths → the same folders here (`QC_PATH_MAP`). Bazarr,
        Sonarr and Radarr report the paths they see, which inside their containers are
        their mount points. The longest matching prefix wins; a path nothing matches is
        used as it is, and must be inside `mediaRoots`/`writableMediaRoots`. The
        module writes it as JSON, so a path may hold `:`, `;` or `=`.
      '';
    };

    clipsDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/quipclipper-web/clips";
      description = "Directory where finished clips are written and served from.";
    };

    clipsGroup = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "users";
      description = ''
        Group that owns the clips directory. Both the service user and nginx are
        added to it, so they can write and serve clips. Set this — together with a
        group-readable `clipsMode` like `"2775"` — to share `clipsDir` with other
        users and services (an SMB export, a Jellyfin library, …) rather than
        keeping clips private to quipclipper. The group must already exist (e.g.
        `users`, or one defined by your Samba config). `null` uses the service's
        own group, keeping clips private.
      '';
    };

    clipsMode = lib.mkOption {
      type = lib.types.str;
      default = "0750";
      example = "2775";
      description = ''
        Permission mode for `clipsDir` when the module manages it
        (`manageClipsDir`). The default `0750` keeps clips private to the service
        and nginx. Use a setgid, group-writable mode like `"2775"` so finished
        clips inherit `clipsGroup` and are group-readable — letting `clipsDir`
        double as a shared folder.
      '';
    };

    manageClipsDir = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether the module creates `clipsDir` and sets its owner/group/mode via
        systemd-tmpfiles. Leave `true` for a module-owned directory. Set `false`
        when `clipsDir` is provisioned and owned elsewhere — e.g. a pre-existing
        SMB share or mergerfs pool with its own permissions (say `root:users`
        setgid `2775`) — so the module won't chown/chmod it. The module still adds
        the service user and nginx to `clipsGroup` and grants the unit write
        access; you must ensure the directory already exists and is group-writable
        (setgid) for the service to write into it.
      '';
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address the backend binds to (nginx proxies to it).";
    };

    listenPort = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "Port the backend listens on.";
    };

    maxConcurrentJobs = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = "Maximum number of concurrent ffmpeg/mkvmerge clip jobs.";
    };

    subtitleLangs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "en" ];
      example = [ "eng" "spa" ];
      description = "Ordered subtitle-language preference for auto-selection.";
    };

    virtualHost = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "clips.example.com";
      description = ''
        nginx server name. When null, a default catch-all vhost is used
        (suitable for LAN-only access by IP). Only applies when `nginx.enable`.
      '';
    };

    nginx = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Front the backend with a host nginx vhost (serves the frontend, proxies
          `/api`, serves `/clips`, applies the basic-auth gate). When `false`, only
          the hardened backend runs on `listenAddress:listenPort` and you bring your
          own front (nginx/caddy/Tailscale serve) proxying to it — the backend then
          serves clips itself, so no `/clips` static mapping is needed. `openFirewall`
          and the bundled vhost are skipped entirely.
        '';
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 80;
        description = "Port the bundled nginx vhost listens on (when `nginx.listen` is null).";
      };

      listen = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf (lib.types.attrsOf lib.types.anything));
        default = null;
        example = [ { addr = "172.17.0.1"; port = 8080; } ];
        description = ''
          Explicit nginx `listen` entries, passed straight to
          `services.nginx.virtualHosts.<name>.listen`. Overrides `nginx.port` —
          use it to bind a specific address (e.g. a Tailscale/Docker bridge) and/or
          port. `openFirewall` opens whatever ports these entries declare.
        '';
      };

      trustedProxies = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "172.30.88.2" "10.0.0.0/8" ];
        description = ''
          Addresses (or CIDR ranges) of reverse proxies in front of the bundled nginx, such
          as a Tailscale serve sidecar, Caddy or Traefik. For requests from them, nginx takes
          the client's address from `nginx.realIpHeader` (`set_real_ip_from`), so the
          pairing banner's "From", its per-address rate limit and nginx's own `allow`/`deny`
          see the real caller instead of the proxy. List only proxies you control: a client
          that can reach nginx from a listed address can claim any address.
        '';
      };

      realIpHeader = lib.mkOption {
        type = lib.types.str;
        default = "X-Forwarded-For";
        example = "X-Real-IP";
        description = ''
          The header a `nginx.trustedProxies` proxy names the client in. With
          `X-Forwarded-For` nginx reads its last entry, the one the nearest proxy added.
        '';
      };

      defaultServer = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Make the bundled vhost nginx's `default_server` (catch-all). Set `false`
          so quipclipper can coexist with another web service on the same host/port
          without grabbing every unmatched request. (Forced off when a
          `virtualHost` server name is set.)
        '';
      };
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open the bundled nginx vhost's port(s) in the firewall. No-op when
        `nginx.enable = false` (bring your own front and open its port yourself).
      '';
    };

    passwordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Path to an nginx htpasswd file. When set, the site is gated behind
        HTTP basic auth. Kept out of the Nix store.
      '';
    };

    apiTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/quipclipper-api-token";
      description = ''
        Path to a systemd EnvironmentFile that sets an API token for programmatic
        clients (agents/scripts), e.g. a file containing
        `QC_API_TOKEN=<long-random-token>` (comma-separate several for rotation).
        A client presents the token as an `X-API-Key` / `Authorization: Bearer`
        header (or the HTTP Basic password) and is then exempt from the browser
        CSRF-header requirement. Kept out of the Nix store.

        To pass the token through the bundled nginx basic-auth gate with a single
        credential (`curl -u api:<token>`), add an `api` line to `passwordFile`
        (`htpasswd -B <file> api`); otherwise reach the backend with a normal
        basic-auth user plus the `X-API-Key` header. See docs/API.md.
      '';
    };

    hardwareAcceleration = {
      enable = lib.mkEnableOption ''
        Intel Quick Sync (VAAPI) hardware H.264 encoding for the browser-preview
        video re-encode. Adds the service user to the render group, exposes the
        render node, and enables the Intel media driver. Without it the re-encode
        falls back to software (libx264)'';

      device = lib.mkOption {
        type = lib.types.str;
        default = "/dev/dri/renderD128";
        description = "VAAPI render node to encode on (a second GPU may be renderD129).";
      };

      driverName = lib.mkOption {
        type = lib.types.str;
        default = "iHD";
        example = "i965";
        description = ''
          libva driver name. iHD covers Intel Gen8+ (Broadwell and newer); older
          iGPUs need i965 (and `intel-vaapi-driver` in hardware.graphics).
        '';
      };
    };

    transcribe = {
      enable = lib.mkEnableOption ''
        machine transcription with whisper.cpp (M21): on request, the web app and CLI
        transcribe a waveform selection, the gaps between a file's subtitle lines, a
        whole file or a folder, so lines the subtitles leave out become searchable.
        Nothing ever runs on its own: there is no timer and no hook, only the buttons
        and commands that ask. Every run is idle priority (SCHED_IDLE, nice 19, idle
        I/O) with `threads` threads, one at a time. Enabling it installs whisper.cpp and
        the models and sets `QC_TRANSCRIBE=1`; left off, the unit has no whisper, so the
        app leaves transcription out'';

      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.whisper-cpp;
        defaultText = lib.literalExpression "pkgs.whisper-cpp";
        description = ''
          whisper.cpp, for its `whisper-cli`. The CPU build picks the best backend for the
          host (AVX2, …); `pkgs.whisper-cpp.override { cudaSupport = true; }` uses a GPU.
        '';
      };

      models = lib.mkOption {
        type = lib.types.listOf (lib.types.enum (lib.attrNames whisperModels));
        default = [ "small.en" ];
        example = [ "small.en" "medium.en" ];
        description = ''
          The English ggml models to install, each fetched from Hugging Face pinned by
          hash: `small.en` (466 MB; quick, a few word errors) and `medium.en` (1.5 GB;
          close to subtitle quality on lyrics, about three times slower on a CPU). A run
          picks one of these; the web UI offers the choice when there are two.
        '';
      };

      modelFiles = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        default = { };
        example = lib.literalExpression ''{ "medium.en" = "/var/lib/whisper/ggml-medium.en.bin"; }'';
        description = ''
          Local ggml files for some models, by name; a name not set here uses its pinned
          Hugging Face download (fetchurl by hash). A file outside the Nix store is
          bound read-only into the service's sandbox, so one under /home works too.
        '';
      };

      defaultModel = lib.mkOption {
        type = lib.types.enum (lib.attrNames whisperModels);
        default = "small.en";
        description = "The model a run uses unless it asks for another (`QC_WHISPER_MODEL`).";
      };

      vad = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Install the Silero voice-activity model (1 MB). It is used only for whole-file
          runs, where it skips silences; ranges and gaps run without it, because it drops
          sung lines (the main thing they are transcribed for).
        '';
      };

      threads = lib.mkOption {
        type = lib.types.ints.between 1 3;
        default = 2;
        description = ''
          whisper threads per run (`QC_WHISPER_THREADS`), at most 3: the host is shared,
          and a whisper run on every core starved on-demand HLS encoding once (the app
          caps it at 3 as well).
        '';
      };
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "quipclipper-web";
      description = "User the service runs as.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "quipclipper-web";
      description = "Group the service runs as.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        # Each goes into nginx's config as written: only addresses and ranges.
        assertion = builtins.all (p: builtins.match "[0-9A-Fa-f.:]+(/[0-9]+)?" p != null) cfg.nginx.trustedProxies;
        message = "services.quipclipper-web.nginx.trustedProxies takes IP addresses or CIDR ranges (e.g. 172.30.88.2, 10.0.0.0/8), not host names.";
      }
      {
        assertion = builtins.match "[A-Za-z0-9-]+" cfg.nginx.realIpHeader != null;
        message = "services.quipclipper-web.nginx.realIpHeader must be a header name such as X-Forwarded-For.";
      }
      {
        assertion = cfg.mediaRoots != [ ] || cfg.writableMediaRoots != [ ];
        message = ''
          services.quipclipper-web needs at least one media directory in
          mediaRoots or writableMediaRoots — otherwise the app starts with an
          empty, unusable library.
        '';
      }
      {
        # A misspelled clipsGroup evaluates fine but the service/nginx then join a
        # group that doesn't exist, and clip downloads 403 at runtime. Catch the
        # typo at build time. (Skipped when clipsGroup is null / the service's own
        # group, which the module always defines.)
        assertion = cfg.clipsGroup == null
          || cfg.clipsGroup == cfg.group
          || config.users.groups ? ${cfg.clipsGroup};
        message = ''
          services.quipclipper-web.clipsGroup = "${toString cfg.clipsGroup}" is not
          a group defined in config.users.groups. Define it (e.g. in
          users.groups.${toString cfg.clipsGroup}), or fix the spelling — otherwise
          the service and nginx join a non-existent group and clip downloads 403.
        '';
      }
      {
        # The hard rule for a shared host (M21, D-037): whisper never gets more than 3
        # threads, so it can't take every core from playback even when it runs.
        assertion = !stt.enable || stt.threads <= 3;
        message = ''
          services.quipclipper-web.transcribe.threads = ${toString stt.threads}: at most 3.
          whisper shares the host with playback; a run on every core starved HLS encoding.
        '';
      }
      {
        assertion = !stt.enable || lib.all (name: sttFiles ? ${name}) stt.models;
        message = ''
          services.quipclipper-web.transcribe.modelFiles has no file for
          ${lib.concatStringsSep ", " (lib.filter (n: !(sttFiles ? ${n})) stt.models)}.
        '';
      }
      {
        assertion = !stt.enable || builtins.elem stt.defaultModel stt.models;
        message = ''
          services.quipclipper-web.transcribe.defaultModel = "${stt.defaultModel}" is not in
          transcribe.models (${lib.concatStringsSep ", " stt.models}).
        '';
      }
    ];

    # The basic-auth gate is enforced by the bundled nginx. With nginx.enable =
    # false, passwordFile enforces nothing (the backend has no auth of its own) —
    # yet the UI still reports auth as required. Warn so a "bring your own front"
    # setup doesn't ship believing it's password-protected when it isn't.
    warnings = lib.optional ((cfg.jellyfin.url == null) != (cfg.jellyfin.apiKeyFile == null)) ''
      services.quipclipper-web.jellyfin needs both url and apiKeyFile; with only one
      of them, Now playing stays off.
    '' ++ lib.optional (cfg.passwordFile != null && !cfg.nginx.enable) ''
      services.quipclipper-web.passwordFile is set but nginx.enable = false, so
      no HTTP basic-auth gate is applied. Enforce authentication in your own
      reverse proxy, or the service is reachable without a password.
    '' ++ lib.optional (cfg.clipsGroup != null && cfg.manageClipsDir && !clipsModeHasSetgid) ''
      services.quipclipper-web.clipsGroup = "${cfg.clipsGroup}" shares clipsDir,
      but clipsMode = "${cfg.clipsMode}" lacks the setgid bit. New per-source
      subdirs will be owned by the service's own group, so ${cfg.clipsGroup}
      members can't read clips created after startup. Use a setgid mode like
      "2775" so subdirs inherit clipsGroup.
    '' ++ lib.optional
      (cfg.advertise.enable && cfg.nginx.enable
        && lib.all (l: unreachableFromLan (l.addr or "0.0.0.0")) nginxListen) ''
      services.quipclipper-web.advertise.enable is on, but every nginx.listen address
      (${lib.concatMapStringsSep ", " (l: l.addr or "0.0.0.0") nginxListen}) looks
      unreachable from the LAN (loopback, a Docker or private bridge, or the tailnet). The
      LAN learns that quipclipper runs here and can't use it: tailnet-only installs should
      leave advertising off.
    '' ++ lib.optional
      (cfg.nginx.enable && !(builtins.elem cfg.listenAddress [ "127.0.0.1" "::1" "localhost" ])) ''
      services.quipclipper-web.listenAddress = "${cfg.listenAddress}" is not a
      loopback address, so the backend port (${toString cfg.listenPort}) is
      reachable directly on the network — bypassing nginx and its basic-auth
      gate (the backend has no auth of its own). Bind it to 127.0.0.1 (the
      default) and let nginx be the sole entry point, unless another proxy
      enforces access on that address.
    '';

    users.users = {
      ${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
        extraGroups =
          # The render group lets the service open the VAAPI node for HW encoding.
          (lib.optional cfg.hardwareAcceleration.enable "render")
          # When clips are shared via a separate group, join it so the service can
          # write into a group-writable (setgid) shared/SMB directory it doesn't own.
          ++ lib.optional (clipsGroupName != cfg.group) clipsGroupName;
      };
    } // lib.optionalAttrs cfg.nginx.enable {
      # nginx serves the clips dir directly (QC_CLIPS_URL_PREFIX = /clips), so it
      # must be in the group that owns the clips to traverse + read them — else
      # downloads 403. Only relevant when we run nginx (otherwise the backend
      # serves clips, and there's no nginx user to add to the group).
      ${config.services.nginx.user}.extraGroups = [ clipsGroupName ];
    };
    users.groups.${cfg.group} = { };

    # Intel media driver + GPU access for hardware H.264 encoding (opt-in).
    hardware.graphics = lib.mkIf cfg.hardwareAcceleration.enable {
      enable = lib.mkDefault true;
      extraPackages = lib.mkIf (cfg.hardwareAcceleration.driverName == "iHD")
        [ pkgs.intel-media-driver ];
    };

    systemd.services.quipclipper-web = {
      description = "quipclipper web backend";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      # Wait for the filesystems holding clipsDir and the media roots — important
      # when any is an external mount (SMB/mergerfs) that may not be ready at boot.
      # A media root racing its mount would otherwise start with an empty library.
      unitConfig.RequiresMountsFor =
        [ (toString cfg.clipsDir) ]
        ++ map toString cfg.mediaRoots
        ++ map toString cfg.writableMediaRoots
        ++ lib.optional ytDirOwnBind (toString cfg.youtubeDir);

      environment = {
        # The app treats read-only and writable roots the same for browsing;
        # the read/write split is purely a mount-mode concern below.
        QC_MEDIA_ROOTS = lib.concatStringsSep ":"
          (map toString (cfg.mediaRoots ++ cfg.writableMediaRoots));
        QC_CLIPS_DIR = toString cfg.clipsDir;
        QC_STATE_DIR = stateDir;
        QC_BIND = cfg.listenAddress;
        QC_PORT = toString cfg.listenPort;
        QC_MAX_CONCURRENT_JOBS = toString cfg.maxConcurrentJobs;
        QC_SUBTITLE_LANGS = lib.concatStringsSep "," cfg.subtitleLangs;
        QC_INDEX_CHECK_INTERVAL = toString cfg.indexCheckInterval;
      } // lib.optionalAttrs (cfg.serverName != null) {
        QC_SERVER_NAME = cfg.serverName;
      } // lib.optionalAttrs (!cfg.pairing.enable) {
        QC_PAIRING = "0";
      } // lib.optionalAttrs (cfg.youtubeDir != null) {
        QC_YOUTUBE_DIR = toString cfg.youtubeDir;
      } // lib.optionalAttrs (cfg.publicUrl != null) {
        QC_PUBLIC_URL = cfg.publicUrl;
      } // lib.optionalAttrs (cfg.jellyfin.url != null) {
        QC_JELLYFIN_URL = cfg.jellyfin.url;
        # JSON, so a path holding ':', ';' or '=' survives (the plain form splits on them).
        QC_JELLYFIN_PATH_MAP = builtins.toJSON cfg.jellyfin.pathMap;
      } // lib.optionalAttrs (cfg.pathMap != { }) {
        QC_PATH_MAP = builtins.toJSON cfg.pathMap;
      } // lib.optionalAttrs (cfg.hookTokenFile != null) {
        QC_HOOK_TOKEN_FILE = "/run/credentials/quipclipper-web.service/hook-token";
      } // lib.optionalAttrs (cfg.jellyfin.apiKeyFile != null) {
        # The credential below, where systemd puts it for this unit.
        QC_JELLYFIN_API_KEY_FILE = "/run/credentials/quipclipper-web.service/jellyfin-api-key";
      } // lib.optionalAttrs cfg.nginx.enable {
        # The bundled nginx serves the clips dir directly at /clips/ (see the vhost
        # below). Without nginx, the backend serves clips itself under /api.
        QC_CLIPS_URL_PREFIX = "/clips";
      } // lib.optionalAttrs cfg.hardwareAcceleration.enable {
        QC_VAAPI_DEVICE = cfg.hardwareAcceleration.device;
        LIBVA_DRIVER_NAME = cfg.hardwareAcceleration.driverName;
      } // lib.optionalAttrs stt.enable {
        # Machine transcription (M21): forced on (D-053), and whisper-cli by store path, so
        # PATH needn't carry it.
        QC_TRANSCRIBE = "1";
        QC_WHISPER_BIN = "${stt.package}/bin/whisper-cli";
        QC_WHISPER_MODELS = lib.concatMapStringsSep ","
          (name: "${name}=${sttFiles.${name}}") stt.models;
        QC_WHISPER_MODEL = stt.defaultModel;
        QC_WHISPER_THREADS = toString stt.threads;
        QC_WHISPER_LANGUAGE = "en";
      } // lib.optionalAttrs (stt.enable && stt.vad) {
        QC_WHISPER_VAD_MODEL = toString sileroVad;
      } // lib.optionalAttrs (cfg.passwordFile != null) {
        # The gate is enforced by nginx via `basicAuthFile = passwordFile` on the
        # vhost below; this env var only makes the backend report auth_required so
        # the UI can reflect it. (Docker builds its htpasswd from a plaintext
        # QC_PASSWORD; the Nix path takes a ready-made htpasswd `passwordFile`.)
        QC_PASSWORD = "set";
      };

      # The advertised URL is the links' fallback without publicUrl (state_dir/advertise.json,
      # as the Docker advertiser writes it); removed again when advertising is off.
      preStart = if cfg.advertise.enable then ''
        printf '%s\n' ${lib.escapeShellArg (builtins.toJSON { url = advertiseUrl; })} \
          > ${stateDir}/advertise.json
      '' else ''
        rm -f ${stateDir}/advertise.json
      '';

      serviceConfig = {
        ExecStart = lib.getExe webPkg;
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        # API token(s) for programmatic clients, loaded from a file so the secret
        # stays out of the Nix store (see apiTokenFile).
        EnvironmentFile = lib.mkIf (cfg.apiTokenFile != null) cfg.apiTokenFile;
        # The Jellyfin API key: systemd reads the (root-only) file and exposes a copy
        # only this unit can read (see jellyfin.apiKeyFile).
        LoadCredential =
          lib.optional (cfg.jellyfin.apiKeyFile != null)
            "jellyfin-api-key:${toString cfg.jellyfin.apiKeyFile}"
          # The hook token (see hookTokenFile), the same way.
          ++ lib.optional (cfg.hookTokenFile != null)
            "hook-token:${toString cfg.hookTokenFile}";

        # Hardening. Media roots are exposed read-only; only the clips + state
        # dirs are writable. These are created up front by the tmpfiles rules
        # below — the namespace bind mounts require them to already exist.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        # Use Bind*Paths, not Read{Only,Write}Paths: ProtectHome=true hides all of
        # /home *after* the latter re-mount, so a mediaRoot/clipsDir under /home
        # would be invisible. BindReadOnlyPaths/BindPaths bind the source in
        # explicitly, so /home roots keep working under ProtectHome.
        BindReadOnlyPaths = map toString cfg.mediaRoots
          # "-": a missing model file fails its transcriptions, not the whole app.
          ++ lib.optionals stt.enable (map (p: "-" + p) sttLocalFiles);
        # Writable: clips, state, the writable media roots, and (if set and not
        # already covered by one of those) the YouTube download dir.
        BindPaths = [ (toString cfg.clipsDir) stateDir ]
          ++ map toString cfg.writableMediaRoots
          ++ lib.optional ytDirOwnBind (toString cfg.youtubeDir);

        # When the clips dir is shared (clipsGroup set), write group-writable so
        # the shared group can delete/manage clips, not just read them. With the
        # setgid clips dir (clipsMode 2775) this yields per-source subdirs 2775
        # and clip files 0664, group-owned by clipsGroup. Default (private) keeps
        # systemd's 0022 → 0755/0644 (group-readable for nginx, not writable).
        UMask = lib.mkIf (cfg.clipsGroup != null) "0002";
      };
    };

    # Create the writable dirs before the service starts (its hardened mount
    # namespace bind-mounts them, so they must exist). The state dir is always
    # module-owned; the clips dir is owned at clipsMode/clipsGroup unless the
    # admin manages it externally (manageClipsDir = false, e.g. an SMB share).
    systemd.tmpfiles.rules = [
      "d ${stateDir} 0750 ${cfg.user} ${cfg.group} - -"
      # Recursively (re)own the state tree on every activation ("Z", mode left
      # alone). Self-heals UID drift: cache/bookmark files written under an old
      # UID (user recreated, host migration) would otherwise be unreadable and
      # break subtitle loading until someone chowns them by hand.
      "Z ${stateDir} - ${cfg.user} ${cfg.group} - -"
    ] ++ lib.optional cfg.manageClipsDir
      "d ${toString cfg.clipsDir} ${cfg.clipsMode} ${cfg.user} ${clipsGroupName} - -"
      # Create a standalone youtubeDir (world-readable, so Jellyfin/Samba can
      # read the downloads) — it must exist before the service's bind mount. A
      # youtubeDir nested in a writableMediaRoot is created lazily by the app.
      ++ lib.optional ytDirOwnBind
        "d ${toString cfg.youtubeDir} 0755 ${cfg.user} ${cfg.group} - -";

    services.nginx = lib.mkIf cfg.nginx.enable {
      enable = true;
      virtualHosts.${vhostName} = lib.mkMerge [ {
        # Catch-all only when asked and unnamed — a named vhost shouldn't grab
        # every unmatched request, and two services can't both be default_server.
        default = cfg.nginx.defaultServer && cfg.virtualHost == null;
        listen = nginxListen;
        root = "${frontendPkg}";
        locations."/" = {
          tryFiles = "$uri $uri/ /index.html";
        };
        # Minimal hardening, mirroring web/nginx/nginx.conf. `always` so error
        # responses carry them too. nginx add_header does NOT inherit into a
        # location that sets its own add_header, so the html|js|css block below
        # repeats them.
        extraConfig = ''
          add_header X-Content-Type-Options "nosniff" always;
          add_header X-Frame-Options "SAMEORIGIN" always;
        '' + realIpConfig;
        # App shell + assets have no content hash, so always revalidate (nginx
        # still 304s when unchanged) — a deploy is picked up without a hard
        # refresh instead of being served stale from the browser cache.
        locations."~* \\.(?:html|js|css)$" = {
          tryFiles = "$uri /index.html";
          extraConfig = ''
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "SAMEORIGIN" always;
            add_header Cache-Control "no-cache";
          '';
        };
        locations."/api/" = {
          proxyPass = proxyTarget;
          recommendedProxySettings = false;
          extraConfig = apiProxyHeaders;
        };
        # Raw media streaming: don't spool large files through nginx's proxy
        # temp dir. Longest-prefix matching keeps /api/media/transcode (below)
        # taking precedence over this.
        locations."/api/media" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_buffering off;
          '';
        };
        # Subtitle pre-indexing streams one progress line per file and can take
        # several minutes — disable buffering (so the UI sees progress) and raise
        # the read timeout well past nginx's 60s default.
        locations."/api/search/folder/index" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_buffering off;
            proxy_read_timeout 600s;
          '';
        };
        # Folder dialogue search extracts subtitles from many files and can be slow.
        locations."/api/search/folder" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_read_timeout 300s;
          '';
        };
        # Per-item reindex re-extracts subs, or re-fetches a YouTube transcript
        # via yt-dlp — either can exceed the 60s default. (Adding a YouTube URL
        # is a background job, so /api/youtube itself returns instantly.)
        locations."/api/items/subtitles/reindex" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_read_timeout 300s;
          '';
        };
        # HLS playlist — the first request blocks while ffmpeg produces the
        # opening segment, which a software re-encode of a long source can drag
        # out (see _HLS_START_TIMEOUT). Longest-prefix wins over /api/media
        # above, whose 60s default would otherwise cut in first and turn the
        # app's clean 504 into a gateway timeout.
        locations."/api/media/hls" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_buffering off;
            proxy_read_timeout 180s;
          '';
        };
        # Clips library downloads: `?as=flac` converts the clip before the first
        # byte (FlacCache, up to 900 s for a long multichannel clip), and a clip
        # can be large, so stream it unbuffered and wait as long as that.
        locations."/api/clips/download" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_buffering off;
            proxy_read_timeout 900s;
          '';
        };
        # Transcode stream — a long-running live ffmpeg StreamingResponse feeding
        # the <video> element. Disable buffering so output reaches the browser
        # immediately, and allow a long connection.
        locations."/api/media/transcode" = {
          proxyPass = proxyTarget;
          extraConfig = ''
            proxy_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
          '';
        };
        # OpenAPI schema + interactive docs (FastAPI serves them at the root, not
        # under /api/). Exact-match so nothing else is exposed; they inherit the
        # vhost's basic-auth gate like everything else. Mirrors web/nginx/nginx.conf.
        locations."= /openapi.json" = {
          proxyPass = proxyTarget;
        };
        locations."= /docs" = {
          proxyPass = proxyTarget;
        };
        locations."= /redoc" = {
          proxyPass = proxyTarget;
        };
        locations."/clips/" = {
          alias = "${cfg.clipsDir}/";
        };
        basicAuthFile = lib.mkIf (cfg.passwordFile != null) cfg.passwordFile;
      } (lib.mkIf cfg.pairing.enable {
        # Pairing (docs/API.md): an app asking for access (POST /api/pair) and polling its
        # own request (GET /api/pair/<id>, which needs that request's poll secret) get past
        # the password gate. Listing, approving and denying stay behind it.
        locations."= /api/pair" = {
          proxyPass = proxyTarget;
          recommendedProxySettings = false;
          extraConfig = apiProxyHeaders + ''
            error_page 418 = @quipclipper_pair_open;
            if ($request_method = POST) { return 418; }
          '';
        };
        locations."@quipclipper_pair_open" = {
          proxyPass = proxyTarget;
          recommendedProxySettings = false;
          extraConfig = apiProxyHeaders + pairOpen;
        };
        locations."~ ^/api/pair/[A-Za-z0-9_-]+$" = {
          proxyPass = proxyTarget;
          recommendedProxySettings = false;
          extraConfig = apiProxyHeaders + pairOpen + ''
            limit_except GET { deny all; }
          '';
        };
      }) (lib.mkIf gated {
        # A paired app's client token (qcc_..., as X-API-Key or Bearer) passes the gate like
        # the api login: `satisfy any` takes the password or a token the backend says is
        # current. A request with no client token is answered here, without the backend.
        extraConfig = ''
          satisfy any;
          auth_request /_quipclipper_client;
        '';
        locations."= /_quipclipper_client" = {
          proxyPass = "${proxyTarget}/api/clients/check";
          recommendedProxySettings = false;
          extraConfig = ''
            internal;
            set $quipclipper_client_token "";
            if ($http_x_api_key ~ "^qcc_") { set $quipclipper_client_token 1; }
            if ($http_authorization ~* "^bearer +qcc_") { set $quipclipper_client_token 1; }
            if ($quipclipper_client_token = "") { return 401; }
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header Host $host;
          '';
        };
      }) ];
    };

    # LAN advertising (opt-in): a static Avahi service file, published with the host's
    # address records so the advertised <hostName>.local resolves.
    services.avahi = lib.mkIf cfg.advertise.enable {
      enable = true;
      publish.enable = true;
      publish.addresses = lib.mkDefault true;
      extraServiceFiles.quipclipper = ''
        <?xml version="1.0" standalone="no"?>
        <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
        <service-group>
          ${if cfg.serverName != null
            then "<name>${lib.escapeXML cfg.serverName}</name>"
            else ''<name replace-wildcards="yes">%h</name>''}
          <service>
            <type>_quipclipper._tcp</type>
            <port>${toString advertisePort}</port>
            <txt-record>v=1</txt-record>
            <txt-record>url=${lib.escapeXML advertiseUrl}</txt-record>
          </service>
        </service-group>
      '';
    };

    networking.firewall = lib.mkIf (cfg.openFirewall && cfg.nginx.enable) {
      allowedTCPPorts = nginxFirewallPorts;
    };
  };
}
