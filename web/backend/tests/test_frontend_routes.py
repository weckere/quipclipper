"""Deep links (M18): every view has a hash route, and every route a view.

``app.js`` keeps a ``ROUTES`` table (route name -> the id of the view's ``<section>``) and each
view's section in ``index.html`` lists the routes that show it in ``data-route``. A view added
without a route can't be linked to or reached with Back; a route left pointing at a renamed
section opens nothing. Both fail here. The encoder is checked against the server's copy
(``deeplinks.py``) by reading the JavaScript's rules, since there's no JS runtime in the tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                    reason="web/frontend is not part of this source tree")

# The views: <section> elements directly inside <main> (the library, clips, bookmarks and
# the workspace).
_MAIN = re.compile(r"<main\b.*?</main>", re.S)
_SECTION = re.compile(r'^      <section\b([^>]*)>', re.M)
_ATTR = lambda name: re.compile(rf'\b{name}="([^"]*)"')  # noqa: E731


def _views() -> dict[str, set[str]]:
    """View section id -> the routes its data-route names."""
    main = _MAIN.search((FRONTEND / "index.html").read_text()).group(0)
    out = {}
    for attrs in _SECTION.findall(main):
        sid = _ATTR("id").search(attrs).group(1)
        route = _ATTR("data-route").search(attrs)
        out[sid] = set(route.group(1).split()) if route else set()
    return out


def _route_table() -> dict[str, str]:
    js = (FRONTEND / "app.js").read_text()
    block = re.search(r"const ROUTES = \{(.*?)\};", js, re.S).group(1)
    return dict(re.findall(r'(\w+): "([\w-]+)"', block))


@needs_frontend
def test_the_scanners_find_the_views_and_routes():
    assert set(_views()) == {"browser", "clips-browser", "bookmarks-browser", "item"}
    assert {"library", "item", "search", "bookmarks", "clips", "nowplaying"} <= set(_route_table())


@needs_frontend
def test_every_view_has_a_route():
    bare = sorted(v for v, routes in _views().items() if not routes)
    assert not bare, f"views with no data-route (no deep link, no Back): {bare}"


@needs_frontend
def test_every_route_has_a_view_and_they_agree():
    views, table = _views(), _route_table()
    for route, view in table.items():
        assert view in views, f"ROUTES.{route} names #{view}, which isn't a view in index.html"
        assert route in views[view], f'#{view} should list "{route}" in data-route'
    for view, routes in views.items():
        for route in routes:
            assert table.get(route) == view, f'#{view} lists route "{route}" but ROUTES says {table.get(route)}'


@needs_frontend
def test_the_encoder_matches_the_servers():
    """routeValue / routeTime in app.js and deeplinks.py must write the same links."""
    js = (FRONTEND / "app.js").read_text()
    assert ('const routeValue = (v) => encodeURIComponent(v).replace(/%2F/g, "/")'
            '.replace(/%3A/g, ":");') in js
    assert "const routeTime = (t) => String(+(+t).toFixed(3));" in js
    from quipclipper_web import deeplinks
    assert deeplinks._SAFE == "/:!*'()"  # encodeURIComponent's extras, plus / and :
