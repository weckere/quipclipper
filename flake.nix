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
              nodes.machine = { ... }: {
                imports = [ self.nixosModules.default ];
                # A media root the service is allowed to read, an htpasswd (user
                # "quip", password "test123") for the basic-auth gate, and a probe
                # clip planted in the 0750 clips dir (owned by the service user)
                # so we can prove nginx serves /clips/ despite the tight perms.
                systemd.tmpfiles.rules = [
                  "d /srv/media 0755 root root - -"
                  "d /var/lib/quipclipper-web/clips/probe 0755 quipclipper-web quipclipper-web - -"
                  "f /var/lib/quipclipper-web/clips/probe/clip.txt 0644 quipclipper-web quipclipper-web - hello-clip"
                ];
                environment.etc."quipclipper.htpasswd".text =
                  "quip:$apr1$NF6aL6Je$uO.ixyjrDUHxSp2DgD2Rj0\n";
                services.quipclipper-web = {
                  enable = true;
                  mediaRoots = [ "/srv/media" ];
                  passwordFile = "/etc/quipclipper.htpasswd";
                };
              };
              testScript = ''
                machine.wait_for_unit("quipclipper-web.service")
                machine.wait_for_unit("nginx.service")
                machine.wait_for_open_port(8000)  # uvicorn backend bound + ready
                machine.wait_for_open_port(80)
                # Basic-auth gate: no credentials is rejected, correct ones pass.
                machine.fail("curl -fsS http://localhost/")
                machine.succeed("curl -fsS -u quip:test123 http://localhost/ | grep -q quipclipper")
                machine.succeed("curl -fsS -u quip:test123 http://localhost/api/health | grep -q ok")
                machine.succeed("curl -fsS -u quip:test123 http://localhost/api/library/roots | grep -q /srv/media")
                # Finished clips are served by nginx straight from the 0750 clips
                # dir — only works because nginx is in the service group. Without
                # that, this download 403s.
                machine.succeed("curl -fsS -u quip:test123 http://localhost/clips/probe/clip.txt | grep -q hello-clip")
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
