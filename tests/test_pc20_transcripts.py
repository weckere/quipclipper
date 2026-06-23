"""Podcasting 2.0 transcript shapes + speaker attribution.

Fixtures in fixtures/pc20/ are well-formed spec/real-world shapes (not all
time-aligned to one audio file) — they exercise the parsers and speaker handling.
"""

from pathlib import Path

from quipclipper.subtitles import load_subtitles

PC20 = Path(__file__).parent / "fixtures" / "pc20"


def test_podcast_namespace_json_speakers():
    cues = load_subtitles(PC20 / "transcript.podcast.json")
    assert [c.speaker for c in cues] == ["Adam", "Dave", "Adam"]
    assert cues[0].text == "Podcasting 2.0 for the win."
    assert cues[0].start == 0.5 and cues[0].end == 3.2


def test_whisper_json_has_no_speakers():
    cues = load_subtitles(PC20 / "transcript.whisper.json")
    assert len(cues) == 3
    assert all(c.speaker is None for c in cues)
    assert cues[0].text == "Welcome to the show."


def test_vtt_voice_tags_become_speakers():
    cues = load_subtitles(PC20 / "transcript.vtt")
    assert [c.speaker for c in cues] == ["Chris", "Wes", "Brent", "Chris", None]
    # The voice tag is stripped from the visible text.
    assert cues[0].text == "Hello, friends, and welcome back to the show."
    # A styled tag (<v.loud Brent>) still yields the speaker.
    assert cues[2].speaker == "Brent"


def test_srt_recurring_name_prefix_is_a_speaker():
    cues = load_subtitles(PC20 / "transcript.speakers.srt")
    assert [c.speaker for c in cues] == ["HOST", "GUEST", "HOST", "GUEST"]
    # The recurring "HOST:"/"GUEST:" prefix is moved into speaker, not the text…
    assert cues[0].text == "Welcome to the recurring-speaker test."
    # …but a non-recurring "Wait:" inside a line is left in the dialogue text.
    assert cues[1].speaker == "GUEST"
    assert cues[1].text == "Wait: I can explain the namespace."
