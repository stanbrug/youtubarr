"""End-to-end run inside the youtubarr-e2e container:
    python -u /t/test.py <step>     (PLEX_TOKEN in the environment)
Steps: plex, add, verify, play, remove, rules, unsubscribe, status.
"""

import json
import os
import sys
import time

import requests

APP = "http://127.0.0.1:8080"
PLEX = "http://plex-e2e:32400"
MOCK = "http://yt-mock:9900"
H = {"Accept": "application/json", "X-Plex-Token": os.environ.get("PLEX_TOKEN", ""),
     "X-Plex-Client-Identifier": "youtubarr-e2e", "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
PREFS = {
    "GenerateBIFBehavior": "never", "GenerateChapterThumbBehavior": "never",
    "GenerateIntroMarkerBehavior": "never", "GenerateCreditsMarkerBehavior": "never",
    "GenerateAdMarkerBehavior": "never", "GenerateVADBehavior": "never",
    "LoudnessAnalysisBehavior": "never", "MusicAnalysisBehavior": "never",
    "ButlerTaskDeepMediaAnalysis": "0", "ButlerTaskUpgradeMediaAnalysis": "0",
    "allowMediaDeletion": "1",
}


def plex(method, path, **kw):
    r = requests.request(method, PLEX + path, headers=H, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if "json" in r.headers.get("Content-Type", "") and r.content else None


def app(method, path, **kw):
    r = requests.request(method, APP + path, timeout=120, **kw)
    return r.json()


def wait(label, cond, timeout=300, every=3):
    t = time.time()
    while time.time() - t < timeout:
        value = cond()
        if value:
            print(f"  {label}: ok after {time.time() - t:.0f}s")
            return value
        time.sleep(every)
    raise SystemExit(f"  {label}: TIMEOUT; status: {json.dumps(app('GET', '/api/status')['plex'])}")


def meta(key):
    return plex("GET", f"/library/metadata/{key}")["MediaContainer"]["Metadata"][0]


def shows(section):
    return plex("GET", f"/library/sections/{section}/all", params={"type": 2})["MediaContainer"].get("Metadata", []) or []


def leaves(show_key):
    return plex("GET", f"/library/metadata/{show_key}/allLeaves")["MediaContainer"].get("Metadata", []) or []


def section():
    return app("GET", "/api/settings")["plex_section_id"]


def step_plex():
    plex("PUT", "/:/prefs", params=PREFS)
    for d in plex("GET", "/library/sections")["MediaContainer"].get("Directory", []) or []:
        plex("DELETE", f"/library/sections/{d['key']}")  # start from an empty server

    def create():
        # A freshly claimed server rejects new libraries (400) for a little while.
        try:
            plex("POST", "/library/sections", params={"name": "YouTube", "type": "show", "agent": "tv.plex.agents.none",
                                                      "scanner": "Plex TV Series", "language": "xn", "location": "/youtube"})
            return True
        except requests.HTTPError:
            return False
    wait("library created", create, timeout=180, every=10)
    key = next(d["key"] for d in plex("GET", "/library/sections")["MediaContainer"]["Directory"] if d["title"] == "YouTube")
    app("POST", "/api/settings", json={"plex_token": H["X-Plex-Token"], "plex_section_id": key})
    test = app("GET", "/api/plex/test")
    print("plex test from youtubarr:", test.get("ok"), test.get("version"), [s["title"] for s in test.get("sections", [])])


def step_add():
    r1 = app("POST", "/api/subscriptions", json={"input": "https://www.youtube.com/@BlenderOfficial/videos", "min_duration": 61})
    r2 = app("POST", "/api/subscriptions", json={"input": "https://www.youtube.com/playlist?list=PLmockOpenMovies000000000000000000"})
    for r in (r1, r2):
        s = r.get("subscription") or {}
        print("added:", r.get("ok"), s.get("title"), s.get("kind"), s.get("mode"), s.get("folder"), r.get("error", ""))
    print("duplicate:", app("POST", "/api/subscriptions", json={"input": "@BlenderOfficial"}))
    wait("sync (3 + 2 videos)", lambda: sorted(s["videos"] for s in app("GET", "/api/subscriptions")) == [2, 3])
    # The mock's hidden video ("Spring") is uploaded later in step_upload.

    def listing(path=""):
        html = requests.get(f"{APP}/library/{path}", timeout=30).text
        return [requests.utils.unquote(c.split('"', 1)[0]) for c in html.split('<a href="')[1:]]
    for show in listing():
        for season in listing(requests.utils.quote(show)):
            for f in listing(requests.utils.quote(show + season)):
                print("   ", show + season + f)

    wait("Plex: all entries matched + analyzed", lambda: all(
        v["plex_key"] and v["plex_analyzed_at"] for s in app("GET", "/api/subscriptions")
        for v in app("GET", f"/api/subscriptions/{s['id']}/videos")), timeout=400)
    wait("metadata + posters + shows pushed", lambda: (lambda q: q["metadata"] == 0 and q["posters"] == 0)(
        app("GET", "/api/status")["plex"]["queue"]) and all(s["show_pushed_at"] for s in app("GET", "/api/subscriptions")))
    st = app("GET", "/api/status")
    print("  quota used:", st["quota_today"], "| plex stats:", st["plex"]["stats"], "| cache:", st["cache"]["files"], "files")


def step_upload():
    """A new upload on an existing channel: quick check -> partial scan -> metadata."""
    requests.post(f"{MOCK}/_test/add", params={"id": "WhWc3b3KhnY"}, timeout=10)
    sub = next(s for s in app("GET", "/api/subscriptions") if s["kind"] == "channel")
    app("POST", f"/api/subscriptions/{sub['id']}/check")
    wait("new upload in Plex with metadata", lambda: any(
        e["title"].startswith("Spring") for e in leaves(_show_key("Blender"))), timeout=180)
    ep = _episode("Spring")
    wait("its poster pushed", lambda: all(v["poster_pushed_at"] for v in app("GET", f"/api/subscriptions/{sub['id']}/videos")), timeout=120)
    print(f"  S{ep.get('parentIndex')}E{ep.get('index')} '{ep['title']}'")


def step_verify():
    for show in shows(section()):
        m = meta(show["ratingKey"])
        locked = sorted(f["name"] for f in m.get("Field", []) if f.get("locked"))
        print(f"show '{m['title']}': summary={m.get('summary', '')[:60]!r} thumb={'thumb' in m} art={'art' in m} locked={locked}")
        for ep in leaves(show["ratingKey"]):
            e = meta(ep["ratingKey"])
            locked = sorted(f["name"] for f in e.get("Field", []) if f.get("locked"))
            media = (e.get("Media") or [{}])[0]
            print(f"   S{e.get('parentIndex')}E{e.get('index')} '{e['title']}' date={e.get('originallyAvailableAt')} "
                  f"thumb={'thumb' in e} locked={locked} media={media.get('videoCodec')} {media.get('width')}x{media.get('height')}")
            if "Big Buck" in e["title"] and show["title"] == "Blender":
                print("   cleaned summary:\n      " + e.get("summary", "").replace("\n", "\n      "))


def _episode(title_part, show_title="Blender"):
    for show in shows(section()):
        if show["title"] == show_title:
            for ep in leaves(show["ratingKey"]):
                if title_part in ep["title"]:
                    return ep
    raise SystemExit(f"episode {title_part} not found")


def step_play():
    ep = _episode(sys.argv[2] if len(sys.argv) > 2 else "Tears of Steel")
    part = meta(ep["ratingKey"])["Media"][0]["Part"][0]
    before = meta(ep["ratingKey"])["Media"][0]
    t = time.time()
    r = requests.get(PLEX + part["key"], headers={**H, "Range": "bytes=0-1048575"}, timeout=300)
    ebml_magic = r.content[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3])
    print(f"direct play first MB: HTTP {r.status_code} {len(r.content)} bytes in {time.time() - t:.1f}s, EBML={ebml_magic}")
    # Plex answers ranges against the file size it stats now (the real size
    # once the download finished), so seek within that.
    real = int(requests.head(PLEX + part["key"], headers=H, timeout=60).headers.get("Content-Length", 0) or part["size"])
    t = time.time()
    r = requests.get(PLEX + part["key"], headers={**H, "Range": f"bytes={real // 3}-{real // 3 + 1048575}"}, timeout=300)
    print(f"seek to 1/3 of {real}: HTTP {r.status_code} {len(r.content)} bytes in {time.time() - t:.1f}s")
    wait("Plex re-analysis after download (real resolution)", lambda: meta(ep["ratingKey"])["Media"][0].get("width") != before.get("width")
         or meta(ep["ratingKey"])["Media"][0]["Part"][0].get("size") != part.get("size"), timeout=180, every=5)
    after = meta(ep["ratingKey"])["Media"][0]
    print(f"  before: {before.get('width')}x{before.get('height')} size {part.get('size')} -> after: "
          f"{after.get('width')}x{after.get('height')} size {after['Part'][0].get('size')} bitrate {after.get('bitrate')}")


def step_remove():
    ep = _episode("Tears of Steel")
    requests.post(f"{MOCK}/_test/remove", params={"id": "R6MlUcmOul8"}, timeout=10)
    sub = next(s for s in app("GET", "/api/subscriptions") if s["kind"] == "channel")
    app("POST", f"/api/subscriptions/{sub['id']}/check")

    def gone():
        try:
            meta(ep["ratingKey"])
            return False
        except requests.HTTPError as e:
            return e.response.status_code == 404
    wait("removed video deleted from Plex", gone, timeout=180)
    print("  channel now has", [e["title"] for e in leaves(_show_key("Blender"))])


def _show_key(title):
    return next(s["ratingKey"] for s in shows(section()) if s["title"] == title)


def step_rules():
    rules = app("GET", "/api/settings")["rules"]
    rules["timestamps"] = False
    app("POST", "/api/settings", json={"rules": rules})
    ep = _episode("Big Buck")
    wait("re-pushed summary keeps chapters now", lambda: "00:00 Opening" in meta(ep["ratingKey"]).get("summary", ""), timeout=120)
    rules["timestamps"] = True
    app("POST", "/api/settings", json={"rules": rules})


def step_unsubscribe():
    sub = next(s for s in app("GET", "/api/subscriptions") if s["kind"] == "playlist")
    app("DELETE", f"/api/subscriptions/{sub['id']}")
    wait("playlist show removed from Plex", lambda: "Open Movies" not in [s["title"] for s in shows(section())], timeout=180)
    print("  subscriptions left:", [s["title"] for s in app("GET", "/api/subscriptions")])


def step_status():
    st = app("GET", "/api/status")
    print(json.dumps({k: st[k] for k in ("quota_today", "counts", "cache", "plex")}, indent=1)[:1500])
    print("\n".join(st["log"][-25:]))


if __name__ == "__main__":
    globals()[f"step_{sys.argv[1]}"]()
