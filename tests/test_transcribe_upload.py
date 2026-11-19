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
