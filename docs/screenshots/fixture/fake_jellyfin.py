"""A fake Jellyfin for the screenshots: two paused clients, titles and artwork.

    FIXTURE=<make-fixture.sh OUT_DIR> python3 fake_jellyfin.py

Run it, then start the server with QC_JELLYFIN_URL=http://127.0.0.1:8097,
QC_JELLYFIN_API_KEY=anything and
QC_JELLYFIN_PATH_MAP=/data/movies=$FIXTURE/Movies:/data/shows=$FIXTURE/Shows.
It answers the Now playing sessions, the library listing the title index reads, and
the posters and stills, which it cuts from the fixture's own frames with ffmpeg on
the first start (into $FIXTURE/art).
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TICKS = 10_000_000
FIXTURE = os.environ["FIXTURE"]
MOVIE_PATH = "/data/movies/Large Locutus Leporid (2016)/Large Locutus Leporid (2016).mp4"
SEASON = "/data/shows/The Next Leporation/Season 01/"

# Jellyfin ids and image tags are 32 hex digits.
MOVIE, SERIES, SEASON_ID = "1" * 32, "2" * 32, "3" * 32
EPISODES = [("4" * 32, 1, "Encounter at Meadowpoint", 20),
            ("5" * 32, 2, "The Best of Both Burrows", 34),
            ("6" * 32, 3, "Q Who Hops", 42)]
TAG = "a" * 32

# Image id -> (source file under FIXTURE, second, crop). Posters are 2:3, stills 16:9.
POSTER = "crop=ih*2/3:ih,scale=400:600"
STILL = "scale=640:360"
MOVIE_FILE = "Movies/Large Locutus Leporid (2016)/Large Locutus Leporid (2016).mp4"
ART = {MOVIE: (MOVIE_FILE, 40.1, POSTER), SERIES: (MOVIE_FILE, 71.0, POSTER)}
for ep_id, n, title, dur in EPISODES:
    ART[ep_id] = (f"Shows/The Next Leporation/Season 01/"
                  f"The Next Leporation - S01E0{n} - {title}.mp4", dur / 2, STILL)


def make_art():
    out = os.path.join(FIXTURE, "art")
    os.makedirs(out, exist_ok=True)
    for item_id, (src, t, vf) in ART.items():
        dst = os.path.join(out, item_id + ".jpg")
        if not os.path.exists(dst):
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(t), "-i",
                            os.path.join(FIXTURE, src), "-frames:v", "1", "-vf", vf,
                            "-q:v", "3", dst], check=True)
    return out


def movie_item():
    return {"Id": MOVIE, "Name": "Large Locutus Leporid", "ProductionYear": 2016,
            "Type": "Movie", "RunTimeTicks": 100 * TICKS, "Path": MOVIE_PATH,
            "ImageTags": {"Primary": TAG}}


def episode_item(ep_id, n, title, dur):
    return {"Id": ep_id, "Name": title, "Type": "Episode", "RunTimeTicks": dur * TICKS,
            "SeriesName": "The Next Leporation", "SeriesId": SERIES,
            "SeriesPrimaryImageTag": TAG, "SeasonId": SEASON_ID, "SeasonName": "Season 1",
            "ParentIndexNumber": 1, "IndexNumber": n, "ProductionYear": 2016,
            "Path": f"{SEASON}The Next Leporation - S01E0{n} - {title}.mp4",
            "ImageTags": {"Primary": TAG}}


ITEMS = [movie_item()] + [episode_item(*e) for e in EPISODES]
SESSIONS = [
    {"Id": "s-lr", "UserName": "jean-luc", "DeviceName": "Living room TV",
     "Client": "Jellyfin Android TV", "NowPlayingItem": ITEMS[2],
     "PlayState": {"PositionTicks": int(6.2 * TICKS), "IsPaused": True}},
    {"Id": "s-rr", "UserName": "will", "DeviceName": "Ready room iPad",
     "Client": "Jellyfin Mobile (iOS)", "NowPlayingItem": ITEMS[0],
     "PlayState": {"PositionTicks": int(40.1 * TICKS), "IsPaused": True}},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, data, ctype="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = url.path.strip("/").split("/")
        if parts[0] == "Sessions":
            return self.send(json.dumps(SESSIONS).encode())
        if parts[0] == "Items" and len(parts) == 4 and parts[2] == "Images":
            path = os.path.join(ART_DIR, parts[1] + ".jpg")
            if not os.path.exists(path):
                return self.send(b"{}", status=404)
            with open(path, "rb") as f:
                return self.send(f.read(), "image/jpeg")
        if parts == ["Items"]:
            if "Ids" in query:
                items = [i for i in ITEMS if i["Id"] in query["Ids"][0].split(",")]
            else:  # the title index reads the listing in pages
                items = ITEMS if query.get("StartIndex", ["0"])[0] == "0" else []
            return self.send(json.dumps({"Items": items}).encode())
        self.send(json.dumps({"Items": []}).encode())


ART_DIR = make_art()
ThreadingHTTPServer(("127.0.0.1", 8097), Handler).serve_forever()
