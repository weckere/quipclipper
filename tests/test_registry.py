import re
from pathlib import Path

from quipclipper import commands as reg

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_ids_and_shortcuts_are_unique_and_well_formed():
    ids = [c.id for c in reg.COMMANDS]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z]+\.[a-z-]+", i) for i in ids)
    shortcuts = [c.shortcut for c in reg.COMMANDS if c.shortcut]
    assert len(shortcuts) == len(set(shortcuts))
    assert not set(shortcuts) & set(ids)


def test_path_placeholders_match_path_params():
    for c in reg.COMMANDS:
        if not c.path:
            assert c.mode == "client", c.id
            continue
        placeholders = set(re.findall(r"\{(\w+)\}", c.path))
        assert placeholders == {p.name for p in c.params if p.where == reg.PATH}, c.id


def test_param_names_unique_and_mains_first():
    for c in reg.COMMANDS:
        names = [p.name for p in c.params]
        assert len(names) == len(set(names)), c.id
        mains = [p for p in c.params if p.main]
        # A required positional can't follow an optional one.
        assert all(p.required for p in mains[:-1]) or len(mains) < 2, c.id


def test_docs_cli_md_is_current():
    expected = (DOCS / "CLI.head.md").read_text() + reg.render_markdown()
    actual = (DOCS / "CLI.md").read_text()
    assert actual == expected, (
        "docs/CLI.md is stale: regenerate it with "
        "`python -m quipclipper.commands > docs/CLI.md`")
