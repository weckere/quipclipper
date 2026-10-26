"""Who a pairing request came from reaches the backend only from a trusted proxy.

The bundled nginx (the Docker image's and the NixOS module's) passes Tailscale serve's
Tailscale-User-Login/-Name on only when its own peer is a trusted proxy (QC_TRUSTED_PROXIES
/ `nginx.trustedProxies`, whose address part test_nginx_realip.py covers), and blanks them
otherwise, so a caller who sends them itself names nobody. The backend side
(QC_FORWARDED_ALLOW_IPS) is tested in web/backend's test_pairing.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from test_nginx_gate import MODULE, NGINX, _locations

ROOT = Path(__file__).resolve().parents[1]
REALIP_SH = ROOT / "web/nginx/45-quip-realip.sh"
DOCKERFILE = ROOT / "web/nginx/Dockerfile"

pytestmark = pytest.mark.skipif(not NGINX.is_file(), reason="not a full checkout")


def _conf(path: Path) -> str:
    return re.sub(r"#.*", "", path.read_text())


def test_docker_nginx_passes_the_tailnet_user_only_from_a_trusted_proxy():
    conf = _conf(NGINX)
    # Keyed on the connection's own peer, never the X-Forwarded-For it claims.
    assert re.search(r"geo \$realip_remote_addr \$quip_via_proxy \{\s*default 0;\s*"
                     r"include /etc/nginx/quip-trusted-proxies.conf;\s*\}", conf)
    for var, header in (("login", "login"), ("name", "name")):
        assert re.search(rf"map \$quip_via_proxy \$quip_ts_{var} \{{\s*1 "
                         rf"\$http_tailscale_user_{header};\s*default \"\";\s*\}}", conf)
    # Only the maps read the client's headers.
    assert conf.count("$http_tailscale_user_") == 2
    # Every location that says where the caller is also says who, from the maps.
    locs = {s: b for s, b in _locations(conf).items() if "X-Real-IP" in b}
    assert "= /api/pair" in locs and "@quip_pair_open" in locs and "^~ /api/" in locs
    for spec, body in locs.items():
        assert "proxy_set_header Tailscale-User-Login $quip_ts_login;" in body, spec
        assert "proxy_set_header Tailscale-User-Name $quip_ts_name;" in body, spec
    assert "include /etc/nginx/quip-realip.conf;" in conf


def test_docker_image_bakes_the_geo_list():
    assert ": > /etc/nginx/quip-trusted-proxies.conf" in DOCKERFILE.read_text()


def _geo(tmp_path: Path, value: str | None) -> str:
    env = {"PATH": os.environ.get("PATH", ""),
           "QC_REALIP_CONF": str(tmp_path / "quip-realip.conf")}
    if value is not None:
        env["QC_TRUSTED_PROXIES"] = value
    subprocess.run(["sh", str(REALIP_SH)], env=env, check=True, capture_output=True)
    return (tmp_path / "quip-trusted-proxies.conf").read_text()


def test_realip_entrypoint_lists_the_same_proxies_for_the_geo(tmp_path):
    assert _geo(tmp_path, "172.30.88.2, fd7a:115c:a1e0::/48") == (
        "172.30.88.2 1;\nfd7a:115c:a1e0::/48 1;\n")
    assert _geo(tmp_path, None) == ""                     # unset: nobody's are passed on
    assert _geo(tmp_path, "10.0.0.1;deny_all proxy.example 10.0.0.2") == "10.0.0.2 1;\n"


def test_nixos_module_passes_the_tailnet_user_only_from_a_trusted_proxy():
    nix = MODULE.read_text()
    assert "geo $realip_remote_addr $quipclipper_via_proxy {" in nix
    assert "default 0;" in nix
    assert '(p: "  ${p} 1;\\n") cfg.nginx.trustedProxies' in nix
    assert nix.count("$http_tailscale_user_") == 2      # only in the maps
    headers = re.search(r"apiProxyHeaders = ''(.*?)'';", nix, re.S).group(1)
    assert "proxy_set_header Tailscale-User-Login $quipclipper_ts_login;" in headers
    assert "proxy_set_header Tailscale-User-Name $quipclipper_ts_name;" in headers
    assert "appendHttpConfig = trustedProxyHttp;" in nix


def test_nixos_module_tells_the_backend_whose_word_to_take():
    nix = MODULE.read_text()
    assert 'QC_FORWARDED_ALLOW_IPS = lib.concatStringsSep "," cfg.forwardedAllowIps;' in nix
    # Loopback, plus a non-loopback backend address nginx connects to it at.
    opt = re.search(r"forwardedAllowIps = lib.mkOption \{(.*?)\n    \};", nix, re.S).group(1)
    assert 'default = [ "127.0.0.1" "::1" ]' in opt and "cfg.listenAddress" in opt
    assert "cfg.forwardedAllowIps;" in nix.split("assertions = [", 1)[1]
