"""The password gate's exemptions stay exactly what pairing needs (docs/API.md, Pairing).

Behind QC_PASSWORD / passwordFile, nginx lets two requests through without the password:
an app asking to pair (POST /api/pair) and polling its own request (GET /api/pair/<id>).
A request with a client token gets through only after the backend says the token is
current (auth_request to /api/clients/check). Both nginx configurations (the Docker image's
and the NixOS module's) are read here, so a new `auth_basic off` somewhere else fails.
A live check of the same rules runs in the NixOS VM test (flake.nix).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NGINX = ROOT / "web/nginx/nginx.conf"
AUTH_SH = ROOT / "web/nginx/40-quip-auth.sh"
MODULE = ROOT / "nix/nixos-module.nix"

pytestmark = pytest.mark.skipif(not NGINX.is_file(), reason="not a full checkout")

PAIR_POLL = '"^/api/pair/[A-Za-z0-9_-]+$"'


def _locations(conf: str) -> dict[str, str]:
    """Each ``location <spec> {…}`` block (nested ones too), by spec, with its body."""
    out: dict[str, str] = {}
    for m in re.finditer(r"location\s+([^{]+?)\s*\{", conf):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(conf[i], 0)
            i += 1
        out[m.group(1).strip()] = conf[m.end():i - 1]
    return out


def _own(body: str) -> str:
    """A block's body without its nested location blocks."""
    return re.sub(r"location\s+[^{]+\{(?:[^{}]|\{[^{}]*\})*\}", "", body)


def test_docker_nginx_exempts_only_the_pairing_requests():
    conf = re.sub(r"#.*", "", NGINX.read_text())
    locs = _locations(conf)
    open_ = {spec for spec, body in locs.items() if "auth_basic off" in _own(body)}
    assert open_ == {"@quip_pair_open", f"~ {PAIR_POLL}"}
    # POST /api/pair goes to the open named location; GET /api/pair (the list) stays gated.
    exact = _own(locs["= /api/pair"])
    assert "error_page 418 = @quip_pair_open;" in exact
    assert "if ($request_method = POST) { return 418; }" in exact
    assert "auth_basic" not in exact
    # The poll URL is open for GET only; approve/deny (/api/pair/<id>/…) don't match it.
    poll = _own(locs[f"~ {PAIR_POLL}"])
    assert "limit_except GET { deny all; }" in poll and "auth_request off;" in poll
    assert not re.match(PAIR_POLL.strip('"'), "/api/pair/abc/approve")
    # Both live inside a prefix that outranks the generic `^~ /api/` block.
    assert "^~ /api/pair/" in locs


def test_docker_gate_asks_the_backend_about_client_tokens():
    sh = AUTH_SH.read_text()
    assert "echo 'satisfy any;'" in sh and "echo 'auth_request /_quip_client;'" in sh
    check = _own(_locations(re.sub(r"#.*", "", NGINX.read_text()))["= /_quip_client"])
    assert "internal;" in check
    assert "proxy_pass http://app:8000/api/clients/check;" in check
    # No client token: nginx answers itself and the password decides.
    assert 'if ($quip_client_token = "") { return 401; }' in check


def test_docker_api_proxy_keeps_the_browsers_port():
    locs = _locations(re.sub(r"#.*", "", NGINX.read_text()))
    assert "proxy_set_header X-Quip-Host $http_host;" in locs["^~ /api/"]
    # Host itself stays $host: nixpkgs' config check (gixy) refuses Host $http_host.
    assert not re.search(r"set_header Host \$http_host", NGINX.read_text() + MODULE.read_text())
    assert "X-Forwarded-Proto" not in locs["^~ /api/"]   # a front proxy's passes through


def test_nixos_module_exempts_only_the_pairing_requests():
    nix = MODULE.read_text()
    assert nix.count("auth_basic off;") == 1           # in pairOpen, nothing else
    uses = re.findall(r"extraConfig = apiProxyHeaders \+ pairOpen", nix)
    assert len(uses) == 2                               # the named location and the poll
    assert 'locations."@quipclipper_pair_open"' in nix
    assert 'locations."~ ^/api/pair/[A-Za-z0-9_-]+$"' in nix
    assert "limit_except GET { deny all; }" in nix
    assert "error_page 418 = @quipclipper_pair_open;" in nix
    assert "auth_request /_quipclipper_client;" in nix
    assert '"${proxyTarget}/api/clients/check"' in nix
