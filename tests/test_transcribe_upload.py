"""Transcribing one audio file with word timings (POST /api/transcribe, transcribe.file).

whisper.cpp is faked: a script that writes the ``-ojf`` JSON it would. The audio is a
tiny WAV made here, sample by sample, so nothing depends on timing or chance."""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import sys
import textwrap
import wave
from pathlib import Path

import pytest

from quipclipper import transcribe as tr

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def tok(text: str, a: int, b: int, **extra) -> dict:
    return {"text": text, "offsets": {"from": a, "to": b}, **extra}


def write_wav(path: Path, seconds: float, rate: int = 22050, channels: int = 2) -> Path:
    """A 440 Hz tone, the same samples every time."""
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) * channels
            for i in range(frames)))
    return path


# --- tokens into words -----------------------------------------------------------------


def test_tokens_merge_into_words():
    tokens = [tok("[_BEG_]", 0, 0), tok(" Hail", 500, 700), tok("ing", 700, 900),
              tok(" frequencies", 900, 1300), tok(" open", 1300, 1450), tok(",", 1450, 1460),
              tok(" sir", 1500, 1700), tok(".", 1700, 1710), tok("[_TT_85]", 1710, 1710),
              tok("<|endoftext|>", 1710, 1710)]
    assert tr.words_from_tokens(tokens, offset=10.0) == [
        {"start": 10.5, "end": 10.9, "text": "Hailing"},
        {"start": 10.9, "end": 11.3, "text": "frequencies"},
        {"start": 11.3, "end": 11.46, "text": "open,"},
        {"start": 11.5, "end": 11.71, "text": "sir."}]


def test_leading_punctuation_joins_the_word_after_and_times_never_run_back():
    tokens = [tok(' "', 0, 100), tok("Run", 100, 300), tok(" now", 250, 200),
              tok(" -", 400, 450), tok(" go", 500, 600)]
    assert tr.words_from_tokens(tokens) == [
        {"start": 0.0, "end": 0.3, "text": '"Run'},
        {"start": 0.25, "end": 0.45, "text": "now -"},
        {"start": 0.5, "end": 0.6, "text": "go"}]


def test_a_token_without_times_is_left_out_and_no_tokens_means_no_words():
    assert tr.words_from_tokens([{"text": " hi"}, tok(" there", 100, 200)]) == [
        {"start": 0.1, "end": 0.2, "text": "there"}]
    assert tr.words_from_tokens([]) == []


def test_parse_whisper_words_keeps_what_parse_whisper_json_keeps():
    data = {"transcription": [
        {"offsets": {"from": 500, "to": 1500}, "text": " Hailing frequencies open.",
         "tokens": [tok(" Hail", 450, 700), tok("ing", 700, 900),
                    tok(" frequencies", 900, 1300), tok(" open", 1300, 1500),
                    tok(".", 1500, 1600)]},
        {"offsets": {"from": 1600, "to": 3000}, "text": " [Music]", "tokens": []},
        {"offsets": {"from": 3000, "to": 4000}, "text": " Thanks for watching!",
         "tokens": [tok(" Thanks", 3000, 3500)]},
        {"offsets": {"from": 9000, "to": 9500}, "text": " Outside.",
         "tokens": [tok(" Outside", 9000, 9500)]}]}
    cues = tr.parse_whisper_words(data, offset=2.0, keep=(0.0, 8.0))
    assert [c["text"] for c in cues] == [c.text for c in
                                         tr.parse_whisper_json(data, offset=2.0, keep=(0.0, 8.0))]
    assert cues == [{"start": 2.45, "end": 3.6, "text": "Hailing frequencies open.",
                     "machine": True,
                     "words": [{"start": 2.45, "end": 2.9, "text": "Hailing"},
                               {"start": 2.9, "end": 3.3, "text": "frequencies"},
                               {"start": 3.3, "end": 3.6, "text": "open."}]}]


def test_whisper_argv_full_json():
    cfg = tr.WhisperConfig(binary="/x/whisper-cli", models={"small.en": "/m.bin"})
    argv = tr.whisper_argv(cfg, "/m.bin", "/a.wav", "/out", full=True)
    assert "-ojf" in argv and "-oj" not in argv and "-dtw" not in argv
    assert "-oj" in tr.whisper_argv(cfg, "/m.bin", "/a.wav", "/out")


