"""Trusted proxies (QC_TRUSTED_PROXIES / nginx.trustedProxies): nginx takes the client's
address from the proxy's header, so the pairing banner's "From" and its rate limit see the
real caller. Only addresses and ranges may reach nginx's config.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "web/nginx/45-quip-realip.sh"
NGINX = ROOT / "web/nginx/nginx.conf"
MODULE = ROOT / "nix/nixos-module.nix"
DOCKERFILE = ROOT / "web/nginx/Dockerfile"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file() or shutil.which("sh") is None, reason="not a full checkout")


def _run(tmp_path: Path, **env: str) -> str:
    conf = tmp_path / "quip-realip.conf"
    full = {"PATH": os.environ.get("PATH", ""), "QC_REALIP_CONF": str(conf), **env}
    subprocess.run(["sh", str(SCRIPT)], env=full, check=True, capture_output=True)
    return conf.read_text()


def test_unset_leaves_the_snippet_empty(tmp_path):
    assert _run(tmp_path) == ""


def test_trusted_proxies_name_the_client_header(tmp_path):
    out = _run(tmp_path, QC_TRUSTED_PROXIES="172.30.88.2, 10.0.0.0/8 fd00::/8")
    assert out == ("set_real_ip_from 172.30.88.2;\nset_real_ip_from 10.0.0.0/8;\n"
                   "set_real_ip_from fd00::/8;\nreal_ip_header X-Forwarded-For;\n")
    assert _run(tmp_path, QC_TRUSTED_PROXIES="10.0.0.1", QC_REAL_IP_HEADER="X-Real-IP").endswith(
        "real_ip_header X-Real-IP;\n")


def test_nothing_but_addresses_reaches_nginx(tmp_path):
    out = _run(tmp_path, QC_TRUSTED_PROXIES="10.0.0.1;deny_all proxy.example 10.0.0.2")
    assert out == "set_real_ip_from 10.0.0.2;\nreal_ip_header X-Forwarded-For;\n"
    assert _run(tmp_path, QC_TRUSTED_PROXIES="proxy.example") == ""
    assert _run(tmp_path, QC_TRUSTED_PROXIES="10.0.0.1", QC_REAL_IP_HEADER="X-A; deny all") == ""


def test_the_image_runs_it_and_nginx_includes_it():
    assert "include /etc/nginx/quip-realip.conf;" in NGINX.read_text()
    docker = DOCKERFILE.read_text()
    assert "45-quip-realip.sh /docker-entrypoint.d/45-quip-realip.sh" in docker
    assert "/docker-entrypoint.d/45-quip-realip.sh" in docker.split("RUN chmod", 1)[1]


def test_the_module_writes_the_same_directives():
    module = MODULE.read_text()
    assert "trustedProxies = lib.mkOption" in module and "realIpHeader = lib.mkOption" in module
    assert '"set_real_ip_from ${p};' in module and '"real_ip_header ${cfg.nginx.realIpHeader};' in module
    assert "'' + realIpConfig;" in module
