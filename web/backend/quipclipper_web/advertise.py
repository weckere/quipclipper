"""LAN advertising (opt-in, ``QC_ADVERTISE=1``): announce this server over mDNS/Bonjour.

``quipclipper-web advertise`` runs beside the app and announces

    _quipclipper._tcp.local.   instance = server_name, port = QC_ADVERTISE_PORT
    TXT  v=1  url=<canonical base URL>  id=<server_id>

so LeadStinger can list "QuipClipper on nas" without anyone typing an address. The
canonical URL is ``QC_PUBLIC_URL``, else ``http://<host>.local:<port>``. It also writes
``state_dir/advertise.json`` (``{"url", "name"}``), which the app reads: Send to
LeadStinger links fall back to that URL when ``QC_PUBLIC_URL`` isn't set, and in Docker
the app takes the host's name from it. The file is removed when the advertiser stops.

Multicast doesn't cross Docker's bridge network, so in Docker this runs as its own
service with ``network_mode: host`` (docker-compose.example.yml). The NixOS module
advertises with Avahi instead (``advertise.enable``). An install reached only over a
tailnet should not advertise: it tells the whole LAN where quipclipper is.

It uses python-zeroconf (LGPL-2.1, a dependency; no code of it is copied here).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from quipclipper_web import lslink, pairing
from quipclipper_web.config import Settings

SERVICE_TYPE = "_quipclipper._tcp.local."
TXT_VERSION = "1"
INSTANCE_MAX_BYTES = 63
# Interfaces that aren't the LAN: container bridges, VPNs and tailnets. Announcing there
# reaches nobody who should see it, and a tailnet address must not leak onto the LAN.
_SKIP_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "tailscale", "zt",
                  "wg", "utun", "tun", "tap", "lo")
_TAILNET4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class AdvertiseError(Exception):
    """Advertising can't start (it is off, or no port is known)."""


def short_hostname(name: str | None = None) -> str:
    """This host's name as one mDNS label: ``nas`` for ``nas``, ``nas.local`` or
    ``nas.example.com``."""
    raw = (name if name is not None else socket.gethostname()).strip().rstrip(".")
    return raw.split(".", 1)[0] or "quipclipper"


def instance_name(server_name: str) -> str:
    """``server_name`` as a DNS-SD instance label: no dots (they would split the label),
    at most 63 bytes of UTF-8."""
    name = " ".join(server_name.replace(".", "-").split()) or "quipclipper"
    out = name.encode("utf-8")[:INSTANCE_MAX_BYTES].decode("utf-8", "ignore")
    return out.strip() or "quipclipper"


def advertise_port(settings: Settings, port: int | None = None) -> int:
    """The port browsers use: ``--port``, ``QC_ADVERTISE_PORT``, else ``QC_PUBLIC_URL``'s."""
    if port:
        return port
    if settings.advertise_port:
        return settings.advertise_port
    if settings.public_url:
        parts = urlsplit(settings.public_url)
        return parts.port or (443 if parts.scheme == "https" else 80)
    raise AdvertiseError("Set QC_ADVERTISE_PORT (or --port) to the port browsers use, "
                         "e.g. 8896 for the Docker compose example.")


def canonical_url(public_url: str, hostname: str, port: int) -> str:
    """``QC_PUBLIC_URL``, else ``http://<hostname>.local:<port>`` (no ``:80``)."""
    if public_url:
        return lslink.normalize_server(public_url)
    return f"http://{hostname}.local" + ("" if port == 80 else f":{port}")


def build_record(*, server_name: str, url: str, server_id: str, hostname: str, port: int,
                 addresses: list[str]) -> dict:
    """Everything the announcement says, as plain data (what :func:`run` registers)."""
    return {
        "type": SERVICE_TYPE,
        "name": f"{instance_name(server_name)}.{SERVICE_TYPE}",
        "server": f"{hostname}.local.",
        "port": port,
        "properties": {"v": TXT_VERSION, "url": url, "id": server_id},
        "addresses": list(addresses),
    }


