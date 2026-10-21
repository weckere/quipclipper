"""Peak and snap maths (quipclipper.waveform) on synthetic signals."""

from __future__ import annotations

import math
import random
import shutil
import subprocess
from array import array

import pytest

from quipclipper import waveform

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

RATE = 48000


def sine(freq: float, seconds: float, rate: int = RATE, amp: float = 0.5,
         phase: float = 0.0) -> array:
    n = round(seconds * rate)
    return array("h", (round(amp * 32767 * math.sin(2 * math.pi * freq * i / rate + phase))
                       for i in range(n)))


def noise(n: int, seed: int = 1) -> array:
    rnd = random.Random(seed)
    return array("h", (rnd.randint(-32768, 32767) for _ in range(n)))


def brute(samples, a: int, b: int) -> tuple[int, int]:
    s = samples[a:b]
    return min(s), max(s)


# --- buckets and the pyramid ------------------------------------------------------


def test_minmax_buckets_short_last_bucket():
    mins, maxs = waveform.minmax_buckets(array("h", [1, -2, 3, 4, -5]), 2)
    assert list(mins) == [-2, 3, -5]
    assert list(maxs) == [1, 4, -5]


def test_pyramid_levels_shrink_by_factor_to_one():
    pyr = waveform.PeakPyramid.from_samples(noise(10_000), 10, RATE)
    sizes = [len(lo) for lo, _ in pyr.levels]
    assert sizes[0] == 1000
    assert sizes[-1] == 1
    assert all(b == math.ceil(a / waveform.FACTOR) for a, b in zip(sizes, sizes[1:]))


def test_pyramid_peak_is_the_largest_magnitude():
    data = noise(5000)
    pyr = waveform.PeakPyramid.from_samples(data, 16, RATE)
    assert pyr.peak() == max(abs(min(data)), abs(max(data)))
    assert waveform.PeakPyramid.from_samples(array("h", [0] * 64), 16, RATE).peak() == 0


@pytest.mark.parametrize("count", [1, 7, 100, 1000])
def test_pyramid_bins_match_brute_force_on_whole_buckets(count):
    data = noise(64 * 4096, seed=count)
    bucket = 64
    pyr = waveform.PeakPyramid.from_samples(data, bucket, RATE)
    out = pyr.bins(0.0, len(data) / RATE, count)
    assert len(out) == 2 * count
    per_bin = len(data) / bucket / count
    for i in range(count):
        # Bin i covers whole level-0 buckets floor(i*per_bin)..floor((i+1)*per_bin).
        a = math.floor(i * per_bin) * bucket
        b = max(math.floor((i + 1) * per_bin), math.floor(i * per_bin) + 1) * bucket
        assert (out[2 * i], out[2 * i + 1]) == brute(data, a, b)


def test_pyramid_bins_window_outside_audio_is_flat():
    pyr = waveform.PeakPyramid.from_samples(sine(100, 1.0), 160, RATE)
    out = pyr.bins(5.0, 6.0, 10)
    assert list(out) == [0] * 20


def test_pyramid_bytes_round_trip():
    pyr = waveform.PeakPyramid.from_samples(noise(20_000), 160, 8000)
    back = waveform.PeakPyramid.from_bytes(pyr.to_bytes())
    assert back.bucket == 160 and back.rate == 8000 and back.entries == pyr.entries
    assert back.levels[0] == pyr.levels[0]
    assert back.bins(0, 2.5, 50) == pyr.bins(0, 2.5, 50)


def test_pyramid_bytes_rejects_garbage_and_truncation():
    with pytest.raises(ValueError):
        waveform.PeakPyramid.from_bytes(b"NOPE 1 2 3\n")
    data = waveform.PeakPyramid.from_samples(noise(1000), 10, RATE).to_bytes()
    with pytest.raises(ValueError):
        waveform.PeakPyramid.from_bytes(data[:-3])


# --- raw-sample bins -----------------------------------------------------------------


def test_bins_of_a_sine_reach_its_amplitude():
    s = sine(1000, 0.1, amp=0.5)  # 100 cycles
    out = waveform.bins(s, 10)
    for i in range(10):
        assert out[2 * i] == pytest.approx(-16384, abs=60)
        assert out[2 * i + 1] == pytest.approx(16384, abs=60)


