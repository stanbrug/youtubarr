"""Proof of concept for the stub header + download-on-play (spec steps 3+4).

Run inside the youtubarr container:  python /poc/plex_test.py <step>

  setup     library + Plex prefs, add 3 videos, scan, wait for analysis
  play      direct play (raw part with Range reads, including a seek)
  stream    transcoder/direct-stream session starting at an offset (seek)
  rescan    after downloads finished: rescan, compare Plex's media info
  report    events log summary
"""

import json
import os
import sys
import time

import requests

PLEX = "http://plex-poc:32400"
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
APP = "http://127.0.0.1:8080"
VIDEOS = [
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",  # Big Buck Bunny (10:35)
    "https://www.youtube.com/watch?v=eRsGyueVLvQ",  # Sintel (14:48)
    "https://www.youtube.com/watch?v=R6MlUcmOul8",  # Tears of Steel (12:14)
]
HEADERS = {
    "Accept": "application/json",
    "X-Plex-Client-Identifier": "youtubarr-poc",
    "X-Plex-Product": "Plex Web",
    "X-Plex-Platform": "Chrome",
    "X-Plex-Device": "Linux",
    **({"X-Plex-Token": PLEX_TOKEN} if PLEX_TOKEN else {}),
}
PREFS = {
    # Everything that would read whole files (and so download every video).
    "GenerateBIFBehavior": "never",
    "GenerateChapterThumbBehavior": "never",
    "GenerateIntroMarkerBehavior": "never",
    "GenerateCreditsMarkerBehavior": "never",
    "GenerateAdMarkerBehavior": "never",
    "GenerateVADBehavior": "never",
    "LoudnessAnalysisBehavior": "never",
    "MusicAnalysisBehavior": "never",
    "ButlerTaskDeepMediaAnalysis": "0",
    "ButlerTaskUpgradeMediaAnalysis": "0",
    "ButlerTaskRefreshLibraries": "0",
    "FSEventLibraryUpdatesEnabled": "0",
    "ScheduledLibraryUpdatesEnabled": "0",
}


