# Declarative NixOS module: services.quipclipper-web.
#
# Runs the backend as a hardened systemd service and configures the host nginx
# to serve the frontend, proxy /api, and serve /clips — optionally behind
# basic-auth. Option names mirror the Docker env vars 1:1 (see
# docs/WEBAPP_PLAN.md §6) so both deployments behave identically.
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
        (suitable for LAN-only access by IP).
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the nginx HTTP port (80) in the firewall.";
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
    ];

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      extraGroups =
        # The render group lets the service open the VAAPI node for HW encoding.
        (lib.optional cfg.hardwareAcceleration.enable "render")
        # When clips are shared via a separate group, join it so the service can
        # write into a group-writable (setgid) shared/SMB directory it doesn't own.
        ++ lib.optional (clipsGroupName != cfg.group) clipsGroupName;
    };
    users.groups.${cfg.group} = { };

    # nginx serves the clips dir directly (QC_CLIPS_URL_PREFIX = /clips). It must
    # be in the group that owns the clips so it can traverse the dir and read the
    # finished clips — otherwise downloads 403. (For a shared clipsDir this is the
    # shared clipsGroup; by default it's the service's own group.)
    users.users.${config.services.nginx.user}.extraGroups = [ clipsGroupName ];

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
      # Wait for the filesystem holding clipsDir — important when it's an external
      # mount (SMB/mergerfs) the admin manages (manageClipsDir = false).
      unitConfig.RequiresMountsFor = [ (toString cfg.clipsDir) ];

      environment = {
        QC_MEDIA_ROOTS = lib.concatStringsSep ":" (map toString cfg.mediaRoots);
        QC_CLIPS_DIR = toString cfg.clipsDir;
        # nginx serves the clips dir directly at /clips/ (see virtualHost below)
        QC_CLIPS_URL_PREFIX = "/clips";
        QC_STATE_DIR = stateDir;
        QC_BIND = cfg.listenAddress;
        QC_PORT = toString cfg.listenPort;
        QC_MAX_CONCURRENT_JOBS = toString cfg.maxConcurrentJobs;
        QC_SUBTITLE_LANGS = lib.concatStringsSep "," cfg.subtitleLangs;
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
        ReadOnlyPaths = map toString cfg.mediaRoots;
        ReadWritePaths = [ (toString cfg.clipsDir) stateDir ];

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

    services.nginx = {
      enable = true;
      virtualHosts.${vhostName} = {
        default = cfg.virtualHost == null;
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
          proxyPass = "http://${cfg.listenAddress}:${toString cfg.listenPort}";
        };
        locations."/clips/" = {
          alias = "${cfg.clipsDir}/";
        };
        basicAuthFile = lib.mkIf (cfg.passwordFile != null) cfg.passwordFile;
      };
    };

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [ 80 ];
    };
  };
}