def lan_addresses(adapters=None) -> list[str]:
    """This host's LAN addresses: IPv4 and global or unique-local IPv6 on interfaces that
    aren't loopback, container bridges, VPNs or the tailnet."""
    if adapters is None:
        import ifaddr  # a dependency of python-zeroconf

        adapters = ifaddr.get_adapters()
    found: list[str] = []
    for adapter in adapters:
        nice = (getattr(adapter, "nice_name", "") or getattr(adapter, "name", "")).lower()
        if nice.startswith(_SKIP_PREFIXES):
            continue
        for ip in adapter.ips:
            raw = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            try:
                addr = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local or addr.is_multicast:
                continue
            if addr in (_TAILNET4 if addr.version == 4 else _TAILNET6):
                continue
            if str(addr) not in found:
                found.append(str(addr))
    return found


def write_state(state_dir: Path, url: str, name: str) -> Path:
    path = Path(state_dir) / pairing.ADVERTISE_FILE
    pairing.write_atomic(path, json.dumps({"url": url, "name": name}) + "\n", mode=0o644)
    return path


def remove_state(state_dir: Path) -> None:
    try:
        (Path(state_dir) / pairing.ADVERTISE_FILE).unlink()
    except OSError:
        pass


def _zeroconf(addresses: list[str]):
    from zeroconf import IPVersion, Zeroconf

    if addresses:
        return Zeroconf(interfaces=addresses)
    return Zeroconf(ip_version=IPVersion.All)


def run(settings: Settings, *, port: int | None = None, stop: threading.Event | None = None,
        zeroconf_factory: Callable | None = None, addresses: list[str] | None = None,
        hostname: str | None = None, log: Callable[[str], None] | None = None) -> dict:
    """Announce until ``stop`` is set; returns the record. Raises AdvertiseError when
    advertising is off or no port is known."""
    log = log or (lambda msg: print(f"[quipclipper advertise] {msg}", file=sys.stderr,
                                    flush=True))
    if not settings.advertise:
        raise AdvertiseError("Advertising is off: set QC_ADVERTISE=1 to announce this "
                             "server on the local network.")
    browser_port = advertise_port(settings, port)
    host = short_hostname(hostname)
    name = pairing.server_name(settings.server_name or host)
    url = canonical_url(settings.public_url, host, browser_port)
    addrs = lan_addresses() if addresses is None else addresses
    record = build_record(server_name=name, url=url,
                          server_id=pairing.server_id(settings.state_dir),
                          hostname=host, port=browser_port, addresses=addrs)
    if not addrs:
        log("no LAN address found; announcing the service without address records")

    from zeroconf import ServiceInfo

    info = ServiceInfo(record["type"], record["name"], port=record["port"],
                       properties=record["properties"], server=record["server"],
                       parsed_addresses=addrs or None)
    zc = (zeroconf_factory or _zeroconf)(addrs)
    stop = stop or threading.Event()
    try:
        zc.register_service(info, allow_name_change=True)
        write_state(settings.state_dir, url, name)
        log(f"announcing {info.name} at {url} ({', '.join(addrs) or 'no addresses'})")
        stop.wait()
    finally:
        remove_state(settings.state_dir)
        try:
            zc.unregister_service(info)
        finally:
            zc.close()
        log("stopped")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quipclipper-web advertise",
        description="Announce this quipclipper server on the local network (mDNS, "
                    "_quipclipper._tcp) until stopped. Needs QC_ADVERTISE=1. Tailnet-only "
                    "installs should not advertise.")
    parser.add_argument("--port", type=int, default=None,
                        help="The port browsers use (default: QC_ADVERTISE_PORT, else "
                             "QC_PUBLIC_URL's port).")
    args = parser.parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        run(Settings.from_env(), port=args.port, stop=stop)
    except (AdvertiseError, lslink.LinkError) as exc:
        print(f"quipclipper-web advertise: {exc}", file=sys.stderr)
        return 2
    return 0
