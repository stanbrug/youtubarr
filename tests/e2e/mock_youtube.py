"""Stand-in for the YouTube Data API v3 (only the calls youtubarr makes).

The video ids are real Blender open movies, so playback downloads real files;
the metadata is shaped like the API's. POST /_test/remove?id=... makes a
video disappear (deleted on YouTube)."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CHANNEL = "UCSMOQeBJ2RAnuFungnQOxLg"
UPLOADS = "UUSMOQeBJ2RAnuFungnQOxLg"
PLAYLIST = "PLmockOpenMovies000000000000000000"

DESCRIPTION = """Big Buck Bunny tells the story of a giant rabbit with a heart bigger than himself.

This film was sponsored by the Blender Development Fund, use code BUNNY for 10% off!

00:00 Opening
03:12 The bullies
08:40 Revenge

Download the film: https://peach.blender.org/download/
Support us at https://fund.blender.org

Follow us:
Twitter: https://twitter.com/blender
► Instagram: https://instagram.com/blender.official

#blender #animation #openmovie"""

VIDEOS = {
    "aqz-KE-bpKQ": ("Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film", "2014-11-10T14:05:55Z", "PT10M35S", DESCRIPTION),
    "eRsGyueVLvQ": ("Sintel - Open Movie by Blender Foundation", "2010-09-30T00:17:46Z", "PT14M48S",
                    "Sintel is an independently produced short film.\n\nhttps://durian.blender.org\n#sintel"),
    "R6MlUcmOul8": ("Tears of Steel - Blender VFX Open Movie", "2012-09-26T13:48:02Z", "PT12M14S",
                    "Tears of Steel was realized with crowd-funding.\n0:00 Start\n#tearsofsteel"),
    "shortshort1": ("A Short", "2024-05-01T10:00:00Z", "PT0M40S", "#shorts"),
    "upcomingliv": ("Upcoming stream", "2026-10-01T10:00:00Z", "P0D", "Live soon"),
    "WhWc3b3KhnY": ("Spring - Blender Open Movie", "2019-04-04T13:30:00Z", "PT7M44S",
                    "Spring is the story of a shepherd girl.\nhttps://cloud.blender.org"),
}
ORDER = ["upcomingliv", "shortshort1", "WhWc3b3KhnY", "aqz-KE-bpKQ", "R6MlUcmOul8", "eRsGyueVLvQ"]  # newest first
PLAYLIST_ORDER = ["eRsGyueVLvQ", "aqz-KE-bpKQ"]
REMOVED = {"WhWc3b3KhnY"}  # "uploaded" later with /_test/add


def thumbs(video_id):
    base = f"https://i.ytimg.com/vi/{video_id}"
    return {"default": {"url": f"{base}/default.jpg"}, "high": {"url": f"{base}/hqdefault.jpg"},
            "maxres": {"url": f"{base}/maxresdefault.jpg"}}


def video_resource(video_id):
    title, published, duration, desc = VIDEOS[video_id]
    return {
        "id": video_id,
        "snippet": {"publishedAt": published, "channelId": CHANNEL, "title": title, "description": desc,
                    "thumbnails": thumbs(video_id), "channelTitle": "Blender",
                    "liveBroadcastContent": "upcoming" if video_id == "upcomingliv" else "none"},
        "contentDetails": {"duration": duration, "definition": "hd"},
        "status": {"uploadStatus": "processed", "privacyStatus": "public"},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[yt]", self.command, self.path.split("key=")[0], flush=True)

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/_test/remove":
            REMOVED.add(q["id"])
            return self._json({"ok": True})
        if url.path == "/_test/add":
            REMOVED.discard(q["id"])
            return self._json({"ok": True})
        self._json({}, 404)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if q.get("key") != "mock-key":
            return self._json({"error": {"code": 400, "message": "API key not valid",
                                         "errors": [{"reason": "keyInvalid"}]}}, 400)
        resource = url.path.rsplit("/", 1)[-1]
        if resource == "channels":
            if q.get("id") == CHANNEL or q.get("forHandle", "").lower() == "@blenderofficial":
                return self._json({"items": [{
                    "id": CHANNEL,
                    "snippet": {"title": "Blender", "description": "The official channel of Blender, the free and open source 3D creation suite.\nhttps://www.blender.org",
                                "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/aqz-KE-bpKQ/hqdefault.jpg"}}},
                    "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                    "brandingSettings": {"image": {"bannerExternalUrl": "https://i.ytimg.com/vi/eRsGyueVLvQ/maxresdefault.jpg"}},
                }]})
            return self._json({"items": []})
        if resource == "playlists":
            if q.get("id") == PLAYLIST:
                return self._json({"items": [{"id": PLAYLIST, "snippet": {
                    "title": "Open Movies", "description": "Our favourite open movies.", "channelId": CHANNEL,
                    "thumbnails": thumbs("eRsGyueVLvQ")}}]})
            return self._json({"items": []})
        if resource == "playlistItems":
            order = ORDER if q.get("playlistId") == UPLOADS else PLAYLIST_ORDER
            items = []
            for pos, vid in enumerate(v for v in order if v not in REMOVED):
                items.append({"snippet": {"title": VIDEOS[vid][0], "position": pos},
                              "contentDetails": {"videoId": vid, "videoPublishedAt": VIDEOS[vid][1]},
                              "status": {"privacyStatus": "public"}})
            return self._json({"items": items, "pageInfo": {"totalResults": len(items)}})
        if resource == "videos":
            ids = [i for i in q.get("id", "").split(",") if i in VIDEOS and i not in REMOVED]
            return self._json({"items": [video_resource(i) for i in ids]})
        self._json({"error": {"code": 404, "message": "unknown resource"}}, 404)


if __name__ == "__main__":
    print("mock youtube api on :9900", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9900), Handler).serve_forever()