def test_bins_zoomed_past_one_sample_repeat_samples():
    s = array("h", [10, -20, 30])
    out = waveform.bins(s, 6)
    assert list(out) == [10, 10, 10, 10, -20, -20, -20, -20, 30, 30, 30, 30]


# --- zero crossings ----------------------------------------------------------------------


def test_zero_crossings_of_a_sine_are_where_the_maths_says():
    # 1 kHz at 48 kHz with a quarter-sample phase shift: no exact zeros, a sign change
    # every 24 samples, between sample 24k and 24k+1 (k >= 1).
    s = sine(1000, 0.05, phase=-2 * math.pi * 1000 * 0.25 / RATE)
    for target in (30, 100, 500, 1000):
        i = waveform.nearest_zero_crossing(s, target, 24)
        assert i is not None
        k = round(i / 24)
        assert i in (24 * k, 24 * k + 1)
        assert abs(s[i]) <= abs(s[24 * k + (1 if i == 24 * k else 0)])
        assert abs(i - target) <= 12


def test_zero_crossing_picks_the_smaller_side_and_exact_zero():
    assert waveform.nearest_zero_crossing(array("h", [500, 400, -100, -300]), 0, 10) == 2
    assert waveform.nearest_zero_crossing(array("h", [500, 50, -400, -300]), 3, 10) == 1
    assert waveform.nearest_zero_crossing(array("h", [5, 3, 0, 2, 7]), 4, 10) == 2


def test_zero_crossing_ties_go_earlier():
    s = array("h", [-1, 0, 5, 9, 5, 0, -1])
    assert waveform.nearest_zero_crossing(s, 3, 5) == 1


def test_zero_crossing_none_within_radius():
    s = array("h", [100] * 50 + [-100] * 50)
    assert waveform.nearest_zero_crossing(s, 10, 20) is None
    assert waveform.nearest_zero_crossing(s, 10, 40) == 49
    assert waveform.nearest_zero_crossing(array("h"), 0, 10) is None


# --- quiet points -------------------------------------------------------------------------------


def test_nearest_quiet_finds_the_gap_nearest_the_target():
    rate = 16000
    loud = sine(440, 0.3, rate=rate, amp=0.5)
    gap = array("h", [0] * round(0.1 * rate))
    s = loud + gap + loud + gap + loud  # gaps at 0.3-0.4 s and 0.7-0.8 s
    i, level, quiet = waveform.nearest_quiet(s, round(0.62 * rate), rate)
    assert quiet
    assert 0.7 <= i / rate <= 0.8
    assert level == float("-inf")
    i, _, quiet = waveform.nearest_quiet(s, round(0.2 * rate), rate)
    assert quiet and 0.3 <= i / rate <= 0.4


def test_nearest_quiet_without_silence_returns_the_quietest():
    rate = 16000
    s = sine(440, 0.2, rate=rate, amp=0.6) + sine(440, 0.2, rate=rate, amp=0.05) \
        + sine(440, 0.2, rate=rate, amp=0.6)
    i, level, quiet = waveform.nearest_quiet(s, 0, rate, threshold_db=-60)
    assert not quiet
    assert 0.2 <= i / rate <= 0.4
    assert level == pytest.approx(20 * math.log10(0.05 / math.sqrt(2)), abs=1.0)


def test_rms_db_of_full_scale_sine():
    assert waveform.rms_db(sine(1000, 0.1, amp=1.0)) == pytest.approx(-3.01, abs=0.05)
    assert waveform.rms_db(array("h", [0, 0])) == float("-inf")


# --- decoding --------------------------------------------------------------------------


def test_decode_args_seek_before_input_with_microseconds():
    cmd = waveform.decode_args("/m/a.mkv", audio=2, rate=8000, start=1.2345678, duration=0.5)
    assert cmd[cmd.index("-ss") + 1] == "1.234568"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-map") + 1] == "0:a:2"
    assert cmd[cmd.index("-ar") + 1] == "8000"
    assert cmd[-2:] == ["pcm_s16le", "-"] or cmd[-1] == "-"


def test_stream_buckets_matches_minmax_buckets():
    import io
    data = noise(10_000)
    got_lo, got_hi = array("h"), array("h")
    total = 0
    for lo, hi, total in waveform.stream_buckets(io.BytesIO(data.tobytes()), 160, 1000):
        got_lo.extend(lo)
        got_hi.extend(hi)
    lo, hi = waveform.minmax_buckets(data, 160)
    assert (got_lo, got_hi, total) == (lo, hi, len(data))


