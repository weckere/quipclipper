{
  description = "Find and cut audio/video clips by searching subtitle dialogue";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # yt-dlp bit-rots against YouTube changes, and the main nixpkgs pin can lag
    # months behind its ~weekly releases. Pin it separately so `nix flake update
    # nixpkgs-ytdlp` refreshes ONLY yt-dlp (and its deps) — not the whole
    # python/ffmpeg closure.
    nixpkgs-ytdlp.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs, flake-utils, nixpkgs-ytdlp }:
    let
      perSystem = flake-utils.lib.eachDefaultSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          # yt-dlp from its own fresh pin (see the input comment above).
          yt-dlp-fresh = nixpkgs-ytdlp.legacyPackages.${system}.yt-dlp;
          python = pkgs.python313;
          # pyproject.toml is the one version source (scripts/release.sh bumps it).
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;

          quipclipper = python.pkgs.buildPythonApplication {
            pname = "quipclipper";
            inherit version;
            pyproject = true;

            src = ./.;

            build-system = [ python.pkgs.hatchling ];

            dependencies = with python.pkgs; [
              pysubs2
              rapidfuzz
              typer
            ];

            nativeCheckInputs = [ python.pkgs.pytestCheckHook ];

            makeWrapperArgs = [
              "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [
                pkgs.ffmpeg
                pkgs.mkvtoolnix-cli
              ])
            ];

            meta = {
              description = "Find and cut audio/video clips by searching subtitle dialogue";
              homepage = "https://github.com/weckere/quipclipper";
              license = pkgs.lib.licenses.mit;
              mainProgram = "quipclipper";
            };
          };

          hls-js = pkgs.callPackage ./nix/hls-js.nix { };

          quipclipper-web-frontend = pkgs.callPackage ./nix/frontend.nix {
            inherit hls-js;
          };

          quipclipper-web = pkgs.callPackage ./nix/quipclipper-web.nix {
            inherit quipclipper;
            python3Packages = python.pkgs;
            yt-dlp = yt-dlp-fresh;
          };
        in {
          packages = {
            default = quipclipper;
            inherit quipclipper quipclipper-web quipclipper-web-frontend hls-js;
          };

          devShells.default = pkgs.mkShell {
            inputsFrom = [ quipclipper ];
            packages = [
              python.pkgs.pytest
              python.pkgs.httpx
              python.pkgs.fastapi
              python.pkgs.uvicorn
              yt-dlp-fresh   # YouTube subtitle/metadata fetch + stream-URL resolution
            ];

            # The dev server serves web/frontend straight off disk, so the pinned
            # hls.js has to exist there — the nix frontend derivation's copy is
            # only in the store. Copied, not symlinked: StaticFiles resolves the
            # path and 404s anything landing outside the served directory, so a
            # link into /nix/store is not servable. `install` replaces the
            # (read-only) store copy in place, so a new pin overwrites cleanly.
            shellHook = ''
              if [ -d web/frontend ]; then
                install -Dm644 ${hls-js}/hls.min.js web/frontend/vendor/hls.min.js
              fi
            '';
          };

          # VM test for the NixOS module: enable the service, then assert nginx
          # serves the app, the API responds, and the basic-auth gate works.
          # Linux-only (nixosTest runs a QEMU VM); run in CI on a Linux runner.
          checks = pkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
            nixos-module = pkgs.testers.runNixOSTest {
              name = "quipclipper-web-module";
              # A second host with the bundled nginx disabled (nginx.enable=false):
              # only the hardened backend runs, on listenAddress:listenPort, for a
              # BYO front — proving quipclipper can stay off :80.
              nodes.backendonly = { ... }: {
                imports = [ self.nixosModules.default ];
                # A root-only Jellyfin key, as a sops secret would be: the service user
                # can't read it, so health's jellyfin=true proves LoadCredential works.
                systemd.tmpfiles.rules = [
                  "d /srv/media 0755 root root - -"
                  "f /etc/jellyfin-key 0400 root root - test-key"
                ];
                services.quipclipper-web = {
                  enable = true;
                  mediaRoots = [ "/srv/media" ];
                  listenAddress = "0.0.0.0";  # reachable on the test net for the assert
                  nginx.enable = false;
                  # Machine transcription (M21), with a stand-in model file so the test
                  # doesn't download one: health shows whisper-cli and the model, and
                  # the unit carries the thread cap.
                  transcribe = {
                    enable = true;
                    vad = false;
                    modelFiles."small.en" = pkgs.writeText "ggml-small.en.bin" "stand-in";
                    # A stand-in whisper-cli that "hears" the CPU policy and niceness it
                    # runs under, so the test sees a whole run inside the hardened unit
                    # (the idle launcher, ffmpeg, the store) and that it ran idle.
                    package = pkgs.writeShellScriptBin "whisper-cli" ''
                      while [ $# -gt 0 ]; do
                        if [ "$1" = -of ]; then of=$2; fi
                        shift
                      done
                      policy=$(${pkgs.util-linux}/bin/chrt -p $$ | ${pkgs.gawk}/bin/awk -F': ' '{print $2; exit}')
                      nice=$(${pkgs.gawk}/bin/awk '{print $19}' /proc/$$/stat)
                      printf '{"transcription":[{"offsets":{"from":500,"to":2500},"text":"heard at %s nice %s"}]}' \
                        "$policy" "$nice" > "$of.json"
                    '';
                  };
                  jellyfin = {
                    url = "http://127.0.0.1:8096";
                    apiKeyFile = "/etc/jellyfin-key";
                    pathMap."/movies" = "/srv/media";
                  };
                };
              };
              nodes.machine = { ... }: {
                imports = [ self.nixosModules.default ];
                # A media root the service may read; an htpasswd (user "quip",
                # password "test123") for the basic-auth gate; a shared "media"
                # group with a stand-in service account ("reader", as samba or
                # Jellyfin would run) to prove finished clips are readable outside
                # quipclipper; and a probe clip planted as a real one would be.
                users.groups.media = { };
                users.users.reader = { isSystemUser = true; group = "media"; };
                # Probe clip planted as the service now writes them under a shared
                # clipsGroup: per-source subdir 2775 (setgid) and file 0664, both
                # group-owned by "media" — i.e. group-writable so the share can
                # delete/manage clips, not just read them.
                systemd.tmpfiles.rules = [
                  "d /srv/media 0755 root root - -"
                  "d /var/lib/quipclipper-web/clips/probe 2775 quipclipper-web media - -"
                  "f /var/lib/quipclipper-web/clips/probe/clip.txt 0664 quipclipper-web media - hello-clip"
                ];
                environment.etc."quipclipper.htpasswd".text =
                  "quip:$apr1$NF6aL6Je$uO.ixyjrDUHxSp2DgD2Rj0\n";
                services.quipclipper-web = {
                  enable = true;
                  mediaRoots = [ "/srv/media" ];
                  passwordFile = "/etc/quipclipper.htpasswd";
                  # Share the clips dir with the "media" group (SMB/Jellyfin flow).
                  clipsGroup = "media";
                  clipsMode = "2775";
                };
              };
              testScript = ''
                machine.wait_for_unit("quipclipper-web.service")
                machine.wait_for_unit("nginx.service")
                machine.wait_for_open_port(8000)  # uvicorn backend bound + ready
                machine.wait_for_open_port(80)
                # Basic-auth gate: no credentials is rejected, correct ones pass.
                machine.fail("curl -fsS http://localhost/")
                # Fetch to a file before grepping: `curl | grep -q` races —
                # grep -q closes the pipe on first match, curl then exits 23
                # (write error) and pipefail fails the (otherwise-fine) command.
                machine.succeed("curl -fsS -u quip:test123 http://localhost/ -o /tmp/index.html")
                machine.succeed("grep -q quipclipper /tmp/index.html")
                machine.succeed("curl -fsS -u quip:test123 http://localhost/api/health -o /tmp/health.json")
                machine.succeed("grep -q ok /tmp/health.json")
                machine.succeed("curl -fsS -u quip:test123 http://localhost/api/library/roots -o /tmp/roots.json")
                machine.succeed("grep -q /srv/media /tmp/roots.json")
                # nginx serves finished clips straight from clipsDir (it's in the
                # clips group); without that this download 403s.
                machine.succeed("curl -fsS -u quip:test123 http://localhost/clips/probe/clip.txt -o /tmp/clip.txt")
                machine.succeed("grep -q hello-clip /tmp/clip.txt")
                # Shared clips: the module owns clipsDir setgid to the shared group,
                # so a separate service account (samba/Jellyfin) in that group can
                # read finished clips — the whole point of clipsGroup/clipsMode.
                machine.succeed("stat -c '%a %G' /var/lib/quipclipper-web/clips | grep -xq '2775 media'")
                machine.succeed("runuser -u reader -- cat /var/lib/quipclipper-web/clips/probe/clip.txt | grep -q hello-clip")
                # Shared clips must be group-WRITABLE: the service writes with
                # UMask 0002, and a "media" account can delete a finished clip.
                machine.succeed("systemctl show quipclipper-web.service -p UMask | grep -q '=0002$'")
                machine.succeed("runuser -u reader -- rm /var/lib/quipclipper-web/clips/probe/clip.txt")
                machine.fail("test -e /var/lib/quipclipper-web/clips/probe/clip.txt")

                # nginx.enable = false: backend runs alone, no nginx, nothing on :80.
                backendonly.wait_for_unit("quipclipper-web.service")
                backendonly.wait_for_open_port(8000)
                backendonly.fail("systemctl is-active nginx.service")
                backendonly.fail("curl -fsS http://localhost:80/")
                backendonly.succeed("curl -fsS http://localhost:8000/api/health -o /tmp/h.json")
                backendonly.succeed("grep -q ok /tmp/h.json")
                # Jellyfin: the root-only key reached the service (Now playing is on);
                # the default host has no Jellyfin, so it's off there.
                backendonly.succeed("grep -q '\"jellyfin\": *true' /tmp/h.json")
                machine.succeed("grep -q '\"jellyfin\": *false' /tmp/health.json")
                backendonly.succeed("curl -fsS http://localhost:8000/api/library/roots -o /tmp/r.json")
                backendonly.succeed("grep -q /srv/media /tmp/r.json")
                # Machine transcription: whisper-cli and the model are there, capped at 2
                # threads; it's off where it isn't enabled.
                backendonly.succeed("grep -q '\"whisper-cli\": *true' /tmp/h.json")
                backendonly.succeed("grep -q '\"available\": *true' /tmp/h.json")
                backendonly.succeed("systemctl show quipclipper-web.service -p Environment | grep -q QC_WHISPER_THREADS=2")
                backendonly.succeed("systemctl show quipclipper-web.service -p Environment | grep -q QC_TRANSCRIBE=1")
                # A whole run on request, through the API: it finishes, its line is
                # stored and searchable, and whisper ran at SCHED_IDLE, nice 19.
                backendonly.succeed("${pkgs.ffmpeg-headless}/bin/ffmpeg -v error -f lavfi -i sine=duration=5 /srv/media/tone.flac")
                job = backendonly.succeed(
                    "curl -fsS -H 'X-Quipclipper: 1' -H 'Content-Type: application/json'"
                    " -d '{\"path\": \"/srv/media/tone.flac\", \"mode\": \"file\"}'"
                    " http://localhost:8000/api/items/transcribe"
                    " | ${pkgs.jq}/bin/jq -r .job_id").strip()
                backendonly.wait_until_succeeds(
                    f"curl -fsS http://localhost:8000/api/jobs/{job} | grep -q '\"status\": *\"done\"'",
                    timeout=120)
                backendonly.succeed("curl -fsS 'http://localhost:8000/api/items/transcript?path=/srv/media/tone.flac' -o /tmp/t.json")
                backendonly.succeed("grep -q 'heard at SCHED_IDLE nice 19' /tmp/t.json")
                # Off by default (experimental, D-040): health doesn't mention it, and its
                # endpoints answer 404 with the reason.
                machine.fail("grep -q transcribe /tmp/health.json")
                machine.succeed(
                    "curl -sS -u quip:test123 -o /dev/null -w '%{http_code}' -H 'X-Quipclipper: 1'"
                    " 'http://localhost/api/items/transcript?path=/srv/media/tone.flac'"
                    " | grep -qx 404")
              '';
            };
          };
        });
    in
    perSystem // {
      # System-independent: the declarative deployment module. `self` is passed
      # so the module can resolve the per-system packages above.
      nixosModules.default = import ./nix/nixos-module.nix { inherit self; };
    };
}
