# hls.js — HLS playback in browsers with no native support for it (Chrome,
# Firefox). Safari/iOS play HLS natively and never load this.
#
# Pinned by hash from the npm registry rather than vendored into the repo: a
# 543 KB minified blob in git history is something no reviewer can meaningfully
# diff, whereas a hash pins exactly which upstream bytes are being served. The
# devShell symlinks the result into web/frontend/vendor/ so the dev server (which
# serves that directory straight off disk) has it too.
{ lib, fetchurl, stdenvNoCC }:

let
  version = "1.6.16";
in
stdenvNoCC.mkDerivation {
  pname = "hls.js";
  inherit version;

  src = fetchurl {
    url = "https://registry.npmjs.org/hls.js/-/hls.js-${version}.tgz";
    hash = "sha256-0oIzn+0JoJh9VbSbQUMKZ9Ahhhwk0zci515couUXnHc=";
  };

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm444 dist/hls.min.js "$out/hls.min.js"
    install -Dm444 LICENSE "$out/LICENSE"
    runHook postInstall
  '';

  meta = {
    description = "JavaScript HLS client using Media Source Extensions";
    homepage = "https://github.com/video-dev/hls.js";
    license = lib.licenses.asl20;
  };
}
