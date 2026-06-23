{
  description = "Find and cut audio/video clips by searching subtitle dialogue";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      perSystem = flake-utils.lib.eachDefaultSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python313;

          quipclipper = python.pkgs.buildPythonApplication {
            pname = "quipclipper";
            version = "0.1.0";
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

          quipclipper-web-frontend = pkgs.callPackage ./nix/frontend.nix { };

          quipclipper-web = pkgs.callPackage ./nix/quipclipper-web.nix {
            inherit quipclipper;
            python3Packages = python.pkgs;
          };
        in {
          packages = {
            default = quipclipper;
            inherit quipclipper quipclipper-web quipclipper-web-frontend;
          };

          devShells.default = pkgs.mkShell {
            inputsFrom = [ quipclipper ];
            packages = [
              python.pkgs.pytest
              python.pkgs.httpx
              python.pkgs.fastapi
              python.pkgs.uvicorn
            ];
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
                systemd.tmpfiles.rules = [ "d /srv/media 0755 root root - -" ];
                services.quipclipper-web = {
                  enable = true;
                  mediaRoots = [ "/srv/media" ];
                  listenAddress = "0.0.0.0";  # reachable on the test net for the assert
                  nginx.enable = false;
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
                backendonly.succeed("curl -fsS http://localhost:8000/api/library/roots -o /tmp/r.json")
                backendonly.succeed("grep -q /srv/media /tmp/r.json")
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
