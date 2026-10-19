from pathlib import Path

from quipclipper.clip import ClipRange
from quipclipper.naming import (
    DEFAULT_CLIP_TEMPLATE,
    clean_title,
    clip_template_context,
    render_clip_template,
)


def test_default_template_matches_web_layout():
    ctx = clip_template_context(
        Path("/movies/The Sandlot (1993)/The.Sandlot.1993.1080p.mkv"),
        ClipRange(start=1678.4, end=1683.0), "You're killing me, Smalls!",
    )
    assert render_clip_template(DEFAULT_CLIP_TEMPLATE, ctx) == (
        "The.Sandlot.1993.1080p/00-27-58_You're_killing_me_Smalls_The_Sandlot_1993"
    )


def test_render_drops_traversal_and_empty_tokens():
    ctx = {"cue": "", "title": "x"}
    assert render_clip_template("../{cue}/{title}__{nope}", ctx) == "x"


def test_clean_title_steps_past_season():
    assert clean_title(Path("/shows/TNG/Season 4/ep.mkv")) == "TNG"
