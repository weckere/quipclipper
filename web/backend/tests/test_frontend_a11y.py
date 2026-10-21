"""Every control has hover text and an accessible name (M16).

A ``title`` tells a mouse user what a control does before they click it; an accessible name is
what a screen reader says for it. Both are easy to forget on a new button, and nothing fails when
one is missing. This test does:

* every ``button``, ``input``, ``select``, ``a`` and ``[role=button]`` in ``index.html`` has a
  ``title``, or sits in a ``<label>`` that has one (the browser shows the label's), or carries
  ``data-notitle="<why>"``;
* every one of them has an accessible name: ``aria-label``, ``aria-labelledby``, a ``<label>``,
  or text that isn't hidden from assistive technology;
* the controls ``app.js`` builds follow the same rules: its markup templates carry a ``title``,
  glyph-only buttons carry an ``aria-label``, and ``document.createElement`` makes no control
  outside the ``el()`` helper, whose callers pass a ``title``.

``title`` never carries the only copy of anything: touch and keyboard users don't see it, so
what it says is also in the label, the visible text or the help next to the control.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                    reason="web/frontend is not part of this source tree")

CONTROL_TAGS = {"button", "input", "select", "a", "textarea"}
VOID = {"input", "br", "img", "meta", "link", "hr", "source", "track", "col", "area", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def text(self) -> str:
        """Text content, skipping subtrees hidden from assistive technology."""
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.attrs.get("aria-hidden") != "true" and c.tag not in ("svg", "script", "style"):
                out.append(c.text())
        return " ".join(out)


class Tree(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.all: list[Node] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        self.all.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        self.all.append(node)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _is_control(n: Node) -> bool:
    if n.tag == "input" and n.attrs.get("type") == "hidden":
        return False
    if n.tag == "a" and "href" not in n.attrs:
        return False
    return n.tag in CONTROL_TAGS or n.attrs.get("role") in ("button", "slider", "link")


def _enclosing_label(n: Node) -> Node | None:
    return next((a for a in n.ancestors() if a.tag == "label"), None)


def _has_hover_text(n: Node) -> bool:
    if n.attrs.get("title") or n.attrs.get("data-notitle"):
        return True
    label = _enclosing_label(n)
    return bool(label is not None and label.attrs.get("title"))


def _words(s: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", s or ""))


def _accessible_name(n: Node, tree: Tree) -> str:
    a = n.attrs
    if a.get("aria-labelledby"):
        by_id = {x.attrs.get("id"): x for x in tree.all}
        return " ".join(by_id[i].text() for i in a["aria-labelledby"].split() if i in by_id)
    if a.get("aria-label"):
        return a["aria-label"]
    if n.tag in ("input", "select", "textarea"):
        if a.get("id"):
            for lab in tree.all:
                if lab.tag == "label" and lab.attrs.get("for") == a["id"]:
                    return lab.text()
        label = _enclosing_label(n)
        return label.text() if label is not None else ""
    return n.text()


def _html_tree() -> Tree:
    return Tree((FRONTEND / "index.html").read_text())


def test_tree_helpers():
    t = Tree('<label title="t"><input id="a" type="checkbox"> Pick</label>'
             '<button><span aria-hidden="true">✕</span></button><button aria-label="Close">✕</button>'
             '<label for="s">Size</label><select id="s"></select><a href="#x">Go</a>')
    controls = [n for n in t.all if _is_control(n)]
    assert [n.tag for n in controls] == ["input", "button", "button", "select", "a"]
    names = [_accessible_name(n, t).strip() for n in controls]
    assert names == ["Pick", "", "Close", "Size", "Go"]
    assert [_has_hover_text(n) for n in controls] == [True, False, False, False, False]


@needs_frontend
def test_every_control_in_index_html_has_hover_text():
    tree = _html_tree()
    missing = [_describe(n) for n in tree.all if _is_control(n) and not _has_hover_text(n)]
    assert not missing, f"controls with no title (or data-notitle reason): {missing}"


@needs_frontend
def test_every_control_in_index_html_has_an_accessible_name():
    tree = _html_tree()
    nameless = [_describe(n) for n in tree.all
                if _is_control(n) and not _words(_accessible_name(n, tree))]
    assert not nameless, f"controls a screen reader can't name: {nameless}"


def _describe(n: Node) -> str:
    a = n.attrs
    return f"<{n.tag}" + "".join(f' {k}="{a[k]}"' for k in ("id", "class") if a.get(k)) + ">"


# --- app.js ------------------------------------------------------------------------------

# A control tag inside a template string: <button ...>, <input ...>, <select ...>, <a href ...>.
_TEMPLATE_CONTROL = re.compile(r"<(button|input|select|a)\b([^>]*)>", re.S)
_ROLE_BUTTON = re.compile(r"<\w+\b([^>]*\brole=\"button\"[^>]*)>", re.S)
_TEMPLATE_BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
_CREATE = re.compile(r"""document\.createElement\(\s*["'](button|input|select|a|textarea)["']\s*\)""")
_EL_CALL = re.compile(r"""\bel\(\s*["'](button|input|select|a|textarea)["']\s*,\s*\{(.*?)\}\s*\)""", re.S)


def _js() -> str:
    """app.js without its comments (a comment may mention a <select> in passing)."""
    js = (FRONTEND / "app.js").read_text()
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))


