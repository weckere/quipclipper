# The quipclipper-web backend, packaged like the CLI: a buildPythonApplication
# with ffmpeg + mkvmerge wrapped onto PATH. `quipclipper` (the engine) is passed
# in by the flake so both share one Python.
{ lib
, python3Packages
, ffmpeg
, mkvtoolnix-cli
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

  # Tests use fastapi's TestClient (httpx); run them in the dev shell rather
  # than during the build for now.
  doCheck = false;

  makeWrapperArgs = [
    "--prefix" "PATH" ":" (lib.makeBinPath [ ffmpeg mkvtoolnix-cli ])
  ];

  meta = {
    description = "Web app for quipclipper — find and cut clips from a browser";
    homepage = "https://github.com/weckere/quipclipper";
    license = lib.licenses.mit;
    mainProgram = "quipclipper-web";
  };
}
