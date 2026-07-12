# The quipclipper-web backend, packaged like the CLI: a buildPythonApplication
# with ffmpeg + mkvmerge + yt-dlp wrapped onto PATH. `quipclipper` (the engine)
# is passed in by the flake so both share one Python.
{ lib
, python3Packages
, ffmpeg
, mkvtoolnix-cli
, yt-dlp
, quipclipper
}:

python3Packages.buildPythonApplication {
  pname = "quipclipper-web";
  version = "0.1.0";
  pyproject = true;

  src = ../web/backend;

  build-system = [ python3Packages.hatchling ];

  dependencies = [
    quipclipper
    python3Packages.fastapi
    python3Packages.uvicorn
  ];

  # web/backend/tests/ exercises the API via fastapi's TestClient (httpx). Run
  # them at build time so `nix flake check` (and every rebuild) covers the
  # backend, not just the Linux VM test. quipclipper (the engine) is already on
  # the import path via dependencies; ffmpeg/mkvmerge are wrapped on for the
  # /api/health tool probe (the tests mock out the actual cut calls).
  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    python3Packages.httpx
    ffmpeg
    mkvtoolnix-cli
    yt-dlp
  ];

  makeWrapperArgs = [
    "--prefix" "PATH" ":" (lib.makeBinPath [ ffmpeg mkvtoolnix-cli yt-dlp ])
  ];

  meta = {
    description = "Web app for quipclipper — find and cut clips from a browser";
    homepage = "https://github.com/weckere/quipclipper";
    license = lib.licenses.mit;
    mainProgram = "quipclipper-web";
  };
}
