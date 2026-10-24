"""Who sent a request: the address and, behind Tailscale serve, the tailnet user.

The pairing banner and Settings ▸ Connected apps show it ("From ericwecker@example.com,
100.101.102.103"), and the pairing rate limit counts by its address.

Only a trusted proxy can say who the caller is. The bundled nginx sends ``X-Real-IP``
(its client: after ``set_real_ip_from``, the peer of a trusted proxy in front of it) and
``Tailscale-User-Login`` / ``Tailscale-User-Name``, which it passes on only from a trusted
proxy in front of it and blanks otherwise (``nginx.trustedProxies`` in the NixOS module,
``QC_TRUSTED_PROXIES`` for the Docker image). The backend believes those headers only from a
peer in ``QC_FORWARDED_ALLOW_IPS`` (default loopback, where the NixOS module's nginx connects
from), or from anyone once the ``QC_PROXY_SECRET`` check has passed, which only nginx can.
From any other peer they're ignored and the connection's own address is the answer, so a
direct caller can't put a name or an address in the banner.
"""

from __future__ import annotations

import ipaddress
import re
from email.header import decode_header, make_header

DEFAULT_TRUSTED = "127.0.0.1,::1"
_MAX = 200   # chars kept of a forwarded name
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(value: str) -> tuple[Network, ...]:
    """``QC_FORWARDED_ALLOW_IPS``: addresses or CIDR networks, comma- or space-separated.
    Raises ValueError on an entry that is neither."""
    out = []
    for part in re.split(r"[,\s]+", value or ""):
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                raise ValueError(f"QC_FORWARDED_ALLOW_IPS: {part!r} is not an address or "
                                 "network") from None
    return tuple(out)


def _ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def trusted(peer: str | None, networks: tuple[Network, ...]) -> bool:
    ip = _ip(peer)
    if ip is None:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in networks)


def _text(value: str | None) -> str:
    """A forwarded name as plain text. Tailscale MIME-encodes a non-ASCII display name
    (``=?utf-8?q?...?=``)."""
    value = (value or "").strip()
    if value.startswith("=?"):
        try:
            value = str(make_header(decode_header(value)))
        except (ValueError, LookupError, UnicodeDecodeError):
            pass
    return _CONTROL.sub("", value).strip()[:_MAX]


def caller(headers, peer: str | None, networks: tuple[Network, ...],
           proxied: bool = False) -> dict:
    """``{"address", "user", "user_name"}`` for a request from ``peer`` with ``headers``.
    ``proxied`` says the proxy secret already proved nginx sent it."""
    own = str(_ip(peer) or peer or "unknown")
    if not (proxied or trusted(peer, networks)):
        return {"address": own, "user": "", "user_name": ""}
    real = _ip(headers.get("x-real-ip"))
    user = _text(headers.get("tailscale-user-login"))
    name = _text(headers.get("tailscale-user-name"))
    return {"address": str(real) if real else own, "user": user,
            "user_name": name if user else ""}
