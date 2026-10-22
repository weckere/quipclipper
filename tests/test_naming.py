from pathlib import Path

from quipclipper.clip import ClipRange
from quipclipper.naming import (
    DEFAULT_CLIP_TEMPLATE,
    clean_title,
    clip_template_context,
    render_clip_template,
    resolve_template,
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


def _rng():
    return ClipRange(start=1678.4, end=1683.0)


TNG = Path("/shows/Star Trek - The Next Generation/Season 3/Star.Trek.TNG.S03E26.mkv")


def test_media_server_values_replace_the_parsed_ones():
    meta = {"title": "The Best of Both Worlds (1)", "show": "Star Trek: The Next Generation",
            "season": 3, "episode": 26, "year": 1990}
    ctx = clip_template_context(TNG, _rng(), "Mr. Worf, fire!", meta)
    assert render_clip_template("{show}/S{season}E{episode}_{title}_{year}", ctx) == (
        "Star_Trek_The_Next_Generation/S03E26_The_Best_of_Both_Worlds_1_1990")
    assert ctx["folder"] == "Star_Trek_-_The_Next_Generation"


def test_missing_or_odd_media_server_values_keep_the_parsed_ones():
    meta = {"title": "", "show": None, "season": True, "episode": "26", "year": 99999}
    ctx = clip_template_context(TNG, _rng(), "", meta)
    parsed = clip_template_context(TNG, _rng(), "")
    assert ctx == parsed
    assert (parsed["title"], parsed["show"], parsed["season"], parsed["episode"]) == (
        "Star_Trek_-_The_Next_Generation", "Star_Trek_-_The_Next_Generation", "03", "26")


def test_a_title_cant_add_folders():
    ctx = clip_template_context(TNG, _rng(), "", {"title": "../../etc/passwd"})
    assert render_clip_template("{title}", ctx) == "etcpasswd"


def test_the_default_names_a_clip_as_the_earlier_default_did():
    ctx = clip_template_context(TNG, _rng(), "Fire!", {"title": "Real Title", "show": "Show"})
    old = "{source}/{timestamp}_{cue}_{title}"
    assert resolve_template(old) == resolve_template(None) == resolve_template(" ") \
        == DEFAULT_CLIP_TEMPLATE
    assert render_clip_template(resolve_template(old), ctx) == (
        "Star.Trek.TNG.S03E26/00-27-58_Fire_Star_Trek_-_The_Next_Generation")
    assert resolve_template("{title}") == "{title}"


def test_a_range_with_no_line_names_cleanly():
    # A range dragged out on the waveform (or set with I / O) has no dialogue: the default
    # template drops {cue} and its separator, leaving neither "__" nor a trailing "_".
    ctx = clip_template_context(
        Path("/tv/Garth Marenghi's Darkplace/Season 1/Darkplace.S01E06.mkv"),
        ClipRange(start=353.2, end=401.0), "",
    )
    assert render_clip_template(DEFAULT_CLIP_TEMPLATE, ctx) == (
        "Darkplace.S01E06/00-05-53_Garth_Marenghi's_Darkplace"
    )
    for template in ("{cue}_{timestamp}", "{timestamp}_{cue}", "{timestamp}-{cue}-{end}"):
        name = render_clip_template(template, ctx)
        assert name in ("00-05-53", "00-05-53-00-06-41"), name
