"""Unit tests. Run from the repository root:  python -m unittest discover -s tests -v"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from youtubarr import cache as cache_mod  # noqa: E402
from youtubarr import describe, ebml, layout, server, stub  # noqa: E402
from youtubarr.db import DEFAULT_RULES, Database  # noqa: E402
from youtubarr.sync import Syncer  # noqa: E402
from youtubarr.youtube import YouTubeError, parse_duration, parse_input  # noqa: E402


class YouTubeParsingTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_duration("PT45S"), 45)
        self.assertEqual(parse_duration("P1DT1S"), 86401)
        self.assertEqual(parse_duration("P0D"), 0)
        self.assertEqual(parse_duration(""), 0)

    def test_inputs(self):
        self.assertEqual(parse_input("UCSMOQeBJ2RAnuFungnQOxLg"), ("channel", "UCSMOQeBJ2RAnuFungnQOxLg"))
        self.assertEqual(parse_input("@blender"), ("handle", "@blender"))
        self.assertEqual(parse_input("https://www.youtube.com/@blender/videos"), ("handle", "@blender"))
        self.assertEqual(parse_input("youtube.com/channel/UCSMOQeBJ2RAnuFungnQOxLg"), ("channel", "UCSMOQeBJ2RAnuFungnQOxLg"))
        self.assertEqual(parse_input("https://www.youtube.com/playlist?list=PLa1F2ddGya_-UvuAqHAksYnB0qL9yWDO6"),
                         ("playlist", "PLa1F2ddGya_-UvuAqHAksYnB0qL9yWDO6"))
        self.assertEqual(parse_input("https://www.youtube.com/watch?v=abc&list=PLxyzxyzxyzxyz"), ("playlist", "PLxyzxyzxyzxyz"))
        self.assertEqual(parse_input("https://www.youtube.com/user/blenderfoundation"), ("username", "blenderfoundation"))
        self.assertEqual(parse_input("https://www.youtube.com/c/BlenderFoundation"), ("custom", "BlenderFoundation"))
        with self.assertRaises(YouTubeError):
            parse_input("https://www.youtube.com/watch?v=aqz-KE-bpKQ")


class DescribeTests(unittest.TestCase):
    rules = dict(DEFAULT_RULES)

    def test_full_cleanup(self):
        text = """In deze video bouwen we een schuur.

Deze video is gesponsord door Hout BV, gebruik kortingscode SCHUUR10!
Check out my gear: https://amzn.to/abc

00:00 Intro
01:23 Fundering
1:02:03 Afwerking

Meer info op https://example.com/schuur en www.example.nl.

Volg mij:
Instagram: https://instagram.com/bouwer
► TikTok: https://tiktok.com/@bouwer
@bouwer op X

#schuur #diy #hout"""
        out = describe.clean(text, self.rules)
        self.assertEqual(out, "In deze video bouwen we een schuur.")

    def test_sentence_with_link_keeps_text(self):
        out = describe.clean("We used the tools from https://example.com in the whole build of this shed.", self.rules)
        self.assertEqual(out, "We used the tools from in the whole build of this shed.")

    def test_rules_can_be_off(self):
        rules = dict(self.rules, timestamps=False, urls=False, social=False, hashtags=False, sponsor=False)
        text = "00:00 Intro\nhttps://x.com/a\n#tag"
        self.assertEqual(describe.clean(text, rules), text)

    def test_short_closing_line_kept(self):
        self.assertEqual(describe.clean("Nice video.\nThanks!", self.rules), "Nice video.\nThanks!")

    def test_max_length(self):
        text = ("Zin nummer een is lang genoeg. " * 60).strip()
        out = describe.clean(text, dict(self.rules, max_length=100))
        self.assertLessEqual(len(out), 101)
        self.assertTrue(out.endswith(".") or out.endswith("…"))

    def test_overrides(self):
        rules = describe.effective_rules(self.rules, {"urls": False, "nonsense": True, "hashtags": None})
        self.assertFalse(rules["urls"])
        self.assertTrue(rules["hashtags"])
        self.assertNotIn("nonsense", rules)


class LayoutTests(unittest.TestCase):
    video = {"id": "aqz-KE-bpKQ", "title": 'Big: Buck "Bunny"?', "published_at": "2014-11-10T14:05:55Z"}

    def test_channel_slot_and_name(self):
        self.assertEqual(layout.channel_slot(self.video, set()), (2014, 111001, "Season 2014"))
        self.assertEqual(layout.channel_slot(self.video, {111001, 111002}), (2014, 111003, "Season 2014"))
        self.assertEqual(layout.filename("Blender", 2014, 111001, "channel", self.video),
                         "Blender - s2014e111001 - Big Buck Bunny [aqz-KE-bpKQ].mkv")
        self.assertEqual(layout.filename("My List", 1, 7, "playlist", self.video),
                         "My List - s01e007 - Big Buck Bunny [aqz-KE-bpKQ].mkv")
        self.assertEqual(layout.show_folder("Blender/Official", "UCx"), "BlenderOfficial [UCx]")

    def test_january_first_keeps_six_digits(self):
        v = dict(self.video, published_at="2020-01-02T00:00:00Z")
        season, episode, _ = layout.channel_slot(v, set())
        self.assertEqual(layout.filename("S", season, episode, "channel", v)[:18], "S - s2020e010201 -")


