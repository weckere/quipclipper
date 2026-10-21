# The static frontend as a plain derivation (no build step in Phase 0 — the SPA
# is hand-written HTML/CSS/JS). The NixOS module points nginx's root at this.
#
# The one third-party asset, hls.js, is installed here from its pinned
# derivation rather than committed to the repo (see ./hls-js.nix).
{ stdenvNoCC, hls-js }:

stdenvNoCC.mkDerivation {
  pname = "quipclipper-web-frontend";
  # The root pyproject.toml is the one version source.
  version = (builtins.fromTOML (builtins.readFile ../pyproject.toml)).project.version;

  src = ../web/frontend;

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -r ./* "$out/"
    # vendor/ is gitignored in the checkout (the devShell copies it there for
    # the dev server), so it must be installed from the pin here.
    install -Dm444 ${hls-js}/hls.min.js "$out/vendor/hls.min.js"
    install -Dm444 ${hls-js}/LICENSE "$out/vendor/hls.min.js.LICENSE"
    runHook postInstall
  '';

  meta.description = "Static frontend assets for quipclipper-web";
}