def plex(method, path, **kw):
    r = requests.request(method, PLEX + path, headers={**HEADERS, **kw.pop("headers", {})}, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.content and "json" in r.headers.get("Content-Type", "") else r


def app(method, path, **kw):
    r = requests.request(method, APP + path, timeout=300, **kw)
    return r.json()


def events(since=0):
    return [e for e in app("GET", "/api/events") if e["t"] >= since]


def episodes(section):
    data = plex("GET", f"/library/sections/{section}/all", params={"type": 4})
    return data["MediaContainer"].get("Metadata", []) or []


def section_id():
    for d in plex("GET", "/library/sections")["MediaContainer"].get("Directory", []) or []:
        if d["title"] == "YouTube PoC":
            return d["key"]
    return None


def step_setup():
    ident = plex("GET", "/identity")
    print("plex", ident["MediaContainer"].get("version"))
    plex("PUT", "/:/prefs", params=PREFS)

    agents = plex("GET", "/system/agents", params={"mediaType": 2})["MediaContainer"].get("Agent", [])
    print("show agents:", [(a["identifier"], a.get("name")) for a in agents])
    # "Plex Personal Media" is the current successor of the legacy "Personal
    # Media Shows" agent (com.plexapp.agents.none), which only works with the
    # deprecated legacy scanner.
    ids = [a["identifier"] for a in agents]
    agent = "tv.plex.agents.none" if "tv.plex.agents.none" in ids else "com.plexapp.agents.none"

    section = section_id()
    if section is None:
        plex("POST", "/library/sections", params={
            "name": "YouTube PoC", "type": "show", "agent": agent, "scanner": "Plex TV Series",
            "language": "xn", "location": "/youtube",
        })
        section = section_id()
    print("section", section, "agent", agent)
    app("POST", "/api/settings", json={"plex_url": PLEX, "plex_section_id": section, "plex_token": PLEX_TOKEN})

    for url in VIDEOS:
        r = app("POST", "/api/poc/add", json={"url": url})
        v = r.get("video") or {}
        print("added", r.get("ok"), v.get("id"), v.get("title"), v.get("duration"), f"{v.get('width')}x{v.get('height')}",
              r.get("error", ""))

    t0 = time.time()
    plex("GET", f"/library/sections/{section}/refresh")
    for _ in range(60):
        time.sleep(5)
        vids = app("GET", "/api/videos")
        if all(v["plex_analyzed_at"] for v in vids):
            break
    print(f"analysis wait {time.time() - t0:.0f}s")
    for ep in episodes(section):
        for m in ep.get("Media", []):
            print(f"  {ep.get('grandparentTitle')} S{ep.get('parentIndex')}E{ep.get('index')} '{ep.get('title')}': "
                  f"duration={ep.get('duration')}ms container={m.get('container')} video={m.get('videoCodec')} "
                  f"{m.get('width')}x{m.get('height')} audio={m.get('audioCodec')}/{m.get('audioChannels')}ch "
                  f"size={m['Part'][0].get('size')}")
    ev = events(t0)
    modes = {}
    for e in ev:
        if not e.get("done"):
            modes[e["mode"]] = modes.get(e["mode"], 0) + 1
    print("requests during scan+analysis by mode:", modes)
    print("videos:", [(v["id"], v["mode"]) for v in app("GET", "/api/videos")])


def _part(section, video_id):
    for ep in episodes(section):
        for m in ep.get("Media", []):
            for p in m.get("Part", []):
                if f"[{video_id}]" in p.get("file", ""):
                    return ep, m, p
    raise SystemExit(f"{video_id} not in Plex")


def _timed_range(key, start, length):
    t = time.time()
    r = requests.get(PLEX + key, headers={**HEADERS, "Range": f"bytes={start}-{start + length - 1}"},
                     timeout=600, stream=True)
    first = None
    got = 0
    for chunk in r.iter_content(64 * 1024):
        if first is None:
            first = time.time() - t
        got += len(chunk)
    return r.status_code, got, first, time.time() - t, r.headers.get("Content-Range")


def step_play():
    """Direct play = Plex hands the client the raw file (the client demuxes)."""
    section = section_id()
    video_id = app("GET", "/api/videos")[0]["id"]
    ep, media, part = _part(section, video_id)
    size = int(part["size"])
    print(f"direct play of {ep['title']} ({video_id}), part {part['key']}, size {size}")
    for label, start, length in (("start", 0, 2 * 1024 * 1024), ("seek to 60%", int(size * 0.6), 2 * 1024 * 1024),
                                 ("seek back to 20%", int(size * 0.2), 1024 * 1024)):
        status, got, first, total, crange = _timed_range(part["key"], start, length)
        print(f"  {label:18s} HTTP {status} {got} bytes, first byte {first:.1f}s, done {total:.1f}s ({crange})")
    r = requests.get(PLEX + part["key"], headers={**HEADERS, "Range": "bytes=0-65535"}, timeout=120)
    print("  starts with EBML magic:", r.content[:4] == b"\x1a\x45\xdf\xa3")
    print("  decision:", json.dumps(transcode_decision(ep["ratingKey"], direct_play=True))[:400])

    # A direct-play client demuxing the raw part itself (ffmpeg stands in for
    # mpv-based clients): probe, then seek around by time.
    import subprocess
    other = app("GET", "/api/videos")[2]["id"]
    ep2, _m2, part2 = _part(section, other)
    url = PLEX + part2["key"]
    hdr = f"X-Plex-Token: {PLEX_TOKEN}\r\n"
    print(f"direct play as a client of {ep2['title']} ({other})")
    for label, args in (("probe", None), ("play 00:00", ["-t", "5"]), ("seek 06:00", ["-ss", "360", "-t", "5"]),
                        ("seek 02:00", ["-ss", "120", "-t", "5"]), ("seek near end", ["-ss", str(int(ep2["duration"] / 1000) - 20), "-t", "5"])):
        t = time.time()
        if args is None:
            out = subprocess.run(["ffprobe", "-v", "error", "-headers", hdr, "-show_entries",
                                  "format=duration,size:stream=codec_name,width,height", "-of", "compact=p=0", url],
                                 capture_output=True, text=True)
            print(f"  {label:14s} {time.time() - t:5.1f}s  {out.stdout.strip().replace(chr(10), ' | ')} {out.stderr.strip()[:200]}")
            continue
        out = subprocess.run(["ffmpeg", "-v", "error", "-y", "-headers", hdr, *args[:-2], "-i", url, *args[-2:],
                              "-c", "copy", "/tmp/client.mkv"], capture_output=True, text=True)
        got = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "/tmp/client.mkv"],
                             capture_output=True, text=True).stdout.strip()
        print(f"  {label:14s} {time.time() - t:5.1f}s  rc={out.returncode} clip={got}s {out.stderr.strip()[:200]}")