class FakeYouTube:
    """playlist -> [(video_id, position, unavailable)], videos -> {id: dict}"""

    def __init__(self):
        self.items = {}
        self.meta = {}
        self.calls = []

    def playlist_items(self, playlist_id, max_pages=None, stop_when_known=None):
        self.calls.append(("items", playlist_id, max_pages))
        yield from self.items.get(playlist_id, [])

    def videos(self, ids):
        self.calls.append(("videos", tuple(ids)))
        return {i: self.meta.get(i) for i in ids}


def vid(video_id, published, duration=600, playable=True):
    return {"id": video_id, "channel_id": "UCx", "channel_title": "Chan", "title": f"Title {video_id}",
            "description": "desc", "published_at": published, "duration": duration, "width": 1920,
            "height": 1080, "thumbnails": {}, "playable": playable}


class FakePlexWorker:
    def __init__(self):
        self.scans, self.deleted = [], []

    def request_scan(self, sub_id, delay=5):
        self.scans.append(sub_id)

    def request_delete_key(self, key):
        self.deleted.append(key)


class FakeApp:
    def __init__(self, db):
        self.db = db
        self.plex = FakePlexWorker()
        self.cache = type("C", (), {"remove": lambda self, v: None})()

    def invalidate(self):
        pass


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.app = FakeApp(self.db)
        self.syncer = Syncer(self.app)
        self.yt = self.syncer.yt = FakeYouTube()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, mode="channel", min_duration=0):
        sub_id = self.db.add_subscription({
            "kind": "channel", "source_id": "UCx", "playlist_id": "UUx", "channel_id": "UCx", "title": "Chan",
            "description": "", "avatar_url": "", "banner_url": "", "folder": "Chan [UCx]", "mode": mode,
            "min_duration": min_duration,
        })
        return self.db.subscription(sub_id)

    def test_channel_mode_numbering_and_removal(self):
        sub = self.add(min_duration=61)
        # Newest first, as the uploads playlist returns them.
        self.yt.items["UUx"] = [("v3", 2, False), ("v2", 1, False), ("v1", 0, False), ("short", 3, False), ("priv", 4, True)]
        self.yt.meta = {
            "v1": vid("v1", "2024-09-15T08:00:00Z"), "v2": vid("v2", "2024-09-15T20:00:00Z"),
            "v3": vid("v3", "2025-01-02T10:00:00Z"), "short": vid("short", "2024-09-16T10:00:00Z", duration=30),
        }
        added, removed = self.syncer._check(sub, full=True)
        self.assertEqual(sorted(added), ["v1", "v2", "v3"])
        entries = {e["video_id"]: e for e in self.db.entries(sub["id"])}
        self.assertEqual((entries["v1"]["season"], entries["v1"]["episode"]), (2024, 91501))
        self.assertEqual((entries["v2"]["season"], entries["v2"]["episode"]), (2024, 91502))
        self.assertEqual(entries["v3"]["filename"], "Chan - s2025e010201 - Title v3 [v3].mkv")

        # v1 disappears (deleted on YouTube) -> removed on the next full check;
        # numbering of the others doesn't move.
        self.db.update_entry(sub["id"], "v1", plex_key="101")
        self.yt.items["UUx"] = [("v3", 1, False), ("v2", 0, False)]
        self.yt.meta.pop("v1")
        added, removed = self.syncer._check(self.db.subscription(sub["id"]), full=True)
        self.assertEqual((added, removed), ([], ["v1"]))
        self.assertEqual(self.db.entry(sub["id"], "v2")["episode"], 91502)
        self.assertEqual(self.db.entry(sub["id"], "v1")["removed"], 1)
        self.assertEqual(self.db.video("v1")["removed"], 1)
        tree = layout.build(self.db)
        names = [n for season in tree["Chan [UCx]"].values() for n in season]
        self.assertNotIn("Chan - s2024e091501 - Title v1 [v1].mkv", names)
        self.assertEqual(len(names), 2)

    def test_quick_check_only_fetches_new(self):
        sub = self.add()
        self.yt.items["UUx"] = [("v1", 0, False)]
        self.yt.meta = {"v1": vid("v1", "2024-01-01T00:00:00Z")}
        self.syncer._check(sub, full=True)
        self.yt.calls.clear()
        self.yt.items["UUx"] = [("v2", 0, False), ("v1", 1, False)]
        self.yt.meta["v2"] = vid("v2", "2024-02-01T00:00:00Z")
        added, _ = self.syncer._check(self.db.subscription(sub["id"]), full=False)
        self.assertEqual(added, ["v2"])
        self.assertEqual(self.yt.calls[-1], ("videos", ("v2",)))  # no re-fetch of v1

    def test_playlist_mode_renumbers(self):
        sub = self.add(mode="playlist")
        self.yt.items["UUx"] = [("a", 0, False), ("b", 1, False)]
        self.yt.meta = {"a": vid("a", "2024-01-01T00:00:00Z"), "b": vid("b", "2023-01-01T00:00:00Z")}
        self.syncer._check(sub, full=True)
        self.assertEqual(self.db.entry(sub["id"], "b")["filename"], "Chan - s01e002 - Title b [b].mkv")
        self.db.update_entry(sub["id"], "b", plex_key="55")
        self.yt.items["UUx"] = [("b", 0, False), ("a", 1, False)]
        self.syncer._check(self.db.subscription(sub["id"]), full=True)
        b = self.db.entry(sub["id"], "b")
        self.assertEqual((b["episode"], b["filename"], b["plex_key"]), (1, "Chan - s01e001 - Title b [b].mkv", None))
        self.assertEqual(self.app.plex.deleted, ["55"])  # old item of the old filename

    def test_revived_video(self):
        sub = self.add()
        self.yt.items["UUx"] = [("v1", 0, False)]
        self.yt.meta = {"v1": vid("v1", "2024-01-01T00:00:00Z")}
        self.syncer._check(sub, full=True)
        self.yt.items["UUx"] = [("v1", 0, True)]  # went private
        self.syncer._check(self.db.subscription(sub["id"]), full=True)
        self.assertEqual(self.db.entry(sub["id"], "v1")["removed"], 1)
        self.yt.items["UUx"] = [("v1", 0, False)]  # public again
        added, _ = self.syncer._check(self.db.subscription(sub["id"]), full=True)
        self.assertEqual(added, ["v1"])
        self.assertEqual(self.db.entry(sub["id"], "v1")["removed"], 0)


