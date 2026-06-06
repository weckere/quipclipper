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
        });
    in
    perSystem // {
      # System-independent: the declarative deployment module. `self` is passed
      # so the module can resolve the per-system packages above.
      nixosModules.default = import ./nix/nixos-module.nix { inherit self; };
    };
}