@needs_ffmpeg
def test_decode_a_generated_sine(tmp_path):
    src = tmp_path / "sine.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=48000:duration=2", str(src)], check=True)
    s = waveform.decode(src, start=0.5, duration=0.25)
    assert len(s) == 12000
    assert waveform.native_rate(src) == 48000
    # ffmpeg's sine starts at phase 0: the cut lands on a crossing every 24 samples.
    assert abs(s[0]) < 200
    assert waveform.nearest_zero_crossing(s, 30, 24) in (24, 23, 25)


def test_anchor_rounds_float_noise_away():
    """A snap on a 10 s mark comes back as start - before = 29.999999999999996: it must
    anchor where 30.0 does, or the cut and the snap decode from different anchors."""
    assert waveform.anchor(30.0) == waveform.anchor(30.0 - 1e-12) == 29.0
    assert waveform.anchored_trim(29.999999999999996, 48000) == (29.0, 48000)


def test_decode_args_pad_the_start_to_file_time():
    cmd = waveform.decode_args("/m/a.mkv", rate=8000)
    assert cmd[cmd.index("-af") + 1] == "aresample=first_pts=0"
    cmd = waveform.decode_args("/m/a.mkv", start=13.0, duration=1, src_rate=48000)
    assert cmd[cmd.index("-af") + 1] == (
        "aresample=first_pts=0,atrim=start_sample=192000,asetpts=PTS-STARTPTS")


def _delayed_audio(path):
    """20 s of video whose audio starts 0.5 s in (as an MKV audio delay does), with a
    20 ms beep at every file time x.5 s."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=64x48:rate=25:duration=20", "-itsoffset", "0.5", "-f", "lavfi",
         "-i", "aevalsrc='if(lt(mod(t,1),0.02),0.8*sin(2*PI*1000*t),0)':s=48000:d=19.5",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-g", "250", "-c:a", "flac",
         str(path)], check=True)
    return path


def _first_beep(samples, rate=48000) -> float | None:
    i = next((k for k, v in enumerate(samples) if abs(v) > 8000), None)
    return None if i is None else round(i / rate, 3)


@needs_ffmpeg
@pytest.mark.parametrize("start", [3.0, 13.0])
def test_anchored_decode_counts_from_file_time_when_audio_starts_late(tmp_path, start):
    """Below the first anchor there is no -ss: the decode must still start at file time
    0, not at the delayed audio's first sample (a 0.5 s error before the fix)."""
    src = _delayed_audio(tmp_path / "delayed.mkv")
    t0 = waveform.anchored_start(start, 48000)
    s = waveform.decode(src, rate=48000, start=t0, duration=1.0, src_rate=48000)
    assert _first_beep(s) == 0.5
    overview = waveform.decode(src, rate=48000)
    assert _first_beep(overview) == 0.5


@needs_ffmpeg
@pytest.mark.parametrize("start", [3.0, 13.0])
def test_exact_cuts_keep_late_audio_in_sync(tmp_path, start):
    from quipclipper.clip import ClipRange, cut_clip

    src = _delayed_audio(tmp_path / "delayed.mkv")
    for kind, lossless, codec, ext in (("audio", True, "wav", "wav"),
                                       ("video", False, None, "mp4")):
        out = cut_clip(src, ClipRange(start, start + 1.0), kind=kind, lossless=lossless,
                       audio_codec=codec, out=tmp_path / f"cut-{kind}.{ext}")
        assert _first_beep(waveform.decode(out, rate=48000)) == 0.5, kind


@needs_ffmpeg
def test_keyframes_count_from_the_file_start(tmp_path):
    """A TS starts at 1.4 s: its keyframes (every 10 s) are at 0, 10, 20 as the player
    and -ss count them, not at 1.4, 11.4, 21.4."""
    src = tmp_path / "plain.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=64x48:rate=25:duration=30", "-c:v", "libx264",
                    "-g", "250", "-output_ts_offset", "1.4", str(src)], check=True)
    assert waveform.start_time(src) > 1
    assert waveform.keyframes(src, 5, 25) == pytest.approx([10.0, 20.0], abs=0.05)
