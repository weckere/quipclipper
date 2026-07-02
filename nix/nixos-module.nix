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
  # Backend URL for the nginx reverse proxy. Bracket IPv6 literals (e.g. ::1)
  # so proxy_pass gets http://[::1]:8000, not the invalid http://::1:8000.
  proxyAddr = if lib.hasInfix ":" cfg.listenAddress
    then "[${cfg.listenAddress}]" else cfg.listenAddress;
  proxyTarget = "http://${proxyAddr}:${toString cfg.listenPort}";
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
        assertion = cfg.mediaRoots != [ ];
        message = ''
          services.quipclipper-web.mediaRoots must list at least one media
          directory — otherwise the app starts with an empty, unusable library.
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
    ];

    # The basic-auth gate is enforced by the bundled nginx. With nginx.enable =
    # false, passwordFile enforces nothing (the backend has no auth of its own) —
    # yet the UI still reports auth as required. Warn so a "bring your own front"
    # setup doesn't ship believing it's password-protected when it isn't.
    warnings = lib.optional (cfg.passwordFile != null && !cfg.nginx.enable) ''
      services.quipclipper-web.passwordFile is set but nginx.enable = false, so
      no HTTP basic-auth gate is applied. Enforce authentication in your own
      reverse proxy, or the service is reachable without a password.
    '' ++ lib.optional (cfg.clipsGroup != null && cfg.manageClipsDir && !clipsModeHasSetgid) ''
      services.quipclipper-web.clipsGroup = "${cfg.clipsGroup}" shares clipsDir,
      but clipsMode = "${cfg.clipsMode}" lacks the setgid bit. New per-source
      subdirs will be owned by the service's own group, so ${cfg.clipsGroup}
      members can't read clips created after startup. Use a setgid mode like
      "2775" so subdirs inherit clipsGroup.
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
        [ (toString cfg.clipsDir) ] ++ map toString cfg.mediaRoots;

      environment = {
        QC_MEDIA_ROOTS = lib.concatStringsSep ":" (map toString cfg.mediaRoots);
        QC_CLIPS_DIR = toString cfg.clipsDir;
        QC_STATE_DIR = stateDir;
        QC_BIND = cfg.listenAddress;
        QC_PORT = toString cfg.listenPort;
        QC_MAX_CONCURRENT_JOBS = toString cfg.maxConcurrentJobs;
        QC_SUBTITLE_LANGS = lib.concatStringsSep "," cfg.subtitleLangs;
      } // lib.optionalAttrs cfg.nginx.enable {
        # The bundled nginx serves the clips dir directly at /clips/ (see the vhost
        # below). Without nginx, the backend serves clips itself under /api.
        QC_CLIPS_URL_PREFIX = "/clips";
      } // lib.optionalAttrs cfg.hardwareAcceleration.enable {
        QC_VAAPI_DEVICE = cfg.hardwareAcceleration.device;
        LIBVA_DRIVER_NAME = cfg.hardwareAcceleration.driverName;
      } // lib.optionalAttrs (cfg.passwordFile != null) {
        # The gate is enforced by nginx via `basicAuthFile = passwordFile` on the
        # vhost below; this env var only makes the backend report auth_required so
        # the UI can reflect it. (Docker builds its htpasswd from a plaintext
        # QC_PASSWORD; the Nix path takes a ready-made htpasswd `passwordFile`.)
        QC_PASSWORD = "set";
      };

      serviceConfig = {
        ExecStart = lib.getExe webPkg;
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";

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
        BindReadOnlyPaths = map toString cfg.mediaRoots;
        BindPaths = [ (toString cfg.clipsDir) stateDir ];

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
    ] ++ lib.optional cfg.manageClipsDir
      "d ${toString cfg.clipsDir} ${cfg.clipsMode} ${cfg.user} ${clipsGroupName} - -";

    services.nginx = lib.mkIf cfg.nginx.enable {
      enable = true;
      virtualHosts.${vhostName} = {
        # Catch-all only when asked and unnamed — a named vhost shouldn't grab
        # every unmatched request, and two services can't both be default_server.
        default = cfg.nginx.defaultServer && cfg.virtualHost == null;
        listen = nginxListen;
        root = "${frontendPkg}";
        locations."/" = {
          tryFiles = "$uri $uri/ /index.html";
        };
        # App shell + assets have no content hash, so always revalidate (nginx
        # still 304s when unchanged) — a deploy is picked up without a hard
        # refresh instead of being served stale from the browser cache.
        locations."~* \\.(?:html|js|css)$" = {
          tryFiles = "$uri /index.html";
          extraConfig = ''
            add_header Cache-Control "no-cache";
          '';
        };
        locations."/api/" = {
          proxyPass = proxyTarget;
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
        locations."/clips/" = {
          alias = "${cfg.clipsDir}/";
        };
        basicAuthFile = lib.mkIf (cfg.passwordFile != null) cfg.passwordFile;
      };
    };

    networking.firewall = lib.mkIf (cfg.openFirewall && cfg.nginx.enable) {
      allowedTCPPorts = nginxFirewallPorts;
    };
  };
}
