{
  description = "Find and cut audio/video clips by searching subtitle dialogue";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
      in {
        packages.default = python.pkgs.buildPythonApplication {
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
          };
        };

        devShells.default = pkgs.mkShell {
          inputsFrom = [ self.packages.${system}.default ];
          packages = [ python.pkgs.pytest ];
        };
      });
}
