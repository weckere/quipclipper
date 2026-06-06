# The static frontend as a plain derivation (no build step in Phase 0 — the SPA
# is hand-written HTML/CSS/JS). The NixOS module points nginx's root at this.
{ stdenvNoCC }:

stdenvNoCC.mkDerivation {
  pname = "quipclipper-web-frontend";
  version = "0.1.0";

  src = ../web/frontend;

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -r ./* "$out/"
    runHook postInstall
  '';

  meta.description = "Static frontend assets for quipclipper-web";
}
