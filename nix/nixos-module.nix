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

    jellyfin = {
      url = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://localhost:8096";
        description = "Optional Jellyfin base URL for metadata enrichment.";
      };
      apiKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Path to a file containing the Jellyfin API key (kept out of the store).";
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
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
    };
    users.groups.${cfg.group} = { };

    systemd.services.quipclipper-web = {
      description = "quipclipper web backend";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        QC_MEDIA_ROOTS = lib.concatStringsSep ":" (map toString cfg.mediaRoots);
        QC_CLIPS_DIR = toString cfg.clipsDir;
        # nginx serves the clips dir directly at /clips/ (see virtualHost below)
        QC_CLIPS_URL_PREFIX = "/clips";
        QC_STATE_DIR = "/var/lib/quipclipper-web/state";
        QC_BIND = cfg.listenAddress;
        QC_PORT = toString cfg.listenPort;
        QC_MAX_CONCURRENT_JOBS = toString cfg.maxConcurrentJobs;
      } // lib.optionalAttrs (cfg.jellyfin.url != null) {
        QC_JELLYFIN_URL = cfg.jellyfin.url;
      } // lib.optionalAttrs (cfg.passwordFile != null) {
        # Reserved, not enforced (no auth gate yet — planned for phase 6);
        # this just lets the UI reflect that a password was configured.
        QC_PASSWORD = "set";
      };

      serviceConfig = {
        ExecStart = lib.getExe webPkg;
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = "quipclipper-web";
        Restart = "on-failure";

        # Hardening. Media roots are exposed read-only; only the state dir
        # (and clipsDir, if outside it) is writable.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadOnlyPaths = map toString cfg.mediaRoots;
        ReadWritePaths = [ (toString cfg.clipsDir) ];
      };
    };

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