# --- limits and languages ------------------------------------------------------------


def test_upload_limits_from_env():
    assert tr.UploadLimits.from_env({}).describe() == {"max_bytes": 64 * 1024 * 1024,
                                                       "max_seconds": 300.0}
    small = tr.UploadLimits.from_env({"QC_TRANSCRIBE_UPLOAD_MAX_MB": "0.5",
                                      "QC_TRANSCRIBE_UPLOAD_MAX_SECONDS": "30"})
    assert small.describe() == {"max_bytes": 524288, "max_seconds": 30.0}
    assert tr.UploadLimits.from_env({"QC_TRANSCRIBE_UPLOAD_MAX_MB": "0"}).describe() is False
    assert tr.UploadLimits.from_env({"QC_TRANSCRIBE_UPLOAD_MAX_SECONDS": "0"}).enabled is False
    assert tr.UploadLimits.from_env({"QC_TRANSCRIBE_UPLOAD_MAX_MB": "lots"}).max_bytes == \
        64 * 1024 * 1024


def test_check_language():
    cfg = tr.WhisperConfig(models={"small.en": "/m.bin", "small": "/s.bin"})
    assert tr.check_language(cfg, "small.en", None) == "en"
    assert tr.check_language(cfg, "small.en", "EN") == "en"
    assert tr.check_language(cfg, "small", "de") == "de"
    assert tr.check_language(cfg, "small", "auto") == "auto"
    with pytest.raises(ValueError, match="English only"):
        tr.check_language(cfg, "small.en", "de")
    with pytest.raises(ValueError, match="language code"):
        tr.check_language(cfg, "small", "en; rm")


# --- decoding an upload --------------------------------------------------------------


@needs_ffmpeg
def test_decode_upload_measures_and_converts(tmp_path):
    src = write_wav(tmp_path / "in.wav", 1.5)
    out = tmp_path / "out.wav"
    assert tr.decode_upload(src, out, max_seconds=10) == 1.5
    with wave.open(str(out)) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, tr.SAMPLE_RATE)