class ByteLevelTests(unittest.TestCase):
    def test_ebml_sizes(self):
        self.assertEqual(ebml.encode_size(126), b"\xfe")
        self.assertEqual(ebml.encode_size(127), b"\x40\x7f")
        self.assertEqual(ebml.void_element(11), b"\xec\x89" + b"\0" * 9)
        self.assertEqual(ebml.read_size(ebml.UNKNOWN_SIZE_8, 0), (None, 8))

    def test_stub_read_range(self):
        header = b"HEADER"
        self.assertEqual(stub.read_range(header, 20, 0, 3), b"HEAD")
        self.assertEqual(stub.read_range(header, 20, 4, 9), b"ER\0\0\0\0")
        self.assertEqual(stub.read_range(header, 20, 18, 99), b"\0\0")

    def test_padding_is_a_void_to_the_advertised_end(self):
        real, total = 100, 200
        pad = b"".join(cache_mod._padding(p, min(p + 49, total - 1), real, total) for p in range(real, total, 50))
        self.assertEqual(len(pad), total - real)
        element_id, id_len = ebml.read_id(pad, 0)
        size, size_len = ebml.read_size(pad, id_len)
        self.assertEqual(element_id, ebml.VOID)
        self.assertEqual(id_len + size_len + size, total - real)

    def test_range_parsing(self):
        self.assertEqual(server._parse_range(None, 100), (0, 99))
        self.assertEqual(server._parse_range("bytes=10-", 100), (10, 99))
        self.assertEqual(server._parse_range("bytes=10-2000", 100), (10, 99))
        self.assertEqual(server._parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(server._parse_range("bytes=100-", 100), (None, None))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_stub_probes_like_a_real_file(self):
        import json
        import subprocess
        d = tempfile.mkdtemp()
        try:
            builder = stub.StubBuilder(d)
            size = 50 * 1024 * 1024
            header = builder.header(1234.5, size, 1280, 720, title="x")
            path = os.path.join(d, "stub.mkv")
            with open(path, "wb") as f:
                f.write(stub.read_range(header, size, 0, size - 1))
            out = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                 "-of", "json", path], capture_output=True, text=True).stdout)
            self.assertAlmostEqual(float(out["format"]["duration"]), 1234.5, places=1)
            self.assertEqual([s["codec_name"] for s in out["streams"]], ["h264", "aac"])
            self.assertEqual((out["streams"][0]["width"], out["streams"][0]["height"]), (1280, 720))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
