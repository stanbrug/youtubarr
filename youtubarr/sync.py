"""Subscription sync with the YouTube Data API.

Quick check (every check_interval_minutes): read the newest pages of the
uploads/playlist until a page holds only known videos -- usually 1 unit plus
1 per 50 new videos.

Full check (every full_check_hours, and right after adding): read the whole
playlist. Entries no longer in it, or that the API no longer returns as
playable, are marked removed (they leave the listing and Plex); videos that
came back are revived. In playlist mode, positions are refreshed.
"""

import logging
import threading
import time

from . import layout
from .youtube import YouTube, YouTubeError

log = logging.getLogger("youtubarr.sync")

QUICK_MAX_PAGES = 4


class Syncer(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="sync")
        self.app = app
        self.db = app.db
        self.yt = YouTube(app.db)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._forced = set()
        self._lock = threading.Lock()
        self.current = None

    # --- API for the web UI -----------------------------------------------------

    def add(self, text, mode=None, min_duration=0):
        info = self.yt.resolve(text)
        existing = self.db.one("SELECT id FROM subscriptions WHERE source_id = ?", (info["source_id"],))
        if existing:
            raise YouTubeError("this channel/playlist is already added")
        mode = mode if mode in ("channel", "playlist") else info["kind"]
        folder = layout.show_folder(info["title"], info["source_id"])
        sub_id = self.db.add_subscription({**info, "folder": folder, "mode": mode, "min_duration": int(min_duration or 0)})
        log.info(f"subscription added: {info['title']} ({info['source_id']}, {mode} mode)")
        self.force(sub_id)
        self.app.invalidate()
        return self.db.subscription(sub_id)

    def force(self, sub_id):
        """Run a full check of this subscription as soon as possible."""
        with self._lock:
            self._forced.add(sub_id)
        self.wake.set()

    # --- loop ----------------------------------------------------------------------

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            with self._lock:
                forced = set(self._forced)
                self._forced.clear()
            for sub in self.db.q("SELECT * FROM subscriptions WHERE enabled >= 0"):
                if self.stop.is_set():
                    break
                full = sub["id"] in forced or self._full_due(sub)
                if not (full or self._quick_due(sub)) or (sub["enabled"] == 0 and sub["id"] not in forced):
                    continue
                self.check(sub, full=full)
            self.wake.wait(60)

    def _quick_due(self, sub):
        interval = max(5, self.db.int_setting("check_interval_minutes", 60)) * 60
        return time.time() - (sub["last_check_at"] or 0) >= interval

    def _full_due(self, sub):
        interval = max(1, self.db.int_setting("full_check_hours", 24)) * 3600
        return time.time() - (sub["last_full_at"] or 0) >= interval

    # --- one subscription --------------------------------------------------------

    def check(self, sub, full):
        self.current = sub["title"]
        try:
            added, removed = self._check(sub, full)
            now = time.time()
            fields = {"last_check_at": now, "last_error": None}
            if full:
                fields["last_full_at"] = now
            self.db.update_subscription(sub["id"], **fields)
            if added or removed:
                log.info(f"{sub['title']}: {len(added)} new, {len(removed)} removed ({'full' if full else 'quick'} check)")
                self.app.invalidate()
                self.app.plex.request_scan(sub["id"])
                for video_id in removed:
                    self._drop_cache_if_orphaned(video_id)
        except YouTubeError as e:
            log.warning(f"{sub['title']}: {e}")
            self.db.update_subscription(sub["id"], last_error=str(e), last_check_at=time.time())
        except Exception as e:
            log.exception(f"{sub['title']}: check failed")
            self.db.update_subscription(sub["id"], last_error=f"{type(e).__name__}: {e}", last_check_at=time.time())
        finally:
            self.current = None

    def _check(self, sub, full):
        entries = {e["video_id"]: e for e in self.db.entries(sub["id"], include_removed=True)}
        active = {vid for vid, e in entries.items() if not e["removed"]}

        seen = []          # (video_id, position) in playlist order, available ones
        unavailable = set()
        items = self.yt.playlist_items(
            sub["playlist_id"],
            max_pages=None if full else QUICK_MAX_PAGES,
            stop_when_known=None if full else (lambda ids: all(i in entries for i in ids)),
        )
        for video_id, position, is_unavailable in items:
            if is_unavailable:
                unavailable.add(video_id)
            else:
                seen.append((video_id, position))
        seen_ids = {vid for vid, _ in seen}

        # Details for everything we don't have an active entry for; on a
        # full check also re-verify active entries that vanished from the list.
        need = [vid for vid, _ in seen if vid not in active]
        vanished = (active - seen_ids) if full else set()
        details = self.yt.videos(need + sorted(vanished)) if (need or vanished) else {}

        added, removed = [], []
        min_duration = int(sub["min_duration"] or 0)
        new_videos = []
        for vid in need:
            v = details.get(vid)
            if not v or not v["playable"] or v["duration"] < min_duration:
                continue
            new_videos.append(v)

        # Oldest first, so same-day uploads get their NN in publish order.
        positions = dict(seen)
        new_videos.sort(key=lambda v: (v["published_at"], v["id"]))
        for v in new_videos:
            self.db.upsert_video(v)
            if vid_entry := entries.get(v["id"]):
                self.db.update_entry(sub["id"], v["id"], removed=0, plex_key=None, plex_analyzed_at=None,
                                     meta_pushed_at=None, poster_pushed_at=None, plex_deleted_at=None)
                added.append(v["id"])
                continue
            if not self._create_entry(sub, v, positions.get(v["id"])):
                continue
            added.append(v["id"])

        for vid in (active & unavailable) | {vid for vid in vanished if not (details.get(vid) or {}).get("playable")}:
            self.db.update_entry(sub["id"], vid, removed=1)
            removed.append(vid)
            if details.get(vid) is None or vid in unavailable:
                self.db.x("UPDATE videos SET removed = 1 WHERE id = ?", (vid,))

        if sub["mode"] == "playlist" and full:
            self._renumber(sub, positions)
        return added, removed

    def _create_entry(self, sub, video, position):
        if sub["mode"] == "playlist":
            if position is None:
                return False
            season, episode, season_folder = 1, position + 1, "Season 01"
        else:
            year = layout.published(video).year
            taken = {r["episode"] for r in self.db.q(
                "SELECT episode FROM entries WHERE subscription_id = ? AND season = ?", (sub["id"], year))}
            slot = layout.channel_slot(video, taken)
            if slot is None:
                log.warning(f"{sub['title']}: more than 99 uploads on one day, skipping {video['id']}")
                return False
            season, episode, season_folder = slot
        self.db.x(
            """INSERT INTO entries(subscription_id, video_id, season, episode, season_folder, filename,
                                   position, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sub["id"], video["id"], season, episode, season_folder,
             layout.filename(sub["title"], season, episode, sub["mode"], video), position, time.time()),
        )
        return True

    def _renumber(self, sub, positions):
        """Playlist mode: episode = current position. A moved video gets a new
        filename, i.e. a new Plex item (its metadata is pushed again)."""
        for e in self.db.entries(sub["id"]):
            pos = positions.get(e["video_id"])
            if pos is None or pos == e["position"]:
                continue
            video = self.db.video(e["video_id"])
            if e["plex_key"]:
                self.app.plex.request_delete_key(e["plex_key"])  # the item of the old filename
            self.db.update_entry(sub["id"], e["video_id"], position=pos, episode=pos + 1,
                                 filename=layout.filename(sub["title"], 1, pos + 1, "playlist", video),
                                 plex_key=None, plex_analyzed_at=None, meta_pushed_at=None, poster_pushed_at=None)

    def _drop_cache_if_orphaned(self, video_id):
        still = self.db.one("SELECT COUNT(*) n FROM entries WHERE video_id = ? AND removed = 0", (video_id,))["n"]
        if not still:
            self.app.cache.remove(video_id)
