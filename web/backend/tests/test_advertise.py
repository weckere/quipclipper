"""LAN advertising (QC_ADVERTISE=1): the record and advertise.json, without real multicast."""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from quipclipper_web import advertise, pairing
from quipclipper_web.__main__ import main as web_main
from quipclipper_web.config import Settings


def _settings(tmp_path, **env) -> Settings:
    return Settings.from_env({"QC_STATE_DIR": str(tmp_path / "state"), "QC_ADVERTISE": "1",
                              **env})


class FakeZeroconf:
    """Stands in for zeroconf.Zeroconf: records what would go on the network."""

    def __init__(self, addresses):
        self.addresses = addresses
        self.registered = []
        self.unregistered = []
        self.closed = False

    def register_service(self, info, allow_name_change=False):
        self.registered.append((info, allow_name_change))

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


def _run(settings, **kw):
    """Run the advertiser with a fake zeroconf; ``on_up`` sees the state while it runs."""
    made = []
    stop = threading.Event()
    seen = {}

    def factory(addrs):
        zc = FakeZeroconf(addrs)
        made.append(zc)
        return zc

    def log(msg):
        if msg.startswith("announcing"):
            path = settings.state_dir / "advertise.json"
            seen["state"] = json.loads(path.read_text()) if path.exists() else None
            stop.set()

    record = advertise.run(settings, stop=stop, zeroconf_factory=factory, log=log, **kw)
    return record, made[0], seen


def test_the_record_says_v_url_and_id(tmp_path):
    s = _settings(tmp_path, QC_ADVERTISE_PORT="8896", QC_SERVER_NAME="nas")
    record, zc, seen = _run(s, addresses=["192.168.1.20"], hostname="nas")
    sid = pairing.server_id(s.state_dir)
    assert record == {
        "type": "_quipclipper._tcp.local.",
        "name": "nas._quipclipper._tcp.local.",
        "server": "nas.local.",
        "port": 8896,
        "properties": {"v": "1", "url": "http://nas.local:8896", "id": sid},
        "addresses": ["192.168.1.20"],
    }
    info, renames = zc.registered[0]
    assert renames is True   # another "nas" on the LAN: zeroconf picks a free name
    assert info.type == "_quipclipper._tcp.local." and info.port == 8896
    assert info.server == "nas.local."
    assert {k.decode(): v.decode() for k, v in info.properties.items()} == record["properties"]
    assert info.parsed_addresses() == ["192.168.1.20"]
    assert zc.addresses == ["192.168.1.20"]   # bound to the LAN interfaces only
    # advertise.json while it runs, gone after; unregistered and closed.
    assert seen["state"] == {"url": "http://nas.local:8896", "name": "nas"}
    assert not (s.state_dir / "advertise.json").exists()
    assert zc.unregistered == [info] and zc.closed


def test_the_url_is_qc_public_url_when_set(tmp_path):
    s = _settings(tmp_path, QC_PUBLIC_URL="https://quip.example/qc/")
    record, _, seen = _run(s, addresses=[], hostname="nas")
    assert record["properties"]["url"] == "https://quip.example/qc"
    assert record["port"] == 443   # QC_PUBLIC_URL's
    assert seen["state"]["url"] == "https://quip.example/qc"


def test_the_app_and_the_advertiser_share_one_server_id(tmp_path):
    s = _settings(tmp_path, QC_ADVERTISE_PORT="8896")
    first = pairing.server_id(s.state_dir)
    record, _, _ = _run(s, addresses=[], hostname="nas")
    assert record["properties"]["id"] == first


def test_advertising_is_off_unless_switched_on(tmp_path):
    s = Settings.from_env({"QC_STATE_DIR": str(tmp_path), "QC_ADVERTISE_PORT": "8896"})
    with pytest.raises(advertise.AdvertiseError, match="QC_ADVERTISE=1"):
        advertise.run(s, zeroconf_factory=FakeZeroconf, addresses=[])
    for off in ("0", "false", "no", ""):
        assert not Settings.from_env({"QC_ADVERTISE": off}).advertise
    assert Settings.from_env({"QC_ADVERTISE": "1"}).advertise


