"""item.listen (M26): the CLI side of the search results' Listen button.

The server command saves the excerpt ``/api/media/audio`` streams, named by the reply's
Content-Disposition; ``--play`` saves it to a temporary folder (unless ``--out-dir`` says
where) and hands it to the first system player found. No player, no ffmpeg: the transport
and the player lookup are faked, so nothing here depends on the machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from quipclipper import cli
from quipclipper import commands as reg
from quipclipper.client import Response

AAC = b"\xff\xf1fake-adts"


class AudioTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list]] = []

    def request(self, method, path, query, body, stream=False):
        self.calls.append((method, path, query))
        return Response(200, {"content-type": "audio/aac",
                              "content-disposition": "inline; filename*=UTF-8''movie_1-3.aac"},
                        AAC)


def _run(argv, capsys, transport=None, env=None):
    with patch.dict("os.environ", {"QC_URL": "http://qc.test", **(env or {})}):
        code = cli.main(["--json", *argv], transport=transport or AudioTransport())
    return code, capsys.readouterr().out.strip()


def test_registered_as_the_search_rows_action():
    cmd = reg.BY_ID["item.listen"]
    assert (cmd.method, cmd.path, cmd.response) == ("GET", "/api/media/audio", "file")
    assert "search result" in cmd.ui
    assert {p.name for p in cmd.params if p.where == reg.CLIENT} == {"out_dir", "play"}
    assert {p.name for p in cmd.params if p.where == reg.QUERY} == {"path", "start", "end", "audio"}


def test_saves_the_excerpt_under_the_servers_name(tmp_path: Path, capsys):
    t = AudioTransport()
    code, out = _run(["item.listen", "/m/movie.mkv", "--start", "1", "--end", "3",
                      "--out-dir", str(tmp_path)], capsys, t)
    assert code == 0, out
    data = json.loads(out.splitlines()[-1])
    assert data == {"saved": str(tmp_path / "movie_1-3.aac"), "size": len(AAC)}
    assert (tmp_path / "movie_1-3.aac").read_bytes() == AAC
    method, path, query = t.calls[0]
    assert (method, path) == ("GET", "/api/media/audio")
    assert dict(query) == {"path": "/m/movie.mkv", "start": "1.0", "end": "3.0"}
    assert "play" not in dict(query) and "out_dir" not in dict(query)


def test_play_hands_a_temporary_file_to_the_first_player_found(tmp_path: Path, capsys):
    played: list[list[str]] = []

    def which(name):
        return "/usr/bin/" + name if name == "ffplay" else None

    with patch("quipclipper.cli.shutil.which", side_effect=which), \
         patch("quipclipper.cli.subprocess.run", side_effect=lambda a, **k: played.append(a)), \
         patch("quipclipper.cli.tempfile.mkdtemp", return_value=str(tmp_path)):
        code, out = _run(["item.listen", "/m/movie.mkv", "--start", "1", "--end", "3",
                          "--play"], capsys)
    assert code == 0, out
    data = json.loads(out.splitlines()[-1])
    assert data["played_with"] == "ffplay"
    assert data["saved"] == str(tmp_path / "movie_1-3.aac")
    assert played == [["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", data["saved"]]]


def test_play_without_a_player_keeps_the_file_and_says_where(tmp_path: Path, capsys):
    with patch("quipclipper.cli.shutil.which", return_value=None), \
         patch("quipclipper.cli.tempfile.mkdtemp", return_value=str(tmp_path)):
        code, out = _run(["item.listen", "/m/movie.mkv", "--start", "1", "--end", "3",
                          "--play"], capsys)
    assert code != 0
    assert (tmp_path / "movie_1-3.aac").exists()