def test_js_scanners():
    js = ('x.innerHTML = `<button class="a" title="Do">Go</button><a href="${u}">dl</a>`;'
          ' b = el("button", { title: "Hi", text: "x" }); c = el("input", { type: "checkbox" });'
          ' d = `<button aria-label="Close">✕</button><button>✕</button>`;')
    tags = [(m.group(1), "title=" in m.group(2)) for m in _TEMPLATE_CONTROL.finditer(js)]
    assert tags == [("button", True), ("a", False), ("button", False), ("button", False)]
    assert [("title" in m.group(2)) for m in _EL_CALL.finditer(js)] == [True, False]
    glyph_only = [m.group(2) for m in _TEMPLATE_BUTTON.finditer(js)
                  if "aria-label" not in m.group(1) and not _template_words(m.group(2))]
    assert glyph_only == ["✕"]


def _template_words(inner: str) -> bool:
    """Whether a template button's content gives it a name: words, or an interpolation."""
    inner = re.sub(r'<(\w+)[^>]*aria-hidden="true"[^>]*>.*?</\1>', "", inner, flags=re.S)
    inner = re.sub(r"<[^>]+>", "", inner)
    return "${" in inner or _words(inner)


@needs_frontend
def test_every_control_app_js_templates_has_hover_text():
    js = _js()
    missing = [m.group(0)[:90] for m in _TEMPLATE_CONTROL.finditer(js)
               if "title=" not in m.group(2) and "data-notitle=" not in m.group(2)]
    missing += [m.group(0)[:90] for m in _ROLE_BUTTON.finditer(js) if "title=" not in m.group(1)]
    assert not missing, f"controls built in app.js templates with no title: {missing}"


@needs_frontend
def test_glyph_only_buttons_in_app_js_have_an_aria_label():
    bad = [m.group(0)[:90] for m in _TEMPLATE_BUTTON.finditer(_js())
           if "aria-label" not in m.group(1) and not _template_words(m.group(2))]
    assert not bad, f"buttons whose only content is a glyph need an aria-label: {bad}"


@needs_frontend
def test_app_js_builds_controls_with_the_el_helper():
    """``document.createElement("button")`` and friends skip the title; ``el()`` asks for one.
    A line may opt out with a ``// notitle: <why>`` comment (an element never put on the page)."""
    js = _js()
    raw = (FRONTEND / "app.js").read_text()
    stray = [line.strip() for line in raw.splitlines()
             if _CREATE.search(line) and "// notitle:" not in line]
    assert not stray, f"use el(tag, {{ title, ... }}) to build controls: {stray}"
    untitled = [m.group(0)[:90] for m in _EL_CALL.finditer(js)
                if not re.search(r"\btitle\s*:", m.group(2)) and "notitle" not in m.group(2)]
    assert not untitled, f"el() calls that build a control without a title: {untitled}"


@needs_frontend
def test_make_activatable_rows_get_hover_text():
    """A row made keyboard-operable by makeActivatable(el, title) says what activating it does."""
    js = _js()
    calls = re.findall(r"makeActivatable\(([^()]*)\)", js)
    assert calls, "app.js no longer calls makeActivatable; move this guard with it"
    bad = [c for c in calls if "," not in c]
    assert not bad, f"makeActivatable calls without a title: {bad}"


@needs_frontend
def test_every_image_has_alt_text_and_templates_load_lazily():
    """Jellyfin artwork (M18): each <img> says what it shows (alt), and the ones app.js builds
    for list rows load lazily, so a long folder doesn't fetch every poster at once."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    js = _js()
    imgs = re.findall(r"<img\b[^>]*>", html) + re.findall(r"<img\b[^`]*?>", js)
    assert imgs, "no <img> in the frontend; move this guard with the artwork"
    assert not [i for i in imgs if not re.search(r"\balt=", i)], "an <img> without alt"
    templates = re.findall(r"<img\b[^`]*?>", js)
    assert not [i for i in templates if 'loading="lazy"' not in i], "a row image not lazy"
    # The header's image is filled in by script: it gets its alt text there.
    assert re.search(r'\$\("item-art"\)', js) and re.search(r"img\.alt\s*=\s*`", js)
