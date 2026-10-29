"""The Docker web image and the Nix frontend ship the same hls.js.

web/frontend/vendor/ is gitignored, so the image can't COPY it from the checkout:
web/nginx/Dockerfile fetches the npm tarball nix/hls-js.nix pins, by the same hash.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "web/nginx/Dockerfile"
HLS_NIX = ROOT / "nix/hls-js.nix"

pytestmark = pytest.mark.skipif(
    not (DOCKERFILE.is_file() and HLS_NIX.is_file()), reason="not a full checkout")


def _arg(name: str) -> str:
    found = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(), re.M)
    assert found, f"web/nginx/Dockerfile has no ARG {name}"
    return found.group(1)


def test_docker_image_fetches_the_hls_js_version_nix_pins() -> None:
    nix = HLS_NIX.read_text()
    version = re.search(r'version = "([^"]+)";', nix).group(1)
    sri = re.search(r'hash = "sha256-([^"]+)";', nix).group(1)
    assert _arg("HLS_JS_VERSION") == version
    assert _arg("HLS_JS_SHA256") == base64.b64decode(sri).hex()


def test_docker_image_installs_hls_js_where_the_frontend_loads_it() -> None:
    app = (ROOT / "web/frontend/app.js").read_text()
    assert 's.src = "vendor/hls.min.js"' in app
    assert "/usr/share/nginx/html/vendor/hls.min.js" in DOCKERFILE.read_text()


def test_release_moves_latest_only_after_both_images_pushed() -> None:
    """NIX-7: a matrix build pushed X.Y.Z, X.Y and latest per image, so one failed build
    left latest on a mix of two releases. Builds push X.Y.Z only; one later job retags."""
    wf = (ROOT / ".github/workflows/release.yml").read_text()
    images = wf[wf.index("\n  images:"):wf.index("\n  tags:")]
    tags = wf[wf.index("\n  tags:"):wf.index("\n  release:")]
    assert "value=latest" not in images
    assert "{{major}}.{{minor}}" not in images
    assert "needs: [check, images]" in tags
    assert '-t "$repo-$image:latest"' in tags and '-t "$repo-$image:$minor"' in tags
    assert tags.index(':$minor"') < tags.index(':latest"')
    assert "needs: [check, tags]" in wf[wf.index("\n  release:"):]


@pytest.mark.parametrize("version,tags,moved", [
    ("0.5.2", ["v0.5.1", "v0.5.2"], ["0.5", "latest"]),
    # An older release's run finishing last, or re-run: neither tag goes back.
    ("0.5.2", ["v0.5.2", "v0.5.3", "v0.6.0"], []),
    # A patch of an older minor: its X.Y moves, latest stays on the newer minor.
    ("0.5.4", ["v0.5.3", "v0.5.4", "v0.6.0", "v0.10.0-rc1"], ["0.5"]),
    ("0.10.0", ["v0.9.9", "v0.10.0"], ["0.10", "latest"]),
])
def test_release_moves_x_y_and_latest_only_forwards(tmp_path, version, tags, moved) -> None:
    """Two release runs overlapping (or an old one re-run) used to point latest at the one
    that finished last."""
    wf = (ROOT / ".github/workflows/release.yml").read_text()
    step = wf[wf.index("- name: Tag X.Y and latest"):wf.index("\n  release:")]
    script = textwrap.dedent(step[step.index("run: |") + len("run: |"):])
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text("#!/bin/sh\n" + "".join(
        f"echo 'abc\trefs/tags/{t}'\n" for t in tags))
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$5" >> {tmp_path}/moved\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "VERSION": version,
           "GITHUB_REPOSITORY": "weckere/quipclipper", "GITHUB_SERVER_URL": "https://github.com"}
    subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, check=True)
    log = (tmp_path / "moved").read_text().split() if (tmp_path / "moved").exists() else []
    assert sorted({t.rsplit(":", 1)[1] for t in log}) == sorted(moved)
    assert len(log) == 2 * len(moved)  # both images