def transcode_decision(rating_key, direct_play):
    params = {
        "path": f"/library/metadata/{rating_key}", "mediaIndex": 0, "partIndex": 0,
        "protocol": "hls", "directPlay": int(direct_play), "directStream": 1, "directStreamAudio": 1,
        "videoResolution": "1920x1080", "maxVideoBitrate": 20000, "session": "poc-decision",
        "X-Plex-Client-Profile-Extra": "add-direct-play-profile(type=videoProfile&container=mkv&videoCodec=h264&audioCodec=aac)",
    }
    d = plex("GET", "/video/:/transcode/universal/decision", params=params)["MediaContainer"]
    return {k: d.get(k) for k in ("generalDecisionCode", "generalDecisionText", "directPlayDecisionCode",
                                  "directPlayDecisionText", "transcodeDecisionCode", "transcodeDecisionText")}


def step_stream():
    """Plex's own transcoder reading the file, starting 5 minutes in -- the
    path Plex Web and most clients take (direct stream / remux to HLS)."""
    section = section_id()
    video_id = app("GET", "/api/videos")[1]["id"]
    ep, media, part = _part(section, video_id)
    print(f"direct stream of {ep['title']} ({video_id}) from offset 300s")
    print("  decision:", transcode_decision(ep["ratingKey"], direct_play=False))
    params = {
        "path": f"/library/metadata/{ep['ratingKey']}", "mediaIndex": 0, "partIndex": 0,
        "protocol": "hls", "offset": 300, "directPlay": 0, "directStream": 1, "directStreamAudio": 1,
        "videoResolution": "1920x1080", "maxVideoBitrate": 20000, "session": "poc-stream",
        "fastSeek": 1, "copyts": 1,
    }
    t = time.time()
    master = requests.get(PLEX + "/video/:/transcode/universal/start.m3u8", params=params, headers=HEADERS, timeout=600)
    print(f"  master playlist HTTP {master.status_code} after {time.time() - t:.1f}s")
    sub = next((l for l in master.text.splitlines() if l and not l.startswith("#")), None)
    if not sub:
        print(master.text[:500])
        return
    base = PLEX + "/video/:/transcode/universal/"
    for attempt in range(60):
        playlist = requests.get(base + sub, headers=HEADERS, timeout=120)
        segs = [l for l in playlist.text.splitlines() if l and not l.startswith("#")]
        if segs:
            break
        time.sleep(2)
    print(f"  media playlist: {len(segs)} segments after {time.time() - t:.1f}s")
    # 1-second segments; a session started at an offset returns blank
    # segments before it, the picture starts at segment <offset>.
    seg_url = base + sub.rsplit("/", 1)[0] + "/" + segs[300]
    seg = requests.get(seg_url, headers=HEADERS, timeout=600)
    print(f"  first segment HTTP {seg.status_code}, {len(seg.content)} bytes after {time.time() - t:.1f}s")
    with open("/tmp/seg0.ts", "wb") as f:
        f.write(seg.content)
    import subprocess
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height:format=start_time,duration",
                            "-of", "json", "/tmp/seg0.ts"], capture_output=True, text=True)
    print("  segment probe:", probe.stdout.replace("\n", " ")[:400], probe.stderr[:200])
    requests.get(PLEX + "/video/:/transcode/universal/stop", params={"session": "poc-stream"}, headers=HEADERS, timeout=30)


def step_rescan():
    section = section_id()
    before = {p.get("file"): (m.get("width"), m.get("height"), p.get("size")) for ep in episodes(section)
              for m in ep.get("Media", []) for p in m.get("Part", [])}
    t0 = time.time()
    plex("GET", f"/library/sections/{section}/refresh")
    time.sleep(60)
    for ep in episodes(section):
        for m in ep.get("Media", []):
            p = m["Part"][0]
            print(f"  {ep['title']}: before {before.get(p.get('file'))} -> now {(m.get('width'), m.get('height'), p.get('size'))} "
                  f"video={m.get('videoCodec')} duration={ep.get('duration')}")
    modes = {}
    for e in events(t0):
        if not e.get("done"):
            modes[e["mode"]] = modes.get(e["mode"], 0) + 1
    print("requests during rescan by mode:", modes)


def step_report():
    for v in app("GET", "/api/videos"):
        print(v["id"], v["mode"], v["size"], v["title"])
    ev = app("GET", "/api/events")
    print(len(ev), "events; last 40:")
    for e in ev[-40:]:
        print(" ", e)


if __name__ == "__main__":
    globals()[f"step_{sys.argv[1]}"]()