def test_a_port_is_needed(tmp_path):
    with pytest.raises(advertise.AdvertiseError, match="QC_ADVERTISE_PORT"):
        advertise.run(_settings(tmp_path), zeroconf_factory=FakeZeroconf, addresses=[])
    assert advertise.advertise_port(_settings(tmp_path), 9000) == 9000
    assert advertise.advertise_port(_settings(tmp_path, QC_ADVERTISE_PORT="8896")) == 8896
    assert advertise.advertise_port(_settings(tmp_path, QC_PUBLIC_URL="http://q:81")) == 81
    assert advertise.advertise_port(_settings(tmp_path, QC_PUBLIC_URL="http://q")) == 80


def test_canonical_url():
    assert advertise.canonical_url("", "nas", 8896) == "http://nas.local:8896"
    assert advertise.canonical_url("", "nas", 80) == "http://nas.local"
    assert advertise.canonical_url("https://q.example/", "nas", 1) == "https://q.example"


def test_names_become_single_labels():
    assert advertise.short_hostname("nas.example.com") == "nas"
    assert advertise.short_hostname("nas.local.") == "nas"
    assert advertise.instance_name("Den.NAS  box") == "Den-NAS box"
    long = advertise.instance_name("é" * 40)
    assert len(long.encode()) <= 63 and long == "é" * 31


def _adapter(name, *ips):
    return SimpleNamespace(nice_name=name, ips=[SimpleNamespace(ip=ip) for ip in ips])


def test_lan_addresses_skip_loopback_bridges_and_the_tailnet():
    adapters = [
        _adapter("lo", "127.0.0.1", ("::1", 0, 0)),
        _adapter("eth0", "192.168.1.20", ("fe80::1", 0, 2), ("2001:db8::20", 0, 0),
                 ("fd00::20", 0, 0)),
        _adapter("docker0", "172.17.0.1"),
        _adapter("br-1234", "172.18.0.1"),
        _adapter("veth99", "169.254.3.3"),
        _adapter("tailscale0", "100.101.102.103", ("fd7a:115c:a1e0::1", 0, 0)),
        _adapter("wlan0", "10.0.0.5", "100.64.0.9", "169.254.1.1"),
    ]
    assert advertise.lan_addresses(adapters) == [
        "192.168.1.20", "2001:db8::20", "fd00::20", "10.0.0.5"]


def test_quipclipper_web_advertise_refuses_when_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QC_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("QC_ADVERTISE", raising=False)
    assert web_main(["advertise", "--port", "8896"]) == 2
    assert "QC_ADVERTISE=1" in capsys.readouterr().err


def test_quipclipper_web_usage(capsys):
    assert web_main(["--help"]) == 0
    assert "advertise" in capsys.readouterr().out
    assert web_main(["nonsense"]) == 2


def test_the_app_reads_what_the_advertiser_wrote(tmp_path):
    from fastapi.testclient import TestClient

    from quipclipper_web.app import create_app

    s = _settings(tmp_path, QC_ADVERTISE_PORT="8896", QC_CLIPS_DIR=str(tmp_path / "clips"))
    (tmp_path / "clips").mkdir()
    stop = threading.Event()
    seen = {}

    def log(msg):
        if msg.startswith("announcing"):
            cfg = TestClient(create_app(s)).get("/api/config", headers={"host": "x:1"}).json()
            seen.update(cfg)
            stop.set()

    advertise.run(s, stop=stop, zeroconf_factory=FakeZeroconf, addresses=[],
                  hostname="nas", log=log)
    assert seen["link_server"] == "http://nas.local:8896"
    assert re.fullmatch(r"[0-9a-f]{32}", seen["server_id"])
