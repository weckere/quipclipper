"""``pair list`` and ``clients list`` say who asked to pair, as the web app's banner does."""

from __future__ import annotations

from quipclipper import cli


def test_pair_list_names_the_tailnet_user_and_scopes(capsys):
    cli._fmt_pairs({"requests": [{
        "id": "r1", "name": "Jellyfin", "host": "tv", "code": "7F3K", "address": "100.64.0.9",
        "user": "jo@example.com", "user_name": "Jo Example", "scopes": ["index.file"],
        "expires_at": "2026-10-26T12:00:00Z"}]})
    out = capsys.readouterr().out
    assert "7F3K  Jellyfin on tv  from Jo Example (jo@example.com), 100.64.0.9" in out
    assert "asks for scopes index.file" in out


def test_clients_list_says_who_paired_each_app(capsys):
    cli._fmt_clients({"clients": [
        {"id": "a", "name": "LeadStinger", "host": "mac", "address": "100.64.0.9",
         "user": "jo@example.com", "user_name": "jo", "scopes": None,
         "created": "2026-10-26T10:00:00Z", "last_used": None},
        # Paired before the server kept who asked: nothing to say.
        {"id": "b", "name": "Old", "host": "", "scopes": None,
         "created": "2026-10-20T10:00:00Z", "last_used": None},
    ]})
    a, b = capsys.readouterr().out.splitlines()
    assert "full access  paired 2026-10-26T10:00:00Z from jo@example.com, 100.64.0.9" in a
    assert " from " not in b
