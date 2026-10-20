"""A fake Jellyfin for the Now playing screenshot: two clients, both paused.

Run it, then start the server with QC_JELLYFIN_URL=http://127.0.0.1:8097,
QC_JELLYFIN_API_KEY=anything and
QC_JELLYFIN_PATH_MAP=/data/movies=$FIXTURE/Movies:/data/shows=$FIXTURE/Shows.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TICKS = 10_000_000
SESSIONS = [
    {"Id": "s-lr", "UserName": "jean-luc", "DeviceName": "Living room TV",
     "Client": "Jellyfin Android TV",
     "NowPlayingItem": {"Id": "e2", "Name": "The Best of Both Burrows",
                        "SeriesName": "The Next Leporation", "ParentIndexNumber": 1,
                        "IndexNumber": 2, "Type": "Episode", "RunTimeTicks": 34 * TICKS,
                        "Path": "/data/shows/The Next Leporation/Season 01/"
                                "The Next Leporation - S01E02 - The Best of Both Burrows.mp4"},
     "PlayState": {"PositionTicks": int(6.2 * TICKS), "IsPaused": True}},
    {"Id": "s-rr", "UserName": "will", "DeviceName": "Ready room iPad",
     "Client": "Jellyfin Mobile (iOS)",
     "NowPlayingItem": {"Id": "m1", "Name": "Large Locutus Leporid", "ProductionYear": 2016,
                        "Type": "Movie", "RunTimeTicks": 100 * TICKS,
                        "Path": "/data/movies/Large Locutus Leporid (2016)/"
                                "Large Locutus Leporid (2016).mp4"},
     "PlayState": {"PositionTicks": int(40.1 * TICKS), "IsPaused": True}},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = SESSIONS if self.path.startswith("/Sessions") else {"Items": []}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", 8097), Handler).serve_forever()