@needs_ffmpeg
def test_decode_upload_refuses_what_is_not_audio_or_too_long(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not audio at all " * 64)
    with pytest.raises(tr.UnreadableAudio):
        tr.decode_upload(junk, tmp_path / "a.wav", max_seconds=10)
    src = write_wav(tmp_path / "in.wav", 2.0)
    with pytest.raises(tr.TooLong) as err:
        tr.decode_upload(src, tmp_path / "b.wav", max_seconds=1.0)
    assert err.value.limit == 1.0 and err.value.seconds > 1.0
    # Only a second past the cap is decoded, however long the file is.
    assert err.value.seconds <= 2.0


# --- the whole file, against a fake whisper-cli ----------------------------------------

# Hears "Hailing frequencies open." 0.5-1.5 s into whatever piece it is given, with tokens,
# and a "[Music]" it should drop; logs its argv; FAKE_WHISPER_SLEEP makes it slow.
FAKE_WHISPER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    a = sys.argv[1:]
    log = os.environ.get("FAKE_WHISPER_LOG")
    if log:
        open(log, "a").write(json.dumps(a) + "\\n")
    time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP") or 0))
    t = lambda s, f, e: {{"text": s, "offsets": {{"from": f, "to": e}}, "p": 0.9}}
    segs = [{{"offsets": {{"from": 500, "to": 1500}}, "text": " Hailing frequencies open.",
              "tokens": [t("[_BEG_]", 0, 0), t(" Hail", 500, 700), t("ing", 700, 900),
                         t(" frequencies", 900, 1300), t(" open", 1300, 1450),
                         t(".", 1450, 1500)]}},
            {{"offsets": {{"from": 1500, "to": 2000}}, "text": " [Music]", "tokens": []}}]
    out = {{"result": {{"language": "en"}}, "transcription": segs}}
    json.dump(out, open(a[a.index("-of") + 1] + ".json", "w"))
''')


@pytest.fixture
def fake_whisper(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "whisper-cli"
    exe.parent.mkdir()
    exe.write_text(FAKE_WHISPER.format(python=sys.executable))
    exe.chmod(0o755)
    model = tmp_path / "ggml-small.en.bin"
    model.write_bytes(b"model")
    log = tmp_path / "whisper.log"
    monkeypatch.setenv("FAKE_WHISPER_LOG", str(log))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    cfg = tr.WhisperConfig.from_env({"QC_WHISPER_BIN": str(exe), "QC_WHISPER_MODEL": str(model)})
    return cfg, log


@needs_ffmpeg
def test_transcribe_file_returns_cues_with_words(tmp_path, fake_whisper):
    cfg, log = fake_whisper
    wav = tmp_path / "a.wav"
    duration = tr.decode_upload(write_wav(tmp_path / "in.wav", 3.0), wav, max_seconds=10)
    seen = []
    got = tr.transcribe_file(wav, duration, cfg=cfg, workdir=tmp_path,
                             progress=lambda d, t: seen.append((d, t)))
    assert got == {
        "cues": [{"start": 0.5, "end": 1.5, "text": "Hailing frequencies open.",
                  "machine": True,
                  "words": [{"start": 0.5, "end": 0.9, "text": "Hailing"},
                            {"start": 0.9, "end": 1.3, "text": "frequencies"},
                            {"start": 1.3, "end": 1.5, "text": "open."}]}],
        "cues_source": {"kind": "machine", "engine": "whisper.cpp", "model": "small.en",
                        "lang": "en"},
        "duration": 3.0}
    assert seen == [(0.0, 3.0), (3.0, 3.0)]
    [argv] = [json.loads(x) for x in log.read_text().splitlines()]
    assert "-ojf" in argv and "--vad" not in argv and argv[argv.index("-l") + 1] == "en"
    # Its pieces were heard in the work folder and are gone.
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("qc-stt-")) == []


@needs_ffmpeg
def test_transcribe_file_in_chunks_keeps_times_inside_the_file(tmp_path, fake_whisper,
                                                                monkeypatch):
    cfg, log = fake_whisper
    monkeypatch.setattr(tr, "CHUNK", 4.0)
    wav = tmp_path / "a.wav"
    duration = tr.decode_upload(write_wav(tmp_path / "in.wav", 9.0), wav, max_seconds=10)
    got = tr.transcribe_file(wav, duration, cfg=cfg, workdir=tmp_path)
    # Chunks (0,4) and (4,9), each heard from CONTEXT before: the fake's line lands 0.5 s
    # into each piece, so at 0.5 (kept by the first) and 2.5 (the second's context: dropped).
    assert len(log.read_text().splitlines()) == 2
    assert [(c["start"], c["words"][0]["start"]) for c in got["cues"]] == [(0.5, 0.5)]
    assert all(0 <= w["start"] <= w["end"] <= duration
               for c in got["cues"] for w in c["words"])


@needs_ffmpeg
def test_transcribe_file_cancelled(tmp_path, fake_whisper):
    cfg, _ = fake_whisper
    wav = tmp_path / "a.wav"
    duration = tr.decode_upload(write_wav(tmp_path / "in.wav", 1.0), wav, max_seconds=10)
    with pytest.raises(tr.Cancelled):
        tr.transcribe_file(wav, duration, cfg=cfg, workdir=tmp_path, cancelled=lambda: True)


def test_transcribe_file_refuses_a_model_or_language_it_lacks(tmp_path, fake_whisper):
    cfg, _ = fake_whisper
    with pytest.raises(ValueError, match="No whisper model"):
        tr.transcribe_file(tmp_path / "a.wav", 1.0, cfg=cfg, model="large-v3")
    with pytest.raises(ValueError, match="English only"):
        tr.transcribe_file(tmp_path / "a.wav", 1.0, cfg=cfg, lang="fr")


# --- word times moved onto the audio's pauses ------------------------------------------


def write_bursts(path: Path, seconds: float, bursts: list[tuple[float, float]], *,
                 rate: int = 16000, under: int = 0, level: int = 8000) -> Path:
    """A 16 kHz mono WAV: a 220 Hz tone at ``level`` inside each burst ``(start, end)``,
    and a 97 Hz tone at ``under`` (0: digital silence) everywhere else. The same samples
    every time."""
    def sample(i: int) -> int:
        t = i / rate
        if any(a <= t < b for a, b in bursts):
            return int(level * math.sin(2 * math.pi * 220 * t))
        return int(under * math.sin(2 * math.pi * 97 * t))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", sample(i)) for i in range(int(seconds * rate))))
    return path


def seg(text: str, *words: tuple[str, float, float]) -> dict:
    """One whisper segment of ``words`` (text, start, end in seconds), as ``-ojf`` lists it."""
    toks = [tok(" " + t, round(a * 1000), round(b * 1000)) for t, a, b in words]
    return {"offsets": {"from": toks[0]["offsets"]["from"], "to": toks[-1]["offsets"]["to"]},
            "text": " " + text, "tokens": toks}


def heard(*segs: dict) -> list[dict]:
    return tr.parse_whisper_words({"transcription": list(segs)})


def spans(cues: list[dict]) -> list[tuple[str, float, float]]:
    return [(w["text"], w["start"], w["end"]) for c in cues for w in c["words"]]


def assert_in_order(cues: list[dict], duration: float) -> None:
    words = [w for c in cues for w in c["words"]]
    assert all(0 <= w["start"] <= w["end"] <= duration for w in words)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))  # never overlap
    for c in cues:
        assert (c["start"], c["end"]) == (c["words"][0]["start"], c["words"][-1]["end"])


def near(got: float, want: float) -> bool:
    return abs(got - want) <= 0.02  # one 10 ms energy frame either way, plus smoothing


def test_pauses_are_the_quiet_stretches_between_bursts(tmp_path):
    wav = write_bursts(tmp_path / "a.wav", 4.0, [(0.5, 1.5), (2.3, 3.0), (3.1, 3.6)])
    got = tr.pauses(wav, 4.0)
    # 0-0.5, 1.5-2.3 and 3.6-4.0; the 0.1 s between the last two bursts is too short.
    assert [(near(a, x), near(b, y)) for (a, b), (x, y) in
            zip(got, [(0.0, 0.5), (1.5, 2.3), (3.6, 4.0)])] == [(True, True)] * 3
    assert len(got) == 3


def test_a_word_after_a_pause_starts_at_the_onset(tmp_path):
    # Speech 0.5-1.5 and 2.3-3.0; whisper glues "open" to the end of "frequencies".
    wav = write_bursts(tmp_path / "a.wav", 4.0, [(0.5, 1.5), (2.3, 3.0)])
    cues = heard(seg("Hailing frequencies", ("Hailing", 0.5, 0.9), ("frequencies", 0.9, 1.5)),
                 seg("open.", ("open.", 1.5, 2.9)))
    got = tr.refine_words(cues, wav, 4.0)
    (_, s1, e1), (_, s2, e2), (_, s3, e3) = spans(got)
    assert near(s1, 0.5) and near(e1, 0.9) and e1 == s2  # the words before it stay
    assert near(e2, 1.5) and near(s3, 2.3)
    assert near(e3, 3.0)  # and runs on to where the sound stops
    assert_in_order(got, 4.0)


def test_words_squeezed_into_the_silence_before_a_line_move_onto_it(tmp_path):
    # The line starts 1.1 s in; whisper puts its first three words in the silence before it
    # (as it did with "How am I" on real film dialogue) and the rest where they belong.
    wav = write_bursts(tmp_path / "a.wav", 3.0, [(1.1, 2.5)])
    cues = heard(seg("How am I supposed to", ("How", 0.0, 0.3), ("am", 0.3, 0.5),
                     ("I", 0.5, 0.6), ("supposed", 1.15, 1.9), ("to", 1.9, 2.5)))
    got = tr.refine_words(cues, wav, 3.0)
    words = spans(got)
    assert near(words[0][1], 1.1) and near(got[0]["start"], 1.1)
    assert [w[0] for w in words] == ["How", "am", "I", "supposed", "to"]
    assert near(words[-1][2], 2.5)
    assert_in_order(got, 3.0)


def test_a_phrase_heard_early_moves_to_its_pause_and_ends_stop_at_the_next(tmp_path):
    # Two lines with a pause 3.0-3.9 between them and another at 6.0-6.4; whisper puts the
    # whole second line 0.8 s early, its first word still in the first line's sound.
    wav = write_bursts(tmp_path / "a.wav", 8.0, [(0.5, 3.0), (3.9, 6.0), (6.4, 7.5)])
    cues = heard(seg("load this on a wagon,", ("load", 0.5, 1.2), ("this", 1.2, 1.8),
                     ("on", 1.8, 2.2), ("a", 2.2, 2.4), ("wagon,", 2.4, 2.9)),
                 seg("and float it across the river", ("and", 3.1, 3.4), ("float", 3.4, 3.9),
                     ("it", 3.9, 4.1), ("across", 4.1, 4.6), ("the", 4.6, 4.8),
                     ("river", 4.8, 5.6)),
                 seg("and over.", ("and", 6.0, 6.2), ("over.", 6.2, 7.4)))
    got = tr.refine_words(cues, wav, 8.0)
    words = spans(got)
    assert near(words[4][2], 3.0)  # "wagon," runs on to where the sound stops
    assert near(words[5][1], 3.9)  # "and" starts where the second line does
    assert near(words[10][2], 6.0) and near(words[11][1], 6.4)
    assert near(got[1]["start"], 3.9) and near(got[1]["end"], 6.0)
    assert_in_order(got, 8.0)


def test_nothing_moves_where_the_audio_has_no_clear_pause(tmp_path):
    cues = heard(seg("Hailing frequencies", ("Hailing", 0.5, 0.9), ("frequencies", 0.9, 1.5)),
                 seg("open.", ("open.", 1.5, 2.9)))
    want = spans(cues)
    # Music or noise under the gap, only 10 dB below the "speech": not a clear pause.
    noisy = write_bursts(tmp_path / "noisy.wav", 4.0, [(0.5, 1.5), (2.3, 3.0)], under=2500)
    # One steady sound all through.
    steady = write_bursts(tmp_path / "steady.wav", 4.0, [(0.0, 4.0)])
    silent = write_bursts(tmp_path / "silent.wav", 4.0, [])
    for wav in (noisy, steady, silent):
        assert tr.pauses(wav, 4.0) == [] or wav == silent
        assert spans(tr.refine_words(json.loads(json.dumps(cues)), wav, 4.0)) == want


def test_whisper_late_start_is_not_moved_earlier_and_order_holds(tmp_path):
    # A pause that ends well before whisper's start of the next word doesn't pull it back;
    # words at one instant and words past the end of the file stay in order, inside it.
    wav = write_bursts(tmp_path / "a.wav", 3.0, [(0.2, 0.8), (1.2, 2.9)])
    cues = heard(seg("Yes.", ("Yes.", 0.2, 0.8)),
                 seg("Two words now", ("Two", 1.8, 1.8), ("words", 1.8, 1.8),
                     ("now", 1.8, 3.4)))
    got = tr.refine_words(cues, wav, 3.0)
    words = spans(got)
    assert words[1][1] >= 1.8 - tr.START_EARLY
    assert_in_order(got, 3.0)


def test_cues_without_words_are_left_alone(tmp_path):
    wav = write_bursts(tmp_path / "a.wav", 2.0, [(1.0, 1.5)])
    cues = [{"start": 0.0, "end": 1.5, "text": "Hi.", "machine": True, "words": []}]
    assert tr.refine_words(json.loads(json.dumps(cues)), wav, 2.0) == cues


@needs_ffmpeg
def test_transcribe_file_moves_words_onto_the_audio(tmp_path, fake_whisper):
    # The fake hears "Hailing frequencies open." at 0.5-1.5; the sound is at 0.8-1.6.
    cfg, _ = fake_whisper
    wav = tmp_path / "a.wav"
    duration = tr.decode_upload(write_bursts(tmp_path / "in.wav", 3.0, [(0.8, 1.6)]), wav,
                                max_seconds=10)
    [cue] = tr.transcribe_file(wav, duration, cfg=cfg, workdir=tmp_path)["cues"]
    assert near(cue["start"], 0.8) and near(cue["words"][0]["start"], 0.8)
    assert near(cue["end"], 1.6) and [w["text"] for w in cue["words"]] == [
        "Hailing", "frequencies", "open."]
    assert_in_order([cue], duration)
